import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from modules.actions.capcut_check_space import (
    SpaceCheckError, _all_pages, _teams_only, _owner_username, check_and_reduce_spaces,
    check_owned_fam_members,
    out_all_spaces,
)
from modules.ui.capcut_space_check import CheckSpaceMixin


def space(wid, **kwargs):
    return {"workspace_id": wid, "name": "Teams", "role": "collaborator", "owner": "owner-id",
            "team_vip_status": 1, "team_vip_end": 10, **kwargs}


class CheckSpaceTests(unittest.TestCase):
    def run_flow(self, lists, record=None, call=None):
        with (patch("modules.actions.capcut_check_space._read_teams", side_effect=lists),
              patch("modules.actions.capcut_check_space._owner_username", return_value="user123456789"),
              patch("modules.actions.capcut_check_space._call", call or Mock()) as api,
              patch("modules.actions.capcut_check_space._wait")):
            result = check_and_reduce_spaces(
                Mock(), {"user": "test@example.com", "userID": "user111111111"},
                None, record or Mock(),
            )
            return result, api

    def test_one_team_does_not_leave(self):
        result, api = self.run_flow([[space("a")]])
        self.assertEqual(result["after"], 1)
        api.assert_not_called()

    def test_zero_teams_is_not_claimed_as_one(self):
        result, api = self.run_flow([[]])
        self.assertEqual(result["status"], "no teams")
        api.assert_not_called()

    def test_two_teams_export_before_leave_then_verify(self):
        events = []
        api = Mock(side_effect=lambda *args: self.assertIn("leave_pending", [e[0] for e in events]))
        result, api = self.run_flow([[space("a"), space("b")]] * 2 + [[space("a")]],
                                    record=lambda *args: events.append(args), call=api)
        api.assert_called_once()
        self.assertEqual(api.call_args.args[2], {"workspace_id": "b"})
        self.assertEqual(result["after"], 1)
        self.assertEqual([e[0] for e in events], ["owner_found", "leave_pending", "left_verified"])
        self.assertEqual(result["left"][0]["owner_username"], "user123456789")

    def test_timeout_after_success_never_repeats_leave(self):
        result, api = self.run_flow([[space("a"), space("b")]] * 2 + [[space("a")]],
                                    call=Mock(side_effect=SpaceCheckError("timeout")))
        self.assertEqual(result["status"], "ok")
        api.assert_called_once()

    def test_failed_export_prevents_leave(self):
        api = Mock()
        with self.assertRaises(OSError):
            self.run_flow([[space("a"), space("b")]] * 2,
                          record=Mock(side_effect=OSError("disk full")), call=api)
        api.assert_not_called()

    def test_unverified_leave_does_not_report_success_or_repeat(self):
        api = Mock()
        with self.assertRaises(SpaceCheckError):
            self.run_flow([[space("a"), space("b")]] * 5, call=api)
        api.assert_called_once()

    def test_concurrent_membership_change_prevents_leave(self):
        api = Mock()
        with self.assertRaises(SpaceCheckError):
            self.run_flow([[space("a"), space("b")], [space("b")]], call=api)
        api.assert_not_called()

    def test_owned_team_is_kept(self):
        owned = space("z", role="owner")
        result, api = self.run_flow([[space("a"), owned]] * 2 + [[owned]])
        self.assertEqual(api.call_args.args[2], {"workspace_id": "a"})
        self.assertEqual(result["after"], 1)

    def test_multiple_owned_teams_are_not_deleted(self):
        api = Mock()
        with self.assertRaises(SpaceCheckError):
            self.run_flow([[space("a", role="owner"), space("b", role="owner")]], call=api)
        api.assert_not_called()

    def test_out_fam_leaves_one_team_and_exports_owner(self):
        events = []
        with (patch("modules.actions.capcut_check_space._read_teams",
                    side_effect=[[space("a")], [space("a")], []]),
              patch("modules.actions.capcut_check_space._owner_username",
                    return_value="user123456789"),
              patch("modules.actions.capcut_check_space._call") as api,
              patch("modules.actions.capcut_check_space._wait")):
            result = out_all_spaces(Mock(), {"user": "test@example.com", "userID": "user111111111"}, None,
                                    lambda *args: events.append(args))
        self.assertEqual(result["after"], 0)
        self.assertEqual(api.call_args.args[2], {"workspace_id": "a"})
        self.assertEqual(
            [event[0] for event in events],
            ["owner_found", "leave_pending", "left_verified"],
        )
        self.assertEqual(result["left"][0]["owner_username"], "user123456789")

    def test_out_fam_leaves_two_teams(self):
        lists = [
            [space("a"), space("b")],
            [space("a"), space("b")],
            [space("b")],
            [space("b")],
            [],
        ]
        with (patch("modules.actions.capcut_check_space._read_teams", side_effect=lists),
              patch("modules.actions.capcut_check_space._owner_username",
                    return_value="user123456789"),
              patch("modules.actions.capcut_check_space._call") as api,
              patch("modules.actions.capcut_check_space._wait")):
            result = out_all_spaces(
                Mock(), {"user": "test@example.com", "userID": "user111111111"}, None, Mock()
            )
        self.assertEqual(result["after"], 0)
        self.assertEqual(api.call_count, 2)

    def test_out_fam_does_not_attempt_to_leave_owned_team(self):
        events = []
        with (patch("modules.actions.capcut_check_space._read_teams",
                    return_value=[space("a", role="owner")]),
              patch("modules.actions.capcut_check_space._owner_username",
                    return_value="user111111111"),
              patch("modules.actions.capcut_check_space._call") as api):
            with self.assertRaises(SpaceCheckError):
                out_all_spaces(
                    Mock(), {"user": "test@example.com", "userID": "user111111111"}, None,
                    lambda *args: events.append(args),
                )
        api.assert_not_called()
        self.assertEqual(events[0][0], "owner_found")
        self.assertEqual(events[0][1]["owner_username"], "user111111111")

    def test_personal_space_and_free_space_not_counted_by_name(self):
        teams = _teams_only([space("a"), space("b", team_vip_status=0, team_vip_end=1),
                             space("c", team_vip_status=0, team_vip_end=0)])
        self.assertEqual([s["workspace_id"] for s in teams], ["a", "b"])
        with self.assertRaises(SpaceCheckError):
            _teams_only([space("a", team_vip_status=None)])

    def test_all_pages_are_read_and_repeated_cursors_rejected(self):
        payloads = [{"data": {"workspace_infos": [space("a")], "has_more": True, "next_cursor": "1"}},
                    {"data": {"workspace_infos": [space("b")], "has_more": False}}]
        with patch("modules.actions.capcut_check_space._call", side_effect=payloads):
            rows = _all_pages(Mock(), "getUserWorkSpace", "workspace_infos", "mail", None)
        self.assertEqual(len(rows), 2)
        payloads[1]["data"].update(has_more=True, next_cursor="1")
        with patch("modules.actions.capcut_check_space._call", side_effect=payloads), self.assertRaises(SpaceCheckError):
            _all_pages(Mock(), "getUserWorkSpace", "workspace_infos", "mail", None)

    def test_owner_is_verified_and_display_name_is_not_username(self):
        members = [{"uid": "owner-id", "role": "owner", "nickname": "Display name"}]
        with patch("modules.actions.capcut_check_space._all_pages", return_value=members):
            with self.assertRaises(SpaceCheckError):
                _owner_username(Mock(), space("a"), "mail", None)
            members[0]["nickname"] = "user123456789"
            self.assertEqual(_owner_username(Mock(), space("a"), "mail", None), "user123456789")
            with self.assertRaises(SpaceCheckError):
                _owner_username(Mock(), space("a", owner="someone-else"), "mail", None)

    def test_check_fam_origin_counts_unique_members_in_owned_spaces_only(self):
        spaces = [space("owned", role="owner"), space("joined")]
        members = [
            {"uid": "owner-id", "role": "owner", "username": "user123456789"},
            {"uid": "member-1", "role": "collaborator"},
            {"uid": "member-1", "role": "collaborator"},
        ]
        events = []
        with (patch("modules.actions.capcut_check_space._read_teams", return_value=spaces),
              patch("modules.actions.capcut_check_space._all_pages", return_value=members) as pages):
            result = check_owned_fam_members(
                Mock(), {"user": "owner@example.com"}, None,
                lambda *args: events.append(args),
            )
        self.assertEqual(result["owned_spaces"], 1)
        self.assertEqual(result["owner_username"], "user123456789")
        self.assertEqual(result["total_members"], 1)
        self.assertEqual(result["total_accounts"], 2)
        self.assertEqual(result["spaces"][0]["member_count"], 1)
        self.assertEqual(result["spaces"][0]["account_count"], 2)
        self.assertEqual(events, [("fam_checked", result["spaces"][0])])
        self.assertEqual(pages.call_args.kwargs["workspace_id"], "owned")

    def test_check_fam_origin_rejects_unverified_owner(self):
        members = [{"uid": "different-owner", "role": "owner", "username": "user123456789"}]
        with (patch("modules.actions.capcut_check_space._read_teams",
                    return_value=[space("owned", role="owner")]),
              patch("modules.actions.capcut_check_space._all_pages", return_value=members),
              self.assertRaises(SpaceCheckError)):
            check_owned_fam_members(Mock(), {"user": "owner@example.com"}, None, Mock())

    def test_check_fam_origin_reports_no_owned_teams(self):
        with patch("modules.actions.capcut_check_space._read_teams", return_value=[space("joined")]):
            result = check_owned_fam_members(Mock(), {"user": "member@example.com"}, None, Mock())
        self.assertEqual(result["status"], "no owned teams")
        self.assertEqual(result["total_members"], 0)
        self.assertEqual(result["total_accounts"], 0)

    def test_owner_journal_is_persistent_and_appended(self):
        with tempfile.TemporaryDirectory() as directory:
            app = CheckSpaceMixin()
            app.check_space_directory = Path(directory)
            app.check_space_lock = threading.Lock()
            data = {"workspace_id": "b", "member_username": "user111111111",
                    "owner_username": "user123456789"}
            app._record_space_event("mail", "owner_found", data)
            app._record_space_event("mail", "leave_pending", data)
            app._record_space_event("mail", "left_verified", data)
            self.assertEqual(
                (Path(directory) / "owners.txt").read_text().strip(),
                "mail|user111111111|user123456789",
            )
            rows = [json.loads(s) for s in (Path(directory) / "results.jsonl").read_text().splitlines()]
            self.assertEqual(
                [r["event"] for r in rows],
                ["owner_found", "leave_pending", "left_verified"],
            )

    def test_fam_check_writes_readable_member_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            app = CheckSpaceMixin()
            app.check_space_directory = Path(directory)
            app.check_space_lock = threading.Lock()
            app._record_space_event("owner@example.com", "fam_checked", {
                "workspace_id": "space-1",
                "space_name": "Owner space",
                "owner_username": "user123456789",
                "member_count": 6,
                "account_count": 7,
            })
            self.assertEqual(
                (Path(directory) / "members.txt").read_text(encoding="utf-8"),
                "owner@example.com|user123456789|space-1|Owner space|6|7\n",
            )

    def test_manual_export_filters_selected_accounts_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory) / "out fam"
            out_dir.mkdir()
            (out_dir / "owners.txt").write_text(
                "selected@example.com|user111111111|user222222222\n"
                "selected@example.com|user111111111|user222222222\n"
                "other@example.com|user333333333|user444444444\n",
                encoding="utf-8",
            )
            app = CheckSpaceMixin()
            app.add_link_account_selected = {"selected@example.com"}
            with patch("modules.ui.capcut_space_check.Path",
                       side_effect=lambda value: Path(directory) / value):
                app.export_out_fam_accounts()
            self.assertEqual(
                (out_dir / "accounts.txt").read_text(encoding="utf-8"),
                "selected@example.com|user111111111|user222222222\n",
            )

    def test_manual_fam_export_uses_latest_result_for_checked_accounts(self):
        with tempfile.TemporaryDirectory() as directory:
            result_dir = Path(directory) / "check gốc fam"
            result_dir.mkdir()
            entries = [
                {"account": "first@example.com", "event": "completed", "owner_username": "user111111111", "total_members": 2},
                {"account": "other@example.com", "event": "completed", "owner_username": "user999999999", "total_members": 9},
                {"account": "first@example.com", "event": "error", "owner_username": "user111111111", "total_members": 99},
                {"account": "first@example.com", "event": "completed", "owner_username": "user111111111", "total_members": 6},
                {"account": "zero@example.com", "event": "completed", "owner_username": "user000000000", "total_members": 0},
            ]
            (result_dir / "results.jsonl").write_text(
                "\n".join(json.dumps(entry) for entry in entries) + "\n",
                encoding="utf-8",
            )
            app = CheckSpaceMixin()
            app.add_link_account_selected = {"first@example.com", "zero@example.com"}
            app.add_link_accounts = [
                {"user": "zero@example.com"},
                {"user": "first@example.com"},
                {"user": "other@example.com"},
            ]
            with patch("modules.ui.capcut_space_check.Path",
                       side_effect=lambda value: Path(directory) / value):
                app.export_checked_fam_members()
            self.assertEqual(
                (result_dir / "accounts.txt").read_text(encoding="utf-8"),
                "zero@example.com|user000000000|0\n"
                "first@example.com|user111111111|6\n",
            )


if __name__ == "__main__":
    unittest.main()
