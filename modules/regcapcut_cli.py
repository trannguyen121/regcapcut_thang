"""Console entrypoint for building Reg CapCut with Nuitka."""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.settings import AppSettings, load_settings
from modules.browser.chromium import ChromiumSession, resolve_chromium_154, parse_browser_proxy
from modules.actions.capcut_workflow import (
    CAPCUT_SIGN_UP_URL,
    CapCutWorkflowInterrupted,
    run_capcut_workflow,
)
from modules.proxy.thuecloud import ThueCloudClient, parse_static_proxy_line, reset_change_ip_url


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def safe_print(message: str) -> None:
    try:
        print(message, flush=True)
    except Exception:
        pass


def clean_error_text(value: object) -> str:
    text = str(value)
    for path in (Path.cwd(), app_dir()):
        try:
            text = text.replace(str(path), ".")
        except Exception:
            pass
    return text


def parse_mail_api_line(line: str) -> dict | None:
    parts = line.strip().split("|")
    # Accept 4 parts (email|passmail|refreshtoken|clientid) or 5 parts (ignore 5th)
    if len(parts) < 4:
        return None
    # If 5 parts, take only first 4 (ignore 5th column)
    email, passmail, refresh_token, client_id = parts[:4]
    if not email or not passmail or not refresh_token or not client_id:
        return None
    api = "|".join((email, passmail, refresh_token, client_id))
    return {
        "picked": True,
        "user": email,
        "email": email,
        "pass": "",
        "passmail": passmail,
        "passnew": "",
        "api": api,
        "status": "",
    }


def mail_parts(account: dict) -> tuple[str, str, str, str]:
    parts = str(account.get("api", "")).split("|")
    if len(parts) >= 4:
        return parts[0], parts[1], parts[2], parts[3]
    return account["user"], account.get("passmail", ""), "", ""


def oauth_export_line(account: dict) -> str:
    email, passmail, refresh_token, client_id = mail_parts(account)
    return f"{email}|{passmail}|{refresh_token}|{client_id}"


def backup_pass_export_line(account: dict) -> str:
    email, passmail, refresh_token, client_id = mail_parts(account)
    return f'{account["passnew"]}|{email}|{passmail}|{refresh_token}|{client_id}'


def append_result(account: dict, success: bool, export_dir: Path, lock: threading.Lock) -> None:
    export_dir.mkdir(parents=True, exist_ok=True)
    with lock:
        target = export_dir / ("success.txt" if success else "error.txt")
        with target.open("a", encoding="utf-8") as file:
            file.write(oauth_export_line(account) + "\n")
        if success:
            with (export_dir / "BackupPass.txt").open("a", encoding="utf-8") as file:
                file.write(backup_pass_export_line(account) + "\n")


def load_accounts(path: Path, pass_capcut: str) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Mail file not found: {path.name}")
    accounts = []
    seen = set()
    skipped = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        account = parse_mail_api_line(line)
        if account is None:
            skipped += 1
            continue
        key = (account["user"], account["passmail"])
        if key in seen:
            continue
        seen.add(key)
        account["passnew"] = pass_capcut
        accounts.append(account)
    safe_print(f"Loaded {len(accounts)} mail API lines from {path}")
    if skipped:
        safe_print(f"Skipped {skipped} invalid lines. Expected: email|passmail|refresh_token|client_id|(optional mailbox)")
    if not accounts:
        raise RuntimeError("No valid mail API lines loaded")
    return accounts


def load_proxies(path: Path | None) -> list:
    if path is None or not path.exists():
        return []
    proxies = []
    skipped = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            proxies.append(parse_static_proxy_line(line))
        except ValueError as exc:
            skipped += 1
            safe_print(f"Skipped proxy line: {exc}")
    safe_print(f"Loaded {len(proxies)} proxies from {path}")
    if skipped:
        safe_print(f"Skipped {skipped} invalid proxy lines")
    return proxies


def load_app_settings(chromium_path=None) -> AppSettings:
    settings = load_settings()
    configured = chromium_path or os.getenv("REGCAPCUT_CHROMIUM_PATH")
    if configured:
        settings.chromium_path = configured

    thue_enabled = os.getenv("REGCAPCUT_THUECLOUD_ENABLED")
    if thue_enabled is not None:
        settings.thuecloud.enabled = thue_enabled.strip().lower() in {"1", "true", "yes", "y", "on"}
    for env_name, attr_name in (
        ("REGCAPCUT_THUECLOUD_API_BASE_URL", "api_base_url"),
        ("REGCAPCUT_THUECLOUD_ACCESS_TOKEN", "access_token"),
        ("REGCAPCUT_THUECLOUD_PROXY_ENDPOINT", "proxy_endpoint"),
        ("REGCAPCUT_THUECLOUD_PROXY_METHOD", "proxy_method"),
        ("REGCAPCUT_THUECLOUD_PROXY_API_KEY", "proxy_api_key"),
    ):
        value = os.getenv(env_name)
        if value is not None:
            setattr(settings.thuecloud, attr_name, value.strip())

    capmonster_key = os.getenv("REGCAPCUT_CAPMONSTER_API_KEY")
    if capmonster_key is not None:
        settings.capmonster.api_key = capmonster_key.strip()
    return settings


def mask_proxy(raw_proxy: str) -> str:
    try:
        return (parse_browser_proxy(raw_proxy) or {}).get("server", "")
    except RuntimeError:
        return "<invalid proxy>"


def reset_loaded_proxy_ips(proxies: list) -> None:
    seen = set()
    for proxy in proxies:
        if not proxy.change_ip_url or proxy.change_ip_url in seen:
            continue
        seen.add(proxy.change_ip_url)
        masked_proxy = mask_proxy(proxy.raw_proxy)
        safe_print(f"Resetting proxy IP: {masked_proxy}")
        try:
            response = reset_change_ip_url(proxy.change_ip_url)
            if response:
                safe_print(f"Change-IP response for {masked_proxy}: {response[:200]}")
        except Exception as exc:
            safe_print(f"Change-IP warning for {masked_proxy}: {exc}")


def proxy_for_worker(proxies: list, index: int, max_threads: int, account_count: int):
    if not proxies:
        return None
    active_slots = min(max_threads, max(1, account_count))
    slot = index % active_slots
    proxy_index = min(len(proxies) - 1, (slot * len(proxies)) // active_slots)
    return proxies[proxy_index]


def raw_proxy_for_account(settings: AppSettings, account: dict, proxy=None) -> str:
    if proxy is not None:
        safe_print(f'Attached imported proxy for {account["user"]}: {mask_proxy(proxy.raw_proxy)}')
        return proxy.raw_proxy
    if not settings.thuecloud.enabled:
        return ""
    raw_proxy = ThueCloudClient(settings.thuecloud).get_raw_proxy()
    if not raw_proxy:
        raise RuntimeError("ThueCloud returned empty proxy")
    safe_print(f'Attached ThueCloud proxy for {account["user"]}')
    return raw_proxy


def run_worker(
    account: dict,
    index: int,
    total: int,
    max_threads: int,
    semaphore: threading.Semaphore,
    settings: AppSettings,
    proxies: list,
    export_dir: Path,
    export_lock: threading.Lock,
) -> None:
    with semaphore:
        session = None
        try:
            account["status"] = "running"
            proxy = proxy_for_worker(proxies, index, max_threads, total)
            raw_proxy = raw_proxy_for_account(settings, account, proxy)
            session = ChromiumSession(
                resolve_chromium_154(settings.chromium_path), raw_proxy=raw_proxy,
                window_settings=settings.browser_window,
                index=index % min(max_threads, total), total_windows=min(max_threads, total),
            )
            ok = bool(run_capcut_workflow(account, {"start_result": session}))
            account["status"] = "true" if ok else "change fail"
            append_result(account, ok, export_dir, export_lock)
            safe_print(("Success" if ok else "Failed") + f': {account["user"]}')
        except CapCutWorkflowInterrupted as exc:
            account["status"] = "stopped"
            safe_print(f'Workflow stopped: {account["user"]} - {exc}')
        except Exception as exc:
            account["status"] = "change fail"
            append_result(account, False, export_dir, export_lock)
            safe_print(f'Error: {account["user"]} - {exc.__class__.__name__}: {clean_error_text(exc)}')
        finally:
            if session is not None:
                session.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reg CapCut account creator")
    parser.add_argument("--mail-file", default=os.getenv("REGCAPCUT_MAIL_FILE", "account.txt"))
    parser.add_argument("--proxy-file", default=os.getenv("REGCAPCUT_PROXY_FILE", "proxy.txt"))
    parser.add_argument("--pass-capcut", default=os.getenv("REGCAPCUT_PASS_CAPCUT", "1234567"))
    parser.add_argument("--chromium-path", default=os.getenv("REGCAPCUT_CHROMIUM_PATH", ""))
    parser.add_argument("--threads", type=int, default=int(os.getenv("REGCAPCUT_THREADS", "3")))
    parser.add_argument("--delay", type=float, default=float(os.getenv("REGCAPCUT_DELAY", "1")))
    parser.add_argument("--output-dir", default=os.getenv("REGCAPCUT_OUTPUT_DIR", "export"))
    return parser.parse_args()


def main() -> int:
    try:
        base_dir = app_dir()
        args = parse_args()
        pass_capcut = args.pass_capcut.strip()
        if not pass_capcut:
            safe_print("Missing Pass CapCut. Set REGCAPCUT_PASS_CAPCUT or pass --pass-capcut.")
            return 2
        output_dir = (base_dir / args.output_dir).resolve() if not Path(args.output_dir).is_absolute() else Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        mail_file = (base_dir / args.mail_file).resolve() if not Path(args.mail_file).is_absolute() else Path(args.mail_file)
        proxy_file = (base_dir / args.proxy_file).resolve() if args.proxy_file and not Path(args.proxy_file).is_absolute() else Path(args.proxy_file) if args.proxy_file else None
        settings = load_app_settings(args.chromium_path)
        resolve_chromium_154(settings.chromium_path)
        accounts = load_accounts(mail_file, pass_capcut)
        proxies = load_proxies(proxy_file)
        if proxies:
            reset_loaded_proxy_ips(proxies)
        max_threads = max(1, args.threads)
        semaphore = threading.Semaphore(max_threads)
        export_lock = threading.Lock()
        workers = []
        for index, account in enumerate(accounts):
            worker = threading.Thread(
                target=run_worker,
                args=(account, index, len(accounts), max_threads, semaphore, settings, proxies, output_dir, export_lock),
                daemon=False,
            )
            workers.append(worker)
            worker.start()
            if args.delay > 0 and index < len(accounts) - 1:
                time.sleep(args.delay)
        for worker in workers:
            worker.join()
        success_count = sum(1 for account in accounts if account.get("status") == "true")
        fail_count = len(accounts) - success_count
        safe_print(f"Completed. Success={success_count} Failed={fail_count} Output={output_dir}")
        return 0 if fail_count == 0 else 1
    except Exception as exc:
        safe_print(f"Fatal: {exc.__class__.__name__}: {clean_error_text(exc)}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
