import base64
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from playwright.sync_api import sync_playwright

from core.orchestrator import Orchestrator
from core.settings import BrowserWindowSettings
from modules.browser.chromium import ChromiumSession, open_workflow_page, parse_browser_proxy, resolve_chromium_154
from modules.ui.reg_capcut_tab import RegCapCutApp, registration_proxy_for_account


class ChromiumSessionTests(unittest.TestCase):
    def test_all_gpm_profile_starts_force_incognito_once(self):
        app = Orchestrator.__new__(Orchestrator)
        app.settings = SimpleNamespace(browser_window=BrowserWindowSettings())
        app.gpm = Mock()
        app.gpm.start_profile.return_value = SimpleNamespace(success=True)

        app.start_profile("profile-1", addination_args="--disable-extensions")
        self.assertEqual(
            app.gpm.start_profile.call_args.kwargs["addination_args"],
            "--disable-extensions --incognito",
        )

        app.start_profile("profile-2", addination_args="--incognito")
        self.assertEqual(
            app.gpm.start_profile.call_args.kwargs["addination_args"],
            "--incognito",
        )

    def test_hold_reuses_startup_incognito_context_for_new_tabs(self):
        page = Mock()
        startup_context = Mock()
        startup_context.pages = [page]
        browser = Mock()
        browser.contexts = [startup_context]
        session = SimpleNamespace(reuse_startup_context=True, proxy=None)

        self.assertIs(open_workflow_page(browser, {"start_result": session}), page)
        browser.new_context.assert_not_called()
        startup_context.new_page.assert_not_called()

    def test_hold_configures_authenticated_proxy_on_startup_context(self):
        page = Mock()
        cdp = Mock()
        startup_context = Mock()
        startup_context.pages = [page]
        startup_context.new_cdp_session.return_value = cdp
        browser = Mock()
        browser.contexts = [startup_context]
        session = SimpleNamespace(
            reuse_startup_context=True,
            proxy={"server": "http://proxy:8080", "username": "user", "password": "secret"},
        )

        self.assertIs(open_workflow_page(browser, {"start_result": session}), page)
        startup_context.new_cdp_session.assert_called_once_with(page)
        cdp.send.assert_called_once_with("Fetch.enable", {"handleAuthRequests": True})
        handlers = {entry.args[0]: entry.args[1] for entry in cdp.on.call_args_list}
        handlers["Fetch.authRequired"]({
            "requestId": "auth-1",
            "authChallenge": {"source": "Proxy"},
        })
        self.assertEqual(
            cdp.send.call_args,
            call(
                "Fetch.continueWithAuth",
                {
                    "requestId": "auth-1",
                    "authChallengeResponse": {
                        "response": "ProvideCredentials",
                        "username": "user",
                        "password": "secret",
                    },
                },
            ),
        )

    def test_registration_proxies_are_split_evenly_by_account_order(self):
        proxies = [SimpleNamespace(raw_proxy="proxy-1"), SimpleNamespace(raw_proxy="proxy-2")]
        assigned = [
            registration_proxy_for_account(proxies, index, 10).raw_proxy
            for index in range(10)
        ]
        self.assertEqual(assigned, ["proxy-1"] * 5 + ["proxy-2"] * 5)

    def test_registration_proxy_split_handles_uneven_account_count(self):
        proxies = [SimpleNamespace(raw_proxy="proxy-1"), SimpleNamespace(raw_proxy="proxy-2")]
        assigned = [
            registration_proxy_for_account(proxies, index, 11).raw_proxy
            for index in range(11)
        ]
        self.assertEqual(assigned.count("proxy-1"), 6)
        self.assertEqual(assigned.count("proxy-2"), 5)

    def test_reg_capcut_loads_and_validates_proxy_file(self):
        app = RegCapCutApp.__new__(RegCapCutApp)
        app.accounts = []
        app.reg_account_tree = None
        app.status_label = Mock()
        with tempfile.TemporaryDirectory() as directory:
            app.proxy_path = Path(directory) / "proxy.txt"
            app.proxy_path.write_text(
                "proxy-one:8080\nproxy-two:8081:user:secret\ninvalid\nproxy-one:8080\n",
                encoding="utf-8",
            )
            app.load_proxies()

        self.assertEqual(
            [proxy.raw_proxy for proxy in app.proxies],
            ["proxy-one:8080", "proxy-two:8081:user:secret"],
        )
        app.status_label.configure.assert_called_with(
            text="Loaded: 0 accounts | Created: 0 | Failed: 0 | Proxy: 2"
        )

    def test_workflow_page_rejects_a_regular_chrome_session(self):
        browser = Mock()
        session = SimpleNamespace(reuse_startup_context=False, incognito=False, proxy=None)
        with self.assertRaisesRegex(RuntimeError, "requires an incognito"):
            open_workflow_page(browser, {"start_result": session})
        browser.new_context.assert_not_called()

    def test_delete_checked_reg_accounts_removes_only_checked_rows(self):
        app = RegCapCutApp.__new__(RegCapCutApp)
        app.task_thread = None
        app.accounts = [
            {"user": "one@example.com", "picked": True},
            {"user": "two@example.com", "picked": False},
            {"user": "three@example.com", "picked": True},
        ]
        app.refresh_status = Mock()
        with patch("modules.ui.reg_capcut_tab.messagebox.askyesno", return_value=True):
            app.delete_checked_reg_accounts()
        self.assertEqual([account["user"] for account in app.accounts], ["two@example.com"])
        app.refresh_status.assert_called_once_with()

    def test_delete_checked_reg_accounts_does_nothing_without_checks(self):
        app = RegCapCutApp.__new__(RegCapCutApp)
        app.task_thread = None
        app.accounts = [{"user": "one@example.com", "picked": False}]
        app.refresh_status = Mock()
        with patch("modules.ui.reg_capcut_tab.messagebox.showinfo") as info:
            app.delete_checked_reg_accounts()
        info.assert_called_once()
        app.refresh_status.assert_not_called()

    def test_hold_waits_for_process_exit_without_cdp_probe(self):
        app = RegCapCutApp.__new__(RegCapCutApp)
        app.stop_event = Mock()
        app.stop_event.is_set.return_value = False
        session = Mock()
        session.poll.side_effect = [None, None, 0]
        with patch("modules.ui.reg_capcut_tab.urlopen", side_effect=OSError("temporary outage")) as probe:
            app._wait_for_hold_profile(session, "test")
        probe.assert_not_called()
        self.assertEqual(session.poll.call_count, 3)
        self.assertEqual([call.args[0] for call in app.stop_event.wait.call_args_list], [0.5, 0.5, 3])
        session.terminate.assert_not_called()

    def test_stop_interrupts_hold_without_waiting_for_process_exit(self):
        app = RegCapCutApp.__new__(RegCapCutApp)
        app.stop_event = threading.Event()
        session = Mock()
        session.poll.return_value = None
        with patch.object(app.stop_event, "wait", side_effect=lambda _: app.stop_event.set()):
            app._wait_for_hold_profile(session, "test")
        self.assertEqual(session.poll.call_count, 1)

    def test_proxy_credentials_are_separate_from_browser_arguments(self):
        for raw in ("host:8080:user:secret", "http://user:secret@host:8080"):
            self.assertEqual(parse_browser_proxy(raw), {
                "server": "http://host:8080", "username": "user", "password": "secret",
            })
        self.assertEqual(parse_browser_proxy("socks5://host:1080"), {"server": "socks5://host:1080"})
        self.assertIsNone(parse_browser_proxy(""))
        with self.assertRaises(RuntimeError) as error:
            parse_browser_proxy("socks5://user:secret@host:1080")
        self.assertNotIn("secret", str(error.exception))

    def test_account_browser_launches_without_proxy_and_keeps_thread_layout(self):
        app = RegCapCutApp.__new__(RegCapCutApp)
        account = {"user": "mail"}
        app._start_standalone_chromium = Mock(return_value=("process", "result"))
        self.assertEqual(app._start_account_browser(account, 4, 3), ("process", "result"))
        kwargs = app._start_standalone_chromium.call_args.kwargs
        self.assertNotIn("raw_proxy", kwargs)
        self.assertEqual(kwargs["max_threads"], 3)

    def test_account_browser_can_reuse_the_visible_startup_incognito_context(self):
        app = RegCapCutApp.__new__(RegCapCutApp)
        account = {"user": "mail"}
        app.hold_mode = False
        app._start_standalone_chromium = Mock(return_value=("process", "result"))

        result = app._start_account_browser(
            account,
            0,
            1,
            reuse_startup_context=True,
        )

        self.assertEqual(result, ("process", "result"))
        self.assertTrue(
            app._start_standalone_chromium.call_args.kwargs["reuse_startup_context"]
        )

    def test_registration_uses_configured_chrome_154_incognito_session(self):
        app = RegCapCutApp.__new__(RegCapCutApp)
        account = {"user": "mail"}
        app.hold_mode = False
        app._start_standalone_chromium = Mock(return_value=("process", "result"))

        result = app._start_registration_browser(account, 1, 3)

        self.assertEqual(result, ("process", "result"))
        kwargs = app._start_standalone_chromium.call_args.kwargs
        self.assertNotIn("browser_override", kwargs)
        self.assertEqual(kwargs["raw_proxy"], "")
        self.assertEqual(kwargs["max_threads"], 3)
        self.assertNotIn("expected_major", kwargs)
        self.assertFalse(kwargs["reuse_startup_context"])

    def test_registration_hold_reuses_original_incognito_window(self):
        app = RegCapCutApp.__new__(RegCapCutApp)
        app.hold_mode = True
        app._start_standalone_chromium = Mock(return_value=("process", "result"))

        result = app._start_registration_browser({"user": "mail"}, 0, 2)

        self.assertEqual(result, ("process", "result"))
        self.assertTrue(
            app._start_standalone_chromium.call_args.kwargs[
                "reuse_startup_context"
            ]
        )
        self.assertEqual(
            app._start_standalone_chromium.call_args.kwargs["profile_root"],
            Path("reg treo") / "chromium_profiles" / "1",
        )

    def test_registration_passes_assigned_proxy_to_chromium(self):
        app = RegCapCutApp.__new__(RegCapCutApp)
        app.hold_mode = True
        app._start_standalone_chromium = Mock(return_value=("process", "result"))

        app._start_registration_browser(
            {"user": "mail", "registration_proxy": "proxy.local:8080:user:secret"},
            0,
            2,
        )

        kwargs = app._start_standalone_chromium.call_args.kwargs
        self.assertEqual(kwargs["raw_proxy"], "proxy.local:8080:user:secret")
        self.assertTrue(kwargs["reuse_startup_context"])

    def test_registration_buttons_select_normal_or_hold_mode(self):
        app = RegCapCutApp.__new__(RegCapCutApp)
        app._start_registration = Mock()

        app.start()
        app.start_registration_hold()

        self.assertEqual(
            app._start_registration.call_args_list,
            [call(hold_open=False), call(hold_open=True)],
        )

    def test_standalone_browser_resolves_and_enforces_chrome_154(self):
        app = RegCapCutApp.__new__(RegCapCutApp)
        app.app_settings = SimpleNamespace(chromium_path="custom-chrome", browser_window=None)
        app.stop_event = threading.Event()
        app.active_profiles_lock = threading.Lock()
        app.active_standalone_processes = {}
        session = SimpleNamespace(profile_dir=Path("temporary-profile"))

        with (
            patch("modules.ui.reg_capcut_tab.resolve_chromium_154", return_value=Path("chrome.exe")) as resolve,
            patch("modules.ui.reg_capcut_tab.ChromiumSession", return_value=session) as chromium_session,
        ):
            process, result = app._start_standalone_chromium({"user": "mail"}, 0)

        resolve.assert_called_once_with("custom-chrome")
        self.assertEqual(chromium_session.call_args.args[0], Path("chrome.exe"))
        self.assertEqual(chromium_session.call_args.kwargs["expected_major"], "154")
        self.assertEqual(
            chromium_session.call_args.kwargs["startup_url"],
            "https://www.capcut.com/signup",
        )
        self.assertIs(process, session)
        self.assertIs(result, session)

    def test_stop_closes_every_session_without_gpm(self):
        app = RegCapCutApp.__new__(RegCapCutApp)
        app.stop_event = threading.Event()
        app.task_thread = SimpleNamespace(is_alive=lambda: True)
        app.active_profiles_lock = threading.Lock()
        first, second = Mock(), Mock()
        app.active_standalone_processes = {"one": first, "two": second}
        app.stop()
        self.assertTrue(app.stop_event.is_set())
        first.terminate.assert_called_once()
        second.terminate.assert_called_once()
        self.assertEqual(app.active_standalone_processes, {})

    def test_capcut_in_combined_ui_does_not_call_gpm(self):
        from tk_ui import App
        app = App.__new__(App)
        app.stop_event = threading.Event()
        app.active_change_profiles_lock = threading.Lock()
        app.active_change_profiles = {}
        app.app_settings = SimpleNamespace(chromium_path="chrome-154", browser_window=None)
        app.mark_account_status = Mock()
        app.proxy_for_worker = Mock(return_value=None)
        app.get_account_raw_proxy = Mock(return_value="")
        app.write_reg_capcut_result = Mock()
        # Deliberately no orchestrator: even a single GPM access fails this run.
        session = Mock()
        account = {"user": "mail"}
        with (
            patch("tk_ui.resolve_chromium_154", return_value="chrome.exe") as chrome,
            patch("tk_ui.ChromiumSession", return_value=session) as chromium_session,
            patch("tk_ui.run_capcut_workflow", return_value=True) as workflow,
        ):
            app.run_change_worker(account, "capcut", 0, 1, threading.Semaphore(1), "Reg CapCut", 1)
        chrome.assert_called_once_with("chrome-154")
        self.assertEqual(chromium_session.call_args.args[0], "chrome.exe")
        self.assertNotIn("expected_major", chromium_session.call_args.kwargs)
        workflow.assert_called_once()
        self.assertIs(workflow.call_args.args[1]["start_result"], session)
        app.write_reg_capcut_result.assert_called_once_with(account, True)
        session.close.assert_called_once()

    def test_check_pro_closes_browser_even_if_result_persistence_fails(self):
        app = RegCapCutApp.__new__(RegCapCutApp)
        app.stop_event = threading.Event()
        app.active_profiles_lock = threading.Lock()
        process = Mock()
        app.active_standalone_processes = {"one": process}
        app._start_account_browser = Mock(return_value=(process, object()))
        app._save_add_link_account_state = Mock(side_effect=[None, OSError("disk full")])
        app.root = SimpleNamespace(after=lambda *args: None)
        app.refresh_add_link_status = lambda: None
        app.append_check_pro_result = Mock()
        app.check_pro_path = "unused"
        with patch("modules.ui.reg_capcut_tab.run_capcut_check_pro_workflow", return_value=True):
            with self.assertRaises(OSError):
                app.run_check_pro_worker({"user": "mail"}, "capcut", 0, 1, threading.Semaphore(1), 1)
        process.terminate.assert_called_once()

    def test_yearly_check_writes_total_and_yearly_result_files(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            app = RegCapCutApp.__new__(RegCapCutApp)
            app.stop_event = threading.Event()
            app.active_profiles_lock = threading.Lock()
            process = Mock()
            app.active_standalone_processes = {"one": process}
            app._start_account_browser = Mock(return_value=(process, object()))
            app._save_add_link_account_state = Mock()
            app.root = SimpleNamespace(after=lambda *args: None)
            app.refresh_add_link_status = lambda: None
            app.refresh_check_pro_status = lambda: None
            app.check_pro_lock = threading.Lock()
            app.check_pro_path = base / "capcut_pro.txt"
            app.check_pro_yearly_path = base / "capcut_pro_yearly.txt"
            app.check_pro_monthly_path = base / "capcut_pro_monthly.txt"
            app.check_not_pro_path = base / "capcut_not_pro.txt"
            account = {"user": "yearly@example.com", "passnew": "password"}

            with patch(
                "modules.ui.reg_capcut_tab.run_capcut_check_pro_workflow",
                return_value="yearly",
            ):
                app.run_check_pro_worker(
                    account, "capcut", 0, 1, threading.Semaphore(1), 1
                )

            self.assertEqual(account["pro_status"], "yearly")
            expected = "yearly@example.com|password\n"
            self.assertEqual(app.check_pro_path.read_text(encoding="utf-8"), expected)
            self.assertEqual(app.check_pro_yearly_path.read_text(encoding="utf-8"), expected)
            self.assertFalse(app.check_pro_monthly_path.exists())
            process.terminate.assert_called_once()


class ChromiumBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.browser_path = resolve_chromium_154()
        except RuntimeError as exc:
            raise unittest.SkipTest(str(exc))

    def test_real_154_sessions_are_incognito_isolated_and_disposable(self):
        first = ChromiumSession(self.browser_path, headless=True)
        second = None
        try:
            self.assertIn("--incognito", first.launch_args)
            self.assertIn("--no-first-run", first.launch_args)
            self.assertIn("--no-default-browser-check", first.launch_args)
            self.assertIn("--disable-popup-blocking", first.launch_args)
            second = ChromiumSession(self.browser_path, headless=True)
            self.assertNotEqual(first.profile_dir, second.profile_dir)
            self.assertNotEqual(first.remote_debugging_address, second.remote_debugging_address)
            with sync_playwright() as p:
                browser1 = p.chromium.connect_over_cdp("http://" + first.remote_debugging_address)
                browser2 = p.chromium.connect_over_cdp("http://" + second.remote_debugging_address)
                self.assertTrue(browser1.version.startswith("154."))
                page1 = open_workflow_page(browser1, {"start_result": first})
                page2 = open_workflow_page(browser2, {"start_result": second})
                for page in (page1, page2):
                    page.route("**/*", lambda route: route.fulfill(content_type="text/html", body="<title>isolated</title>"))
                    page.goto("https://session.test/")
                page1.context.add_cookies([{"name": "account", "value": "first", "url": "https://session.test"}])
                page1.evaluate("localStorage.setItem('account', 'first')")
                self.assertEqual(page2.context.cookies(), [])
                self.assertIsNone(page2.evaluate("localStorage.getItem('account')"))
                cdp = browser1.new_browser_cdp_session()
                ids = cdp.send("Target.getBrowserContexts")["browserContextIds"]
                target = page1.context.new_cdp_session(page1).send("Target.getTargetInfo")["targetInfo"]
                self.assertIn(target["browserContextId"], ids)
                first.close()
                self.assertEqual(page2.title(), "isolated")
        finally:
            first.close()
            if second is not None:
                second.close()
        self.assertFalse(first.profile_dir.exists())
        self.assertIsNotNone(first.poll())
        self.assertFalse(second.profile_dir.exists())

    def test_v75_compatible_profile_is_retained_after_incognito_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "reg treo" / "chromium_profiles" / "1"
            session = ChromiumSession(
                self.browser_path,
                headless=True,
                persistent_profile_dir=profile,
            )
            try:
                self.assertEqual(session.profile_dir, profile.resolve())
                self.assertIn(f"--user-data-dir={profile.resolve()}", session.launch_args)
            finally:
                session.close()
            self.assertTrue(profile.exists())

    def test_authenticated_http_proxy_is_used_by_incognito_context(self):
        authorized = []
        expected = "Basic " + base64.b64encode(b"test:password").decode()

        class Proxy(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.headers.get("Proxy-Authorization") != expected:
                    self.send_response(407)
                    self.send_header("Proxy-Authenticate", 'Basic realm="test"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                authorized.append(self.path)
                body = b"<title>proxied</title>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_CONNECT(self):
                try:
                    self.send_response(502)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for reuse_startup_context in (False, True):
                authorized.clear()
                session = ChromiumSession(
                    self.browser_path,
                    headless=True,
                    raw_proxy=f"127.0.0.1:{server.server_port}:test:password",
                    reuse_startup_context=reuse_startup_context,
                )
                try:
                    with sync_playwright() as p:
                        browser = p.chromium.connect_over_cdp("http://" + session.remote_debugging_address)
                        page = open_workflow_page(browser, {"start_result": session})
                        page.goto("http://proxy-test.invalid/", timeout=15000)
                        self.assertEqual(page.title(), "proxied")
                        self.assertIn("http://proxy-test.invalid/", authorized)
                finally:
                    session.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
