"""Console entrypoint for building Reg Grok with Nuitka."""

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

from core.orchestrator import Orchestrator
from core.settings import AppSettings, GPMSettings, load_settings
from modules.actions.workflow import SIGN_UP_URL, WorkflowInterrupted, run_workflow
from modules.proxy.thuecloud import ThueCloudClient, parse_static_proxy_line, reset_change_ip_url


STABLE_BROWSER_VERSION = "142.0.7444.163"
WINDOWS_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{browser_version} Safari/537.36"
)


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
    if len(parts) < 4:
        return None
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


def load_accounts(path: Path, pass_grok: str) -> list[dict]:
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
        account["passnew"] = pass_grok
        accounts.append(account)
    safe_print(f"Loaded {len(accounts)} mail API lines from {path}")
    if skipped:
        safe_print(f"Skipped {skipped} invalid lines. Expected: email|passmail|refresh_token|client_id")
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


def load_app_settings(gpm_api: str | None) -> AppSettings:
    settings = load_settings()
    gpm_api = (gpm_api or os.getenv("REGGROK_GPM_API") or settings.gpm.api_base_url).strip().rstrip("/")
    settings.gpm = GPMSettings(
        api_base_url=gpm_api,
        profile_url_template=f"{gpm_api}/api/v3/profiles/{{profile_id}}",
        default_group_id=settings.gpm.default_group_id,
        default_page=settings.gpm.default_page,
        default_per_page=settings.gpm.default_per_page,
    )

    thue_enabled = os.getenv("REGGROK_THUECLOUD_ENABLED")
    if thue_enabled is not None:
        settings.thuecloud.enabled = thue_enabled.strip().lower() in {"1", "true", "yes", "y", "on"}
    for env_name, attr_name in (
        ("REGGROK_THUECLOUD_API_BASE_URL", "api_base_url"),
        ("REGGROK_THUECLOUD_ACCESS_TOKEN", "access_token"),
        ("REGGROK_THUECLOUD_PROXY_ENDPOINT", "proxy_endpoint"),
        ("REGGROK_THUECLOUD_PROXY_METHOD", "proxy_method"),
        ("REGGROK_THUECLOUD_PROXY_API_KEY", "proxy_api_key"),
    ):
        value = os.getenv(env_name)
        if value is not None:
            setattr(settings.thuecloud, attr_name, value.strip())

    capmonster_key = os.getenv("REGGROK_CAPMONSTER_API_KEY")
    if capmonster_key is not None:
        settings.capmonster.api_key = capmonster_key.strip()
    return settings


def selected_group_name(orch: Orchestrator) -> str:
    try:
        for group in orch.get_profile_groups():
            if group.name.lower() == "grok":
                return group.name
    except Exception as exc:
        safe_print(f"Load group warning: {exc}")
    return "grok"


def chrome_user_agent() -> str:
    return WINDOWS_CHROME_UA.format(browser_version=STABLE_BROWSER_VERSION)


def build_gpm_profile_payload(profile_name: str, group_name: str, raw_proxy: str = "") -> dict:
    return {
        "profile_name": profile_name,
        "group_name": group_name,
        "browser_core": "chromium",
        "browser_name": "Chrome",
        "is_random_browser_version": False,
        "browser_version": STABLE_BROWSER_VERSION,
        "raw_proxy": raw_proxy,
        "startup_urls": SIGN_UP_URL,
        "is_masked_font": False,
        "is_noise_canvas": False,
        "is_noise_webgl": False,
        "is_noise_client_rect": False,
        "is_noise_audio_context": False,
        "is_random_screen": False,
        "is_masked_webgl_data": False,
        "is_masked_media_device": False,
        "is_random_os": False,
        "os": "Windows 11",
        "webrtc_mode": 1,
        "timezone_base_on_ip": True,
        "is_language_base_on_ip": True,
    }


def mask_proxy(raw_proxy: str) -> str:
    parts = str(raw_proxy).split(":")
    if len(parts) >= 4:
        return ":".join(parts[:3] + ["***"])
    return str(raw_proxy)


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
    orch: Orchestrator,
    settings: AppSettings,
    group_name: str,
    proxies: list,
    export_dir: Path,
    export_lock: threading.Lock,
) -> None:
    with semaphore:
        profile = None
        try:
            account["status"] = "running"
            proxy = proxy_for_worker(proxies, index, max_threads, total)
            raw_proxy = raw_proxy_for_account(settings, account, proxy)
            suffix = datetime.now().strftime("%H%M%S")
            profile_name = f'grok_{account["user"].split("@")[0]}_{index + 1}_{suffix}'
            payload = build_gpm_profile_payload(profile_name, group_name, raw_proxy=raw_proxy)
            profile = orch.create_profile(payload)
            safe_print(f'Created temp profile: {profile.id} for {account["user"]}')
            start_result = orch.start_profile(
                profile_id=profile.id,
                window_index=index % min(max_threads, total),
                total_windows=min(max_threads, total),
            )
            safe_print(f'Started temp profile: {profile.id} - {start_result.success} for {account["user"]}')
            ok = bool(run_workflow(account, {"profile": profile, "start_result": start_result, "start_url": SIGN_UP_URL}))
            account["status"] = "true" if ok else "change fail"
            append_result(account, ok, export_dir, export_lock)
            safe_print(("Success" if ok else "Failed") + f': {account["user"]}')
        except WorkflowInterrupted as exc:
            account["status"] = "stopped"
            safe_print(f'Workflow stopped: {account["user"]} - {exc}')
        except Exception as exc:
            account["status"] = "change fail"
            append_result(account, False, export_dir, export_lock)
            safe_print(f'Error: {account["user"]} - {exc.__class__.__name__}: {clean_error_text(exc)}')
        finally:
            if profile is not None:
                try:
                    orch.close_profile(profile.id)
                    safe_print(f'Closed temp profile: {profile.id} for {account["user"]}')
                except Exception as exc:
                    safe_print(f'Close profile error: {profile.id} - {exc.__class__.__name__}: {clean_error_text(exc)}')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reg Grok account creator")
    parser.add_argument("--mail-file", default=os.getenv("REGGROK_MAIL_FILE", "account.txt"))
    parser.add_argument("--proxy-file", default=os.getenv("REGGROK_PROXY_FILE", "proxy.txt"))
    parser.add_argument("--pass-grok", default=os.getenv("REGGROK_PASS_GROK", ""))
    parser.add_argument("--gpm-api", default=os.getenv("REGGROK_GPM_API", ""))
    parser.add_argument("--threads", type=int, default=int(os.getenv("REGGROK_THREADS", "3")))
    parser.add_argument("--delay", type=float, default=float(os.getenv("REGGROK_DELAY", "1")))
    parser.add_argument("--output-dir", default=os.getenv("REGGROK_OUTPUT_DIR", "export"))
    return parser.parse_args()


def main() -> int:
    try:
        base_dir = app_dir()
        args = parse_args()
        pass_grok = args.pass_grok.strip()
        if not pass_grok:
            safe_print("Missing Pass Grok. Set REGGROK_PASS_GROK or pass --pass-grok.")
            return 2
        output_dir = (base_dir / args.output_dir).resolve() if not Path(args.output_dir).is_absolute() else Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        mail_file = (base_dir / args.mail_file).resolve() if not Path(args.mail_file).is_absolute() else Path(args.mail_file)
        proxy_file = (base_dir / args.proxy_file).resolve() if args.proxy_file and not Path(args.proxy_file).is_absolute() else Path(args.proxy_file) if args.proxy_file else None
        settings = load_app_settings(args.gpm_api)
        orch = Orchestrator(settings)
        accounts = load_accounts(mail_file, pass_grok)
        proxies = load_proxies(proxy_file)
        if proxies:
            reset_loaded_proxy_ips(proxies)
        group_name = selected_group_name(orch)
        max_threads = max(1, args.threads)
        semaphore = threading.Semaphore(max_threads)
        export_lock = threading.Lock()
        workers = []
        for index, account in enumerate(accounts):
            worker = threading.Thread(
                target=run_worker,
                args=(account, index, len(accounts), max_threads, semaphore, orch, settings, group_name, proxies, output_dir, export_lock),
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
