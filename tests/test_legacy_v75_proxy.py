import os
import inspect
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import legacy_regcapcut_v75_proxy as legacy_patch


class LegacyV75RegistrationPatchTests(unittest.TestCase):
    def test_patch_version_is_75100(self):
        self.assertEqual(legacy_patch.LEGACY_PATCH_VERSION, "7.5.100")
        version_file = Path(__file__).resolve().parents[1] / "REGCAPCUT_V75_VERSION.txt"
        self.assertEqual(version_file.read_text(encoding="utf-8").strip(), "7.5.100")

    def test_only_explicit_22000_vnd_offer_is_accepted(self):
        accepted = ("22,000đ", "22000d", "₫22000", "22.000 VND")
        rejected = (
            "222,000đ /month",
            "222000d/month",
            "0đ for 7 days",
            "2,049,000đ yearly",
            "Pro 222,000đ /month 22,000đ",
            "Pro",
        )

        for text in accepted:
            with self.subTest(text=text):
                self.assertTrue(legacy_patch._has_discounted_22000_price(text))
        for text in rejected:
            with self.subTest(text=text):
                self.assertFalse(legacy_patch._has_discounted_22000_price(text))

    def test_checkout_requires_due_today_22000(self):
        self.assertTrue(
            legacy_patch._checkout_due_today_is_22000(
                "Standard price ₫222,000/month\nDue today\n₫22,000"
            )
        )
        self.assertFalse(
            legacy_patch._checkout_due_today_is_22000(
                "Standard price ₫222,000/month\nDue today\n₫222,000"
            )
        )
        self.assertFalse(
            legacy_patch._checkout_due_today_is_22000(
                "Pro 22,000đ first month\nDue today\n₫222,000"
            )
        )
        self.assertTrue(
            legacy_patch._checkout_due_today_is_22000(
                "First-time discount ₫22,000/month\n"
                "Standard price ₫222,000/month\n"
                "Due today ₫ 0 1 2 3 4 5 6 7 8 9 , 0 1 2 3 4 5 6 7 8 9"
            )
        )

    def test_generic_authenticated_my_cloud_is_accepted_for_fresh_accounts(self):
        source = inspect.getsource(legacy_patch.open_pro_checkout_across_five_tabs)

        self.assertIn(r"/my-cloud(?:/[^/?#]+)?(?:[/?#]|$)", source)

    def test_fallback_pages_are_opened_as_sibling_tabs_from_registered_page(self):
        source = inspect.getsource(legacy_patch.open_pro_checkout_across_five_tabs)
        fallback_source = source[source.index("candidate_tabs = []") :]

        self.assertIn("workspace_page.locator", source)
        self.assertIn("link.target = '_blank'", source)
        self.assertNotIn("browser_context.new_page()", fallback_source)
        self.assertLess(
            source.index('attempt_checkout(workspace_page, "registered tab")'),
            source.index("def open_adjacent_tab_from_workspace"),
        )

    def test_checkout_switches_yearly_pricing_to_monthly_before_plan_scan(self):
        source = inspect.getsource(legacy_patch.open_pro_checkout_across_five_tabs)

        self.assertIn("Switched pricing from Yearly to Monthly", source)
        self.assertIn("Monthly|Theo tháng", source)
        self.assertIn("Selected 22,000d Monthly offer", source)
        self.assertIn("Found black Update in exact", source)
        self.assertIn("ancestor-or-self::*[self::button or", source)
        self.assertIn("Purchase|Buy now|Mua|Thanh toán", source)
        self.assertNotIn("free_trial_fallback", source)
        self.assertNotIn('titles = page.get_by_text', source)
        self.assertLess(
            source.index("monthly_selected = select_monthly_pricing(page)"),
            source.index("monthly_prices = page.get_by_text"),
        )
        self.assertLess(
            source.index("Found black Update in exact"),
            source.index("# The newer pricing sheet uses one shared Purchase action"),
        )

    def test_every_checkout_attempt_reloads_before_waiting_for_upgrade(self):
        source = inspect.getsource(legacy_patch.open_pro_checkout_across_five_tabs)
        attempt_source = source[
            source.index("def attempt_checkout") : source.index(
                "# Always use the just-registered incognito tab first"
            )
        ]

        self.assertIn('page.reload(wait_until="domcontentloaded"', source)
        self.assertLess(
            source.index('attempt_checkout(workspace_page, "registered tab")'),
            source.index("for reload_round in range(1, 3)"),
        )
        self.assertIn('attempt_checkout(workspace_page, "registered tab")', source)
        self.assertIn("Reloading all 5 fallback tabs", source)
        self.assertIn("for reload_round in range(1, 3)", source)
        self.assertIn("reload_before_upgrade(page, location)", source)
        self.assertIn("attempt_checkout(page, location, reload_before=False)", source)
        self.assertIn("time.sleep(1)", source)
        fallback_rounds = source[source.index("for reload_round in range(1, 3)") :]
        self.assertNotIn("page.wait_for_timeout(1000)", fallback_rounds)
        self.assertLess(
            source.index("for tab_number, (page, _navigation_error)"),
            source.index("for tab_number, (page, reload_error)"),
        )
        self.assertIn("def wait_for_top_upgrade(page, timeout=60)", source)
        self.assertIn("def find_pro_button(page, timeout=60)", source)
        self.assertIn("amount_deadline = time.time() + 30", source)

    def test_five_tab_checkout_requires_original_incognito_context(self):
        workflow = Mock()
        workflow.__globals__ = {
            "sync_playwright": Mock(),
            "_connect_browser": Mock(),
            "_cdp_url": Mock(return_value="http://127.0.0.1:9222"),
        }

        for session in (
            types.SimpleNamespace(incognito=False, reuse_startup_context=True),
            types.SimpleNamespace(incognito=True, reuse_startup_context=False),
        ):
            with self.subTest(session=session):
                with self.assertRaisesRegex(RuntimeError, "original incognito window"):
                    legacy_patch.open_pro_checkout_across_five_tabs(
                        {"user": "mail@example.com"},
                        {"start_result": session},
                        workflow,
                    )

    def test_reg_treo_start_result_describes_original_incognito_window(self):
        class LegacyApp:
            def proxy_for_worker(self, _index, _max_threads):
                return None

        layout = types.SimpleNamespace(position="0,0", size="800,600")
        settings_module = types.SimpleNamespace(resolve_browser_window_layout=Mock(return_value=layout))
        namespace = {
            "RegCapCutApp": LegacyApp,
            "CAPCUT_SIGN_UP_URL": "https://www.capcut.com/signup",
            "run_capcut_workflow": Mock(return_value=True),
        }
        with patch.dict(sys.modules, {"core.settings": settings_module}):
            legacy_patch.patch_hold_proxy(namespace, Mock(return_value=True))

        with tempfile.TemporaryDirectory() as temp_dir:
            browser = Path(temp_dir) / "chrome.exe"
            browser.touch()
            app = LegacyApp()
            app.app_settings = types.SimpleNamespace(
                chromium_path=str(browser),
                browser_window=object(),
                thuecloud=types.SimpleNamespace(enabled=False),
            )
            listener = MagicMock()
            listener.getsockname.return_value = ("127.0.0.1", 9222)
            connection = MagicMock()
            process = MagicMock()
            popen = MagicMock(return_value=process)
            previous_cwd = os.getcwd()
            try:
                os.chdir(temp_dir)
                with (
                    patch.object(legacy_patch.socket, "socket", return_value=listener),
                    patch.object(legacy_patch.socket, "create_connection", return_value=connection),
                    patch.object(legacy_patch.subprocess, "Popen", popen),
                ):
                    _, result = app._start_standalone_chromium({"user": "mail@example.com"}, 0)
            finally:
                os.chdir(previous_cwd)

        self.assertTrue(result.incognito)
        self.assertTrue(result.reuse_startup_context)
        self.assertIsNone(result.proxy)
        launch_args = popen.call_args.args[0]
        self.assertIn("--disable-component-update", launch_args)
        self.assertIn("--disable-background-networking", launch_args)

    def test_modern_workflow_is_used_only_for_reg_treo(self):
        class LegacyApp:
            def proxy_for_worker(self, _index, _max_threads):
                return None

        legacy_workflow = Mock(return_value=True)
        modern_workflow = Mock(return_value=True)
        namespace = {
            "RegCapCutApp": LegacyApp,
            "CAPCUT_SIGN_UP_URL": "https://www.capcut.com/signup",
            "run_capcut_workflow": legacy_workflow,
        }
        settings_module = types.SimpleNamespace(resolve_browser_window_layout=Mock())

        with patch.dict(sys.modules, {"core.settings": settings_module}):
            legacy_patch.patch_hold_proxy(namespace, modern_workflow)

        account = {"user": "mail@example.com"}
        with patch.object(legacy_patch, "open_pro_checkout_across_five_tabs"):
            namespace["run_capcut_workflow"](
                account,
                {"start_result": types.SimpleNamespace(reg_treo=True)},
            )
            namespace["run_capcut_workflow"](
                account,
                {"start_result": types.SimpleNamespace(reg_treo=False)},
            )

        modern_workflow.assert_called_once()
        legacy_workflow.assert_called_once()

    def test_source_mode_loads_current_registration_workflow(self):
        workflow = legacy_patch.load_modern_registration_workflow()

        self.assertEqual(workflow.__name__, "run_capcut_workflow")
        self.assertTrue(workflow.__code__.co_filename.endswith("capcut_workflow.py"))

    def test_existing_account_waits_before_clicking_sign_in(self):
        workflow = legacy_patch.load_modern_registration_workflow()
        login_helper = workflow.__globals__["_login_existing_capcut_account"]
        source = inspect.getsource(login_helper)

        self.assertIn("sign_in_wait_seconds = max(60.0", source)
        self.assertIn("sign_in_click_timeout = max(45000", source)
        self.assertIn('_wait(2.0, stop_event, user)', source)
        self.assertIn("max(90.0, (step_timeout / 1000.0) * 1.5)", source)
        self.assertLess(
            source.index('_wait(2.0, stop_event, user)'),
            source.index("login_control.click(timeout=sign_in_click_timeout)"),
        )

    def test_registration_polls_otp_faster_and_resends_more_often(self):
        workflow = legacy_patch.load_modern_registration_workflow()
        globals_ = workflow.__globals__
        resend_source = inspect.getsource(globals_["_get_capcut_code_with_resend"])
        poll_source = inspect.getsource(globals_["_wait_for_capcut_code"])
        workflow_source = inspect.getsource(workflow)

        self.assertIn("initial_timeout: float = 25.0", resend_source)
        self.assertIn("max_resends: int = 3", resend_source)
        self.assertIn("resent_timeout: float = 45.0", resend_source)
        self.assertIn("for resend_attempt in range(1, max_resends + 1)", resend_source)
        self.assertIn("_wait(1.0, stop_event, user)", poll_source)
        self.assertIn("otp_fill_attempts = 5", workflow_source)
        self.assertIn("initial_timeout=25.0", workflow_source)


if __name__ == "__main__":
    unittest.main()
