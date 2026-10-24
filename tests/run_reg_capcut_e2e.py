"""Run normal CapCut registration concurrently without starting the Tk UI.

Mail OAuth records are read from stdin by default. The JSON result deliberately
contains only the account email, status, elapsed time, and sanitized error.
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

from modules.actions.capcut_workflow import run_capcut_workflow
from modules.browser.chromium import ChromiumSession, resolve_chromium_154
from modules.regcapcut_cli import load_app_settings, parse_mail_api_line


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--accounts", default="-", help="OAuth mail file, or - for stdin")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--pass-capcut", default="1234567")
    parser.add_argument("--output", default=".tmp/reg_capcut_e2e_result.json")
    args = parser.parse_args()

    raw_text = (
        sys.stdin.read()
        if args.accounts == "-"
        else Path(args.accounts).read_text(encoding="utf-8-sig")
    )
    accounts = []
    for line in raw_text.splitlines():
        account = parse_mail_api_line(line)
        if account is not None:
            account["passnew"] = args.pass_capcut
            accounts.append(account)
    if not accounts:
        parser.error("no valid email|passmail|refresh_token|client_id accounts")

    settings = load_app_settings()
    browser_path = resolve_chromium_154(settings.chromium_path)
    stop_event = threading.Event()
    thread_count = min(max(1, args.threads), len(accounts))
    started = time.monotonic()
    results = []
    results_lock = threading.Lock()

    def register(index: int, account: dict) -> dict:
        session = None
        account_started = time.monotonic()
        print(
            f"[E2E][REG CAPCUT] START {index + 1}/{len(accounts)} {account['user']}",
            flush=True,
        )
        try:
            session = ChromiumSession(
                browser_path,
                stop_event=stop_event,
                window_settings=settings.browser_window,
                index=index % thread_count,
                total_windows=thread_count,
            )
            success = bool(
                run_capcut_workflow(
                    account,
                    {"start_result": session, "stop_event": stop_event},
                )
            )
            status = "success" if success else "failed"
            error = "" if success else "registration returned false"
        except Exception as exc:
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if session is not None:
                session.close()

        result = {
            "user": account["user"],
            "status": status,
            "elapsed_seconds": round(time.monotonic() - account_started, 2),
            "error": error,
        }
        with results_lock:
            results.append(result)
            print(
                f"[E2E][REG CAPCUT] DONE {account['user']} status={status} "
                f"elapsed={result['elapsed_seconds']:.2f}s",
                flush=True,
            )
        return result

    with ThreadPoolExecutor(max_workers=thread_count, thread_name_prefix="reg-capcut") as executor:
        futures = [executor.submit(register, index, account) for index, account in enumerate(accounts)]
        for future in as_completed(futures):
            future.result()

    order = {account["user"]: index for index, account in enumerate(accounts)}
    results.sort(key=lambda item: order[item["user"]])
    payload = {
        "threads": thread_count,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "results": results,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    successful = sum(result["status"] == "success" for result in results)
    print(
        f"[E2E][REG CAPCUT] RESULT {successful}/{len(results)} succeeded in "
        f"{payload['elapsed_seconds']:.2f}s | {output_path}",
        flush=True,
    )
    return 0 if successful == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
