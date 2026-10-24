"""Measure the patched v7.5 Reg treo workflow without driving the Tk UI.

OAuth account records are read from stdin. Results contain no refresh tokens.
"""

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
    parser.add_argument("--output", default=".tmp/legacy_v75_e2e_result.json")
    parser.add_argument("--chrome", default="H:/regcapcut_v7.5/chrome-154")
    parser.add_argument("--force-five-tabs", action="store_true")
    args = parser.parse_args()

    accounts = []
    for line in sys.stdin.read().splitlines():
        account = parse_mail_api_line(line)
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
    run_started = time.monotonic()

    def run_one(index: int, account: dict) -> dict:
        app = app_class.__new__(app_class)
        app.app_settings = settings
        account[legacy_v75._WINDOW_SLOT_KEY] = index % thread_count
        account[legacy_v75._WINDOW_TOTAL_KEY] = thread_count
        process = None
        started = time.monotonic()
        launch_elapsed = None
        try:
            print(f"[E2E][V7.5] START {account['user']}", flush=True)
            process, start_result = app._start_standalone_chromium(account, index)
            launch_elapsed = time.monotonic() - started
            print(
                f"[E2E][V7.5] CHROME {account['user']} {launch_elapsed:.2f}s",
                flush=True,
            )
            context = {
                "start_result": start_result,
                "stop_event": stop_event,
                "force_first_checkout_failure": args.force_five_tabs,
            }
            workflow_success = bool(workflow(account, context))
            checkout_success = context.get("checkout_success")
            success = workflow_success and checkout_success is True
            status = "success" if success else "failed"
            if success:
                error = ""
            elif not workflow_success:
                error = "workflow returned false"
            else:
                error = context.get("checkout_error", "checkout was not verified")
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
            "launch_seconds": round(launch_elapsed, 2) if launch_elapsed is not None else None,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "error": error,
        }
        with lock:
            results.append(result)
            print(
                f"[E2E][V7.5] DONE {account['user']} {status} "
                f"{result['elapsed_seconds']:.2f}s",
                flush=True,
            )
        return result

    with ThreadPoolExecutor(max_workers=thread_count, thread_name_prefix="legacy-v75") as executor:
        futures = [executor.submit(run_one, index, account) for index, account in enumerate(accounts)]
        for future in as_completed(futures):
            future.result()

    order = {account["user"]: index for index, account in enumerate(accounts)}
    results.sort(key=lambda item: order[item["user"]])
    payload = {
        "threads": thread_count,
        "elapsed_seconds": round(time.monotonic() - run_started, 2),
        "results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    succeeded = sum(item["status"] == "success" for item in results)
    print(f"[E2E][V7.5] RESULT {succeeded}/{len(results)} | {output}", flush=True)
    return 0 if succeeded == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
