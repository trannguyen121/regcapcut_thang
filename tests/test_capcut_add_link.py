import json
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.actions.capcut_add_link import (
    AlreadyJoinedSpaceError,
    JoinVerificationError,
    _cant_join_space_visible,
    _cant_submit_request_visible,
    _is_my_cloud_url,
    _login_with_email,
    _login_error_text,
    _pro_plan_visible,
)
from modules.ui.reg_capcut_tab import (
    RegCapCutApp,
    parse_add_link_line,
    parse_capcut_login_line,
)
from modules.actions.capcut_workflow import accept_capcut_cookies, extract_capcut_username


class _Body:
    def __init__(self, text):
        self.text = text

    def inner_text(self, timeout=None):
        return self.text


class _EmptyLocator:
    def filter(self, **kwargs):
        return self

    def count(self):
        return 0


class _Page:
    def __init__(self, text):
        self.text = text
        self.url = "https://www.capcut.com/my-cloud"

    def locator(self, selector):
        if selector == "body":
            return _Body(self.text)
        return _EmptyLocator()

    def get_by_role(self, *args, **kwargs):
        return _EmptyLocator()


class CapCutAddLinkTests(unittest.TestCase):
    def _make_add_link_app(self, directory, accounts):
        app = RegCapCutApp.__new__(RegCapCutApp)
        base = Path(directory)
        app.add_link_accounts = accounts
        app.add_link_account_selected = {a["user"].casefold() for a in accounts}
        app.add_link_account_state_path = base / "account_status.json"
        app.add_link_accounts_path = base / "accounts.txt"
        app.add_link_links = ["https://space-a", "https://space-b"]
        app.add_link_counts = dict.fromkeys(app.add_link_links, 0)
        app.add_link_errors = {link: [] for link in app.add_link_links}
        app.add_link_full = set()
        app.add_link_counts_lock = threading.Lock()
        app.add_link_result_lock = threading.Lock()
        app.active_profiles_lock = threading.Lock()
        app.active_profiles = {}
        app.stop_event = threading.Event()
        app.proxies = []
        app.task_thread = None
        app.add_link_export_path = base / "used.txt"
        app.add_link_error_path = base / "error.txt"
        app.add_link_success_path = base / "success.txt"
        app.add_link_fail_path = base / "fail.txt"
        app.selected_group_name = lambda: "group"
        app.refresh_add_link_status = lambda: None
        app.root = types.SimpleNamespace(after=lambda *args: None)
        app.proxy_for_worker = lambda *args: None
        app.get_account_raw_proxy = lambda *args: ""
        app.build_gpm_profile_payload = lambda *args, **kwargs: {}
        app.active_standalone_processes = {}
        app._start_account_browser = lambda *args, **kwargs: (
            types.SimpleNamespace(terminate=lambda: None),
            types.SimpleNamespace(success=True, incognito=True, proxy=None),
        )
        return app

    def test_successful_accounts_are_not_reused_across_links_or_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            accounts = [{"user": f"mail{i}@example.com", "passnew": "pass"} for i in range(8)]
            accounts.append({"user": "MAIL0@example.com", "passnew": "pass"})
            app = self._make_add_link_app(directory, accounts)
            joined = []

            def workflow(account, link, context):
                # Assignment must already be durable before any external action.
                state = app._read_add_link_account_state()
                self.assertEqual(state[account["user"].casefold()]["assigned_link"], link)
                joined.append((account["user"].casefold(), link))
                return True

            with patch("modules.ui.reg_capcut_tab.run_capcut_add_link_workflow", side_effect=workflow):
                app.run_add_link_accounts(max_threads=3, launch_delay=0)
                app.run_add_link_accounts(max_threads=3, launch_delay=0)

            self.assertEqual(len(joined), 8)
            self.assertEqual(len({email for email, _ in joined}), 8)
            self.assertEqual(app.add_link_counts, {"https://space-a": 6, "https://space-b": 2})

    def test_uncertain_submit_is_never_reassigned_to_another_link(self):
        with tempfile.TemporaryDirectory() as directory:
            account = {"user": "mail@example.com", "passnew": "pass"}
            app = self._make_add_link_app(directory, [account])
            with patch("modules.ui.reg_capcut_tab.run_capcut_add_link_workflow", side_effect=RuntimeError("timeout after Submit")) as workflow:
                app.run_add_link_accounts(max_threads=1, launch_delay=0)
                app.add_link_run_links = ["https://space-b"]
                app.run_add_link_accounts(max_threads=1, launch_delay=0)
                self.assertEqual(workflow.call_count, 1)
            self.assertEqual(account["assigned_link"], "https://space-a")
            # Retry remains possible on the original link.
            app.add_link_run_links = ["https://space-a"]
            with patch("modules.ui.reg_capcut_tab.run_capcut_add_link_workflow", return_value=True) as workflow:
                app.run_add_link_accounts(max_threads=1, launch_delay=0)
                workflow.assert_called_once()

    def test_deleted_account_keeps_history_when_imported_again(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._make_add_link_app(directory, [
                {"user": "mail@example.com", "passnew": "pass", "status": "true", "assigned_link": "https://space-a"}
            ])
            app._save_add_link_account_state()
            app.add_link_accounts = []
            app._save_add_link_account_state()
            app.add_link_accounts_path.write_text("MAIL@example.com|new-password\n", encoding="utf-8")
            app.load_add_link_accounts()
            self.assertEqual(app.add_link_accounts[0]["status"], "true")
            self.assertEqual(app.add_link_accounts[0]["assigned_link"], "https://space-a")
            with patch("modules.ui.reg_capcut_tab.run_capcut_add_link_workflow") as workflow:
                app.run_add_link_accounts(max_threads=2, launch_delay=0)
                workflow.assert_not_called()

    def test_restart_uses_durable_history_if_workspace_is_older(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._make_add_link_app(directory, [
                {"user": "mail@example.com", "passnew": "pass", "status": "true", "assigned_link": "https://space-a"}
            ])
            app._save_add_link_account_state()
            app.add_link_workspace_path = Path(directory) / "workspace.json"
            app.add_link_workspace_path.write_text(json.dumps({"accounts": [
                {"user": "mail@example.com", "passnew": "pass", "status": "running"}
            ], "links": []}), encoding="utf-8")
            app.restore_add_link_workspace()
            self.assertEqual(app.add_link_accounts[0]["status"], "true")
            self.assertEqual(app.add_link_accounts[0]["assigned_link"], "https://space-a")

    def test_import_cannot_replace_accounts_or_links_during_a_run(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._make_add_link_app(directory, [{"user": "mail", "passnew": "pass"}])
            app.task_thread = types.SimpleNamespace(is_alive=lambda: True)
            with patch("modules.ui.reg_capcut_tab.messagebox.showinfo") as notice:
                app.load_add_link_accounts()
                app.load_add_link_links()
            self.assertEqual(notice.call_count, 2)
            self.assertEqual(app.add_link_accounts[0]["user"], "mail")
            self.assertEqual(app.add_link_links, ["https://space-a", "https://space-b"])

    def test_worker_checks_history_before_using_a_stale_account_row(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._make_add_link_app(directory, [
                {"user": "mail@example.com", "passnew": "pass", "status": "true", "assigned_link": "https://space-a"}
            ])
            app._save_add_link_account_state()
            stale = {"user": "MAIL@example.com", "passnew": "pass"}
            with patch("modules.ui.reg_capcut_tab.run_capcut_add_link_workflow") as workflow:
                app.run_add_link_worker(stale, "https://space-b", "group", 0, 1, threading.Semaphore(1), 1)
                workflow.assert_not_called()
            self.assertEqual(stale["status"], "true")
            self.assertFalse(app.add_link_fail_path.exists())

    def test_already_joined_is_saved_without_incrementing_link_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            account = {"user": "mail@example.com", "passnew": "pass", "join_pending": True}
            app = self._make_add_link_app(directory, [account])
            with patch("modules.ui.reg_capcut_tab.run_capcut_add_link_workflow", side_effect=AlreadyJoinedSpaceError("member")):
                app.run_add_link_accounts(max_threads=1, launch_delay=0)
            self.assertEqual(account["status"], "true")
            self.assertFalse(account["join_pending"])
            self.assertEqual(sum(app.add_link_counts.values()), 0)
            self.assertEqual(app._read_add_link_account_state()["mail@example.com"]["status"], "true")

    def test_pending_submit_is_durable_and_restored_after_import(self):
        with tempfile.TemporaryDirectory() as directory:
            account = {"user": "mail@example.com", "passnew": "pass"}
            app = self._make_add_link_app(directory, [account])

            def uncertain_submit(account, link, context):
                account["join_pending"] = True
                context["before_join_submit"]()
                self.assertTrue(app._read_add_link_account_state()["mail@example.com"]["join_pending"])
                raise JoinVerificationError("Request pending")

            with patch("modules.ui.reg_capcut_tab.run_capcut_add_link_workflow", side_effect=uncertain_submit):
                app.run_add_link_accounts(max_threads=1, launch_delay=0)
            self.assertEqual(account["status"], "join unverified")
            self.assertEqual(sum(app.add_link_counts.values()), 0)
            app.add_link_accounts_path.write_text("mail@example.com|pass\n", encoding="utf-8")
            app.load_add_link_accounts()
            self.assertTrue(app.add_link_accounts[0]["join_pending"])
            self.assertEqual(app.add_link_accounts[0]["status"], "join unverified")

    def test_crash_after_submit_restores_unverified_status(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._make_add_link_app(directory, [{
                "user": "mail@example.com", "passnew": "pass", "status": "running",
                "assigned_link": "https://space-a", "join_pending": True,
            }])
            app.add_link_workspace_path = Path(directory) / "workspace.json"
            app.add_link_selected = set(app.add_link_links)
            app._save_add_link_account_state()
            app.restore_add_link_workspace()
            self.assertTrue(app.add_link_accounts[0]["join_pending"])
            self.assertEqual(app.add_link_accounts[0]["status"], "join unverified")

    def test_capcut_accept_all_cookie_button_is_clicked(self):
        class Candidate:
            def __init__(self):
                self.clicked = False

            def is_visible(self):
                return True

            def is_enabled(self):
                return True

            def click(self, **_kwargs):
                self.clicked = True

        class Locator:
            def __init__(self, candidate):
                self.candidate = candidate

            def count(self):
                return 1

            def nth(self, _index):
                return self.candidate

        class CookiePage:
            def __init__(self):
                self.candidate = Candidate()
                self.control = Locator(self.candidate)

            def get_by_role(self, *_args, **_kwargs):
                return self.control

            def get_by_text(self, *_args, **_kwargs):
                return self.control

            def locator(self, *_args, **_kwargs):
                return self.control

        page = CookiePage()

        accepted = accept_capcut_cookies(page, timeout=0)

        self.assertTrue(accepted)
        self.assertTrue(page.candidate.clicked)

    def test_account_txt_format(self):
        account = parse_capcut_login_line("mail@example.com|secret")
        self.assertEqual(account["user"], "mail@example.com")
        self.assertEqual(account["passnew"], "secret")

    def test_link_line_accepts_plain_url_and_saved_usage_count(self):
        self.assertEqual(parse_add_link_line("https://plain-link"), ("https://plain-link", 0))
        self.assertEqual(parse_add_link_line("https://used-link|3"), ("https://used-link", 3))
        self.assertEqual(parse_add_link_line("https://full-link|99"), ("https://full-link", 6))
        self.assertIsNone(parse_add_link_line("not-a-link|2"))

    def test_import_link_file_restores_saved_usage_count(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "links.txt"
            export_path = Path(directory) / "add_link_used.txt"
            path.write_text(
                "https://plain\nhttps://used|4\nhttps://full|6\nhttps://from-export\n",
                encoding="utf-8",
            )
            export_path.write_text("https://from-export|2\n", encoding="utf-8")
            app = RegCapCutApp.__new__(RegCapCutApp)
            app.add_link_links_path = path
            app.add_link_export_path = export_path
            app.add_link_counts = {}
            app.add_link_errors = {}
            app.add_link_full = set()
            app.add_link_counts_lock = threading.Lock()
            app._save_add_link_workspace = lambda: None
            app.refresh_add_link_status = lambda: None

            app.load_add_link_links()

            self.assertEqual(
                app.add_link_links,
                ["https://plain", "https://used", "https://full", "https://from-export"],
            )
            self.assertEqual(
                app.add_link_counts,
                {
                    "https://plain": 0,
                    "https://used": 4,
                    "https://full": 6,
                    "https://from-export": 2,
                },
            )
            self.assertEqual(app.add_link_full, {"https://full"})

    def test_cant_join_is_detected_with_straight_or_curly_apostrophe(self):
        self.assertTrue(_cant_join_space_visible(_Page("Can't join space")))
        self.assertTrue(_cant_join_space_visible(_Page("Can’t join space")))

    def test_cant_submit_means_link_is_full(self):
        self.assertTrue(_cant_submit_request_visible(_Page("Can't submit request")))
        self.assertTrue(_cant_submit_request_visible(_Page("Can’t submit request")))
        self.assertFalse(_cant_submit_request_visible(_Page("Can't join space")))

    def test_common_login_errors_are_detected_immediately(self):
        self.assertEqual(_login_error_text("Email or password is incorrect"), "email or password is incorrect")
        self.assertEqual(_login_error_text("Account not found"), "account not found")
        self.assertEqual(_login_error_text("Too many attempts. Try again later"), "too many attempts")
        self.assertIsNone(_login_error_text("Welcome to CapCut"))

    def test_check_user_login_retries_a_transient_failure(self):
        class Page:
            def __init__(self):
                self.urls = []

            def goto(self, url, **_kwargs):
                self.urls.append(url)

        page = Page()
        account = {"user": "retry@example.com", "passnew": "pass"}
        with (
            patch(
                "modules.actions.capcut_add_link._login_with_email_once",
                side_effect=[RuntimeError("temporary navigation error"), None],
            ) as login_once,
            patch("modules.actions.capcut_add_link._wait"),
        ):
            _login_with_email(page, account, None, max_attempts=3)

        self.assertEqual(login_once.call_count, 2)
        self.assertEqual(page.urls, ["about:blank"])

    def test_success_requires_pro_teams_membership(self):
        self.assertTrue(_pro_plan_visible(_Page("Pro\nYou're enjoying Pro benefits as a\nTeams member")))
        self.assertFalse(_pro_plan_visible(_Page("Joined successfully")))

    def test_official_active_pro_status_is_accepted(self):
        self.assertTrue(_pro_plan_visible(_Page("CapCut Pro: Active\nValid until 2027-01-01")))
        self.assertTrue(_pro_plan_visible(_Page("Subscription active")))
        self.assertFalse(_pro_plan_visible(_Page("Upgrade to Pro\nFree user")))

    def test_only_capcut_my_cloud_url_is_success(self):
        self.assertTrue(_is_my_cloud_url("https://www.capcut.com/my-cloud/123?tab=all"))
        self.assertTrue(_is_my_cloud_url("https://www.capcut.com/my-cloud"))
        self.assertFalse(_is_my_cloud_url("https://www.capcut.com/team-invite/123"))
        self.assertFalse(_is_my_cloud_url("https://example.com/my-cloud/123"))

    def test_check_user_extracts_public_username_from_authenticated_page(self):
        page = _Page("My profile\nuser123456789\nProjects")

        with patch("modules.actions.capcut_workflow._wait"):
            self.assertEqual(extract_capcut_username(page), "user123456789")

    def test_check_user_skips_first_login_survey_then_reopens_my_cloud(self):
        class RedirectedPage:
            def __init__(self):
                self.url = "https://www.capcut.com/my-edit?from_page=landing_page"
                self.navigation_count = 0

            def goto(self, _url, **_kwargs):
                self.navigation_count += 1
                self.url = (
                    "https://www.capcut.com/my-edit?from_page=landing_page"
                    if self.navigation_count == 1
                    else "https://www.capcut.com/my-cloud"
                )

            def locator(self, selector):
                if selector == "body":
                    text = (
                        "Which of the following roles best describes you? Skip"
                        if self.navigation_count < 2
                        else "My profile user246813579 Projects"
                    )
                    return _Body(text)
                return _EmptyLocator()

        page = RedirectedPage()
        dismissed = []

        def dismiss_popup(_page, _user, _stop_event, timeout):
            dismissed.append(timeout)

        with patch("modules.actions.capcut_workflow._wait"):
            username = extract_capcut_username(
                page,
                "survey@example.com",
                dismiss_popups=dismiss_popup,
            )

        self.assertEqual(username, "user246813579")
        self.assertEqual(page.navigation_count, 2)
        self.assertGreaterEqual(len(dismissed), 2)

    def test_check_user_button_schedules_username_only_workflow(self):
        class Entry:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        class Thread:
            def __init__(self, target, args, daemon):
                self.target = target
                self.args = args
                self.daemon = daemon
                self.started = False

            def start(self):
                self.started = True

        app = RegCapCutApp.__new__(RegCapCutApp)
        app.add_link_accounts = [
            {
                "user": "selected@example.com",
                "passnew": "pass",
                "userID": "old-user",
                "pro_status": "true",
            },
            {"user": "skipped@example.com", "passnew": "pass", "userID": "keep-user"},
        ]
        app.add_link_account_selected = {"selected@example.com"}
        app.add_link_threads_entry = Entry("2")
        app.add_link_delay_entry = Entry("0")
        app.task_thread = None
        app.stop_event = threading.Event()
        app._save_add_link_account_state = lambda: None
        app.refresh_add_link_status = lambda: None

        with patch("modules.ui.reg_capcut_tab.threading.Thread", Thread):
            app.start_check_user()

        self.assertEqual(app.check_user_accounts, [app.add_link_accounts[0]])
        self.assertEqual(app.add_link_accounts[0]["userID"], "")
        self.assertEqual(app.add_link_accounts[0]["pro_status"], "true")
        self.assertEqual(app.add_link_accounts[1]["userID"], "keep-user")
        self.assertEqual(app.task_thread.target, app.run_check_user_accounts)
        self.assertTrue(app.task_thread.started)

    def test_check_user_worker_uses_standalone_chrome_152_instead_of_gpm(self):
        class Process:
            def __init__(self):
                self.terminated = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout):
                self.timeout = timeout

        class Root:
            def after(self, *_args):
                return None

        class NoGpm:
            def __getattr__(self, name):
                raise AssertionError(f"GPM must not be used by Check User: {name}")

        app = RegCapCutApp.__new__(RegCapCutApp)
        app.stop_event = threading.Event()
        app.active_profiles_lock = threading.Lock()
        app.active_standalone_processes = {}
        app.root = Root()
        app.orch = NoGpm()
        app._save_add_link_account_state = lambda: None
        app.refresh_add_link_status = lambda: None
        app._request_expressvpn_reset = lambda: None
        chromium = Path("chrome-152") / "chrome.exe"
        app._resolve_chromium_152_browser = lambda: chromium
        process = Process()
        launches = []

        def launch(_account, _index, **kwargs):
            launches.append(kwargs)
            return process, types.SimpleNamespace(remote_debugging_address="127.0.0.1:12345")

        app._start_account_browser = lambda account, index, max_threads, **kwargs: launch(account, index, **kwargs)
        account = {"user": "user@example.com", "passnew": "pass", "userID": ""}
        with patch(
            "modules.ui.reg_capcut_tab.run_capcut_check_user_workflow",
            side_effect=lambda target, _context: target.update(userID="user123456789") or "user123456789",
        ):
            app.run_check_user_worker(
                account,
                index=0,
                total=1,
                semaphore=threading.Semaphore(1),
                max_threads=1,
            )

        self.assertEqual(launches[0]["startup_url"], "https://www.capcut.com/login")
        self.assertTrue(process.terminated)

    def test_successful_registration_keeps_username_from_chromium_workflow(self):
        app = RegCapCutApp.__new__(RegCapCutApp)
        app.stop_event = threading.Event()
        app.hold_mode = False
        app._start_account_browser = lambda *args, **kwargs: (
            types.SimpleNamespace(terminate=lambda: None), types.SimpleNamespace(success=True),
        )
        app.active_profiles = {}
        app.active_profiles_lock = threading.Lock()
        app.export_lock = threading.Lock()
        app.proxy_for_worker = lambda *_args: None
        app.build_temp_profile_payload = lambda *_args, **_kwargs: {}
        statuses = []
        app.mark_account_status = lambda _account, status: statuses.append(status)
        app.queue_sheet_success = lambda _account: None
        account = {"user": "new@example.com", "passnew": "pass", "userID": ""}
        saved = []

        def register_in_chromium(target, _context):
            target["userID"] = "user135792468"
            return True

        with (
            patch(
                "modules.ui.reg_capcut_tab.run_capcut_workflow",
                side_effect=register_in_chromium,
            ),
            patch(
                "modules.ui.reg_capcut_tab.write_result",
                side_effect=lambda target, success, _lock: saved.append((dict(target), success)),
            ),
        ):
            app.run_worker(
                account,
                group_name="capcut",
                index=0,
                total=1,
                semaphore=threading.Semaphore(1),
                max_threads=1,
            )

        self.assertEqual(saved[0][0]["userID"], "user135792468")
        self.assertTrue(saved[0][1])
        self.assertIn("true", statuses)

    def test_result_files_use_link_pipe_count(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            app = RegCapCutApp.__new__(RegCapCutApp)
            app.add_link_counts_lock = threading.Lock()
            app.add_link_counts = {"https://full": 6, "https://error": 2, "https://unused": 0}
            app.add_link_errors = {"https://full": [], "https://error": ["mail|Can't join space"], "https://unused": []}
            app.add_link_export_path = base / "add_link_used.txt"
            app.add_link_remaining_path = base / "add_link_remaining.txt"
            app.add_link_error_path = base / "add_link_error.txt"

            app.write_add_link_results()

            self.assertEqual(
                app.add_link_export_path.read_text(encoding="utf-8").splitlines(),
                ["https://full|6", "https://error|2", "https://unused|0"],
            )
            self.assertEqual(
                app.add_link_remaining_path.read_text(encoding="utf-8").splitlines(),
                ["https://error|4", "https://unused|6"],
            )
            self.assertEqual(
                app.add_link_error_path.read_text(encoding="utf-8"),
                "https://error|2",
            )

    def test_add_link_account_results_are_split_into_true_and_fail_files(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "add link"
            app = RegCapCutApp.__new__(RegCapCutApp)
            app.add_link_success_path = base / "acc_add_true.txt"
            app.add_link_fail_path = base / "acc_add_fail.txt"
            app.add_link_result_lock = threading.Lock()

            app.append_add_link_account_result(
                {"user": "success@example.com", "passnew": "success-pass"},
                True,
            )
            app.append_add_link_account_result(
                {"user": "fail@example.com", "passnew": "fail-pass"},
                False,
            )

            self.assertEqual(
                app.add_link_success_path.read_text(encoding="utf-8"),
                "success@example.com|success-pass\n",
            )
            self.assertEqual(
                app.add_link_fail_path.read_text(encoding="utf-8"),
                "fail@example.com|fail-pass\n",
            )

    def test_add_link_account_status_and_selection_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            app = RegCapCutApp.__new__(RegCapCutApp)
            app.add_link_accounts = [
                {"user": "done@example.com", "passnew": "pass1", "status": "true"},
                {"user": "fail@example.com", "passnew": "pass2", "status": "login fail"},
            ]
            app.add_link_account_selected = {"done@example.com"}
            app.add_link_account_state_path = Path(directory) / "add link" / "account_status.json"
            app.add_link_result_lock = threading.Lock()

            app._save_add_link_account_state()
            state = app._read_add_link_account_state()

            self.assertEqual(
                state["done@example.com"],
                {"status": "true", "pro_status": "", "userID": "", "assigned_link": "", "join_pending": False, "selected": True},
            )
            self.assertEqual(
                state["fail@example.com"],
                {"status": "login fail", "pro_status": "", "userID": "", "assigned_link": "", "join_pending": False, "selected": False},
            )

    def test_import_restores_saved_add_link_status_and_checkboxes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            account_path = base / "account.txt"
            account_path.write_text(
                "done@example.com|pass1\nfail@example.com|pass2\n",
                encoding="utf-8",
            )
            state_path = base / "add link" / "account_status.json"
            state_path.parent.mkdir()
            state_path.write_text(
                '{"done@example.com":{"status":"true","selected":true},'
                '"fail@example.com":{"status":"login fail","selected":false}}',
                encoding="utf-8",
            )
            app = RegCapCutApp.__new__(RegCapCutApp)
            app.add_link_accounts_path = account_path
            app.add_link_account_state_path = state_path
            app.add_link_result_lock = threading.Lock()
            app.refresh_add_link_status = lambda: None

            app.load_add_link_accounts()

            self.assertEqual(
                [account["status"] for account in app.add_link_accounts],
                ["true", "login fail"],
            )
            self.assertEqual(app.add_link_account_selected, {"done@example.com"})

    def test_user_workspace_round_trip_does_not_need_default_txt_files(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace_path = Path(directory) / "add link" / "workspace.json"
            saved = RegCapCutApp.__new__(RegCapCutApp)
            saved.add_link_workspace_path = workspace_path
            saved.add_link_result_lock = threading.Lock()
            saved.add_link_accounts = [
                {
                    "user": "saved@example.com",
                    "passnew": "pass",
                    "status": "true",
                    "pro_status": "true",
                    "userID": "user987654321",
                }
            ]
            saved.add_link_account_selected = {"saved@example.com"}
            saved.add_link_links = ["https://saved-link"]
            saved.add_link_selected = {"https://saved-link"}
            saved.add_link_counts = {"https://saved-link": 3}
            saved.add_link_errors = {"https://saved-link": []}
            saved.add_link_full = set()
            saved._save_add_link_workspace()

            restored = RegCapCutApp.__new__(RegCapCutApp)
            restored.add_link_workspace_path = workspace_path
            restored.refresh_add_link_status = lambda: None
            restored.restore_add_link_workspace()

            self.assertEqual(restored.add_link_accounts[0]["user"], "saved@example.com")
            self.assertEqual(restored.add_link_accounts[0]["status"], "true")
            self.assertEqual(restored.add_link_accounts[0]["pro_status"], "true")
            self.assertEqual(restored.add_link_accounts[0]["userID"], "user987654321")
            self.assertEqual(restored.add_link_account_selected, {"saved@example.com"})
            self.assertEqual(restored.add_link_links, ["https://saved-link"])
            self.assertEqual(restored.add_link_counts, {"https://saved-link": 3})
            self.assertEqual(restored.add_link_selected, {"https://saved-link"})
            self.assertIs(restored.check_pro_accounts, restored.add_link_accounts)

    def test_simulated_successes_are_saved_to_disk_and_restored_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            link = "https://www.capcut.com/sv2/simulated-link/"

            running = RegCapCutApp.__new__(RegCapCutApp)
            running.add_link_workspace_path = base / "add link" / "workspace.json"
            running.add_link_export_path = base / "export" / "add_link_used.txt"
            running.add_link_remaining_path = base / "export" / "add_link_remaining.txt"
            running.add_link_error_path = base / "export" / "add_link_error.txt"
            running.add_link_result_lock = threading.Lock()
            running.add_link_counts_lock = threading.Lock()
            running.add_link_accounts = [
                {"user": "mock1@example.com", "passnew": "pass1", "status": "true"},
                {"user": "mock2@example.com", "passnew": "pass2", "status": "true"},
            ]
            running.add_link_account_selected = {
                "mock1@example.com",
                "mock2@example.com",
            }
            running.add_link_links = [link]
            running.add_link_selected = {link}
            running.add_link_counts = {link: 0}
            running.add_link_errors = {link: []}
            running.add_link_full = set()

            # Simulate the same mutations made by two successful workers.
            with running.add_link_counts_lock:
                running.add_link_counts[link] += 1
                running.add_link_counts[link] += 1
                running.write_add_link_results(lock_held=True)
            running._save_add_link_workspace()

            self.assertEqual(
                running.add_link_export_path.read_text(encoding="utf-8"),
                f"{link}|2",
            )
            self.assertEqual(
                running.add_link_remaining_path.read_text(encoding="utf-8"),
                f"{link}|4",
            )
            workspace = json.loads(
                running.add_link_workspace_path.read_text(encoding="utf-8")
            )
            self.assertEqual(workspace["links"][0]["count"], 2)

            restarted = RegCapCutApp.__new__(RegCapCutApp)
            restarted.add_link_workspace_path = running.add_link_workspace_path
            restarted.refresh_add_link_status = lambda: None
            restarted.restore_add_link_workspace()

            self.assertEqual(restarted.add_link_counts, {link: 2})
            self.assertEqual(restarted.add_link_selected, {link})

    def test_check_pro_summary_works_without_the_removed_separate_tab(self):
        app = RegCapCutApp.__new__(RegCapCutApp)
        app.add_link_accounts = [
            {"user": "shared@example.com", "status": "true", "pro_status": "true"}
        ]
        app.add_link_account_selected = {"shared@example.com"}

        # The current UI exposes Check Pro as a button in Add Link, so there is
        # intentionally no separate check_pro_status_label widget.
        app.refresh_check_pro_status()

    def test_export_writes_only_checked_add_link_accounts(self):
        with tempfile.TemporaryDirectory() as directory:
            app = RegCapCutApp.__new__(RegCapCutApp)
            app.add_link_accounts = [
                {"user": "selected@example.com", "passnew": "pass1"},
                {"user": "skipped@example.com", "passnew": "pass2"},
            ]
            app.add_link_account_selected = {"selected@example.com"}
            app.add_link_account_export_path = Path(directory) / "add link" / "acc_selected.txt"

            with patch("modules.ui.reg_capcut_tab.messagebox.showinfo"):
                app.export_selected_add_link_accounts()

            self.assertEqual(
                app.add_link_account_export_path.read_text(encoding="utf-8"),
                "selected@example.com|pass1\n",
            )

    def test_export_checked_usernames_writes_selected_rows_without_crashing(self):
        with tempfile.TemporaryDirectory() as directory:
            app = RegCapCutApp.__new__(RegCapCutApp)
            app.add_link_accounts = [
                {"user": "selected@example.com", "userID": "user123456789"},
                {"user": "skipped@example.com", "userID": "user987654321"},
            ]
            app.add_link_account_selected = {"selected@example.com"}
            app.add_link_username_export_path = Path(directory) / "login_user.txt"

            app.export_checked_usernames()

            self.assertEqual(
                app.add_link_username_export_path.read_text(encoding="utf-8"),
                "selected@example.com|user123456789\n",
            )

    def test_export_registered_usernames_uses_checked_rows_and_reg_user_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            app = RegCapCutApp.__new__(RegCapCutApp)
            app.accounts = [
                {
                    "picked": True,
                    "user": "selected@example.com",
                    "userID": "user123456789",
                },
                {
                    "picked": False,
                    "user": "unchecked@example.com",
                    "userID": "user987654321",
                },
                {"picked": True, "user": "missing@example.com", "userID": ""},
            ]
            app.reg_username_export_path = Path(directory) / "check user" / "reg_user.txt"

            app.export_registered_usernames()

            self.assertEqual(
                app.reg_username_export_path.read_text(encoding="utf-8"),
                "selected@example.com|user123456789\n",
            )

    def test_account_table_has_clear_add_link_statuses(self):
        self.assertEqual(RegCapCutApp.CHECKBOX_SELECTED, "\u2611")
        self.assertEqual(RegCapCutApp.CHECKBOX_UNSELECTED, "\u2610")
        self.assertNotEqual(RegCapCutApp.CHECKBOX_SELECTED, RegCapCutApp.CHECKBOX_UNSELECTED)
        self.assertEqual(RegCapCutApp._add_link_account_status_text("true"), "Đã add link")
        self.assertEqual(
            RegCapCutApp._add_link_account_status_text("login fail"),
            "Lỗi đăng nhập - chưa add",
        )
        self.assertEqual(RegCapCutApp._add_link_account_status_text(""), "Chưa add link")
        self.assertEqual(RegCapCutApp._check_pro_status_text("true"), "Có CapCut Pro")
        self.assertEqual(RegCapCutApp._check_pro_status_text("false"), "Không có CapCut Pro")
        self.assertEqual(RegCapCutApp._check_pro_status_text(""), "Chưa kiểm tra")

    def test_ctrl_a_highlights_every_row_in_any_table(self):
        class FakeTree:
            def __init__(self):
                self.selected = ()
                self.focused = None
                self.visible = None

            def get_children(self):
                return ("0", "1", "2")

            def selection_set(self, rows):
                self.selected = rows

            def focus(self, row):
                self.focused = row

            def see(self, row):
                self.visible = row

        tree = FakeTree()
        result = RegCapCutApp._select_all_table_rows(types.SimpleNamespace(widget=tree))

        self.assertEqual(result, "break")
        self.assertEqual(tree.selected, ("0", "1", "2"))
        self.assertEqual(tree.focused, "0")
        self.assertEqual(tree.visible, "0")

    def test_clicking_table_moves_keyboard_focus_out_of_threads_entry(self):
        class FakeTree:
            def __init__(self):
                self.has_keyboard_focus = False

            def focus_set(self):
                self.has_keyboard_focus = True

            def identify_row(self, _y):
                return ""

        app = RegCapCutApp.__new__(RegCapCutApp)
        tree = FakeTree()
        app.add_link_account_tree = tree

        result = app._add_link_account_table_press(types.SimpleNamespace(y=10))

        self.assertEqual(result, "break")
        self.assertTrue(tree.has_keyboard_focus)

    def test_delete_removes_only_checked_accounts_from_shared_table(self):
        app = RegCapCutApp.__new__(RegCapCutApp)
        app.task_thread = None
        app.add_link_accounts = [
            {"user": "remove1@example.com"},
            {"user": "keep@example.com"},
            {"user": "remove2@example.com"},
        ]
        app.add_link_account_selected = {
            "remove1@example.com",
            "remove2@example.com",
        }
        app.check_pro_accounts = app.add_link_accounts
        app._save_add_link_account_state = lambda: None
        app.refresh_add_link_status = lambda: None

        with patch("modules.ui.reg_capcut_tab.messagebox.askyesno", return_value=True):
            app.delete_checked_add_link_accounts()

        self.assertEqual(
            [account["user"] for account in app.add_link_accounts],
            ["keep@example.com"],
        )
        self.assertEqual(app.add_link_account_selected, set())
        self.assertIs(app.check_pro_accounts, app.add_link_accounts)

    def test_start_keeps_usage_saved_from_an_earlier_run(self):
        class Entry:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        class Thread:
            def __init__(self, **_kwargs):
                self.started = False

            def start(self):
                self.started = True

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            app = RegCapCutApp.__new__(RegCapCutApp)
            app.add_link_accounts = [{"user": "mail", "passnew": "pass", "status": "true"}]
            app.add_link_accounts.extend([
                {"user": "new", "passnew": "pass", "status": "login fail"},
                {"user": "unchecked", "passnew": "pass", "status": "login fail"},
            ])
            app.add_link_account_selected = {"mail", "new"}
            app.add_link_links = ["https://part-used", "https://full"]
            app.add_link_selected = set(app.add_link_links)
            app.add_link_counts = {"https://part-used": 3, "https://full": 6}
            app.add_link_errors = {link: [] for link in app.add_link_links}
            app.add_link_full = {"https://full"}
            app.add_link_counts_lock = threading.Lock()
            app.add_link_threads_entry = Entry("1")
            app.add_link_delay_entry = Entry("0")
            app.add_link_success_path = base / "acc_add_true.txt"
            app.add_link_fail_path = base / "acc_add_fail.txt"
            app.stop_event = threading.Event()
            app.task_thread = None
            app._save_add_link_account_state = lambda: None
            app._save_add_link_workspace = lambda: None
            app.write_add_link_results = lambda: None
            app.refresh_add_link_status = lambda: None

            with patch("modules.ui.reg_capcut_tab.threading.Thread", Thread):
                app.start_add_link()

            self.assertEqual(
                app.add_link_counts,
                {"https://part-used": 3, "https://full": 6},
            )
            self.assertEqual(app.add_link_full, {"https://full"})
            self.assertTrue(app.task_thread.started)
            self.assertEqual([account["user"] for account in app.current_accounts], ["new"])
            self.assertEqual(app.add_link_accounts[0]["status"], "true")
            self.assertEqual(app.add_link_accounts[2]["status"], "login fail")

    def test_failed_account_is_replaced_until_link_has_six_successes(self):
        app = RegCapCutApp.__new__(RegCapCutApp)
        app.add_link_accounts = [{"user": f"mail{i}"} for i in range(8)]
        app.add_link_links = ["https://space"]
        app.add_link_counts = {"https://space": 0}
        app.add_link_counts_lock = threading.Lock()
        app.stop_event = threading.Event()
        app.proxies = []
        app.current_accounts = list(app.add_link_accounts)
        app.task_thread = object()
        app.add_link_export_path = Path("add_link_used.txt")
        app.add_link_success_path = Path("acc_add_true.txt")
        app.add_link_fail_path = Path("acc_add_fail.txt")
        app.selected_group_name = lambda: "group"
        app.write_add_link_results = lambda *args, **kwargs: None
        attempted = []

        def fake_worker(self, account, link, *args):
            attempted.append(account["user"])
            if account["user"] == "mail0":
                account["status"] = "can't join space"
                return
            account["status"] = "true"
            with self.add_link_counts_lock:
                self.add_link_counts[link] += 1

        app.run_add_link_worker = types.MethodType(fake_worker, app)
        app.run_add_link_accounts(max_threads=1, launch_delay=0)

        self.assertEqual(app.add_link_counts["https://space"], 6)
        self.assertEqual(attempted, [f"mail{i}" for i in range(7)])
        self.assertNotIn("mail7", attempted)

    def test_add_link_scheduler_uses_only_links_selected_in_the_table(self):
        app = RegCapCutApp.__new__(RegCapCutApp)
        app.add_link_accounts = [{"user": f"mail{i}"} for i in range(6)]
        app.add_link_links = ["https://disabled", "https://selected"]
        app.add_link_run_links = ["https://selected"]
        app.add_link_counts = {"https://disabled": 0, "https://selected": 0}
        app.add_link_counts_lock = threading.Lock()
        app.stop_event = threading.Event()
        app.proxies = []
        app.current_accounts = list(app.add_link_accounts)
        app.task_thread = object()
        app.add_link_export_path = Path("add_link_used.txt")
        app.add_link_success_path = Path("acc_add_true.txt")
        app.add_link_fail_path = Path("acc_add_fail.txt")
        app.selected_group_name = lambda: "group"
        app.write_add_link_results = lambda *args, **kwargs: None
        attempted_links = []

        def fake_worker(self, account, link, *args):
            attempted_links.append(link)
            with self.add_link_counts_lock:
                self.add_link_counts[link] += 1

        app.run_add_link_worker = types.MethodType(fake_worker, app)
        app.run_add_link_accounts(max_threads=1, launch_delay=0)

        self.assertEqual(attempted_links, ["https://selected"] * 6)
        self.assertEqual(app.add_link_counts["https://disabled"], 0)
        self.assertEqual(app.add_link_counts["https://selected"], 6)


if __name__ == "__main__":
    unittest.main()
