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

from modules.actions.capcut_add_link import _login_with_email
from modules.actions.capcut_workflow import _connect_browser, run_capcut_workflow
from modules.browser.chromium import open_workflow_page
from modules.regcapcut_cli import load_app_settings, parse_mail_api_line
import legacy_regcapcut_v75_proxy as legacy_v75

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--pass-capcut", default="1234567")
    parser.add_argument("--output", default=".tmp/legacy_v75_checkout_e2e.json")
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
    stop_event = threading.Event()
    thread_count = min(max(1, args.threads), len(accounts))
    results = []
    lock = threading.Lock()

    def run_one(index, account):
        app = app_class.__new__(app_class)
        app.app_settings = settings
        account[legacy_v75._WINDOW_SLOT_KEY] = index % thread_count
        account[legacy_v75._WINDOW_TOTAL_KEY] = thread_count
        process = None
        started = time.monotonic()
        try:
            process, start_result = app._start_standalone_chromium(
                account, index + max(0, args.profile_offset)
            )
            context = {"start_result": start_result, "stop_event": stop_event}
            with run_capcut_workflow.__globals__["sync_playwright"]() as playwright:
                browser = _connect_browser(
                    playwright,
                    run_capcut_workflow.__globals__["_cdp_url"](context),
                    account["user"],
                    stop_event,
                )
                page = open_workflow_page(browser, context)
                try:
                    _login_with_email(page, account, stop_event, max_attempts=1)
                except Exception:
                    try:
                        body = page.locator("body").inner_text(timeout=3000)[:1200]
                        safe_body = body.encode("ascii", "replace").decode("ascii")
                        print(
                            f"[E2E][V7.5][CHECKOUT] Login diagnostic {account['user']} "
                            f"url={page.url} body={safe_body!r}"
                        )
                    except Exception:
                        pass
                    raise
                try:
                    page.goto(
                        "https://www.capcut.com/my-cloud?tab=all",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                except Exception:
                    try:
                        page.evaluate("window.stop()")
                    except Exception:
                        pass
                print(f"[E2E][V7.5][CHECKOUT] Workspace {account['user']}: {page.url}")
            legacy_v75.open_pro_checkout_across_five_tabs(
                account,
                context,
                run_capcut_workflow,
            )
            status = "success"
            error = ""
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
    print(f"[E2E][V7.5][CHECKOUT] RESULT {successes}/{len(results)} | {output}")
    return 0 if successes == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
