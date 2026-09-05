import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from modules.actions.capcut_add_link import (
    AlreadyJoinedSpaceError,
    JoinVerificationError,
    _join_space,
    _parse_invitation_membership,
    _read_invitation_membership,
)


class JoinVerificationTests(unittest.TestCase):
    link = "https://www.capcut.com/team-invite/test"

    def page(self):
        button = Mock()
        button.is_visible.return_value = True
        button.is_enabled.return_value = True
        locator = Mock()
        locator.count.return_value = 1
        locator.nth.return_value = button
        page = Mock()
        page.get_by_role.return_value = locator
        page.locator.return_value = locator
        return page, button

    def join(self, page, reads, **kwargs):
        with (
            patch("modules.actions.capcut_add_link._read_invitation_membership", side_effect=reads),
            patch("modules.actions.capcut_add_link.accept_capcut_cookies"),
            patch("modules.actions.capcut_add_link._cant_submit_request_visible", return_value=False),
            patch("modules.actions.capcut_add_link._cant_join_space_visible", return_value=False),
            patch("modules.actions.capcut_add_link._wait"),
        ):
            _join_space(page, self.link, "test@example.com", None, **kwargs)

    def test_existing_member_is_not_clicked(self):
        page, button = self.page()
        with self.assertRaises(AlreadyJoinedSpaceError):
            self.join(page, [("123", True)])
        button.click.assert_not_called()

    def test_membership_must_be_confirmed_after_single_submit(self):
        page, button = self.page()
        persisted = []
        button.click.side_effect = lambda **kwargs: self.assertEqual(persisted, [True])
        self.join(page, [("123", False), ("123", False), ("123", True)],
                  before_submit=lambda: persisted.append(True))
        button.click.assert_called_once()

    def test_click_timeout_is_verified_without_a_second_click(self):
        page, button = self.page()
        button.click.side_effect = RuntimeError("Click sent, navigation timed out")
        self.join(page, [("123", False), ("123", True)])
        button.click.assert_called_once()

    def test_my_cloud_and_pending_application_are_not_success(self):
        page, button = self.page()
        page.url = "https://www.capcut.com/my-cloud/123"
        with self.assertRaises(JoinVerificationError):
            self.join(page, [("123", False)] * 4)
        button.click.assert_called_once()

    def test_different_workspace_cannot_confirm_success(self):
        page, button = self.page()
        with self.assertRaises(JoinVerificationError):
            self.join(page, [("123", False), ("456", True)])
        button.click.assert_called_once()

    def test_unknown_preflight_never_submits(self):
        page, button = self.page()
        with self.assertRaises(JoinVerificationError):
            self.join(page, [JoinVerificationError("No membership response")])
        button.click.assert_not_called()

    def test_pending_from_previous_run_only_checks_membership(self):
        page, button = self.page()
        with self.assertRaises(JoinVerificationError):
            self.join(page, [("123", False)], pending=True)
        with self.assertRaises(AlreadyJoinedSpaceError):
            self.join(page, [("123", True)], pending=True)
        button.click.assert_not_called()

    def test_failure_to_persist_prevents_submit(self):
        page, button = self.page()
        with self.assertRaises(OSError):
            self.join(page, [("123", False)], before_submit=Mock(side_effect=OSError("disk full")))
        button.click.assert_not_called()

    def test_membership_parser_requires_explicit_server_evidence(self):
        for member in (True, False):
            self.assertEqual(_parse_invitation_membership({
                "ret": "0", "data": {"workspace_info": {"workspace_id": "123", "is_member": member}}
            }), ("123", member))
        for payload in (
            {"ret": "0", "data": {"status": 1}},
            {"ret": "0", "data": {"workspace_info": {"workspace_id": "123"}}},
            {"ret": "0", "data": {"workspace_info": {"workspace_id": "123", "is_member": "false"}}},
            {"ret": "2300", "data": {"workspace_info": {"workspace_id": "123", "is_member": True}}},
        ):
            with self.subTest(payload=payload), self.assertRaises(JoinVerificationError):
                _parse_invitation_membership(payload)

    def test_membership_read_filters_origin_frame_and_invitation(self):
        page = Mock()
        page.url = self.link
        frame = object()
        page.main_frame = frame
        response = SimpleNamespace(
            url="https://www.capcut.com/cc/v1/workspace/get_workspace_info_by_invitation_link",
            request=SimpleNamespace(frame=frame, post_data_json={"invitation_link": self.link}),
            ok=True, status=200,
            json=lambda: {"ret": "0", "data": {"workspace_info": {"workspace_id": "123", "is_member": True}}},
        )

        def register_response(_event, callback):
            self.assertEqual(_event, "response")
            page.response_callback = callback

        def navigate(*args, **kwargs):
            predicate = lambda candidate: candidate is response
            self.assertTrue(predicate(response))
            response.url = "https://evil.example/cc/v1/workspace/get_workspace_info_by_invitation_link"
            page.response_callback(response)
            response.url = "https://www.capcut.com/cc/v1/workspace/get_workspace_info_by_invitation_link"
            page.response_callback(response)

        page.on.side_effect = register_response
        page.goto.side_effect = navigate
        self.assertEqual(_read_invitation_membership(page, self.link, "test", None), ("123", True))
        page.goto.assert_called_once_with(
            self.link, wait_until="commit", timeout=30000,
        )

    def test_membership_read_survives_invitation_navigation_abort(self):
        page = Mock()
        page.url = self.link
        frame = object()
        page.main_frame = frame
        response = SimpleNamespace(
            url="https://www.capcut.com/cc/v1/workspace/get_workspace_info_by_invitation_link",
            request=SimpleNamespace(frame=frame, post_data_json={"invitation_link": self.link}),
            ok=True, status=200,
            json=lambda: {"ret": "0", "data": {"workspace_info": {
                "workspace_id": "123", "is_member": True,
            }}},
        )
        page.on.side_effect = lambda _event, callback: setattr(page, "response_callback", callback)
        def navigate(*args, **kwargs):
            page.response_callback(response)
            raise RuntimeError("net::ERR_ABORTED")
        page.goto.side_effect = navigate

        self.assertEqual(
            _read_invitation_membership(page, self.link, "test@example.com", None),
            ("123", True),
        )
        page.remove_listener.assert_called_once()

    def test_membership_read_replays_read_only_api_when_cdp_body_is_gone(self):
        page = Mock()
        page.url = self.link
        frame = object()
        page.main_frame = frame
        request = SimpleNamespace(
            frame=frame,
            post_data_json={"invitation_link": self.link},
            all_headers=lambda: {
                ":authority": "www.capcut.com",
                "cookie": "secret-cookie",
                "x-csrftoken": "csrf-value",
            },
        )
        response = SimpleNamespace(
            url="https://www.capcut.com/cc/v1/workspace/get_workspace_info_by_invitation_link",
            request=request, ok=True, status=200,
            json=Mock(side_effect=RuntimeError("No resource with given identifier found")),
        )
        retry = SimpleNamespace(
            ok=True, status=200,
            json=lambda: {"ret": "0", "data": {"workspace_info": {
                "workspace_id": "123", "is_member": True,
            }}},
        )
        page.context.request.post.return_value = retry
        page.on.side_effect = lambda _event, callback: setattr(page, "response_callback", callback)
        page.goto.side_effect = lambda *args, **kwargs: page.response_callback(response)

        self.assertEqual(
            _read_invitation_membership(page, self.link, "test@example.com", None),
            ("123", True),
        )
        retry_headers = page.context.request.post.call_args.kwargs["headers"]
        self.assertEqual(retry_headers, {"x-csrftoken": "csrf-value"})


if __name__ == "__main__":
    unittest.main()
