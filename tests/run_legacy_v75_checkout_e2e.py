"""Log in to existing accounts and exercise the v7.5 five-tab checkout flow."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.regcapcut_cli import load_app_settings, parse_mail_api_line
import legacy_regcapcut_v75_proxy as legacy_v75

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--pass-capcut", default="1234567")
    parser.add_argument("--output", default=".tmp/legacy_v75_checkout_e2e.json")
    parser.add_argument(
        "--success-output",
        default="export/payment_success.txt",
        help="append confirmed paid accounts after manual checkout",
    )
    parser.add_argument("--chrome", default="H:/regcapcut_v7.5/chrome-154")
    parser.add_argument("--profile-offset", type=int, default=0)
    args = parser.parse_args()

    accounts = []
    for line in sys.stdin.read().splitlines():
        account = parse_mail_api_line(line)
        if account is None:
            user = line.split("|", 1)[0].strip()
            if "@" in user:
                account = {"user": user}
        if account is not None:
            account["passnew"] = args.pass_capcut
            accounts.append(account)
    if not accounts:
        parser.error("no valid accounts supplied on stdin")

    settings = dataclasses.replace(load_app_settings(), chromium_path=args.chrome)
    namespace = legacy_v75.prepare_legacy_app()
    app_class = namespace["RegCapCutApp"]
    workflow = namespace["run_capcut_workflow"]
    stop_event = threading.Event()
    thread_count = min(max(1, args.threads), len(accounts))
    results = []
    lock = threading.Lock()
    success_output = Path(args.success_output)

    def run_one(index, account):
        app = app_class.__new__(app_class)
        app.app_settings = settings
        app.hold_mode = True
        account[legacy_v75._WINDOW_SLOT_KEY] = index % thread_count
        account[legacy_v75._WINDOW_TOTAL_KEY] = thread_count
        process = None
        started = time.monotonic()
        try:
            process, start_result = app._start_standalone_chromium(
                account, index + max(0, args.profile_offset)
            )
            context = {"start_result": start_result, "stop_event": stop_event}
            workflow_ok = bool(workflow(account, context))
            paid = workflow_ok and context.get("payment_confirmed") is True
            status = "success" if paid else "payment_not_confirmed"
            error = "" if paid else context.get(
                "checkout_error",
                "registration/login or checkout ended without confirmed payment",
            )
            if paid:
                success_output.parent.mkdir(parents=True, exist_ok=True)
                export_line = account.get("api") or (
                    f"{account['user']}|{account.get('passnew', '')}"
                )
                with lock:
                    with success_output.open("a", encoding="utf-8") as file:
                        file.write(str(export_line).rstrip("\r\n") + "\n")
                        file.flush()
        except Exception as exc:
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if process is not None:
                try:
                    process.terminate()
                except Exception:
                    pass
        result = {
            "user": account["user"],
            "status": status,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "payment_confirmed": context.get("payment_confirmed") is True
            if "context" in locals()
            else False,
            "payment_evidence": context.get("payment_evidence", "")
            if "context" in locals()
            else "",
            "error": error,
        }
        with lock:
            results.append(result)
            print(f"[E2E][V7.5][CHECKOUT] DONE {account['user']} {status} {result['elapsed_seconds']}s")
        return result

    with ThreadPoolExecutor(max_workers=thread_count) as executor:
        futures = [executor.submit(run_one, index, account) for index, account in enumerate(accounts)]
        for future in as_completed(futures):
            future.result()

    order = {account["user"]: index for index, account in enumerate(accounts)}
    results.sort(key=lambda item: order[item["user"]])
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"results": results}, indent=2), encoding="utf-8")
    successes = sum(item["status"] == "success" for item in results)
    print(
        f"[E2E][V7.5][CHECKOUT] RESULT {successes}/{len(results)} | "
        f"report={output} paid_accounts={success_output}"
    )
    return 0 if successes == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
