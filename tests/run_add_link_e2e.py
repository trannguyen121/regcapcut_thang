"""Run the real Add Link login/join flow without starting the Tk UI.

Accounts are read from stdin by default, one ``email|password`` per line. The
runner is intentionally capped at the four invitations available in a 5-slot
space (owner plus four invited members).
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.settings import load_settings
from modules.actions.capcut_add_link import (
    AlreadyJoinedSpaceError,
    run_capcut_add_link_workflow,
)
from modules.ui.reg_capcut_tab import (
    ADD_LINK_MEMBER_LIMIT,
    RegCapCutApp,
    normalize_add_link,
    parse_capcut_login_line,
)


def _save_result(path: Path, link: str, accounts: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "link": link,
        "member_limit": ADD_LINK_MEMBER_LIMIT,
        "accounts": [
            {
                "user": account["user"],
                "status": account.get("status", ""),
                "userID": account.get("userID", ""),
                "join_pending": bool(account.get("join_pending")),
                "error": account.get("error", ""),
            }
            for account in accounts
        ],
    }
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--link", required=True)
    parser.add_argument("--accounts", default="-", help="Account file, or - for stdin")
    parser.add_argument("--output", default=".tmp/add_link_e2e_result.json")
    parser.add_argument("--verify-only", action="store_true", help="Never Submit; only verify existing membership")
    args = parser.parse_args()

    link = normalize_add_link(args.link)
    if not link:
        parser.error("invalid invitation URL")
    text = sys.stdin.read() if args.accounts == "-" else Path(args.accounts).read_text(encoding="utf-8-sig")
    accounts = [
        account
        for line in text.splitlines()
        if (account := parse_capcut_login_line(line)) is not None
    ]
    if not accounts:
        parser.error("no valid email|password accounts")
    if len(accounts) > ADD_LINK_MEMBER_LIMIT:
        parser.error(f"a 5-slot space accepts at most {ADD_LINK_MEMBER_LIMIT} invited accounts")

    app = RegCapCutApp.__new__(RegCapCutApp)
    app.app_settings = load_settings()
    app.stop_event = threading.Event()
    app.active_profiles_lock = threading.Lock()
    app.active_standalone_processes = {}
    output_path = Path(args.output)
    verified = 0

    for index, account in enumerate(accounts):
        session = None
        account.update(status="running", join_pending=bool(args.verify_only), error="")
        _save_result(output_path, link, accounts)
        print(f"[E2E][ADD LINK] {index + 1}/{len(accounts)} {account['user']}", flush=True)
        try:
            session, result = app._start_add_link_browser(account, index, 1)

            def before_submit() -> None:
                account["join_pending"] = True
                _save_result(output_path, link, accounts)

            run_capcut_add_link_workflow(
                account,
                link,
                {
                    "start_result": result,
                    "stop_event": app.stop_event,
                    "before_join_submit": before_submit,
                },
            )
            account.update(status="true", join_pending=False)
            verified += 1
            print(f"[E2E][ADD LINK] JOINED {account['user']}", flush=True)
        except AlreadyJoinedSpaceError:
            account.update(status="true", join_pending=False)
            verified += 1
            print(f"[E2E][ADD LINK] ALREADY MEMBER {account['user']}", flush=True)
        except Exception as exc:
            account["status"] = "join unverified" if account.get("join_pending") else "failed"
            account["error"] = f"{type(exc).__name__}: {exc}"
            print(f"[E2E][ADD LINK] FAILED {account['user']}: {account['error']}", flush=True)
        finally:
            _save_result(output_path, link, accounts)
            if session is not None:
                app._close_standalone_process(session)

    print(f"[E2E][ADD LINK] RESULT {verified}/{len(accounts)}", flush=True)
    print(f"[E2E][ADD LINK] OUTPUT {output_path}", flush=True)
    return 0 if verified == len(accounts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
