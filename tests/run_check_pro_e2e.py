"""Run CapCut plan detection concurrently without starting the Tk UI.

Accounts are read from stdin by default, one ``email|password`` per line. The
result file intentionally excludes passwords.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.settings import load_settings
from modules.actions.capcut_add_link import run_capcut_check_pro_workflow
from modules.ui.reg_capcut_tab import RegCapCutApp, parse_capcut_login_line


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--accounts", default="-", help="Account file, or - for stdin")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--expected", choices=("yearly", "monthly"))
    parser.add_argument("--output", default=".tmp/check_pro_e2e_result.json")
    args = parser.parse_args()

    text = sys.stdin.read() if args.accounts == "-" else Path(args.accounts).read_text(encoding="utf-8-sig")
    accounts = [
        account
        for line in text.splitlines()
        if (account := parse_capcut_login_line(line)) is not None
    ]
    if not accounts:
        parser.error("no valid email|password accounts")

    thread_count = min(max(1, args.threads), len(accounts))
    app = RegCapCutApp.__new__(RegCapCutApp)
    app.app_settings = load_settings()
    app.stop_event = threading.Event()
    app.active_profiles_lock = threading.Lock()
    app.active_standalone_processes = {}
    results_lock = threading.Lock()
    started = time.monotonic()
    results: list[dict] = []

    def check_one(index: int, account: dict) -> dict:
        session = None
        account_started = time.monotonic()
        print(f"[E2E][CHECK PRO] START {index + 1}/{len(accounts)} {account['user']}", flush=True)
        try:
            session, start_result = app._start_account_browser(
                account,
                index,
                thread_count,
                startup_url="https://www.capcut.com/login",
            )
            detected = run_capcut_check_pro_workflow(
                account,
                {"start_result": start_result, "stop_event": app.stop_event},
            )
            status = "pro" if detected is True else "false" if not detected else str(detected)
            matches = args.expected is None or status == args.expected or status == f"teams_{args.expected}"
            error = "" if matches else f"expected {args.expected}, detected {status}"
        except Exception as exc:
            status = "error"
            matches = False
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if session is not None:
                app._close_standalone_process(session)
        elapsed = round(time.monotonic() - account_started, 2)
        result = {
            "user": account["user"],
            "status": status,
            "matches_expected": matches,
            "elapsed_seconds": elapsed,
            "error": error,
        }
        with results_lock:
            results.append(result)
            print(
                f"[E2E][CHECK PRO] DONE {account['user']} status={status} "
                f"elapsed={elapsed:.2f}s match={matches}",
                flush=True,
            )
        return result

    with ThreadPoolExecutor(max_workers=thread_count, thread_name_prefix="check-pro") as executor:
        futures = [executor.submit(check_one, index, account) for index, account in enumerate(accounts)]
        for future in as_completed(futures):
            future.result()

    results.sort(key=lambda item: next(
        index for index, account in enumerate(accounts) if account["user"] == item["user"]
    ))
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "expected": args.expected,
        "threads": thread_count,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "results": results,
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    matched = sum(1 for result in results if result["matches_expected"])
    print(
        f"[E2E][CHECK PRO] RESULT {matched}/{len(results)} matched in "
        f"{payload['elapsed_seconds']:.2f}s | {output_path}",
        flush=True,
    )
    return 0 if matched == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
