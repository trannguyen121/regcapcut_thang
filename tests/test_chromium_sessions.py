import base64
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright

from modules.browser.chromium import ChromiumSession, open_workflow_page, parse_browser_proxy, resolve_chromium_154
from modules.ui.reg_capcut_tab import RegCapCutApp


class ChromiumSessionTests(unittest.TestCase):
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

    def test_all_worker_launches_forward_proxy_and_thread_layout(self):
        app = RegCapCutApp.__new__(RegCapCutApp)
        proxy = object()
        account = {"user": "mail"}
        app.proxy_for_worker = Mock(return_value=proxy)
        app.get_account_raw_proxy = Mock(return_value="host:8080:user:pass")
        app._start_standalone_chromium = Mock(return_value=("process", "result"))
        self.assertEqual(app._start_account_browser(account, 4, 3), ("process", "result"))
        app.get_account_raw_proxy.assert_called_once_with(account, proxy)
        kwargs = app._start_standalone_chromium.call_args.kwargs
        self.assertEqual(kwargs["raw_proxy"], "host:8080:user:pass")
        self.assertEqual(kwargs["max_threads"], 3)

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
        app.app_settings = SimpleNamespace(chromium_path="chrome.exe", browser_window=None)
        app.mark_account_status = Mock()
        app.proxy_for_worker = Mock(return_value=None)
        app.get_account_raw_proxy = Mock(return_value="")
        app.write_reg_capcut_result = Mock()
        # Deliberately no orchestrator: even a single GPM access fails this run.
        session = Mock()
        account = {"user": "mail"}
        with (
            patch("tk_ui.resolve_chromium_154", return_value="chrome.exe"),
            patch("tk_ui.ChromiumSession", return_value=session),
            patch("tk_ui.run_capcut_workflow", return_value=True) as workflow,
        ):
            app.run_change_worker(account, "capcut", 0, 1, threading.Semaphore(1), "Reg CapCut", 1)
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
        session = None
        try:
            session = ChromiumSession(self.browser_path, headless=True,
                                      raw_proxy=f"127.0.0.1:{server.server_port}:test:password")
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp("http://" + session.remote_debugging_address)
                page = open_workflow_page(browser, {"start_result": session})
                page.goto("http://proxy-test.invalid/", timeout=15000)
                self.assertEqual(page.title(), "proxied")
                self.assertIn("http://proxy-test.invalid/", authorized)
        finally:
            if session is not None:
                session.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
