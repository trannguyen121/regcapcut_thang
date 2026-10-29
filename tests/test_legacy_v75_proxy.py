import os
import inspect
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import legacy_regcapcut_v75_proxy as legacy_patch


class LegacyV75RegistrationPatchTests(unittest.TestCase):
    def test_patch_version_is_75507(self):
        self.assertEqual(legacy_patch.LEGACY_PATCH_VERSION, "7.5.507")
        version_file = Path(__file__).resolve().parents[1] / "REGCAPCUT_V75_VERSION.txt"
        self.assertEqual(version_file.read_text(encoding="utf-8").strip(), "7.5.507")

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

    def test_pause_event_blocks_workers_and_resume_continues(self):
        control = legacy_patch.PauseAwareEvent()
        control.pause()
        result = []

        worker = threading.Thread(target=lambda: result.append(control.is_set()))
        worker.start()
        self.assertTrue(control.is_paused())
        self.assertTrue(worker.is_alive())

        control.resume()
        worker.join(timeout=1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [False])

    def test_stop_wakes_a_paused_worker(self):
        control = legacy_patch.PauseAwareEvent()
        control.pause()
        result = []
        worker = threading.Thread(target=lambda: result.append(control.wait(30)))
        worker.start()

        control.set()
        worker.join(timeout=1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [True])
        self.assertFalse(control.is_paused())

    def test_pause_button_toggles_pause_and_resume(self):
        class LegacyApp:
            def proxy_for_worker(self, _index, _max_threads):
                return None

        namespace = {
            "RegCapCutApp": LegacyApp,
            "CAPCUT_SIGN_UP_URL": "https://www.capcut.com/signup",
            "run_capcut_workflow": Mock(return_value=False),
            "CapCutWorkflowInterrupted": RuntimeError,
        }
        settings_module = types.SimpleNamespace(resolve_browser_window_layout=Mock())
        with patch.dict(sys.modules, {"core.settings": settings_module}):
            legacy_patch.patch_hold_proxy(namespace, Mock(return_value=True))

        app = LegacyApp.__new__(LegacyApp)
        app.task_thread = Mock()
        app.task_thread.is_alive.return_value = True
        app.stop_event = legacy_patch.PauseAwareEvent()
        app.pause_button = Mock()

        self.assertTrue(app.toggle_pause())
        self.assertTrue(app.stop_event.is_paused())
        app.pause_button.configure.assert_called_with(
            text="Tiếp tục",
            bg="#2563eb",
            activebackground="#1d4ed8",
        )

        self.assertFalse(app.toggle_pause())
        self.assertFalse(app.stop_event.is_paused())
        app.pause_button.configure.assert_called_with(
            text="Tạm dừng",
            bg="#f59e0b",
            activebackground="#d97706",
        )

    def test_pause_button_is_packed_before_stop_so_it_stays_visible(self):
        source = inspect.getsource(legacy_patch.patch_hold_proxy)

        self.assertIn('if child_label == "stop":', source)
        self.assertIn('pause_pack["before"] = stop_button', source)
        self.assertIn('self.pause_button.pack(**pause_pack)', source)

    def test_monthly_context_accepts_current_slash_month_ui(self):
        accepted = ("Monthly", "22,000đ /month", "22.000 VND per month", "Hàng tháng")
        rejected = ("Yearly", "One year", "222,000đ")

        for text in accepted:
            with self.subTest(text=text):
                self.assertTrue(legacy_patch._is_monthly_price_context(text))
        for text in rejected:
            with self.subTest(text=text):
                self.assertFalse(legacy_patch._is_monthly_price_context(text))

    def test_checkout_requires_due_today_22000(self):
        self.assertTrue(
            legacy_patch._checkout_due_today_is_22000(
                "Standard price ₫222,000/month\nDue today\n₫22,000"
            )
        )

    def test_checkout_url_rejects_unrelated_popup_targets(self):
        self.assertTrue(legacy_patch._is_checkout_url("https://cashier-my4a.pipopay.com/payin_checkout/1"))
        self.assertTrue(legacy_patch._is_checkout_url("https://example.com/payment/checkout"))
        self.assertFalse(legacy_patch._is_checkout_url("https://www.capcut.com/my-cloud/123?tab=all"))
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
        self.assertIn("if tab_number < 5:", fallback_source)
        self.assertIn("time.sleep(1.0)", fallback_source)
        self.assertNotIn("browser_context.new_page()", fallback_source)
        self.assertLess(
            source.index('"registered tab"'),
            source.index("def open_adjacent_tab_from_workspace"),
        )

    def test_checkout_waits_for_confirmed_payment_before_success(self):
        source = inspect.getsource(legacy_patch.open_pro_checkout_across_five_tabs)
        primary_attempt = source[
            source.index("# Always inspect the just-registered incognito tab first") :
            source.index("def open_adjacent_tab_from_workspace")
        ]

        self.assertIn('"registered tab"', primary_attempt)
        self.assertIn("stopping all reload/click activity", source)
        self.assertIn("waiting for manual payment confirmation", source)
        self.assertIn("This is intentionally passive", source)
        self.assertIn('context["checkout_tab_closed"] = True', source)
        self.assertIn('context["payment_confirmed"] = True', source)
        self.assertIn('context["payment_confirmed"] = False', source)
        self.assertIn("Payment confirmed", source)
        self.assertIn("Checkout closed without confirmed payment", source)
        self.assertIn("payment_success_pattern", source)
        self.assertIn("paid_plan_visible", source)
        self.assertIn("reveal_account_plan", source)
        self.assertIn("if paid_plan_visible(candidate):", source)
        self.assertIn("candidate.is_closed()", source)
        self.assertIn("checkout_target_seen", source)
        self.assertIn("Checkout target disappeared within 5 seconds", source)
        self.assertIn("checkout_popup is None or checkout_popup.is_closed()", source)
        self.assertIn('force_primary_popup_close', source)
        self.assertIn('force_first_fallback_popup_close', source)
        self.assertIn('checkout_attempt_pages[-1]', source)
        self.assertIn('popup_closed_during_attempt = checkout_target_seen or any_attempt_page_closed', source)
        self.assertIn('if popup_closed_during_attempt:', source)
        self.assertIn('checkout popup opened but closed within 5 seconds', source)
        self.assertIn("def monitor_checkout_while_holding", source)
        self.assertIn("monitor_checkout_while_holding(checkout_popup, location)", source)
        self.assertNotIn("checkout popup closed while Chrome remained open", source)
        self.assertNotIn("force_primary_popup_close_during_hold", source)
        self.assertNotIn("Forced stable checkout popup close", source)
        self.assertIn("passive_page.wait_for_timeout(500)", source)
        self.assertIn('context["payment_workspace_url"] = workspace_url', source)

    def test_cancellation_reopens_workspace_and_failure_keeps_browser_open(self):
        cancel_source = inspect.getsource(legacy_patch.cancel_capcut_subscription)
        worker_source = inspect.getsource(legacy_patch.patch_hold_proxy)

        self.assertIn('context or {}).get("payment_workspace_url")', cancel_source)
        self.assertIn('page.goto(workspace_url', cancel_source)
        self.assertIn('workflow_context["cancellation_pending"] = True', worker_source)
        self.assertIn('reason="CANCEL"', worker_source)
        self.assertIn('and not workflow_context.get("cancellation_pending")', worker_source)
        self.assertIn("avatar_points", cancel_source)
        self.assertIn('(max(1, width - 24), 20)', cancel_source)
        self.assertIn('if "space settings" in menu_lowered:', cancel_source)
        self.assertIn('page.keyboard.press("Escape")', cancel_source)
        self.assertIn("page.wait_for_timeout(5000)", cancel_source)
        self.assertIn("for refresh_attempt in range(1, 4):", cancel_source)
        self.assertIn('page.reload(wait_until="domcontentloaded", timeout=30000)', cancel_source)
        self.assertIn("page.wait_for_timeout(2000)", cancel_source)
        self.assertIn("after 3 reloads", cancel_source)
        self.assertNotIn(
            'page.mouse.click(max(1, int(viewport["width"]) - 55), 75)',
            cancel_source,
        )

    def test_primary_offer_scan_uses_one_current_load_then_fallback(self):
        source = inspect.getsource(legacy_patch.open_pro_checkout_across_five_tabs)
        primary_flow = source[
            source.index("# Always inspect the just-registered incognito tab first") :
            source.index("def open_adjacent_tab_from_workspace")
        ]

        self.assertIn('progress["plan_action_clicked"] = True', source)
        self.assertIn('primary_progress.get("plan_action_clicked")', primary_flow)
        self.assertNotIn("for primary_attempt", primary_flow)
        self.assertIn("without reloading", primary_flow)
        self.assertNotIn("refusing to open fallback tabs", primary_flow)
        self.assertIn("find_pro_button(page, timeout=plan_timeout)", source)
        self.assertIn("f\"{plan_timeout:g} seconds\"", source)

    def test_checkout_switches_yearly_pricing_to_monthly_before_plan_scan(self):
        source = inspect.getsource(legacy_patch.open_pro_checkout_across_five_tabs)

        self.assertIn("Switched pricing from Yearly to Monthly", source)
        self.assertIn("Monthly|Theo tháng", source)
        self.assertIn("Selected 22,000d Monthly offer", source)
        self.assertIn("Found black Upgrade in exact", source)
        self.assertIn("22,000d/month Pro card", source)
        self.assertIn("hasAmount(text, '22000')", source)
        self.assertNotIn("normalize-space()='Update'", source)
        self.assertIn("ancestor-or-self::*[self::button or", source)
        self.assertIn("Purchase|Buy now|Mua|Thanh toán", source)
        self.assertNotIn("free_trial_fallback", source)
        self.assertNotIn('titles = page.get_by_text', source)
        self.assertIn("def dismiss_discount_offer_popup", source)
        self.assertIn("Dismissed blocking Get offer popup", source)
        self.assertIn("dismiss_discount_offer_popup(page, timeout=1.2)", source)
        self.assertIn('div.lv-modal', source)
        self.assertIn('lv-modal-close-icon[role="button"][aria-label="Close"]', source)
        self.assertNotIn("click_control(get_offer", source)
        self.assertLess(
            source.index("monthly_selected = select_monthly_pricing(page)"),
            source.index("monthly_prices = page.get_by_text"),
        )
        self.assertLess(
            source.index("Found black Upgrade in exact"),
            source.index("# The newer pricing sheet uses one shared Purchase action"),
        )

    def test_fallback_tabs_get_one_staggered_parallel_reload_round(self):
        source = inspect.getsource(legacy_patch.open_pro_checkout_across_five_tabs)
        attempt_source = source[
            source.index("def attempt_checkout") : source.index(
                "# Always inspect the just-registered incognito tab first"
            )
        ]

        self.assertNotIn("page.reload(", source)
        self.assertNotIn("window.location.reload()", source)
        self.assertIn("workspace_page,", source)
        self.assertIn("Fast first scan across 5 fallback tabs", source)
        self.assertIn('session.send("Page.reload", {"ignoreCache": True})', source)
        self.assertIn("Staggered reload", source)
        self.assertIn("stop_event.wait(0.7)", source)
        self.assertIn("stop_event.wait(2.0)", source)
        self.assertIn("Scanning reloaded fallback tabs", source)
        self.assertIn("All 5 Upgrade tabs failed after one staggered reload", source)
        self.assertIn("top_timeout=5", source)
        self.assertIn("plan_timeout=4", source)
        self.assertIn("top_timeout=8", source)
        self.assertIn("plan_timeout=6", source)
        self.assertIn("def wait_for_top_upgrade(page, timeout=10)", source)
        self.assertIn("def find_pro_button(page, timeout=60)", source)
        self.assertIn("deadline = time.time() + 5", attempt_source)
        self.assertIn("checkout popup did not open within 5 seconds", attempt_source)
        self.assertNotIn("amount_deadline", source)
        self.assertNotIn("stability_deadline", source)
        self.assertIn("Starting all 5 fallback tab navigations", source)
        self.assertIn('cdp_session.send("Page.navigate", {"url": workspace_url})', source)
        self.assertIn("shared_navigation_deadline = time.time() + 20", source)
        self.assertNotIn("window.location.replace(url)", source)

    def test_upgrade_click_path_uses_fast_timeouts(self):
        source = inspect.getsource(legacy_patch.open_pro_checkout_across_five_tabs)

        self.assertIn("control.click(timeout=1800)", source)
        self.assertIn("control.click(timeout=1800, force=True)", source)
        self.assertIn('control.evaluate("element => element.click()", timeout=1000)', source)
        self.assertNotIn("page.reload(", source)
        self.assertIn('f"top Upgrade did not appear within {top_timeout:g} seconds"', source)
        self.assertIn("find_pro_button(page, timeout=plan_timeout)", source)
        self.assertIn("deadline = time.time() + 5", source)

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
        write_result = Mock()
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
            app.hold_mode = True
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
        self.assertTrue(result.reg_treo)
        self.assertIsNone(result.proxy)
        launch_args = popen.call_args.args[0]
        self.assertIn("--disable-component-update", launch_args)
        self.assertIn("--disable-background-networking", launch_args)

    def test_portable_build_prefers_bundled_chrome_154(self):
        source = inspect.getsource(legacy_patch.patch_hold_proxy)
        spec = (Path(__file__).resolve().parents[1] / "regcapcut_v75_proxy.spec").read_text(
            encoding="utf-8"
        )

        self.assertIn('resource_root() / "chrome-154"', source)
        self.assertIn('application_root() / "chrome-154"', source)
        self.assertIn('self.app_settings.chromium_path = str(chromium_candidate)', source)
        self.assertIn(
            "Tree('H:\\\\regcapcut_v7.5\\\\chrome-154', prefix='chrome-154')",
            spec,
        )

    def test_gui_offers_one_shared_choice_between_chrome_154_and_machine_chrome(self):
        source = inspect.getsource(legacy_patch.patch_hold_proxy)

        self.assertIn('(\"Chrome 154\", _BROWSER_CHOICE_BUNDLED)', source)
        self.assertIn('(\"Chrome máy\", _BROWSER_CHOICE_SYSTEM)', source)
        self.assertIn("variable=self.browser_choice_var", source)
        self.assertIn("self.browser_choice = selected", source)
        self.assertIn("_write_browser_choice(selected)", source)
        self.assertIn('browser_choice == _BROWSER_CHOICE_SYSTEM', source)
        self.assertIn("_find_system_chrome()", source)

    def test_machine_chrome_discovery_finds_installed_google_chrome(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            expected = Path(temp_dir) / "Google" / "Chrome" / "Application" / "chrome.exe"
            expected.parent.mkdir(parents=True)
            expected.touch()
            environment = {
                "PROGRAMFILES": temp_dir,
                "PROGRAMFILES(X86)": str(Path(temp_dir) / "missing-x86"),
                "LOCALAPPDATA": str(Path(temp_dir) / "missing-local"),
            }
            with (
                patch.dict(os.environ, environment, clear=False),
                patch.object(legacy_patch.shutil, "which", return_value=None),
            ):
                result = legacy_patch._find_system_chrome()

        self.assertEqual(result, expected.resolve())

    def test_machine_chrome_choice_launches_detected_system_executable(self):
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
            browser = Path(temp_dir) / "Google" / "Chrome" / "Application" / "chrome.exe"
            browser.parent.mkdir(parents=True)
            browser.touch()
            app = LegacyApp()
            app.hold_mode = True
            app.browser_choice = legacy_patch._BROWSER_CHOICE_SYSTEM
            app._machine_chrome_path = browser
            app.app_settings = types.SimpleNamespace(
                chromium_path=str(Path(temp_dir) / "chrome-154"),
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
                    patch.object(legacy_patch, "_find_system_chrome", return_value=browser.resolve()),
                    patch.object(legacy_patch.socket, "socket", return_value=listener),
                    patch.object(legacy_patch.socket, "create_connection", return_value=connection),
                    patch.object(legacy_patch.subprocess, "Popen", popen),
                ):
                    _, result = app._start_standalone_chromium({"user": "mail@example.com"}, 0)
            finally:
                os.chdir(previous_cwd)

        self.assertTrue(result.incognito)
        self.assertEqual(Path(popen.call_args.args[0][0]), browser.resolve())
        self.assertIn("--incognito", popen.call_args.args[0])

    def test_modern_workflow_is_used_for_reg_treo_and_normal(self):
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
        normal_context = {"start_result": types.SimpleNamespace(reg_treo=False)}
        with patch.object(legacy_patch, "open_pro_checkout_across_five_tabs") as checkout:
            namespace["run_capcut_workflow"](
                account,
                {"start_result": types.SimpleNamespace(reg_treo=True)},
            )
            namespace["run_capcut_workflow"](
                account,
                normal_context,
            )

        self.assertEqual(modern_workflow.call_count, 2)
        legacy_workflow.assert_not_called()
        checkout.assert_called_once()
        self.assertTrue(normal_context["allow_existing_login"])

    def test_normal_worker_uses_standalone_chromium_and_closes_it(self):
        source = inspect.getsource(legacy_patch.patch_hold_proxy)

        self.assertIn("def run_worker", source)
        self.assertIn("self._start_standalone_chromium(account, index)", source)
        self.assertIn('mode_label = "REG TREO" if hold_mode else "REG THUONG"', source)
        self.assertIn("and not hold_mode", source)
        self.assertIn("standalone_process.terminate()", source)
        self.assertIn('app_class.run_worker = run_worker', source)
        self.assertIn('profile_root = "reg treo" if hold_mode else "reg thuong"', source)

    def test_normal_worker_finishes_account_and_advances_without_checkout(self):
        class LegacyApp:
            def proxy_for_worker(self, _index, _max_threads):
                return None

            def run_worker(self, *_args):
                raise AssertionError("legacy worker must be replaced")

        modern_workflow = Mock(return_value=True)
        write_result = Mock()
        namespace = {
            "RegCapCutApp": LegacyApp,
            "CAPCUT_SIGN_UP_URL": "https://www.capcut.com/signup",
            "run_capcut_workflow": Mock(return_value=False),
            "write_result": write_result,
            "CapCutWorkflowInterrupted": RuntimeError,
        }
        settings_module = types.SimpleNamespace(resolve_browser_window_layout=Mock())
        with patch.dict(sys.modules, {"core.settings": settings_module}):
            legacy_patch.patch_hold_proxy(namespace, modern_workflow)

        account = {"user": "normal@example.com", "passnew": "1234567"}
        process = Mock()
        process.poll.return_value = None
        start_result = types.SimpleNamespace(reg_treo=False)
        app = LegacyApp()
        app.hold_mode = False
        app.current_accounts = [account]
        app.stop_event = threading.Event()
        app.mark_account_status = Mock()
        app.queue_sheet_success = Mock()
        app._request_expressvpn_reset = Mock()
        app._start_standalone_chromium = Mock(return_value=(process, start_result))
        app.write_cancelled_result = Mock()
        app.active_profiles_lock = threading.Lock()
        app.active_profiles = {}
        app.export_lock = threading.Lock()

        with patch.object(legacy_patch, "open_pro_checkout_across_five_tabs") as checkout:
            app.run_worker(account, "unused", 0, 1, threading.Semaphore(1), 1)

        modern_workflow.assert_called_once()
        checkout.assert_not_called()
        process.terminate.assert_called_once()
        write_result.assert_called_once_with(account, True, app.export_lock)
        app.queue_sheet_success.assert_called_once_with(account)
        app.mark_account_status.assert_any_call(account, "true")

    def test_reg_treo_worker_exports_only_after_payment_confirmation(self):
        class LegacyApp:
            def proxy_for_worker(self, _index, _max_threads):
                return None

            def run_worker(self, *_args):
                raise AssertionError("legacy worker must be replaced")

        modern_workflow = Mock(return_value=True)
        write_result = Mock()
        namespace = {
            "RegCapCutApp": LegacyApp,
            "CAPCUT_SIGN_UP_URL": "https://www.capcut.com/signup",
            "run_capcut_workflow": Mock(return_value=False),
            "write_result": write_result,
            "CapCutWorkflowInterrupted": RuntimeError,
        }
        settings_module = types.SimpleNamespace(resolve_browser_window_layout=Mock())
        with patch.dict(sys.modules, {"core.settings": settings_module}):
            legacy_patch.patch_hold_proxy(namespace, modern_workflow)

        account = {"user": "hold@example.com", "passnew": "1234567"}
        process = Mock()
        process.poll.return_value = None
        start_result = types.SimpleNamespace(reg_treo=True, activate_proxy=None)
        app = LegacyApp()
        app.hold_mode = True
        app.current_accounts = [account]
        app.stop_event = threading.Event()
        app.mark_account_status = Mock()
        app.queue_sheet_success = Mock()
        app._request_expressvpn_reset = Mock()
        app._wait_for_hold_profile = Mock()
        app._start_standalone_chromium = Mock(return_value=(process, start_result))
        app.write_cancelled_result = Mock()
        app.active_profiles_lock = threading.Lock()
        app.active_profiles = {}
        app.export_lock = threading.Lock()

        def signal_payment_confirmed(_account, context, _workflow):
            context["payment_confirmed"] = True
            return True

        with patch.object(
            legacy_patch,
            "open_pro_checkout_across_five_tabs",
            side_effect=signal_payment_confirmed,
        ), patch.object(
            legacy_patch,
            "cancel_capcut_subscription",
            return_value=True,
        ) as cancel_subscription:
            app.run_worker(account, "unused", 0, 1, threading.Semaphore(1), 1)

        app._wait_for_hold_profile.assert_not_called()
        process.terminate.assert_called_once()
        write_result.assert_called_once_with(account, True, app.export_lock)
        app.queue_sheet_success.assert_called_once_with(account)
        cancel_subscription.assert_called_once()
        app.write_cancelled_result.assert_called_once_with(account, app.export_lock)
        app.mark_account_status.assert_any_call(account, "Đã pay - Đã huỷ gói")

    def test_reg_treo_keeps_paid_export_when_cancellation_fails(self):
        class LegacyApp:
            def proxy_for_worker(self, _index, _max_threads):
                return None

            def run_worker(self, *_args):
                raise AssertionError("legacy worker must be replaced")

        modern_workflow = Mock(return_value=True)
        write_result = Mock()
        namespace = {
            "RegCapCutApp": LegacyApp,
            "CAPCUT_SIGN_UP_URL": "https://www.capcut.com/signup",
            "run_capcut_workflow": Mock(return_value=False),
            "write_result": write_result,
            "CapCutWorkflowInterrupted": RuntimeError,
        }
        settings_module = types.SimpleNamespace(resolve_browser_window_layout=Mock())
        with patch.dict(sys.modules, {"core.settings": settings_module}):
            legacy_patch.patch_hold_proxy(namespace, modern_workflow)

        account = {"user": "paid@example.com", "passnew": "1234567"}
        process = Mock()
        process.poll.side_effect = [None, 0, 0]
        start_result = types.SimpleNamespace(reg_treo=True, activate_proxy=None)
        app = LegacyApp()
        app.hold_mode = True
        app.current_accounts = [account]
        app.stop_event = threading.Event()
        app.mark_account_status = Mock()
        app.queue_sheet_success = Mock()
        app._request_expressvpn_reset = Mock()
        app._wait_for_hold_profile = Mock()
        app._start_standalone_chromium = Mock(return_value=(process, start_result))
        app.write_cancelled_result = Mock()
        app.active_profiles_lock = threading.Lock()
        app.active_profiles = {}
        app.export_lock = threading.Lock()

        def signal_payment_confirmed(_account, context, _workflow):
            context["payment_confirmed"] = True
            return True

        with patch.object(
            legacy_patch,
            "open_pro_checkout_across_five_tabs",
            side_effect=signal_payment_confirmed,
        ), patch.object(
            legacy_patch,
            "cancel_capcut_subscription",
            side_effect=RuntimeError("cancel unavailable"),
        ), patch.object(legacy_patch.time, "sleep"):
            app.run_worker(account, "unused", 0, 1, threading.Semaphore(1), 1)

        write_result.assert_called_once_with(account, True, app.export_lock)
        app.write_cancelled_result.assert_not_called()
        app.mark_account_status.assert_any_call(account, "Đã pay - Chưa huỷ gói")
        process.terminate.assert_not_called()

    def test_reg_treo_saves_unpaid_and_advances_after_user_closes_chrome(self):
        class LegacyApp:
            def proxy_for_worker(self, _index, _max_threads):
                return None

            def run_worker(self, *_args):
                raise AssertionError("legacy worker must be replaced")

        modern_workflow = Mock(return_value=True)
        write_result = Mock()
        namespace = {
            "RegCapCutApp": LegacyApp,
            "CAPCUT_SIGN_UP_URL": "https://www.capcut.com/signup",
            "run_capcut_workflow": Mock(return_value=False),
            "write_result": write_result,
            "CapCutWorkflowInterrupted": RuntimeError,
        }
        settings_module = types.SimpleNamespace(resolve_browser_window_layout=Mock())
        with patch.dict(sys.modules, {"core.settings": settings_module}):
            legacy_patch.patch_hold_proxy(namespace, modern_workflow)

        account = {"user": "unpaid@example.com", "passnew": "1234567"}
        process = Mock()
        process.poll.side_effect = [None, 0, 0]
        start_result = types.SimpleNamespace(reg_treo=True, activate_proxy=None)
        app = LegacyApp()
        app.hold_mode = True
        app.current_accounts = [account]
        app.stop_event = threading.Event()
        app.mark_account_status = Mock()
        app.queue_sheet_success = Mock()
        app._request_expressvpn_reset = Mock()
        app._wait_for_hold_profile = Mock()
        app._start_standalone_chromium = Mock(return_value=(process, start_result))
        app.write_cancelled_result = Mock()
        app.write_unpaid_result = Mock()
        app.active_profiles_lock = threading.Lock()
        app.active_profiles = {}
        app.export_lock = threading.Lock()

        with patch.object(
            legacy_patch,
            "open_pro_checkout_across_five_tabs",
            return_value=False,
        ), patch.object(legacy_patch.time, "sleep"):
            app.run_worker(account, "unused", 0, 1, threading.Semaphore(1), 1)

        write_result.assert_not_called()
        app.write_cancelled_result.assert_not_called()
        app.write_unpaid_result.assert_called_once_with(account, app.export_lock)
        app._wait_for_hold_profile.assert_not_called()
        app.mark_account_status.assert_any_call(account, "Chưa pay")
        process.terminate.assert_not_called()

    def test_stop_immediately_closes_tracked_registration_browsers(self):
        class LegacyApp:
            def proxy_for_worker(self, _index, _max_threads):
                return None

        namespace = {
            "RegCapCutApp": LegacyApp,
            "CAPCUT_SIGN_UP_URL": "https://www.capcut.com/signup",
            "run_capcut_workflow": Mock(return_value=False),
            "write_result": Mock(),
            "CapCutWorkflowInterrupted": RuntimeError,
        }
        settings_module = types.SimpleNamespace(resolve_browser_window_layout=Mock())
        with patch.dict(sys.modules, {"core.settings": settings_module}):
            legacy_patch.patch_hold_proxy(namespace, Mock(return_value=True))

        process = Mock()
        process.poll.return_value = None
        app = LegacyApp.__new__(LegacyApp)
        app.task_thread = Mock()
        app.task_thread.is_alive.return_value = True
        app.stop_event = threading.Event()
        app.active_profiles_lock = threading.Lock()
        app.active_standalone_processes = {"active@example.com": process}

        app.stop()

        self.assertTrue(app.stop_event.is_set())
        process.terminate.assert_called_once_with()

    def test_stop_exception_is_not_exported_as_account_error(self):
        class LegacyApp:
            def proxy_for_worker(self, _index, _max_threads):
                return None

        def interrupted_workflow(_account, context):
            context["stop_event"].set()
            raise RuntimeError("Stop requested for stopped@example.com")

        write_result = Mock()
        namespace = {
            "RegCapCutApp": LegacyApp,
            "CAPCUT_SIGN_UP_URL": "https://www.capcut.com/signup",
            "run_capcut_workflow": Mock(return_value=False),
            "write_result": write_result,
            "CapCutWorkflowInterrupted": type("WorkflowInterrupted", (Exception,), {}),
        }
        settings_module = types.SimpleNamespace(resolve_browser_window_layout=Mock())
        with patch.dict(sys.modules, {"core.settings": settings_module}):
            legacy_patch.patch_hold_proxy(namespace, interrupted_workflow)

        account = {"user": "stopped@example.com", "passnew": "1234567"}
        process = Mock()
        process.poll.return_value = None
        app = LegacyApp.__new__(LegacyApp)
        app.hold_mode = True
        app.current_accounts = [account]
        app.stop_event = threading.Event()
        app.mark_account_status = Mock()
        app._request_expressvpn_reset = Mock()
        app._start_standalone_chromium = Mock(
            return_value=(process, types.SimpleNamespace(reg_treo=True, activate_proxy=None))
        )
        app.active_profiles_lock = threading.Lock()
        app.active_profiles = {}
        app.active_standalone_processes = {}
        app.export_lock = threading.Lock()

        app.run_worker(account, "unused", 0, 1, threading.Semaphore(1), 1)

        write_result.assert_not_called()
        app.mark_account_status.assert_any_call(account, "stopped")
        process.terminate.assert_called_once_with()
        self.assertEqual(app.active_standalone_processes, {})

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

    def test_slow_signup_route_is_not_assumed_to_be_existing_account(self):
        workflow = legacy_patch.load_modern_registration_workflow()
        source = inspect.getsource(workflow)
        route_probe = source[
            source.index("visible_signup = _first_visible_control") :
            source.index("birthday_year = page.locator")
        ]

        self.assertNotIn("_login_existing_capcut_account", route_probe)
        self.assertIn("Signup route still resolving after password", route_probe)
        self.assertIn("Explicit existing-account route detected", source)

    def test_registration_polls_otp_faster_and_resends_more_often(self):
        workflow = legacy_patch.load_modern_registration_workflow()
        globals_ = workflow.__globals__
        resend_source = inspect.getsource(globals_["_get_capcut_code_with_resend"])
        poll_source = inspect.getsource(globals_["_wait_for_capcut_code"])
        workflow_source = inspect.getsource(workflow)

        self.assertIn("initial_timeout: float = 25.0", resend_source)
        self.assertIn("max_resends: int = 3", resend_source)
        self.assertIn("resent_timeout: float = 45.0", resend_source)
        self.assertIn("previous_fingerprints: set[str] | None = None", resend_source)
        self.assertIn("for resend_attempt in range(1, max_resends + 1)", resend_source)
        self.assertIn("_wait(1.0, stop_event, user)", poll_source)
        self.assertIn("timeout=min(5.0, remaining)", poll_source)
        resend_click_source = inspect.getsource(globals_["_resend_capcut_code"])
        self.assertIn("resend(?:\\s+code)?", resend_click_source)
        self.assertIn("candidate.click(timeout=800)", resend_click_source)
        self.assertIn('candidate.evaluate("element => element.click()"', resend_click_source)
        self.assertIn("_get_messages(account, timeout=3.0)", resend_source)
        self.assertIn("resend_e2e_only", workflow_source)
        self.assertIn("stopping before OTP entry", workflow_source)
        self.assertIn("otp_fill_attempts = 5", workflow_source)
        self.assertIn("initial_timeout=25.0", workflow_source)
        self.assertIn("TimeoutError", inspect.getsource(globals_["_get_messages"]))
        self.assertEqual(
            globals_["OAUTH2_MESSAGES_URL"],
            "https://tools.dongvanfb.net/api/get_messages_oauth2",
        )
        payload = globals_["_oauth2_payload"](
            {
                "api": "mail@example.com|mail-pass|refresh-token|client-id",
            }
        )
        self.assertEqual(payload["email"], "mail@example.com")
        self.assertEqual(payload["refresh_token"], "refresh-token")
        self.assertEqual(payload["client_id"], "client-id")
        self.assertEqual(payload["list_mail"], "all")
        self.assertIn("backslashreplace", inspect.getsource(globals_["_get_messages"]))

    def test_birthday_continue_does_not_wait_for_mailbox_snapshot(self):
        workflow = legacy_patch.load_modern_registration_workflow()
        globals_ = workflow.__globals__
        workflow_source = inspect.getsource(workflow)
        ready_source = inspect.getsource(globals_["_birthday_continue_ready"])
        snapshot_source = inspect.getsource(globals_["_start_mailbox_snapshot"])
        birthday_flow = workflow_source[
            workflow_source.index("Birthday form ready") :
            workflow_source.index("_otp_inputs(page).first.wait_for")
        ]

        self.assertIn("threading.Thread", snapshot_source)
        self.assertIn("daemon=True", snapshot_source)
        self.assertIn("_get_messages(account, timeout=3.0)", snapshot_source)
        self.assertLess(
            birthday_flow.index("_start_mailbox_snapshot(account)"),
            birthday_flow.index("_select_birthday(page, user)"),
        )
        self.assertIn("otp_snapshot_thread.join(timeout=0.1)", birthday_flow)
        self.assertNotIn("_get_messages(account)", birthday_flow)
        self.assertIn("continue_button.click(timeout=2500)", birthday_flow)
        self.assertIn("continue_button.click(timeout=1500, force=True)", birthday_flow)
        self.assertNotIn('wait_for(state="visible", timeout=5000)', birthday_flow)
        self.assertIn("continue_button = _first_visible_control(continue_button)", birthday_flow)
        self.assertIn("time.sleep(0.05)", ready_source)
        self.assertIn('state["fingerprints"] = _capcut_message_fingerprints', snapshot_source)
        self.assertIn("previous_fingerprints=otp_message_fingerprints", workflow_source)


if __name__ == "__main__":
    unittest.main()
