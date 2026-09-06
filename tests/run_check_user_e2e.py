"""Run the Check User workflow against account.txt without starting the Tk UI."""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.actions.capcut_add_link import run_capcut_check_user_workflow
from modules.ui.reg_capcut_tab import RegCapCutApp, parse_capcut_login_line


def save_results(path: Path, accounts: list[dict]) -> None:
    path.parent.mkdir(exist_ok=True)
    lines = [
        f'{account["user"]}|{account["userID"]}'
        for account in accounts
        if account.get("userID")
    ]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--accounts", default="account.txt")
    parser.add_argument(
        "--browser",
        default="dist/chrome-win/chrome.exe",
    )
    parser.add_argument("--passes", type=int, default=2)
    args = parser.parse_args()

    account_path = Path(args.accounts)
    browser_path = Path(args.browser).resolve()
    accounts = [
        account
        for line in account_path.read_text(encoding="utf-8-sig").splitlines()
        if (account := parse_capcut_login_line(line)) is not None
    ]
    if not browser_path.is_file():
        raise RuntimeError(f"Chromium 154 not found: {browser_path}")

    app = RegCapCutApp.__new__(RegCapCutApp)
    from core.settings import BrowserWindowSettings
    app.app_settings = SimpleNamespace(chromium_path=str(browser_path), browser_window=BrowserWindowSettings())
    app.active_profiles_lock = threading.Lock()
    app.active_standalone_processes = {}
    stop_event = threading.Event()
    app.stop_event = stop_event
    output_path = Path("check user") / "login_user.txt"
    errors = {}

    pending = list(range(len(accounts)))
    for pass_number in range(1, max(1, args.passes) + 1):
        if not pending:
            break
        print(f"[E2E][CHECK USER] Pass {pass_number}: {len(pending)} pending", flush=True)
        next_pending = []
        for position, index in enumerate(pending, start=1):
            account = accounts[index]
            process = None
            try:
                print(
                    f'[E2E][CHECK USER] {position}/{len(pending)} login {account["user"]}',
                    flush=True,
                )
                process, result = app._start_standalone_chromium(account, index)
                username = run_capcut_check_user_workflow(
                    account,
                    {"start_result": result, "stop_event": stop_event},
                )
                errors.pop(account["user"], None)
                save_results(output_path, accounts)
                print(f'[E2E][CHECK USER] OK {account["user"]}|{username}', flush=True)
            except Exception as exc:
                account["userID"] = ""
                errors[account["user"]] = str(exc)
                next_pending.append(index)
                print(f'[E2E][CHECK USER] RETRY {account["user"]}: {exc}', flush=True)
            finally:
                if process is not None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except Exception:
                        process.kill()
            time.sleep(1.0)
        pending = next_pending
        if pending and pass_number < args.passes:
            print("[E2E][CHECK USER] Cooling down before retry pass", flush=True)
            time.sleep(10.0)

    save_results(output_path, accounts)
    error_path = Path("check user") / "login_user_error.txt"
    error_path.parent.mkdir(exist_ok=True)
    error_path.write_text(
        "\n".join(f"{email}|{message}" for email, message in errors.items())
        + ("\n" if errors else ""),
        encoding="utf-8",
    )
    found = sum(1 for account in accounts if account.get("userID"))
    print(f"[E2E][CHECK USER] RESULT {found}/{len(accounts)}", flush=True)
    print(f"[E2E][CHECK USER] OUTPUT {output_path}", flush=True)
    if errors:
        print(f"[E2E][CHECK USER] ERRORS {error_path}", flush=True)
    return 0 if found == len(accounts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
