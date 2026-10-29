"""Log in to one paid CapCut account and cancel its auto-renewal."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import legacy_regcapcut_v75_proxy as legacy_v75
from modules.regcapcut_cli import load_app_settings, parse_mail_api_line


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome", default="H:/regcapcut_v7.5/chrome-154")
    parser.add_argument("--output", default=".tmp/capcut_cancel_e2e.json")
    parser.add_argument("--pass-capcut", default="1234567")
    args = parser.parse_args()

    line = next((item.strip() for item in sys.stdin.read().splitlines() if item.strip()), "")
    account = parse_mail_api_line(line)
    if account is None:
        parts = line.split("|")
        if len(parts) == 2 and "@" in parts[0]:
            account = {"user": parts[0].strip(), "passnew": parts[1].strip()}
        else:
            parser.error(
                "expected email|CapCut-password or "
                "email|mail-password|refresh_token|client_id on stdin"
            )
    else:
        account["passnew"] = args.pass_capcut

    settings = dataclasses.replace(load_app_settings(), chromium_path=args.chrome)
    namespace = legacy_v75.prepare_legacy_app()
    app_class = namespace["RegCapCutApp"]
    modern_workflow = legacy_v75.load_modern_registration_workflow()
    app = app_class.__new__(app_class)
    app.app_settings = settings
    app.hold_mode = False
    process = None
    context = None
    started = time.monotonic()
    result = {"user": account["user"], "status": "error", "error": ""}
    try:
        account[legacy_v75._WINDOW_SLOT_KEY] = 0
        account[legacy_v75._WINDOW_TOTAL_KEY] = 1
        process, start_result = app._start_standalone_chromium(account, 0)
        context = {
            "start_result": start_result,
            "stop_event": threading.Event(),
            "allow_existing_login": True,
        }
        if not modern_workflow(account, context):
            raise RuntimeError("CapCut login did not complete")
        cancelled = legacy_v75.cancel_capcut_subscription(
            account,
            context,
            modern_workflow,
        )
        result["status"] = "cancelled" if cancelled else "not_cancelled"
        result["evidence"] = context.get("cancellation_evidence", "")
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        if context is not None:
            try:
                workflow_globals = modern_workflow.__globals__
                with workflow_globals["sync_playwright"]() as playwright:
                    browser = workflow_globals["_connect_browser"](
                        playwright,
                        workflow_globals["_cdp_url"](context),
                        account["user"],
                        context["stop_event"],
                    )
                    pages = [page for item in browser.contexts for page in item.pages]
                    if pages:
                        page = pages[0]
                        debug_dir = Path(".tmp/capcut_cancel_debug")
                        debug_dir.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(debug_dir / "failure.png"), full_page=True)
                        result["debug_url"] = str(page.url or "")
                        result["debug_text"] = str(
                            page.locator("body").inner_text(timeout=2000) or ""
                        )[:5000]
            except Exception as debug_exc:
                result["debug_error"] = f"{type(debug_exc).__name__}: {debug_exc}"
    finally:
        if process is not None:
            try:
                process.terminate()
            except Exception:
                pass

    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[E2E][CAPCUT CANCEL] {account['user']} status={result['status']} "
        f"elapsed={result['elapsed_seconds']:.2f}s report={output}",
        flush=True,
    )
    if result["error"]:
        print(f"[E2E][CAPCUT CANCEL] {result['error']}", flush=True)
    return 0 if result["status"] == "cancelled" else 1


if __name__ == "__main__":
    raise SystemExit(main())
