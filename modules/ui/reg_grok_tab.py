"""Reg Grok tab UI wiring and export helpers."""

import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.orchestrator import Orchestrator
from core.settings import GPMSettings, load_settings, save_settings
from modules.actions.workflow import SIGN_UP_URL, WorkflowInterrupted, run_workflow
from modules.proxy.thuecloud import ThueCloudClient, parse_static_proxy_line, reset_change_ip_url
from modules.storage.google_sheets import append_success_rows


def build_tab(app) -> None:
    app.build_account_workflow_tab(
        "Reg Grok",
        "reg_grok",
        app.start_reg_grok,
        default_passnew="",
        default_threads="3",
        default_delay="1",
        show_table=False,
        pass_label="Pass Grok",
        show_load_export=False,
        import_label="Import Mail",
        import_hint="Format: email|passmail|refresh_token|client_id",
    )


def mail_parts(account: dict) -> tuple[str, str, str, str]:
    parts = str(account.get("api", "")).split("|")
    if len(parts) >= 4:
        email, passmail, refresh_token, client_id = parts[:4]
    else:
        email = account["user"]
        passmail = account["passmail"]
        refresh_token = ""
        client_id = ""
    return email, passmail, refresh_token, client_id


def oauth_export_line(account: dict) -> str:
    email, passmail, refresh_token, client_id = mail_parts(account)
    return f"{email}|{passmail}|{refresh_token}|{client_id}"


def backup_pass_export_line(account: dict) -> str:
    email, passmail, refresh_token, client_id = mail_parts(account)
    return f'{account["passnew"]}|{email}|{passmail}|{refresh_token}|{client_id}'


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


def export_accounts(accounts: list[dict], export_dir: Path | str = "export") -> dict:
    export_dir = Path(export_dir)
    export_dir.mkdir(exist_ok=True)
    success_lines = []
    backup_pass_lines = []
    error_lines = []
    for account in accounts:
        line = oauth_export_line(account)
        if account.get("status") == "true":
            success_lines.append(line)
            backup_pass_lines.append(backup_pass_export_line(account))
        else:
            error_lines.append(line)

    success_path = export_dir / "success.txt"
    backup_pass_path = export_dir / "BackupPass.txt"
    error_path = export_dir / "error.txt"
    success_path.write_text("\n".join(success_lines), encoding="utf-8")
    backup_pass_path.write_text("\n".join(backup_pass_lines), encoding="utf-8")
    error_path.write_text("\n".join(error_lines), encoding="utf-8")
    return {
        "success_count": len(success_lines),
        "backup_pass_count": len(backup_pass_lines),
        "error_count": len(error_lines),
        "success_path": success_path,
        "backup_pass_path": backup_pass_path,
        "error_path": error_path,
    }


def write_result(account: dict, success: bool, export_lock, export_dir: Path | str = "export") -> dict:
    export_dir = Path(export_dir)
    export_dir.mkdir(exist_ok=True)
    backup_pass_path = None
    with export_lock:
        target = export_dir / ("success.txt" if success else "error.txt")
        with target.open("a", encoding="utf-8") as file:
            file.write(oauth_export_line(account) + "\n")
            file.flush()
        if success:
            backup_pass_path = export_dir / "BackupPass.txt"
            with backup_pass_path.open("a", encoding="utf-8") as file:
                file.write(backup_pass_export_line(account) + "\n")
                file.flush()
    return {
        "target": target,
        "backup_pass_path": backup_pass_path,
    }


def status_text(accounts: list[dict], proxy_count: int) -> str:
    created_count = sum(1 for account in accounts if account.get("status") == "true")
    failed_count = sum(1 for account in accounts if "fail" in str(account.get("status", "")).lower())
    return f"Loaded: {len(accounts)} accounts | Created: {created_count} | Failed: {failed_count} | Proxy: {proxy_count}"


class RegGrokWriter:
    HIDDEN_UI_PATTERNS = (
        "[CDP] Waiting",
        "[CDP] Connecting",
        "[CDP] Endpoint ready",
        "[OTP] Signup poll",
        "[WAIT] Holding workflow",
        "[SIGNUP] Details form not visible yet",
        "[B8][VERIFY] Visible checkbox candidate",
        "[B8][VERIFY] Still waiting",
        "[B9][COMPLETE] Verification state",
        "[B10][RESULT] Account page check",
    )

    def __init__(self, output_queue):
        self.output_queue = output_queue
        self.lock = threading.Lock()
        self.current_date = None
        self.log_file = None
        self.ui_buffer = ""

    def write(self, text):
        if not text:
            return
        with self.lock:
            self.write_file(text)
            self.write_ui(text)

    def flush(self):
        with self.lock:
            if self.log_file:
                self.log_file.flush()
            if self.ui_buffer:
                if self.should_show_ui(self.ui_buffer):
                    self.output_queue.put(self.ui_buffer)
                self.ui_buffer = ""

    def write_file(self, text):
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            if self.current_date != today or self.log_file is None:
                if self.log_file:
                    self.log_file.close()
                log_dir = Path("log") / today
                log_dir.mkdir(parents=True, exist_ok=True)
                self.log_file = (log_dir / "app.log").open("a", encoding="utf-8")
                self.current_date = today
            self.log_file.write(text)
            self.log_file.flush()
        except Exception:
            pass

    def write_ui(self, text):
        self.ui_buffer += text
        while "\n" in self.ui_buffer:
            line, self.ui_buffer = self.ui_buffer.split("\n", 1)
            line += "\n"
            if self.should_show_ui(line):
                self.output_queue.put(line)

    def should_show_ui(self, text):
        stripped = text.strip()
        if not stripped:
            return True
        return not any(pattern in stripped for pattern in self.HIDDEN_UI_PATTERNS)


class RegGrokApp:
    STABLE_BROWSER_VERSION = "142.0.7444.163"
    WINDOWS_CHROME_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/{browser_version} Safari/537.36"
    )

    def __init__(self, root):
        self.root = root
        self.output_queue = queue.Queue()
        self.orch = Orchestrator()
        self.app_settings = self.orch.settings
        self.accounts_path = Path("account.txt")
        self.proxy_path = Path("proxy.txt")
        self.accounts = []
        self.proxies = []
        self.task_thread = None
        self.stop_event = threading.Event()
        self.current_accounts = []
        self.account_lock = threading.Lock()
        self.export_lock = threading.Lock()
        self.sheet_lock = threading.Lock()
        self.sheet_rows = []
        self.active_profiles = {}
        self.active_profiles_lock = threading.Lock()
        self.settings_window = None
        self.settings_entries = {}
        sys.stdout = sys.stderr = RegGrokWriter(self.output_queue)
        self.setup_style()
        self.build_ui()
        self.root.after(100, self.drain)

    def setup_style(self):
        self.root.configure(bg="#f3f4f7")
        style = ttk.Style()
        style.theme_use("clam")

    def build_ui(self):
        top = tk.Frame(self.root, bg="#4057a7", height=42)
        top.pack(fill="x")
        top.pack_propagate(False)
        tk.Label(top, text="Reg Grok", bg="#4057a7", fg="#ffffff", font=("Tahoma", 12, "bold")).pack(side="left", padx=12)
        tk.Button(
            top,
            text="Settings",
            command=self.open_settings_window,
            bg="#ffffff",
            fg="#1f2937",
            relief="flat",
            padx=12,
            pady=4,
        ).pack(side="right", padx=10, pady=6)

        body = tk.Frame(self.root, bg="#f3f4f7")
        body.pack(fill="both", expand=True, padx=8, pady=8)

        bar = tk.Frame(body, bg="#ffffff", relief="solid", borderwidth=1)
        bar.pack(fill="x", pady=(0, 8))
        self.tool_button(bar, "Import Mail", self.import_accounts, "#0ea5e9", "#ffffff").pack(side="left", padx=8, pady=6)
        self.tool_button(bar, "Import Proxy", self.import_proxies, "#0891b2", "#ffffff").pack(side="left", padx=4, pady=6)
        tk.Label(bar, text="Pass Grok", bg="#ffffff").pack(side="left", padx=(12, 4), pady=6)
        self.passnew_entry = tk.Entry(bar, width=24, relief="solid", borderwidth=1)
        self.passnew_entry.pack(side="left", pady=6)
        tk.Label(bar, text="Threads", bg="#ffffff").pack(side="left", padx=(12, 4), pady=6)
        self.threads_entry = tk.Entry(bar, width=6, relief="solid", borderwidth=1)
        self.threads_entry.insert(0, "3")
        self.threads_entry.pack(side="left", pady=6)
        tk.Label(bar, text="Delay", bg="#ffffff").pack(side="left", padx=(12, 4), pady=6)
        self.delay_entry = tk.Entry(bar, width=6, relief="solid", borderwidth=1)
        self.delay_entry.insert(0, "1")
        self.delay_entry.pack(side="left", pady=6)
        self.tool_button(bar, "Start", self.start, "#4f46e5", "#ffffff").pack(side="left", padx=4, pady=6)
        self.tool_button(bar, "Stop", self.stop, "#dc2626", "#ffffff").pack(side="left", padx=4, pady=6)

        tk.Label(
            body,
            text="Import mail format: email|passmail|refresh_token|client_id",
            bg="#f3f4f7",
            fg="#475569",
        ).pack(anchor="w", padx=4, pady=(0, 6))

        self.status_label = tk.Label(body, text=status_text(self.accounts, len(self.proxies)), bg="#f3f4f7", fg="#111827", font=("Tahoma", 10, "bold"))
        self.status_label.pack(anchor="w", padx=4, pady=(4, 8))

        self.log = tk.Text(self.root, height=18, bg="#f8fafc", fg="#111827", relief="solid", borderwidth=1)
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def tool_button(self, parent, text, command, color="#ffffff", fg="#111827"):
        return tk.Button(parent, text=text, command=command, bg=color, fg=fg, relief="solid", borderwidth=1, padx=10, pady=4)

    def open_settings_window(self):
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            self.settings_window.focus_force()
            return

        self.settings_window = tk.Toplevel(self.root)
        self.settings_window.title("Reg Grok Settings")
        self.settings_window.geometry("560x170")
        self.settings_window.configure(bg="#f3f4f7")
        self.settings_window.transient(self.root)
        self.settings_window.grab_set()
        self.settings_window.protocol("WM_DELETE_WINDOW", self.close_settings_window)

        body = tk.Frame(self.settings_window, bg="#ffffff", relief="solid", borderwidth=1)
        body.pack(fill="both", expand=True, padx=12, pady=12)
        tk.Label(body, text="GPM API", bg="#ffffff", fg="#111827", font=("Tahoma", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 8))

        fields = (
            ("gpm.api_base_url", "API"),
        )
        self.settings_entries = {}
        for index, (key, label) in enumerate(fields, start=1):
            tk.Label(body, text=label, bg="#ffffff", fg="#334155").grid(row=index, column=0, sticky="w", padx=10, pady=5)
            entry = tk.Entry(body, relief="solid", borderwidth=1)
            entry.grid(row=index, column=1, sticky="ew", padx=10, pady=5)
            self.settings_entries[key] = entry
        body.grid_columnconfigure(1, weight=1)

        actions = tk.Frame(body, bg="#ffffff")
        actions.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="ew", padx=10, pady=(12, 10))
        self.tool_button(actions, "Save", self.save_settings_from_ui, "#16a34a", "#ffffff").pack(side="right")
        self.tool_button(actions, "Cancel", self.close_settings_window, "#ffffff", "#111827").pack(side="right", padx=(0, 8))
        self.fill_settings_form()

    def fill_settings_form(self):
        values = {
            "gpm.api_base_url": self.app_settings.gpm.api_base_url,
        }
        for key, entry in self.settings_entries.items():
            entry.delete(0, "end")
            entry.insert(0, values.get(key, ""))

    def save_settings_from_ui(self):
        api_base_url = self.settings_entries["gpm.api_base_url"].get().strip().rstrip("/")
        if not api_base_url:
            messagebox.showerror("Settings", "GPM API cannot be empty")
            return
        gpm = GPMSettings(
            api_base_url=api_base_url,
            profile_url_template=f"{api_base_url}/api/v3/profiles/{{profile_id}}",
            default_group_id=self.app_settings.gpm.default_group_id,
            default_page=self.app_settings.gpm.default_page,
            default_per_page=self.app_settings.gpm.default_per_page,
        )
        self.app_settings.gpm = gpm
        try:
            save_settings(self.app_settings)
            self.app_settings = load_settings()
            self.orch = Orchestrator(self.app_settings)
            print(f"Saved GPM API settings: {self.app_settings.gpm.api_base_url}")
            self.close_settings_window()
        except Exception as exc:
            messagebox.showerror("Settings", f"Could not save settings: {exc}")

    def close_settings_window(self):
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.destroy()
        self.settings_window = None
        self.settings_entries = {}

    def import_accounts(self):
        path = filedialog.askopenfilename(
            title="Open mail file",
            initialdir=str(Path.cwd()),
            initialfile="account.txt",
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
        )
        if not path:
            return
        self.accounts_path = Path(path)
        self.load_accounts()

    def load_accounts(self):
        if not self.accounts_path.exists():
            messagebox.showinfo("Info", f"File not found: {self.accounts_path}")
            return
        loaded = []
        seen = set()
        skipped = 0
        for line in self.accounts_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
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
            loaded.append(account)
        self.accounts = loaded
        self.refresh_status()
        print(f"Loaded {len(self.accounts)} Reg Grok accounts from {self.accounts_path.name}")
        if skipped:
            print(f"Skipped {skipped} invalid mail API lines. Expected: email|passmail|refresh_token|client_id")

    def import_proxies(self):
        path = filedialog.askopenfilename(
            title="Open proxy.txt",
            initialdir=str(Path.cwd()),
            initialfile="proxy.txt",
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
        )
        if not path:
            return
        self.proxy_path = Path(path)
        self.load_proxies()

    def load_proxies(self):
        if not self.proxy_path.exists():
            messagebox.showinfo("Info", f"File not found: {self.proxy_path}")
            return
        loaded = []
        skipped = 0
        for line in self.proxy_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                loaded.append(parse_static_proxy_line(line))
            except ValueError as exc:
                skipped += 1
                print(f"Skipped proxy line: {exc}")
        self.proxies = loaded
        self.refresh_status()
        print(f"Loaded {len(self.proxies)} proxies from {self.proxy_path.name}")
        if skipped:
            print(f"Skipped {skipped} invalid proxy lines")

    def refresh_status(self):
        self.status_label.configure(text=status_text(self.accounts, len(self.proxies)))

    def start(self):
        selected = [account for account in self.accounts if account["picked"]]
        if not selected:
            messagebox.showinfo("Info", "Import mail first")
            return
        passnew = self.passnew_entry.get().strip()
        if not passnew:
            messagebox.showinfo("Info", "Enter Pass Grok")
            return
        try:
            max_threads = max(1, int(self.threads_entry.get().strip() or "1"))
        except ValueError:
            messagebox.showinfo("Info", "Threads must be a number")
            return
        try:
            launch_delay = max(0.0, float(self.delay_entry.get().strip() or "0"))
        except ValueError:
            messagebox.showinfo("Info", "Delay must be a number")
            return
        if self.task_thread and self.task_thread.is_alive():
            messagebox.showinfo("Info", "Reg Grok is running")
            return
        self.stop_event.clear()
        self.current_accounts = selected
        for account in selected:
            account["passnew"] = passnew
        self.refresh_status()
        self.task_thread = threading.Thread(target=self.run_accounts, args=(selected, max_threads, launch_delay), daemon=True)
        self.task_thread.start()

    def run_accounts(self, accounts, max_threads, launch_delay):
        group_name = self.selected_group_name()
        semaphore = threading.Semaphore(max_threads)
        workers = []
        total_accounts = len(accounts)
        if self.proxies:
            print(f"Using {len(self.proxies)} imported proxies for {max_threads} threads")
            self.reset_loaded_proxy_ips()
        for index, account in enumerate(accounts):
            if self.stop_event.is_set():
                self.mark_account_status(account, "stopped")
                print("Stop requested, no longer starting new threads")
                break
            worker = threading.Thread(
                target=self.run_worker,
                args=(account, group_name, index, total_accounts, semaphore, max_threads),
                daemon=True,
            )
            workers.append(worker)
            worker.start()
            if launch_delay > 0 and index < total_accounts - 1:
                waited = 0.0
                while waited < launch_delay:
                    if self.stop_event.is_set():
                        break
                    sleep_for = min(0.1, launch_delay - waited)
                    time.sleep(sleep_for)
                    waited += sleep_for
        for worker in workers:
            worker.join()
        self.flush_sheet_rows(force=True)
        print("Reg Grok workflow stopped" if self.stop_event.is_set() else "Reg Grok workflow completed")
        self.current_accounts = []
        self.task_thread = None

    def run_worker(self, account, group_name, index, total, semaphore, max_threads):
        with semaphore:
            profile = None
            try:
                if self.stop_event.is_set():
                    self.mark_account_status(account, "stopped")
                    return
                self.mark_account_status(account, "running")
                proxy = self.proxy_for_worker(index, max_threads)
                payload = self.build_temp_profile_payload(account, group_name, index, proxy=proxy)
                profile = self.orch.create_profile(payload)
                with self.active_profiles_lock:
                    self.active_profiles[account["user"]] = profile.id
                print(f'Created temp profile: {profile.id} - {profile.name} for {account["user"]}')
                if self.stop_event.is_set():
                    self.mark_account_status(account, "stopped")
                    return
                result = self.orch.start_profile(
                    profile_id=profile.id,
                    window_index=index % min(max_threads, total),
                    total_windows=min(max_threads, total),
                )
                print(f'Started temp profile: {profile.id} - {result.success} for {account["user"]}')
                workflow_ok = bool(run_workflow(account, {"profile": profile, "start_result": result, "start_url": SIGN_UP_URL, "stop_event": self.stop_event}))
                if self.stop_event.is_set():
                    self.mark_account_status(account, "stopped")
                    return
                if workflow_ok:
                    self.mark_account_status(account, "true")
                    write_result(account, True, self.export_lock)
                    self.queue_sheet_success(account)
                    print(f'Success: {account["user"]}')
                else:
                    self.mark_account_status(account, "change fail")
                    write_result(account, False, self.export_lock)
                    print(f'Change failed: {account["user"]}')
            except WorkflowInterrupted as exc:
                self.mark_account_status(account, "stopped")
                print(f'Workflow stopped: {account["user"]} - {exc}')
            except Exception as exc:
                self.mark_account_status(account, f"change fail: {exc}")
                write_result(account, False, self.export_lock)
                print(f'Error: {account["user"]} - {exc}')
            finally:
                with self.active_profiles_lock:
                    self.active_profiles.pop(account["user"], None)
                if profile is not None:
                    try:
                        self.orch.close_profile(profile.id)
                        print(f'Closed temp profile: {profile.id} for {account["user"]}')
                    except Exception as exc:
                        print(f'Close profile error: {profile.id} - {account["user"]} - {exc}')

    def queue_sheet_success(self, account):
        row = [oauth_export_line(account), account.get("passnew", "")]
        with self.sheet_lock:
            self.sheet_rows.append(row)
        self.flush_sheet_rows(force=False)

    def flush_sheet_rows(self, force=False):
        with self.sheet_lock:
            if not self.sheet_rows:
                return
            if not force and len(self.sheet_rows) < 5:
                return
            rows = list(self.sheet_rows)
            self.sheet_rows.clear()
        try:
            append_success_rows(rows)
        except Exception as exc:
            with self.sheet_lock:
                self.sheet_rows = rows + self.sheet_rows

    def selected_group_name(self):
        try:
            for group in self.orch.get_profile_groups():
                if group.name.lower() == "grok":
                    return group.name
        except Exception as exc:
            print(f"Load group warning: {exc}")
        return "grok"

    def proxy_for_worker(self, index, max_threads):
        if not self.proxies:
            return None
        active_slots = min(max_threads, max(1, len(self.current_accounts)))
        slot = index % active_slots
        proxy_index = min(len(self.proxies) - 1, (slot * len(self.proxies)) // active_slots)
        return self.proxies[proxy_index]

    def mask_proxy(self, raw_proxy):
        parts = str(raw_proxy).split(":")
        if len(parts) >= 4:
            return ":".join(parts[:3] + ["***"])
        return str(raw_proxy)

    def reset_loaded_proxy_ips(self):
        seen = set()
        for proxy in self.proxies:
            if not proxy.change_ip_url or proxy.change_ip_url in seen:
                continue
            seen.add(proxy.change_ip_url)
            masked_proxy = self.mask_proxy(proxy.raw_proxy)
            print(f"Resetting proxy IP: {masked_proxy}")
            try:
                response = reset_change_ip_url(proxy.change_ip_url)
                if response:
                    print(f"Change-IP response for {masked_proxy}: {response[:200]}")
            except Exception as exc:
                print(f"Change-IP warning for {masked_proxy}: {exc}")

    def get_account_raw_proxy(self, account, proxy=None):
        if proxy is not None:
            print(f'Attached imported proxy for {account["user"]}: {self.mask_proxy(proxy.raw_proxy)}')
            return proxy.raw_proxy
        if not self.app_settings.thuecloud.enabled:
            return ""
        raw_proxy = ThueCloudClient(self.app_settings.thuecloud).get_raw_proxy()
        if not raw_proxy:
            raise RuntimeError("ThueCloud returned empty proxy")
        print(f'Attached ThueCloud proxy for {account["user"]}')
        return raw_proxy

    def chrome_user_agent(self):
        return self.WINDOWS_CHROME_UA.format(browser_version=self.STABLE_BROWSER_VERSION)

    def build_gpm_profile_payload(self, profile_name, group_name, raw_proxy="", startup_urls=""):
        return {
            "profile_name": profile_name,
            "group_name": group_name,
            "browser_core": "chromium",
            "browser_name": "Chrome",
            "is_random_browser_version": False,
            "browser_version": self.STABLE_BROWSER_VERSION,
            "raw_proxy": raw_proxy,
            "startup_urls": startup_urls,
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

    def build_temp_profile_payload(self, account, group_name, index, proxy=None):
        suffix = datetime.now().strftime("%H%M%S")
        raw_proxy = self.get_account_raw_proxy(account, proxy)
        profile_name = f'grok_{account["user"].split("@")[0]}_{index + 1}_{suffix}'
        return self.build_gpm_profile_payload(profile_name, group_name, raw_proxy=raw_proxy, startup_urls=SIGN_UP_URL)

    def mark_account_status(self, account, status):
        with self.account_lock:
            account["status"] = status
        self.root.after(0, self.refresh_status)

    def stop(self):
        if not self.task_thread or not self.task_thread.is_alive():
            print("No Reg Grok workflow is running")
            return
        self.stop_event.set()
        with self.active_profiles_lock:
            to_close = list(self.active_profiles.items())
        for _, profile_id in to_close:
            try:
                self.orch.close_profile(profile_id)
                print(f"Stop closed profile: {profile_id}")
            except Exception as exc:
                print(f"Stop close profile error: {profile_id} - {exc}")
        print("Stop requested")

    def drain(self):
        while not self.output_queue.empty():
            self.log.insert("end", self.output_queue.get_nowait())
            self.log.see("end")
        self.root.after(100, self.drain)


def main() -> None:
    root = tk.Tk()
    root.title("Reg Grok")
    root.geometry("1100x700")
    RegGrokApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
