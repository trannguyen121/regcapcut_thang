"""Reg CapCut tab UI - standalone application."""

import json
import queue
import re
from urllib.request import urlopen
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


def load_app_version() -> str:
    """Read the single version value used by source runs and packaged builds."""
    roots = []
    if getattr(sys, "frozen", False):
        roots.extend((Path(sys.executable).resolve().parent, Path(getattr(sys, "_MEIPASS", ""))))
    roots.append(PROJECT_ROOT)
    for root in roots:
        try:
            value = (root / "BUILD_VERSION.txt").read_text(encoding="utf-8-sig").strip().lstrip("vV")
        except OSError:
            continue
        if re.fullmatch(r"\d+(?:\.\d+)*", value):
            return value
    return "8.1"


APP_VERSION = load_app_version()
APP_TITLE = f"Reg CapCut v{APP_VERSION}"

from core.settings import load_settings, save_settings
from modules.browser.chromium import ChromiumSession, resolve_chromium_154, resolve_installed_chrome, parse_browser_proxy
from modules.actions.capcut_workflow import (
    CAPCUT_SIGN_UP_URL,
    CapCutWorkflowInterrupted,
    run_capcut_workflow,
)
from modules.actions.capcut_add_link import (
    CAPCUT_LOGIN_URL,
    CapCutLoginError,
    CannotJoinSpaceError,
    LinkFullError,
    AlreadyJoinedSpaceError,
    JoinVerificationError,
    run_capcut_add_link_workflow,
    run_capcut_check_pro_workflow,
    run_capcut_check_user_workflow,
    run_capcut_login_hold_workflow,
)
from modules.proxy.thuecloud import ThueCloudClient, parse_static_proxy_line, reset_change_ip_url
from modules.proxy.expressvpn import ExpressVPNController, ExpressVPNError
from modules.storage.google_sheets import append_success_rows
from modules.ui.capcut_space_check import CheckSpaceMixin


def _add_link_assignment(account: dict) -> str:
    """A failed login without a pending Submit has not used its invitation."""
    if account.get("status") == "login fail" and not account.get("join_pending"):
        return ""
    return account.get("assigned_link") or ""


def build_tab(app) -> None:
    app.build_account_workflow_tab(
        "Reg CapCut",
        "reg_capcut",
        app.start_reg_capcut,
        default_passnew="1234567",
        default_threads="3",
        default_delay="1",
        show_table=True,
        table_columns=("pick", "user", "pass", "userID", "status"),
        pass_label="Pass CapCut",
        show_load_export=True,
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
        "userID": "",
        "api": api,
        "status": "",
    }


def parse_capcut_login_line(line: str) -> dict | None:
    parts = line.strip().split("|")
    if len(parts) < 2:
        return None
    email, password = parts[:2]
    if not email.strip() or not password.strip():
        return None
    return {
        "picked": True,
        "user": email.strip(),
        "email": email.strip(),
        "passnew": password.strip(),
        "status": "",
    }


def parse_add_link_line(line: str) -> tuple[str, int] | None:
    """Parse either a plain URL or an exported ``URL|used_count`` line."""
    value = line.strip()
    if not value:
        return None

    link = value
    count = 0
    candidate, separator, raw_count = value.rpartition("|")
    if separator and raw_count.strip().isdigit():
        link = candidate.strip()
        count = min(6, max(0, int(raw_count.strip())))

    if not link.lower().startswith(("http://", "https://")):
        return None
    return link, count


def assign_links(accounts: list[dict], links: list[str], accounts_per_link: int = 6) -> list[tuple[dict, str]]:
    if accounts_per_link < 1:
        raise ValueError("accounts_per_link must be at least 1")
    capacity = len(links) * accounts_per_link
    return [
        (account, links[index // accounts_per_link])
        for index, account in enumerate(accounts[:capacity])
    ]


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


def export_registered_accounts_file(accounts: list[dict], export_dir: Path | str = "export") -> Path:
    """Export selected Reg CapCut rows as email|password|userID."""
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / "reg_accounts.txt"
    lines = [
        f'{acc.get("user", acc.get("email", ""))}|{acc.get("passnew", acc.get("pass", ""))}|{acc.get("userID", "")}'
        for acc in accounts
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


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


class RegCapCutWriter:
    HIDDEN_UI_PATTERNS = (
        "[CDP] Waiting",
        "[CDP] Connecting",
        "[CDP] Endpoint ready",
        "[OTP] Signup poll",
        "[WAIT] Holding workflow",
        "[CAPCUT][OTP] Poll",
    )

    def __init__(self, output_queue):
        self.output_queue = output_queue
        self.lock = threading.Lock()
        self.current_date = None
        self.log_file = None
        self.ui_buffer = ""
        self.input_buffer = ""

    def write(self, text):
        if not text:
            return
        with self.lock:
            self.input_buffer += text
            while "\n" in self.input_buffer:
                line, self.input_buffer = self.input_buffer.split("\n", 1)
                self._emit_line(line + "\n")

    def flush(self):
        with self.lock:
            if self.input_buffer:
                self._emit_line(self.input_buffer)
                self.input_buffer = ""
            if self.ui_buffer:
                if self.should_show_ui(self.ui_buffer):
                    self.output_queue.put(self.ui_buffer)
                self.ui_buffer = ""
            if self.log_file:
                self.log_file.flush()

    @staticmethod
    def timestamp_line(text):
        """Add one timestamp while preserving the caller's newline."""
        if not text or not text.strip():
            return text
        return f'[{datetime.now().strftime("%H:%M:%S")}] {text}'

    def _emit_line(self, text):
        timestamped = self.timestamp_line(text)
        self.write_file(timestamped)
        self.write_ui(timestamped)

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


class RegCapCutApp(CheckSpaceMixin):
    # Keep these as escapes so Windows consoles, build scripts and source-file
    # encoding conversions cannot replace the checkbox glyphs with "?".
    CHECKBOX_SELECTED = "\u2611"
    CHECKBOX_UNSELECTED = "\u2610"

    def __init__(self, root):
        self.root = root
        self.output_queue = queue.Queue()
        self.app_settings = load_settings()
        self.accounts_path = Path("account.txt")
        self.proxy_path = Path("proxy.txt")
        self.accounts = []
        self.proxies = []
        self.add_link_accounts_path = Path("account.txt")
        self.add_link_links_path = Path("link.txt")
        self.add_link_accounts = []
        self.add_link_account_selected = set()
        self.add_link_account_state_path = Path("add link") / "account_status.json"
        self.add_link_workspace_path = Path("add link") / "workspace.json"
        self.add_link_account_export_path = Path("add link") / "acc_selected.txt"
        self.add_link_username_export_path = Path("check user") / "login_user.txt"
        self.reg_username_export_path = Path("check user") / "reg_user.txt"
        self.add_link_account_tree = None
        self.reg_account_tree = None
        self.reg_account_drag_anchor = None
        self.add_link_account_drag_anchor = None
        self.add_link_links = []
        self.add_link_selected = set()
        self.add_link_run_links = []
        self.add_link_counts = {}
        self.add_link_counts_lock = threading.Lock()
        self.add_link_export_path = Path("export") / "add_link_used.txt"
        self.add_link_remaining_path = Path("export") / "add_link_remaining.txt"
        self.add_link_success_path = Path("add link") / "acc_add_true.txt"
        self.add_link_fail_path = Path("add link") / "acc_add_fail.txt"
        self.add_link_error_path = Path("export") / "add_link_error.txt"
        self.add_link_result_lock = threading.Lock()
        self.add_link_errors = {}
        self.add_link_full = set()
        self.add_link_stats_window = None
        self.add_link_stats_tree = None
        self.add_link_drag_anchor = None
        self.check_pro_accounts_path = Path("account.txt")
        self.check_pro_accounts = []
        self.check_pro_path = Path("check gói") / "capcut_pro.txt"
        self.check_not_pro_path = Path("check gói") / "capcut_not_pro.txt"
        self.check_pro_error_path = Path("check gói") / "capcut_check_error.txt"
        self.check_pro_lock = threading.Lock()
        self.task_thread = None
        self.stop_event = threading.Event()
        self.current_accounts = []
        self.account_lock = threading.Lock()
        self.export_lock = threading.Lock()
        self.sheet_lock = threading.Lock()
        self.sheet_rows = []
        self.active_profiles = {}
        self.active_standalone_processes = {}
        self.active_profiles_lock = threading.Lock()
        self.settings_window = None
        self.settings_entries = {}
        sys.stdout = sys.stderr = RegCapCutWriter(self.output_queue)
        self.setup_style()
        self.build_ui()
        self.restore_add_link_workspace()
        # Prevent Tk's default focus from landing on the first button; without this
        # pressing Space anywhere (including while selecting rows in the account
        # table) would invoke the leftmost toolbar button.
        self.root.after(50, self._reset_initial_focus)
        self.root.after(100, self.drain)
        # Bind globally so any Entry losing focus hands control back to the table
        self.root.bind_class("TEntry", "<FocusOut>", self._entry_lost_focus)
        self.root.bind_class("Entry", "<FocusOut>", self._entry_lost_focus)

    def _reset_initial_focus(self):
        """Send focus to the add-link account table so Space acts on rows, not buttons."""
        try:
            # Walk every focusable widget and tell Tk to skip them with Tab/shortcuts.
            # Then explicitly move keyboard focus into the account table.
            self.root.focus_set()
            self.root.focus_force()
            tree = getattr(self, "add_link_account_tree", None)
            if tree is not None and tree.winfo_exists():
                tree.focus_set()
                tree.focus_force()
                # Ensure at least one row is highlighted so Space has an effect
                if not tree.selection():
                    children = tree.get_children()
                    if children:
                        tree.selection_set(children[0])
                        tree.focus(children[0])
        except Exception:
            pass

    def _entry_lost_focus(self, event=None):
        """Redirect keyboard focus to the add-link table whenever an Entry loses focus.

        Without this handler, Tk falls back to the first focusable widget (a button)
        and Space then triggers that button instead of acting on the table.
        """
        widget = event.widget if event is not None else None
        if widget is not None and not str(widget.cget("takefocus")).strip():
            return
        try:
            tree = getattr(self, "add_link_account_tree", None)
            if tree is None or not tree.winfo_exists():
                self.root.focus_set()
                return
            tree.focus_set()
            if not tree.selection() and tree.get_children():
                first = tree.get_children()[0]
                tree.selection_set(first)
                tree.focus(first)
        except Exception:
            pass

    def setup_style(self):
        self.root.configure(bg="#f3f4f7")
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Checkbox.Treeview", rowheight=29, font=("Tahoma", 11))
        style.configure("Checkbox.Treeview.Heading", font=("Tahoma", 10, "bold"))

    def build_ui(self):
        top = tk.Frame(self.root, bg="#00a884", height=42)
        top.pack(fill="x")
        top.pack_propagate(False)
        tk.Label(top, text=APP_TITLE, bg="#00a884", fg="#ffffff", font=("Tahoma", 12, "bold")).pack(side="left", padx=12)
        tk.Button(
            top,
            text="Settings",
            command=self.open_settings_window,
            bg="#ffffff",
            fg="#1f2937",
            relief="flat",
            padx=12,
            pady=4,
            takefocus=0,
        ).pack(side="right", padx=10, pady=6)

        body = tk.Frame(self.root, bg="#f3f4f7")
        body.pack(fill="x", padx=8, pady=8)

        notebook = ttk.Notebook(body)
        notebook.pack(fill="x", expand=True)
        reg_tab = tk.Frame(notebook, bg="#f3f4f7")
        add_link_tab = tk.Frame(notebook, bg="#f3f4f7")
        notebook.add(reg_tab, text="Đăng ký CapCut")
        notebook.add(add_link_tab, text="Quản lý CapCut")
        bar = tk.Frame(reg_tab, bg="#ffffff", relief="solid", borderwidth=1)
        bar.pack(fill="x", pady=(0, 8))
        self.tool_button(bar, "Import Mail", self.import_accounts, "#00a884", "#ffffff").pack(side="left", padx=8, pady=6)
        self.tool_button(bar, "Import Proxy", self.import_proxies, "#0891b2", "#ffffff").pack(side="left", padx=4, pady=6)
        tk.Label(bar, text="Pass CapCut", bg="#ffffff").pack(side="left", padx=(12, 4), pady=6)
        self.passnew_entry = tk.Entry(bar, width=24, relief="solid", borderwidth=1, takefocus=0)
        self.passnew_entry.insert(0, "1234567")
        self.passnew_entry.pack(side="left", pady=6)
        self.passnew_entry.bind("<FocusOut>", self._entry_lost_focus)
        tk.Label(bar, text="Threads", bg="#ffffff").pack(side="left", padx=(12, 4), pady=6)
        self.threads_entry = tk.Entry(bar, width=6, relief="solid", borderwidth=1, takefocus=0)
        self.threads_entry.insert(0, "3")
        self.threads_entry.pack(side="left", pady=6)
        self.threads_entry.bind("<FocusOut>", self._entry_lost_focus)
        tk.Label(bar, text="Delay", bg="#ffffff").pack(side="left", padx=(12, 4), pady=6)
        self.delay_entry = tk.Entry(bar, width=6, relief="solid", borderwidth=1, takefocus=0)
        self.delay_entry.insert(0, "1")
        self.delay_entry.pack(side="left", pady=6)
        self.delay_entry.bind("<FocusOut>", self._entry_lost_focus)
        self.tool_button(bar, "Reg thường", self.start, "#059669", "#ffffff").pack(side="left", padx=4, pady=6)
        self.tool_button(bar, "Stop", self.stop, "#dc2626", "#ffffff").pack(side="left", padx=4, pady=6)

        tk.Label(
            reg_tab,
            text="Import mail format: email|passmail|refresh_token|client_id",
            bg="#f3f4f7",
            fg="#475569",
        ).pack(anchor="w", padx=4, pady=(0, 6))

        reg_header = tk.Frame(reg_tab, bg="#f3f4f7")
        reg_header.pack(fill="x", padx=4, pady=(0, 4))
        tk.Label(reg_header, text="Bảng tài khoản đăng ký CapCut", bg="#f3f4f7", font=("Tahoma", 10, "bold")).pack(side="left")
        self.tool_button(reg_header, "Export Acc", self.export_registered_accounts, "#2563eb", "#ffffff").pack(side="right")
        self.tool_button(
            reg_header,
            "Xuất email|username",
            self.export_registered_usernames,
            "#1d4ed8",
            "#ffffff",
        ).pack(side="right", padx=(0, 6))
        self.tool_button(
            reg_header,
            "Xóa tích chọn",
            self.delete_checked_reg_accounts,
            "#dc2626",
            "#ffffff",
        ).pack(side="right", padx=(0, 6))
        reg_wrap = tk.Frame(reg_tab, bg="#ffffff", relief="solid", borderwidth=1)
        reg_wrap.pack(fill="both", expand=True, padx=4, pady=(0, 8))
        self.reg_account_tree = ttk.Treeview(reg_wrap, columns=("checked", "user", "pass", "userID", "status"), show="headings", selectmode="extended", style="Checkbox.Treeview", height=9)
        for col, title, width in (("checked", "Chọn", 55), ("user", "User", 260), ("pass", "Pass", 180), ("userID", "Username", 180), ("status", "Status", 180)):
            self.reg_account_tree.heading(col, text=title, command=lambda c=col: self._sort_tree_column(self.reg_account_tree, c))
            self.reg_account_tree.column(col, width=width, anchor="w")
        for col, title, width in (("checked", "☑", 55), ("user", "User", 260), ("pass", "Pass", 180), ("userID", "UserID", 180), ("status", "Status", 180)):
            self.reg_account_tree.heading(col, text=title)
            self.reg_account_tree.column(col, width=width, anchor="w")
        self.reg_account_tree.pack(fill="both", expand=True, padx=4, pady=4)
        self.reg_account_tree.bind("<Control-a>", self._select_all_reg_accounts)
        self.reg_account_tree.bind("<Control-A>", self._select_all_reg_accounts)
        self.reg_account_tree.bind("<space>", self._toggle_reg_accounts)
        self.reg_account_tree.bind("<ButtonPress-1>", self._reg_account_press)
        self.reg_account_tree.bind("<B1-Motion>", self._reg_account_drag)

        self.status_label = tk.Label(reg_tab, text=status_text(self.accounts, len(self.proxies)), bg="#f3f4f7", fg="#111827", font=("Tahoma", 10, "bold"))
        self.status_label.pack(anchor="w", padx=4, pady=(4, 8))

        self.build_add_link_tab(add_link_tab)

        self.log = tk.Text(self.root, height=18, bg="#f8fafc", fg="#111827", relief="solid", borderwidth=1)
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def build_add_link_tab(self, parent):
        bar = tk.Frame(parent, bg="#ffffff", relief="solid", borderwidth=1)
        bar.pack(fill="x", pady=(8, 8))
        self.tool_button(bar, "Nhập tài khoản", self.import_add_link_accounts, "#00a884", "#ffffff").pack(side="left", padx=8, pady=6)
        self.tool_button(bar, "Nhập link", self.import_add_link_links, "#0891b2", "#ffffff").pack(side="left", padx=4, pady=6)
        self.tool_button(bar, "Danh sách link", self.show_add_link_statistics, "#2563eb", "#ffffff").pack(side="left", padx=4, pady=6)
        self.tool_button(bar, "Nhập proxy", self.import_proxies, "#64748b", "#ffffff").pack(side="left", padx=4, pady=6)
        tk.Label(bar, text="Số luồng", bg="#ffffff").pack(side="left", padx=(12, 4), pady=6)
        self.add_link_threads_entry = tk.Entry(bar, width=6, relief="solid", borderwidth=1, takefocus=0)
        self.add_link_threads_entry.insert(0, "3")
        self.add_link_threads_entry.pack(side="left", pady=6)
        # When the user clicks out (Tab, click outside, etc.) drop focus on the
        # add-link table so Space can never accidentally activate a hidden focus.
        self.add_link_threads_entry.bind("<FocusOut>", self._entry_lost_focus)
        tk.Label(bar, text="Delay", bg="#ffffff").pack(side="left", padx=(12, 4), pady=6)
        self.add_link_delay_entry = tk.Entry(bar, width=6, relief="solid", borderwidth=1, takefocus=0)
        self.add_link_delay_entry.insert(0, "1")
        self.add_link_delay_entry.pack(side="left", pady=6)
        self.add_link_delay_entry.bind("<FocusOut>", self._entry_lost_focus)
        self.tool_button(bar, "Bắt đầu", self.start_add_link, "#059669", "#ffffff").pack(side="left", padx=4, pady=6)
        self.tool_button(bar, "Dừng", self.stop, "#dc2626", "#ffffff").pack(side="left", padx=4, pady=6)
        check_buttons = tk.Frame(bar, bg="#ffffff")
        check_buttons.pack(side="right", padx=(4, 8), pady=6)
        self.tool_button(check_buttons, "Kiểm tra CapCut Pro", self.start_check_pro, "#7c3aed", "#ffffff").pack(fill="x")
        space_buttons = tk.Frame(check_buttons, bg="#ffffff")
        space_buttons.pack(fill="x", pady=(4, 0))
        self.tool_button(space_buttons, "Check Space", self.start_check_space, "#0369a1", "#ffffff").pack(side="left")
        self.tool_button(space_buttons, "Check Gốc Fam", self.start_check_fam_origin, "#6d28d9", "#ffffff").pack(side="left", padx=(4, 0))
        self.tool_button(space_buttons, "Out Fam", self.start_out_fam, "#b45309", "#ffffff").pack(side="left", padx=(4, 0))
        login_buttons = tk.Frame(bar, bg="#ffffff")
        login_buttons.pack(side="right", padx=(4, 0), pady=6)
        self.tool_button(login_buttons, "Đăng nhập & lấy user", self.start_check_user, "#0f766e", "#ffffff").pack(fill="x")
        self.tool_button(login_buttons, "Đăng nhập treo", self.start_hold_mode, "#7c3aed", "#ffffff").pack(fill="x", pady=(4, 0))

        tk.Label(
            parent,
            text="Tài khoản: email|mật_khẩu_capcut  •  Link: mỗi dòng một link  •  Tối đa: 6 tài khoản cho mỗi link",
            bg="#f3f4f7",
            fg="#475569",
        ).pack(anchor="w", padx=4, pady=(0, 6))
        self.add_link_status_label = tk.Label(
            parent,
            text=(
                "Tài khoản: 0 (đã chọn: 0) | CapCut Pro: Có 0 / Không 0\n"
                "Link: 0 | Link đã chọn: 0 | Đã thêm: 0 | Link còn lại: 0 | Lượt còn lại: 0"
            ),
            bg="#f3f4f7",
            fg="#111827",
            font=("Tahoma", 10, "bold"),
            justify="left",
        )
        self.add_link_status_label.pack(anchor="w", padx=4, pady=(4, 8))

        account_header = tk.Frame(parent, bg="#f3f4f7")
        account_header.pack(fill="x", padx=4, pady=(0, 4))
        tk.Label(
            account_header,
            text="Bảng acc thêm link",
            bg="#f3f4f7",
            fg="#111827",
            font=("Tahoma", 10, "bold"),
        ).pack(side="left")
        self.tool_button(
            account_header,
            "Xuất tài khoản đã chọn",
            self.export_selected_add_link_accounts,
            "#7c3aed",
            "#ffffff",
        ).pack(side="right", padx=(6, 0))
        self.tool_button(account_header, "Xuất email|username", self.export_checked_usernames, "#1d4ed8", "#ffffff").pack(side="right", padx=(6, 0))
        self.tool_button(
            account_header,
            "Xuất email|user gốc|số member",
            self.export_checked_fam_members,
            "#6d28d9",
            "#ffffff",
        ).pack(side="right", padx=(6, 0))
        self.tool_button(
            account_header,
            "Xuất email|user|owner",
            self.export_out_fam_accounts,
            "#b45309",
            "#ffffff",
        ).pack(side="right", padx=(6, 0))
        self.tool_button(
            account_header,
            "Xóa tài khoản đã tích chọn",
            self.delete_checked_add_link_accounts,
            "#b91c1c",
            "#ffffff",
        ).pack(side="right", padx=(6, 0))
        self.tool_button(
            account_header,
            "Bỏ chọn",
            lambda: self._set_all_add_link_accounts_selected(False),
            "#dc2626",
            "#ffffff",
        ).pack(side="right", padx=(6, 0))
        self.tool_button(
            account_header,
            "Chọn tất cả",
            lambda: self._set_all_add_link_accounts_selected(True),
            "#059669",
            "#ffffff",
        ).pack(side="right")

        account_table = tk.Frame(parent, bg="#ffffff", relief="solid", borderwidth=1)
        account_table.pack(fill="both", expand=True, padx=4, pady=(0, 8))
        tree = ttk.Treeview(
            account_table,
            columns=("checked", "account", "username", "status", "pro_status"),
            show="headings",
            selectmode="extended",
            style="Checkbox.Treeview",
            height=9,
        )
        tree.heading("checked", text="Chọn")
        tree.heading("account", text="Tài khoản")
        tree.heading("username", text="Username")
        tree.heading("status", text="Trạng thái thêm link")
        tree.heading("pro_status", text="Trạng thái CapCut")
        tree.column("checked", width=65, minwidth=60, anchor="center", stretch=False)
        tree.column("account", width=300, minwidth=210, anchor="w")
        tree.column("username", width=180, minwidth=130, anchor="w")
        tree.column("status", width=210, minwidth=160, anchor="w")
        tree.column("pro_status", width=360, minwidth=220, anchor="w")
        scrollbar = ttk.Scrollbar(account_table, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.add_link_account_tree = tree
        for col in tree["columns"]:
            tree.heading(col, command=lambda c=col, t=tree: self._sort_tree_column(t, c))
        tree.bind("<ButtonPress-1>", self._add_link_account_table_press)
        tree.bind("<B1-Motion>", self._add_link_account_table_drag)
        self._bind_space_to_tree(tree)
        tree.bind("<Control-a>", self._select_all_table_rows)
        tree.bind("<Control-A>", self._select_all_table_rows)
        tree.focus_set()
        tree.focus_force()

    @staticmethod
    def _select_all_table_rows(event):
        """Highlight every row in any Treeview; Space performs the checkbox action."""
        tree = event.widget
        rows = tree.get_children()
        if rows:
            tree.selection_set(rows)
            tree.focus(rows[0])
            tree.see(rows[0])
        return "break"

    def _bind_space_to_tree(self, tree):
        """Bind Space/Double-click so they only act when the tree itself has focus."""
        tree.bind("<Key-space>", self._toggle_highlighted_add_link_accounts)
        tree.bind("<Double-1>", self._toggle_highlighted_add_link_accounts)
        # Prevent default Tk behavior on Space when a button accidentally has focus
        for seq in ("<Key-space>", "<Key-Return>"):
            tree.bind(seq, self._toggle_highlighted_add_link_accounts, add="+")

    @staticmethod
    def _add_link_account_status_text(status):
        status = str(status or "").strip()
        if status == "true":
            return "Đã add link"
        if status == "running":
            return "Đang chạy"
        if status == "join unverified":
            return "Chưa xác minh tham gia - không tự Submit lại"
        if status == "login fail":
            return "Lỗi đăng nhập - chưa add"
        if status == "can't join space":
            return "Không thể join - chưa add"
        if status == "link full":
            return "Link đầy - chưa add"
        if status == "stopped":
            return "Đã dừng - chưa add"
        if status.startswith("fail:"):
            return f"Lỗi - chưa add: {status[5:].strip()}"
        return "Chưa add link"

    @staticmethod
    def _check_pro_status_text(status):
        status = str(status or "").strip()
        if status == "true":
            return "Có CapCut Pro"
        if status == "false":
            return "Không có CapCut Pro"
        if status == "running":
            return "Đang kiểm tra"
        if status == "stopped":
            return "Đã dừng kiểm tra"
        if status == "error":
            return "Lỗi kiểm tra"
        return "Chưa kiểm tra"

    @classmethod
    def _capcut_status_text(cls, account):
        space_status = str(account.get("space_status", "")).strip()
        return space_status or cls._check_pro_status_text(account.get("pro_status"))

    def _refresh_add_link_account_table(self, preserve_rows=None):
        tree = self.add_link_account_tree
        if tree is None or not tree.winfo_exists():
            return
        rows = tuple(preserve_rows or tree.selection())
        tree.delete(*tree.get_children())
        for index, account in enumerate(self.add_link_accounts):
            user = account["user"]
            tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    self.CHECKBOX_SELECTED if user.casefold() in self.add_link_account_selected else self.CHECKBOX_UNSELECTED,
                    user,
                    account.get("userID", ""),
                    self._add_link_account_status_text(account.get("status")),
                    self._capcut_status_text(account),
                ),
            )
        valid_rows = [row for row in rows if tree.exists(row)]
        if valid_rows:
            tree.selection_set(valid_rows)

    def _add_link_account_table_press(self, event):
        tree = self.add_link_account_tree
        if tree is not None:
            tree.focus_set()
        row = tree.identify_row(event.y) if tree is not None else ""
        if not row:
            return None if hasattr(tree, "identify_region") and tree.identify_region(getattr(event, "x", 0), event.y) == "heading" else "break"
        self.add_link_account_drag_anchor = row
        if tree.identify_column(event.x) == "#1":
            tree.selection_set(row)
            tree.focus(row)
            self._toggle_highlighted_add_link_accounts()
            return "break"
        if event.state & 0x0004:
            tree.selection_toggle(row)
        else:
            tree.selection_set(row)
        tree.focus(row)
        return "break"

    def _add_link_account_table_drag(self, event):
        tree = self.add_link_account_tree
        if tree is None or not self.add_link_account_drag_anchor:
            return "break"
        row = tree.identify_row(event.y)
        children = list(tree.get_children())
        if not row or row not in children or self.add_link_account_drag_anchor not in children:
            return "break"
        start = children.index(self.add_link_account_drag_anchor)
        end = children.index(row)
        tree.selection_set(children[min(start, end):max(start, end) + 1])
        tree.see(row)
        return "break"

    def _toggle_highlighted_add_link_accounts(self, event=None):
        tree = self.add_link_account_tree
        if tree is None:
            return "break"
        rows = tree.selection()
        users = [
            self.add_link_accounts[int(row)]["user"].casefold()
            for row in rows
            if row.isdigit() and int(row) < len(self.add_link_accounts)
        ]
        if not users:
            return "break"
        select = not all(user in self.add_link_account_selected for user in users)
        if select:
            self.add_link_account_selected.update(users)
        else:
            self.add_link_account_selected.difference_update(users)
        self._save_add_link_account_state()
        self._refresh_add_link_account_table(preserve_rows=rows)
        self.refresh_add_link_status()
        return "break"

    def _set_all_add_link_accounts_selected(self, selected):
        if selected:
            self.add_link_account_selected = {
                account["user"].casefold() for account in self.add_link_accounts
            }
        else:
            self.add_link_account_selected.clear()
        self._save_add_link_account_state()
        self._refresh_add_link_account_table()
        self.refresh_add_link_status()

    def _read_add_link_account_state(self):
        if not hasattr(self, "add_link_account_state_path"):
            return {}
        # Serialize reads with replacement of the history file on Windows.
        with self.add_link_result_lock:
            return self._read_add_link_account_state_unlocked()

    def _read_add_link_account_state_unlocked(self):
        if not hasattr(self, "add_link_account_state_path"):
            return {}
        try:
            data = json.loads(self.add_link_account_state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_add_link_account_state(self):
        self.add_link_account_state_path.parent.mkdir(exist_ok=True)
        with self.add_link_result_lock:
            # Retain history even when an account is removed from the table or
            # another file is imported. Snapshot under the write lock so an
            # older worker cannot overwrite a newer successful result.
            payload = self._read_add_link_account_state_unlocked()
            for account in self.add_link_accounts:
                key = account["user"].casefold()
                previous = payload.get(key, {})
                if isinstance(previous, dict):
                    if previous.get("status") == "true":
                        account["status"] = "true"
                    if _add_link_assignment(previous):
                        account["assigned_link"] = _add_link_assignment(previous)
                    if previous.get("join_pending") and account.get("status") != "true":
                        account["join_pending"] = True
                account["assigned_link"] = _add_link_assignment(account)
                payload[key] = {
                    "status": account.get("status", ""),
                    "pro_status": account.get("pro_status", ""),
                    "space_status": account.get("space_status", ""),
                    "userID": account.get("userID", ""),
                    "assigned_link": account.get("assigned_link", ""),
                    "join_pending": bool(account.get("join_pending")),
                    "selected": account["user"].casefold() in self.add_link_account_selected,
                }
            temp_path = self.add_link_account_state_path.with_suffix(".tmp")
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp_path.replace(self.add_link_account_state_path)
        if hasattr(self, "add_link_workspace_path"):
            self._save_add_link_workspace()

    def _save_add_link_workspace(self):
        """Persist only data explicitly imported or changed in the Add Link UI."""
        self.add_link_workspace_path.parent.mkdir(exist_ok=True)
        with self.add_link_result_lock:
            payload = {
                "accounts": [
                    {
                        "user": account["user"],
                        "passnew": account["passnew"],
                        "status": account.get("status", ""),
                        "pro_status": account.get("pro_status", ""),
                        "space_status": account.get("space_status", ""),
                        "userID": account.get("userID", ""),
                        "assigned_link": account.get("assigned_link", ""),
                        "join_pending": bool(account.get("join_pending")),
                        "selected": account["user"].casefold() in self.add_link_account_selected,
                    }
                    for account in self.add_link_accounts
                ],
                "links": [
                    {
                        "url": link,
                        "selected": link in self.add_link_selected,
                        "count": min(6, max(0, int(self.add_link_counts.get(link, 0)))),
                        "full": link in self.add_link_full,
                        "errors": list(self.add_link_errors.get(link, [])),
                    }
                    for link in self.add_link_links
                ],
            }
            temp_path = self.add_link_workspace_path.with_suffix(".tmp")
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp_path.replace(self.add_link_workspace_path)


    def restore_add_link_workspace(self):
        """Restore the last user-managed table without reading account.txt/link.txt."""
        if not self.add_link_workspace_path.exists():
            return
        try:
            data = json.loads(self.add_link_workspace_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("workspace root must be an object")
        except (OSError, ValueError, TypeError) as exc:
            print(f"[CAPCUT][ADD LINK] Could not restore saved workspace: {exc}")
            return

        accounts = []
        saved_state = self._read_add_link_account_state()
        selected_accounts = set()
        seen_accounts = set()
        for item in data.get("accounts", []):
            if not isinstance(item, dict):
                continue
            user = str(item.get("user", "")).strip()
            password = str(item.get("passnew", "")).strip()
            key = user.casefold()
            if not user or not password or key in seen_accounts:
                continue
            seen_accounts.add(key)
            status = str(item.get("status", ""))
            history = saved_state.get(key, {})
            if not isinstance(history, dict):
                history = {}
            if history.get("status") == "true":
                status = "true"
            join_pending = status != "true" and bool(history.get("join_pending") or item.get("join_pending"))
            if join_pending:
                status = "join unverified"
            pro_status = str(item.get("pro_status", ""))
            space_status = str(history.get("space_status") or item.get("space_status", ""))
            if space_status in {"Chờ kiểm tra", "Đang kiểm tra Space"}:
                space_status = "Đã dừng kiểm tra Space"
            elif space_status in {"Chờ Out Fam", "Đang Out Fam"}:
                space_status = "Đã dừng Out Fam"
            elif space_status in {"Chờ Check Gốc Fam", "Đang Check Gốc Fam"}:
                space_status = "Đã dừng Check Gốc Fam"
            user_id = str(item.get("userID", "")).strip()
            accounts.append(
                {
                    "picked": True,
                    "user": user,
                    "email": user,
                    "passnew": password,
                    "status": "stopped" if status == "running" else status,
                    "pro_status": "stopped" if pro_status == "running" else pro_status,
                    "space_status": space_status,
                    "userID": user_id,
                    "assigned_link": str(history.get("assigned_link") or item.get("assigned_link", "") or "").strip(),
                    "join_pending": join_pending,
                }
            )
            if item.get("selected", True):
                selected_accounts.add(key)

        links = []
        selected_links = set()
        counts = {}
        errors = {}
        full_links = set()
        seen_links = set()
        for item in data.get("links", []):
            if not isinstance(item, dict):
                continue
            link = str(item.get("url", "")).strip()
            if not link or link in seen_links:
                continue
            seen_links.add(link)
            links.append(link)
            try:
                count = min(6, max(0, int(item.get("count", 0))))
            except (TypeError, ValueError):
                count = 0
            counts[link] = count
            raw_errors = item.get("errors", [])
            errors[link] = [str(error) for error in raw_errors] if isinstance(raw_errors, list) else []
            if item.get("selected", True):
                selected_links.add(link)
            if item.get("full", False) or count >= 6:
                full_links.add(link)

        self.add_link_accounts = accounts
        self.check_pro_accounts = self.add_link_accounts
        self.add_link_account_selected = selected_accounts
        self.add_link_links = links
        self.add_link_selected = selected_links
        self.add_link_counts = counts
        self.add_link_errors = errors
        self.add_link_full = full_links
        self.refresh_add_link_status()
        print(
            f"[CAPCUT][ADD LINK] Restored user workspace: "
            f"{len(accounts)} accounts | {len(links)} links"
        )

    def export_selected_add_link_accounts(self):
        accounts = [
            account
            for account in self.add_link_accounts
            if account["user"].casefold() in self.add_link_account_selected
        ]
        if not accounts:
            messagebox.showinfo("Thông báo", "Chưa chọn tài khoản để xuất")
            return
        self.add_link_account_export_path.parent.mkdir(exist_ok=True)
        content = "\n".join(f'{account["user"]}|{account["passnew"]}' for account in accounts)
        self.add_link_account_export_path.write_text(content + "\n", encoding="utf-8")
        messagebox.showinfo(
            "Xuất tài khoản",
            f"Đã xuất {len(accounts)} tài khoản vào {self.add_link_account_export_path}",
        )
        print(f"[CAPCUT][ADD LINK] Exported {len(accounts)} selected accounts to {self.add_link_account_export_path}")

    def delete_checked_add_link_accounts(self):
        if self.task_thread and self.task_thread.is_alive():
            messagebox.showinfo("Thông báo", "Không thể xóa tài khoản khi tác vụ đang chạy")
            return
        checked_users = set(self.add_link_account_selected)
        indexes = [
            index
            for index, account in enumerate(self.add_link_accounts)
            if account["user"].casefold() in checked_users
        ]
        if not indexes:
            messagebox.showinfo("Thông báo", "Hãy tích chọn tài khoản cần xóa")
            return
        if not messagebox.askyesno(
            "Xóa tài khoản",
            f"Xóa {len(indexes)} tài khoản đã tích chọn khỏi bảng?",
        ):
            return

        removed_users = {
            self.add_link_accounts[index]["user"].casefold() for index in indexes
        }
        remove_indexes = set(indexes)
        self.add_link_accounts = [
            account
            for index, account in enumerate(self.add_link_accounts)
            if index not in remove_indexes
        ]
        self.add_link_account_selected.difference_update(removed_users)
        self.check_pro_accounts = self.add_link_accounts
        self._save_add_link_account_state()
        self.refresh_add_link_status()

    def export_checked_usernames(self):
        selected = [a for a in self.add_link_accounts if a["user"].casefold() in self.add_link_account_selected and a.get("userID")]
        if not selected:
            messagebox.showinfo("Thông báo", "Chưa có username của tài khoản đã tích chọn")
            return
        path = getattr(
            self,
            "add_link_username_export_path",
            Path("check user") / "login_user.txt",
        )
        path.parent.mkdir(exist_ok=True)
        path.write_text("".join(f'{a["user"]}|{a["userID"]}\n' for a in selected), encoding="utf-8")
        print(f"[CAPCUT] Exported {len(selected)} email|username accounts to {path}")

    def show_add_link_statistics(self):
        if self.add_link_stats_window is not None and self.add_link_stats_window.winfo_exists():
            self.add_link_stats_window.lift()
            self.add_link_stats_window.focus_force()
            return

        window = tk.Toplevel(self.root)
        window.title("Thêm link - Danh sách link")
        window.geometry("1000x560")
        window.minsize(760, 380)
        window.configure(bg="#f3f4f7")
        self.add_link_stats_window = window

        help_label = tk.Label(
            window,
            text="Kéo chuột để bôi đen nhiều dòng. Nhấn phím Space để tích hoặc bỏ tích các link đang bôi đen.",
            bg="#f3f4f7",
            fg="#475569",
            anchor="w",
        )
        help_label.pack(fill="x", padx=10, pady=(10, 4))
        self.add_link_stats_summary = tk.Label(
            window, bg="#f3f4f7", fg="#111827", font=("Tahoma", 10, "bold"), anchor="w"
        )
        self.add_link_stats_summary.pack(fill="x", padx=10, pady=(0, 8))

        table_frame = tk.Frame(window, bg="#ffffff", relief="solid", borderwidth=1)
        table_frame.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        columns = ("checked", "link", "joined", "remaining", "status")
        tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="extended",
            style="Checkbox.Treeview",
        )
        tree.heading("checked", text="Chọn")
        tree.heading("link", text="Link")
        tree.heading("joined", text="Đã thêm")
        tree.heading("remaining", text="Còn lại")
        tree.heading("status", text="Trạng thái")
        tree.column("checked", width=60, minwidth=55, anchor="center", stretch=False)
        tree.column("link", width=610, minwidth=280, anchor="w")
        tree.column("joined", width=80, minwidth=70, anchor="center", stretch=False)
        tree.column("remaining", width=90, minwidth=80, anchor="center", stretch=False)
        tree.column("status", width=110, minwidth=90, anchor="center", stretch=False)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.add_link_stats_tree = tree
        for col in tree["columns"]:
            tree.heading(col, command=lambda c=col, t=tree: self._sort_tree_column(t, c))

        tree.bind("<ButtonPress-1>", self._add_link_table_press)
        tree.bind("<B1-Motion>", self._add_link_table_drag)
        tree.bind("<Key-space>", self._toggle_highlighted_links)
        tree.bind("<Double-1>", self._toggle_highlighted_links)
        tree.bind("<Control-a>", self._select_all_table_rows)
        tree.bind("<Control-A>", self._select_all_table_rows)
        tree.bind("<Key-space>", lambda e: "break", add="+")

        buttons = tk.Frame(window, bg="#f3f4f7")
        buttons.pack(fill="x", padx=10, pady=(0, 10))
        self.tool_button(buttons, "Chọn tất cả", lambda: self._set_all_links_selected(True), "#059669", "#ffffff").pack(side="left", padx=(0, 6))
        self.tool_button(buttons, "Bỏ chọn tất cả", lambda: self._set_all_links_selected(False), "#dc2626", "#ffffff").pack(side="left")
        self.tool_button(buttons, "Xóa link đã tích chọn", self.delete_checked_links, "#b91c1c", "#ffffff").pack(side="left", padx=(6, 0))
        self.tool_button(buttons, "Đóng", window.destroy, "#64748b", "#ffffff").pack(side="right")

        self._refresh_add_link_table()
        self._schedule_add_link_table_refresh()
        tree.focus_set()

    def delete_checked_links(self):
        """Remove selected links from the persistent add-link workspace."""
        selected = set(getattr(self, "add_link_selected", set()))
        if not selected:
            messagebox.showinfo("Thông báo", "Hãy tích chọn ít nhất một link để xóa")
            return
        self.add_link_links = [link for link in self.add_link_links if link not in selected]
        self.add_link_selected.difference_update(selected)
        self.add_link_full.difference_update(selected)
        for mapping in (self.add_link_counts, self.add_link_errors):
            for link in selected:
                mapping.pop(link, None)
        self._save_add_link_workspace()
        self._refresh_add_link_table()
        self.refresh_add_link_status()

    def _add_link_table_press(self, event):
        tree = self.add_link_stats_tree
        if tree is not None:
            tree.focus_set()
        row = tree.identify_row(event.y) if tree is not None else ""
        if not row:
            return None if hasattr(tree, "identify_region") and tree.identify_region(getattr(event, "x", 0), event.y) == "heading" else "break"
        self.add_link_drag_anchor = row
        if tree.identify_column(event.x) == "#1":
            tree.selection_set(row)
            tree.focus(row)
            self._toggle_highlighted_links()
            return "break"
        if event.state & 0x0004:
            tree.selection_toggle(row)
        else:
            tree.selection_set(row)
        tree.focus(row)
        return "break"

    def _add_link_table_drag(self, event):
        tree = self.add_link_stats_tree
        if tree is None or not self.add_link_drag_anchor:
            return "break"
        row = tree.identify_row(event.y)
        children = list(tree.get_children())
        if not row or row not in children or self.add_link_drag_anchor not in children:
            return "break"
        start = children.index(self.add_link_drag_anchor)
        end = children.index(row)
        tree.selection_set(children[min(start, end):max(start, end) + 1])
        tree.see(row)
        return "break"

    def _toggle_highlighted_links(self, event=None):
        tree = self.add_link_stats_tree
        if tree is None:
            return "break"
        rows = tree.selection()
        links = [self.add_link_links[int(row)] for row in rows if row.isdigit() and int(row) < len(self.add_link_links)]
        if not links:
            return "break"
        select = not all(link in self.add_link_selected for link in links)
        if select:
            self.add_link_selected.update(links)
        else:
            self.add_link_selected.difference_update(links)
        self._save_add_link_workspace()
        self._refresh_add_link_table(preserve_rows=rows)
        self.refresh_add_link_status()
        return "break"

    def _set_all_links_selected(self, selected):
        if selected:
            self.add_link_selected = set(self.add_link_links)
        else:
            self.add_link_selected.clear()
        self._save_add_link_workspace()
        self._refresh_add_link_table()
        self.refresh_add_link_status()

    def _refresh_add_link_table(self, preserve_rows=None):
        tree = self.add_link_stats_tree
        if tree is None or not tree.winfo_exists():
            return
        rows = tuple(preserve_rows or tree.selection())
        with self.add_link_counts_lock:
            counts = dict(self.add_link_counts)
            full_links = set(self.add_link_full)
        tree.delete(*tree.get_children())
        for index, link in enumerate(self.add_link_links):
            joined = min(6, counts.get(link, 0))
            remaining = max(0, 6 - joined)
            status = "Đã đầy" if link in full_links or remaining == 0 else ("Đã dùng một phần" if joined else "Chưa sử dụng")
            tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    self.CHECKBOX_SELECTED if link in self.add_link_selected else self.CHECKBOX_UNSELECTED,
                    link,
                    joined,
                    remaining,
                    status,
                ),
            )
        valid_rows = [row for row in rows if tree.exists(row)]
        if valid_rows:
            tree.selection_set(valid_rows)
        selected_count = sum(1 for link in self.add_link_links if link in self.add_link_selected)
        remaining_links = sum(1 for link in self.add_link_links if counts.get(link, 0) < 6)
        remaining_slots = sum(max(0, 6 - min(6, counts.get(link, 0))) for link in self.add_link_links)
        self.add_link_stats_summary.configure(
            text=f"Tổng số link: {len(self.add_link_links)} | Đã chọn: {selected_count} | Link còn dùng được: {remaining_links} | Tổng lượt còn lại: {remaining_slots}"
        )

    def _schedule_add_link_table_refresh(self):
        window = self.add_link_stats_window
        if window is None or not window.winfo_exists():
            self.add_link_stats_window = None
            self.add_link_stats_tree = None
            return
        self._refresh_add_link_table()
        window.after(500, self._schedule_add_link_table_refresh)

    def build_check_pro_tab(self, parent):
        pass  # Removed - Check Pro is now a button in Add Link tab

    def tool_button(self, parent, text, command, color="#ffffff", fg="#111827"):
        return tk.Button(parent, text=text, command=command, bg=color, fg=fg, relief="solid", borderwidth=1, padx=10, pady=4, takefocus=0)

    def open_settings_window(self):
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            self.settings_window.focus_force()
            return

        self.settings_window = tk.Toplevel(self.root)
        self.settings_window.title("Reg CapCut Settings")
        self.settings_window.geometry("620x280")
        self.settings_window.configure(bg="#f3f4f7")
        self.settings_window.transient(self.root)
        self.settings_window.grab_set()
        self.settings_window.protocol("WM_DELETE_WINDOW", self.close_settings_window)

        body = tk.Frame(self.settings_window, bg="#ffffff", relief="solid", borderwidth=1)
        body.pack(fill="both", expand=True, padx=12, pady=12)
        tk.Label(body, text="Chromium 154 ẩn danh", bg="#ffffff", fg="#111827", font=("Tahoma", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 8))

        fields = (("expressvpn_path", "ExpressVPN"), ("chromium_path", "Chromium 154"))
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
            "expressvpn_path": self.app_settings.expressvpn_path,
            "chromium_path": self.app_settings.chromium_path,
        }
        for key, entry in self.settings_entries.items():
            entry.delete(0, "end")
            entry.insert(0, values.get(key, ""))

    def save_settings_from_ui(self):
        expressvpn_path = self.settings_entries["expressvpn_path"].get().strip()
        chromium_path = self.settings_entries["chromium_path"].get().strip()
        self.app_settings.expressvpn_path = expressvpn_path or r"C:\Program Files\ExpressVPN"
        self.app_settings.chromium_path = chromium_path
        try:
            save_settings(self.app_settings)
            self.app_settings = load_settings()
            print("Đã lưu cài đặt Chromium 154")
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
        self.render_reg_accounts()
        self.refresh_status()
        print(f"Loaded {len(self.accounts)} Reg CapCut accounts from {self.accounts_path.name}")
        if skipped:
            print(f"Skipped {skipped} invalid mail API lines. Expected: email|passmail|refresh_token|client_id")

    def import_add_link_accounts(self):
        path = filedialog.askopenfilename(
            title="Chọn file tài khoản CapCut",
            initialdir=str(Path.cwd()),
            initialfile="account.txt",
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
        )
        if not path:
            return
        self.add_link_accounts_path = Path(path)
        self.load_add_link_accounts()

    def load_add_link_accounts(self):
        task = getattr(self, "task_thread", None)
        if task and task.is_alive():
            messagebox.showinfo("Thông báo", "Dừng tác vụ CapCut trước khi nhập lại tài khoản")
            return
        loaded = []
        seen = set()
        skipped = 0
        saved_state = self._read_add_link_account_state()
        selected = set()
        for line in self.add_link_accounts_path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            account = parse_capcut_login_line(line)
            if account is None:
                skipped += 1
                continue
            key = account["user"].casefold()
            if key in seen:
                continue
            seen.add(key)
            state = saved_state.get(key, {})
            saved_status = state.get("status", "") if isinstance(state, dict) else ""
            saved_pro_status = state.get("pro_status", "") if isinstance(state, dict) else ""
            saved_space_status = state.get("space_status", "") if isinstance(state, dict) else ""
            saved_user_id = state.get("userID", "") if isinstance(state, dict) else ""
            # If the application was closed while a profile was running, keep a
            # truthful persisted result instead of showing it as still running.
            account["status"] = "stopped" if saved_status == "running" else saved_status
            account["pro_status"] = "stopped" if saved_pro_status == "running" else saved_pro_status
            if saved_space_status in {"Chờ kiểm tra", "Đang kiểm tra Space"}:
                account["space_status"] = "Đã dừng kiểm tra Space"
            elif saved_space_status in {"Chờ Out Fam", "Đang Out Fam"}:
                account["space_status"] = "Đã dừng Out Fam"
            elif saved_space_status in {"Chờ Check Gốc Fam", "Đang Check Gốc Fam"}:
                account["space_status"] = "Đã dừng Check Gốc Fam"
            else:
                account["space_status"] = saved_space_status
            account["userID"] = str(saved_user_id or "").strip()
            account["assigned_link"] = str(state.get("assigned_link", "") or "").strip() if isinstance(state, dict) else ""
            account["join_pending"] = bool(state.get("join_pending")) if isinstance(state, dict) else False
            if account["join_pending"] and account["status"] != "true":
                account["status"] = "join unverified"
            if not isinstance(state, dict) or state.get("selected", True):
                selected.add(key)
            loaded.append(account)
        self.add_link_accounts = loaded
        self.check_pro_accounts = self.add_link_accounts
        self.add_link_account_selected = selected
        self._save_add_link_account_state()
        self.refresh_add_link_status()
        print(f"[CAPCUT][ADD LINK] Loaded {len(loaded)} accounts from {self.add_link_accounts_path.name}")
        if skipped:
            print(f"[CAPCUT][ADD LINK] Skipped {skipped} invalid account lines. Expected: email|passcapcut")

    def import_add_link_links(self):
        path = filedialog.askopenfilename(
            title="Chọn file link CapCut",
            initialdir=str(Path.cwd()),
            initialfile="link.txt",
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
        )
        if not path:
            return
        self.add_link_links_path = Path(path)
        self.load_add_link_links()

    def load_add_link_links(self):
        task = getattr(self, "task_thread", None)
        if task and task.is_alive():
            messagebox.showinfo("Thông báo", "Dừng tác vụ CapCut trước khi nhập lại link")
            return
        loaded = []
        imported_counts = {}
        persisted_counts = {}
        seen = set()
        skipped = 0

        # The result file is a second durable source of truth. It lets a plain
        # link.txt recover usage after workspace.json was removed or its table
        # was cleared in an older application version.
        export_path = getattr(self, "add_link_export_path", None)
        if export_path is not None and export_path.exists():
            try:
                for saved_line in export_path.read_text(encoding="utf-8-sig").splitlines():
                    parsed_saved = parse_add_link_line(saved_line)
                    if parsed_saved is None:
                        continue
                    saved_link, saved_count = parsed_saved
                    persisted_counts[saved_link] = max(
                        persisted_counts.get(saved_link, 0), saved_count
                    )
            except OSError as exc:
                print(f"[CAPCUT][ADD LINK] Could not read saved link counts: {exc}")

        for raw_line in self.add_link_links_path.read_text(encoding="utf-8-sig").splitlines():
            parsed = parse_add_link_line(raw_line)
            if parsed is None:
                if raw_line.strip():
                    skipped += 1
                continue
            link, imported_count = parsed
            if link in seen:
                imported_counts[link] = max(imported_counts[link], imported_count)
                continue
            seen.add(link)
            loaded.append(link)
            imported_counts[link] = imported_count
        self.add_link_links = loaded
        # A newly imported file starts with every valid link enabled. Users can
        # disable individual links from the link-list table before Start.
        self.add_link_selected = set(loaded)
        with self.add_link_counts_lock:
            previous_counts = self.add_link_counts
            previous_errors = self.add_link_errors
            # Preserve progress already known by the workspace. A count explicitly
            # imported from add_link_used.txt can only move that progress forward.
            self.add_link_counts = {
                link: max(
                    previous_counts.get(link, 0),
                    imported_counts.get(link, 0),
                    persisted_counts.get(link, 0),
                )
                for link in loaded
            }
            self.add_link_errors = {link: list(previous_errors.get(link, [])) for link in loaded}
            self.add_link_full.intersection_update(loaded)
            self.add_link_full.update(
                link for link, count in self.add_link_counts.items() if count >= 6
            )
        self._save_add_link_workspace()
        self.refresh_add_link_status()
        print(f"[CAPCUT][ADD LINK] Loaded {len(loaded)} links from {self.add_link_links_path.name}")
        if skipped:
            print(f"[CAPCUT][ADD LINK] Skipped {skipped} invalid links")

    def import_check_pro_accounts(self):
        path = filedialog.askopenfilename(
            title="Open CapCut Pro account file",
            initialdir=str(Path.cwd()),
            initialfile="account.txt",
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
        )
        if not path:
            return
        self.check_pro_accounts_path = Path(path)
        self.load_check_pro_accounts()

    def load_check_pro_accounts(self):
        # Check Pro and Add Link intentionally share one account collection.
        # Importing from either tab updates the same persisted account table.
        self.add_link_accounts_path = self.check_pro_accounts_path
        self.load_add_link_accounts()
        self.check_pro_accounts = self.add_link_accounts
        self.refresh_check_pro_status()
        print(
            f"[CAPCUT][CHECK PRO] Shared {len(self.check_pro_accounts)} accounts "
            f"with Bảng acc thêm link"
        )

    def refresh_check_pro_status(self):
        accounts = self.add_link_accounts
        pro_count = sum(1 for account in accounts if account.get("pro_status") == "true")
        failed_count = sum(
            1
            for account in accounts
            if account.get("pro_status") in {"false", "error"}
        )
        selected_count = sum(
            1 for account in accounts if account["user"].casefold() in self.add_link_account_selected
        )
        if hasattr(self, "check_pro_status_label"):
            self.check_pro_status_label.configure(
                text=(
                    f"Tài khoản dùng chung: {len(accounts)} | Đã chọn để kiểm tra: {selected_count} | "
                    f"Có Pro: {pro_count} | Không Pro/Lỗi: {failed_count}"
                )
            )

    def refresh_add_link_status(self):
        joined = sum(self.add_link_counts.values())
        capacity = len(self.add_link_links) * 6
        selected_links = getattr(self, "add_link_selected", set(self.add_link_links))
        selected_count = sum(1 for link in self.add_link_links if link in selected_links)
        selected_accounts = sum(
            1
            for account in self.add_link_accounts
            if account["user"].casefold() in self.add_link_account_selected
        )
        remaining_links = sum(
            1 for link in self.add_link_links if self.add_link_counts.get(link, 0) < 6
        )
        remaining_slots = max(0, capacity - joined)
        # CapCut Pro counts
        pro_count = sum(1 for account in self.add_link_accounts if account.get("pro_status") == "true")
        not_pro_count = sum(1 for account in self.add_link_accounts if account.get("pro_status") in {"false", "error"})
        self.add_link_status_label.configure(
            text=(
                f"Tài khoản: {len(self.add_link_accounts)} (đã chọn: {selected_accounts}) | "
                f"CapCut Pro: Có {pro_count} / Không {not_pro_count}\n"
                f"Link: {len(self.add_link_links)} | Link đã chọn: {selected_count} | "
                f"Đã thêm: {joined} | Link còn lại: {remaining_links} | Lượt còn lại: {remaining_slots}"
            )
        )
        self._refresh_add_link_account_table()
        if hasattr(self, "check_pro_status_label"):
            self.refresh_check_pro_status()

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
        self.render_reg_accounts()

    def render_reg_accounts(self):
        tree = self.reg_account_tree
        if tree is None:
            return
        tree.delete(*tree.get_children())
        for i, acc in enumerate(self.accounts):
            tree.insert("", "end", iid=str(i), values=(self.CHECKBOX_SELECTED if acc.get("picked") else self.CHECKBOX_UNSELECTED, acc.get("user", ""), acc.get("passnew", ""), acc.get("userID", ""), acc.get("status", "")))

    def _sort_tree_column(self, tree, column):
        state = getattr(tree, "_sort_reverse", {})
        state[column] = not state.get(column, False)
        tree._sort_reverse = state
        rows = [(tree.set(item, column), item) for item in tree.get_children("")]
        for index, (_, item) in enumerate(sorted(rows, key=lambda pair: pair[0].casefold(), reverse=state[column])):
            tree.move(item, "", index)

    def _select_all_reg_accounts(self, _=None):
        for acc in self.accounts:
            acc["picked"] = True
        self.render_reg_accounts()
        return "break"

    def _toggle_reg_accounts(self, _=None):
        for item in self.reg_account_tree.selection():
            acc = self.accounts[int(item)]
            acc["picked"] = not acc.get("picked", False)
        self.render_reg_accounts()
        return "break"

    def _reg_account_press(self, event):
        tree = self.reg_account_tree
        row = tree.identify_row(event.y)
        if not row:
            return None if tree.identify_region(event.x, event.y) == "heading" else "break"
        self.reg_account_drag_anchor = row
        if tree.identify_column(event.x) == "#1":
            tree.selection_set(row)
            self._toggle_reg_accounts()
        elif event.state & 0x0004:
            tree.selection_toggle(row)
        else:
            tree.selection_set(row)
        tree.focus(row)
        tree.focus_set()
        return "break"

    def _reg_account_drag(self, event):
        tree = self.reg_account_tree
        if not self.reg_account_drag_anchor:
            return "break"
        row = tree.identify_row(event.y)
        children = list(tree.get_children())
        if not row or row not in children:
            return "break"
        start = children.index(self.reg_account_drag_anchor)
        end = children.index(row)
        tree.selection_set(children[min(start, end):max(start, end) + 1])
        return "break"

    def delete_checked_reg_accounts(self):
        task = getattr(self, "task_thread", None)
        if task and task.is_alive():
            messagebox.showinfo("Thông báo", "Không thể xóa tài khoản khi tác vụ đang chạy")
            return
        checked = [account for account in self.accounts if account.get("picked")]
        if not checked:
            messagebox.showinfo("Thông báo", "Hãy tích chọn tài khoản cần xóa")
            return
        if not messagebox.askyesno(
            "Xóa tài khoản",
            f"Xóa {len(checked)} tài khoản đã tích chọn khỏi bảng Reg CapCut?",
        ):
            return
        self.accounts = [account for account in self.accounts if not account.get("picked")]
        self.refresh_status()
        print(f"[REG CAPCUT] Đã xóa {len(checked)} tài khoản đã tích chọn khỏi bảng")

    def export_registered_accounts(self):
        selected = [acc for acc in self.accounts if acc.get("picked")]
        if not selected:
            messagebox.showinfo("Info", "Hãy tích chọn tài khoản cần xuất")
            return
        path = export_registered_accounts_file(selected)
        print(f"Exported Reg accounts: {len(selected)} to {path}")

    def export_registered_usernames(self):
        selected = [
            account
            for account in self.accounts
            if account.get("picked") and str(account.get("userID", "")).strip()
        ]
        if not selected:
            messagebox.showinfo("Thông báo", "Chưa có username của tài khoản đăng ký đã tích chọn")
            return
        path = getattr(
            self,
            "reg_username_export_path",
            Path("check user") / "reg_user.txt",
        )
        path.parent.mkdir(exist_ok=True)
        content = "".join(
            f'{account.get("user", account.get("email", ""))}|{str(account["userID"]).strip()}\n'
            for account in selected
        )
        path.write_text(content, encoding="utf-8")
        print(f"[CAPCUT][REG USER] Exported {len(selected)} email|username accounts to {path}")

    def start_check_user(self):
        if not self.add_link_accounts:
            messagebox.showinfo("Thông báo", "Hãy nhập tài khoản vào Bảng acc thêm link trước")
            return
        selected_accounts = [
            account
            for account in self.add_link_accounts
            if account["user"].casefold() in self.add_link_account_selected
        ]
        if not selected_accounts:
            messagebox.showinfo("Thông báo", "Hãy tích ít nhất một tài khoản trong Bảng acc thêm link")
            return
        if self.task_thread and self.task_thread.is_alive():
            messagebox.showinfo("Thông báo", "Một tác vụ CapCut đang chạy")
            return
        try:
            max_threads = max(1, int(self.add_link_threads_entry.get().strip() or "1"))
            launch_delay = max(0.0, float(self.add_link_delay_entry.get().strip() or "0"))
        except ValueError:
            messagebox.showinfo("Thông báo", "Số luồng và Delay phải là số")
            return

        self.check_user_accounts = selected_accounts
        for account in self.check_user_accounts:
            account["userID"] = ""
        self._save_add_link_account_state()
        self.stop_event.clear()
        self.current_accounts = list(self.check_user_accounts)
        self.refresh_add_link_status()
        self.task_thread = threading.Thread(
            target=self.run_check_user_accounts,
            args=(max_threads, launch_delay),
            daemon=True,
        )
        self.task_thread.start()

    def run_check_user_accounts(self, max_threads, launch_delay):
        semaphore = threading.Semaphore(max_threads)
        workers = []
        total = len(self.check_user_accounts)
        for index, account in enumerate(self.check_user_accounts):
            if self.stop_event.is_set():
                break
            worker = threading.Thread(
                target=self.run_check_user_worker,
                args=(account, index, total, semaphore, max_threads),
                daemon=True,
            )
            workers.append(worker)
            worker.start()
            if launch_delay > 0 and index < total - 1:
                waited = 0.0
                while waited < launch_delay and not self.stop_event.is_set():
                    sleep_for = min(0.1, launch_delay - waited)
                    time.sleep(sleep_for)
                    waited += sleep_for
        for worker in workers:
            worker.join()
        found = sum(1 for account in self.check_user_accounts if account.get("userID"))
        print(
            "[CAPCUT][CHECK USER] Workflow stopped"
            if self.stop_event.is_set()
            else f"[CAPCUT][CHECK USER] Completed. Found: {found}/{total}"
        )
        self.current_accounts = []
        self.task_thread = None

    def run_check_user_worker(self, account, index, total, semaphore, max_threads):
        with semaphore:
            try:
                if self.stop_event.is_set():
                    return
                username = self._check_user_with_chromium_154(
                    account,
                    index,
                    max_threads=max_threads,
                )
                print(f'[CAPCUT][CHECK USER] {account["user"]}|{username}')
            except CapCutWorkflowInterrupted as exc:
                print(f'[CAPCUT][CHECK USER] Stopped: {account["user"]} - {exc}')
            except Exception as exc:
                error_text = str(exc).lower()
                if any(marker in error_text for marker in ("login", "password", "email step", "sign up")):
                    self._request_expressvpn_reset()
                print(f'[CAPCUT][CHECK USER] Error: {account["user"]} - {exc}')
            finally:
                self._save_add_link_account_state()
                try:
                    self.root.after(0, self.refresh_add_link_status)
                except RuntimeError:
                    pass

    def _check_user_with_chromium_154(self, account, index, max_threads=1):
        session = None
        try:
            session, result = self._start_account_browser(
                account, index, max_threads, startup_url=CAPCUT_LOGIN_URL,
            )
            return run_capcut_check_user_workflow(
                account, {"start_result": result, "stop_event": self.stop_event},
            )
        finally:
            self._close_standalone_process(session)

    def start_check_pro(self):
        if not self.add_link_accounts:
            messagebox.showinfo("Thông báo", "Hãy nhập tài khoản vào Bảng acc thêm link trước")
            return
        self.check_pro_accounts = [
            account
            for account in self.add_link_accounts
            if account["user"].casefold() in self.add_link_account_selected
        ]
        if not self.check_pro_accounts:
            messagebox.showinfo("Thông báo", "Hãy tích ít nhất một tài khoản trong Bảng acc thêm link")
            return
        if self.task_thread and self.task_thread.is_alive():
            messagebox.showinfo("Thông báo", "Một tác vụ CapCut đang chạy")
            return
        try:
            max_threads = max(1, int(self.add_link_threads_entry.get().strip() or "1"))
        except ValueError:
            messagebox.showinfo("Thông báo", "Số luồng phải là một số")
            return
        try:
            launch_delay = max(0.0, float(self.add_link_delay_entry.get().strip() or "0"))
        except ValueError:
            messagebox.showinfo("Thông báo", "Delay phải là một số")
            return

        for account in self.check_pro_accounts:
            account["pro_status"] = ""
        self._save_add_link_account_state()
        self.check_pro_path.parent.mkdir(exist_ok=True)
        for path in (self.check_pro_path, self.check_not_pro_path, self.check_pro_error_path):
            path.write_text("", encoding="utf-8")
        self.stop_event.clear()
        self.current_accounts = list(self.check_pro_accounts)
        self.refresh_add_link_status()
        self.task_thread = threading.Thread(
            target=self.run_check_pro_accounts,
            args=(max_threads, launch_delay),
            daemon=True,
        )
        self.task_thread.start()

    def run_check_pro_accounts(self, max_threads, launch_delay):
        group_name = self.selected_group_name()
        semaphore = threading.Semaphore(max_threads)
        workers = []
        total = len(self.check_pro_accounts)
        if self.proxies:
            print(f"[CAPCUT][CHECK PRO] Using {len(self.proxies)} imported proxies for {max_threads} threads")
            self.reset_loaded_proxy_ips()
        for batch_start in range(0, total, 10):
            batch = self.check_pro_accounts[batch_start:batch_start + 10]
            workers = []
            self._expressvpn_login_blocked = threading.Event()
            self._expressvpn_login_fail_count = 0
            self._expressvpn_login_fail_lock = threading.Lock()
            for offset, account in enumerate(batch):
                index = batch_start + offset
                if self.stop_event.is_set() or self._expressvpn_login_blocked.is_set():
                    account["pro_status"] = "stopped"
                    break
                worker = threading.Thread(
                    target=self.run_check_pro_worker,
                    args=(account, group_name, index, total, semaphore, max_threads),
                    daemon=True,
                )
                workers.append(worker)
                worker.start()
                if launch_delay > 0 and offset < len(batch) - 1:
                    waited = 0.0
                    while waited < launch_delay and not self.stop_event.is_set():
                        sleep_for = min(0.1, launch_delay - waited)
                        time.sleep(sleep_for)
                        waited += sleep_for
            for worker in workers:
                worker.join()
            # A queued login-block reset must still run after workers have joined,
            # even when the user pressed Stop while those workers were closing.
            if self._expressvpn_login_blocked.is_set() or (not self.stop_event.is_set() and len(batch) == 10):
                self.reset_expressvpn_ip("10 tài khoản hoặc đủ 4 lỗi đăng nhập")
            if self.stop_event.is_set():
                break
        print(
            "[CAPCUT][CHECK PRO] Workflow stopped"
            if self.stop_event.is_set()
            else f"[CAPCUT][CHECK PRO] Completed. Pro: {self.check_pro_path} | Not Pro: {self.check_not_pro_path}"
        )
        self.current_accounts = []
        self.task_thread = None
        self.hold_mode = False

    def run_check_pro_worker(self, account, group_name, index, total, semaphore, max_threads):
        with semaphore:
            standalone_process = None
            try:
                if self.stop_event.is_set():
                    account["pro_status"] = "stopped"
                    return
                account["pro_status"] = "running"
                self._save_add_link_account_state()
                try:
                    self.root.after(0, self.refresh_add_link_status)
                except RuntimeError:
                    pass
                standalone_process, result = self._start_account_browser(
                    account, index, max_threads, startup_url=CAPCUT_LOGIN_URL,
                )
                workflow_context = {
                    "start_result": result,
                    "stop_event": self.stop_event,
                }
                for attempt in range(2):
                    try:
                        is_pro = run_capcut_check_pro_workflow(account, workflow_context)
                        break
                    except CapCutWorkflowInterrupted:
                        raise
                    except Exception as exc:
                        if attempt >= 1:
                            raise
                        print(
                            f'[CAPCUT][CHECK PRO] Retry 1/1 after transient error for '
                            f'{account["user"]}: {exc}'
                        )
                        time.sleep(2.0)
                account["pro_status"] = "true" if is_pro else "false"
                self.append_check_pro_result(account, self.check_pro_path if is_pro else self.check_not_pro_path)
                print(f'[CAPCUT][CHECK PRO] {"PRO" if is_pro else "NOT PRO"}: {account["user"]}')
            except CapCutWorkflowInterrupted as exc:
                account["pro_status"] = "stopped"
                print(f'[CAPCUT][CHECK PRO] Stopped: {account["user"]} - {exc}')
            except Exception as exc:
                error_text = str(exc).lower()
                if any(marker in error_text for marker in ("login", "password", "email step", "sign up")):
                    self._request_expressvpn_reset()
                account["pro_status"] = "error"
                self.append_check_pro_result(account, self.check_pro_error_path)
                print(f'[CAPCUT][CHECK PRO] Error: {account["user"]} - {exc}')
            finally:
                try:
                    self._save_add_link_account_state()
                    try:
                        self.root.after(0, self.refresh_check_pro_status)
                        self.root.after(0, self.refresh_add_link_status)
                    except RuntimeError:
                        pass
                finally:
                    self._close_standalone_process(standalone_process)

    def append_check_pro_result(self, account, path):
        with self.check_pro_lock:
            with path.open("a", encoding="utf-8") as file:
                file.write(f'{account["user"]}|{account["passnew"]}\n')
                file.flush()

    def start_add_link(self):
        if self.task_thread and self.task_thread.is_alive():
            messagebox.showinfo("Thông báo", "Một tác vụ CapCut đang chạy")
            return
        if not self.add_link_accounts:
            messagebox.showinfo("Thông báo", "Hãy nhập file tài khoản trước (email|mật khẩu CapCut)")
            return
        if not self.add_link_links:
            messagebox.showinfo("Thông báo", "Hãy nhập file link trước")
            return
        selected_links = getattr(self, "add_link_selected", set(self.add_link_links))
        self.add_link_run_links = [link for link in self.add_link_links if link in selected_links]
        if not self.add_link_run_links:
            messagebox.showinfo("Thông báo", "Chọn ít nhất một link trong Danh sách link")
            return
        try:
            max_threads = max(1, int(self.add_link_threads_entry.get().strip() or "1"))
        except ValueError:
            messagebox.showinfo("Thông báo", "Số luồng phải là một số")
            return
        try:
            launch_delay = max(0.0, float(self.add_link_delay_entry.get().strip() or "0"))
        except ValueError:
            messagebox.showinfo("Thông báo", "Delay phải là một số")
            return

        selected_accounts = getattr(self, "add_link_account_selected", {
            account["user"].casefold() for account in self.add_link_accounts
        })
        self.add_link_run_accounts = [
            account for account in self.add_link_accounts
            if account["user"].casefold() in selected_accounts
            and account.get("status") != "true"
            and (not _add_link_assignment(account) or _add_link_assignment(account) in self.add_link_run_links)
        ]
        if not self.add_link_run_accounts:
            messagebox.showinfo("Thông báo", "Không có tài khoản đã chọn phù hợp: tài khoản đã add thành công sẽ được bỏ qua; tài khoản đã phân link chỉ chạy lại link đó.")
            return
        for account in self.add_link_run_accounts:
            account["assigned_link"] = _add_link_assignment(account)
            account["status"] = ""
        self._save_add_link_account_state()
        with self.add_link_counts_lock:
            # Counts are cumulative per invitation and are persisted in workspace.json.
            # Resetting them here made the table forget every previous successful join
            # whenever Start was pressed again.
            self.add_link_counts = {
                link: min(6, max(0, int(self.add_link_counts.get(link, 0))))
                for link in self.add_link_links
            }
            self.add_link_errors = {link: [] for link in self.add_link_links}
            self.add_link_full.intersection_update(self.add_link_links)
            self.add_link_full.update(
                link for link, count in self.add_link_counts.items() if count >= 6
            )
            self.add_link_success_path.parent.mkdir(exist_ok=True)
            self.add_link_success_path.write_text("", encoding="utf-8")
            self.add_link_fail_path.write_text("", encoding="utf-8")
        self.write_add_link_results()
        self._save_add_link_workspace()
        self.stop_event.clear()
        self.current_accounts = list(self.add_link_run_accounts)
        self.refresh_add_link_status()
        self.task_thread = threading.Thread(
            target=self.run_add_link_accounts,
            args=(max_threads, launch_delay),
            daemon=True,
        )
        self.task_thread.start()

    def run_add_link_accounts(self, max_threads, launch_delay):
        group_name = self.selected_group_name()
        semaphore = threading.Semaphore(max_threads)
        run_links = list(getattr(self, "add_link_run_links", None) or self.add_link_links)
        # Freeze this run's input and deduplicate email identities before any
        # workers are launched, including duplicates differing only by case.
        pending = []
        seen = set()
        for account in getattr(self, "add_link_run_accounts", self.add_link_accounts):
            key = account["user"].strip().casefold()
            if key in seen:
                continue
            seen.add(key)
            if account.get("status") != "true":
                pending.append(account)
        total = len(pending)
        print(
            f"[CAPCUT][ADD LINK][RUN] Starting: accounts={total} | links={len(run_links)} | "
            f"threads={max_threads} | launch_delay={launch_delay}s"
        )
        account_index = 0
        link_index = 0
        running = []
        if self.proxies:
            print(f"[CAPCUT][ADD LINK] Using {len(self.proxies)} imported proxies for {max_threads} threads")
            self.reset_loaded_proxy_ips()
        while not self.stop_event.is_set() and pending and link_index < len(run_links):
            running = [item for item in running if item[0].is_alive()]
            link = run_links[link_index]
            with self.add_link_counts_lock:
                success_count = self.add_link_counts[link]
            in_flight = sum(1 for _, active_link in running if active_link == link)
            if success_count >= 6:
                print(f"[CAPCUT][ADD LINK][SCHEDULER] Link already full ({success_count}/6), skipping: {link}")
                link_index += 1
                continue
            if len(running) >= max_threads or success_count + in_flight >= 6:
                time.sleep(0.1)
                continue

            candidate_index = next((
                i for i, account in enumerate(pending)
                if not _add_link_assignment(account) or _add_link_assignment(account) == link
            ), None)
            if candidate_index is None:
                link_index += 1
                continue
            account = pending.pop(candidate_index)
            index = account_index
            account_index += 1
            print(
                f"[CAPCUT][ADD LINK][SCHEDULER] Assigning {account['user']} -> {link} | "
                f"count={success_count}/6 | in_flight={in_flight} | pending_accounts={len(pending)}"
            )
            worker = threading.Thread(
                target=self.run_add_link_worker,
                args=(account, link, group_name, index, total, semaphore, max_threads),
                daemon=True,
            )
            running.append((worker, link))
            worker.start()
            if launch_delay > 0:
                waited = 0.0
                while waited < launch_delay and not self.stop_event.is_set():
                    sleep_for = min(0.1, launch_delay - waited)
                    time.sleep(sleep_for)
                    waited += sleep_for
        for worker, _ in running:
            worker.join()
        self.write_add_link_results()
        print(
            "[CAPCUT][ADD LINK] Workflow stopped"
            if self.stop_event.is_set()
            else (
                f"[CAPCUT][ADD LINK] Completed. Success accounts: {self.add_link_success_path} | "
                f"Failed accounts: {self.add_link_fail_path} | Link counts: {self.add_link_export_path}"
            )
        )
        self.current_accounts = []
        self.task_thread = None

    def run_add_link_worker(self, account, link, group_name, index, total, semaphore, max_threads):
        with semaphore:
            print(
                f"[CAPCUT][ADD LINK][WORKER] Started {index + 1}/{total}: "
                f"{account['user']} -> {link}"
            )
            history = self._read_add_link_account_state().get(account["user"].casefold(), {})
            if isinstance(history, dict):
                if history.get("status") == "true":
                    account["status"] = "true"
                if _add_link_assignment(history):
                    account["assigned_link"] = _add_link_assignment(history)
                if history.get("join_pending"):
                    account["join_pending"] = True
            if account.get("status") == "true" or (
                _add_link_assignment(account) and _add_link_assignment(account) != link
            ):
                print(
                    f"[CAPCUT][ADD LINK][WORKER] Skipped by saved state: {account['user']} | "
                    f"status={account.get('status', '')} | assigned_link={account.get('assigned_link', '')}"
                )
                return
            standalone_process = None
            submit_succeeded = False
            try:
                if self.stop_event.is_set():
                    account["status"] = "stopped"
                    return
                # Persist the assignment before browser actions. Even a timeout
                # after Submit must never send this account to a different link.
                account["assigned_link"] = link
                account["status"] = "running"
                self._save_add_link_account_state()
                print(f"[CAPCUT][ADD LINK][STATE] Saved running assignment: {account['user']} -> {link}")
                try:
                    self.root.after(0, self.refresh_add_link_status)
                except RuntimeError:
                    pass
                standalone_process, result = self._start_account_browser(
                    account, index, max_threads, startup_url=CAPCUT_LOGIN_URL,
                )
                print(
                    f"[CAPCUT][ADD LINK][BROWSER] Chromium 154 ready: {account['user']} | "
                    f"address={getattr(result, 'remote_debugging_address', 'unknown')}"
                )
                workflow_ok = run_capcut_add_link_workflow(
                    account,
                    link,
                    {
                        "start_result": result, "stop_event": self.stop_event,
                        "before_join_submit": self._save_add_link_account_state,
                    },
                )
                if not workflow_ok:
                    raise RuntimeError("CapCut join space returned false")
                # The workflow confirms membership via a fresh invitation read,
                # matching the workspace identified before the single Submit.
                submit_succeeded = True
                with self.add_link_counts_lock:
                    if link in self.add_link_full:
                        account["status"] = "true"
                        print(f'[CAPCUT][ADD LINK] Success confirmed after link was marked full: {account["user"]} -> {link}')
                        return
                    account["status"] = "true"
                    self.add_link_counts[link] = min(6, self.add_link_counts[link] + 1)
                    self.write_add_link_results(lock_held=True)
                    print(
                        f"[CAPCUT][ADD LINK][RESULT] Updated link count: "
                        f"{self.add_link_counts[link]}/6 | {link}"
                    )
                try:
                    self.root.after(0, self.refresh_add_link_status)
                except RuntimeError as exc:
                    # The join has already succeeded; a closing/headless UI must not
                    # turn the external result into a failed account.
                    print(f"[CAPCUT][ADD LINK] UI refresh warning: {exc}")
                print(f'[CAPCUT][ADD LINK] Success: {account["user"]} -> {link}')
            except AlreadyJoinedSpaceError:
                submit_succeeded = True
                account["status"] = "true"
                print(f'[CAPCUT][ADD LINK] Already joined; no Submit and no extra link use: {account["user"]}')
            except JoinVerificationError as exc:
                account["status"] = "join unverified"
                print(f'[CAPCUT][ADD LINK] Membership unconfirmed: {account["user"]} - {exc}')
            except LinkFullError:
                account["status"] = "link full"
                with self.add_link_counts_lock:
                    self.add_link_full.add(link)
                    self.add_link_counts[link] = 6
                    self.write_add_link_results(lock_held=True)
                print(f'[CAPCUT][ADD LINK] Can\'t submit request; marked link full and skipped: {link}')
            except CannotJoinSpaceError as exc:
                account["status"] = "can't join space"
                with self.add_link_counts_lock:
                    self.write_add_link_results(lock_held=True)
                print(f'[CAPCUT][ADD LINK] Can\'t join space; next account: {account["user"]} -> {link}')
            except CapCutLoginError as exc:
                # This account cannot be used for any invitation. The finally
                # block records it in acc_add_fail.txt and closes its profile;
                # the scheduler then takes the next account for the same link.
                account["status"] = "login fail"
                with self.add_link_counts_lock:
                    self.add_link_errors[link].append(f'{account["user"]}|login fail: {exc}')
                    self.write_add_link_results(lock_held=True)
                print(f'[CAPCUT][ADD LINK] Login failed; closed and moved to acc_add_fail.txt: {account["user"]}')
            except CapCutWorkflowInterrupted as exc:
                account["status"] = "join unverified" if account.get("join_pending") else "stopped"
                print(f'[CAPCUT][ADD LINK] Stopped: {account["user"]} - {exc}')
            except Exception as exc:
                account["status"] = "join unverified" if account.get("join_pending") else f"fail: {exc}"
                with self.add_link_counts_lock:
                    self.add_link_errors[link].append(f'{account["user"]}|{exc}')
                    self.write_add_link_results(lock_held=True)
                print(f'[CAPCUT][ADD LINK] Error: {account["user"]} -> {link} - {exc}')
            finally:
                if submit_succeeded:
                    account["status"] = "true"
                    account["join_pending"] = False
                try:
                    self._save_add_link_account_state()
                    self.append_add_link_account_result(account, submit_succeeded)
                    print(
                        f"[CAPCUT][ADD LINK][STATE] Final state saved: {account['user']} | "
                        f"status={account.get('status', '')} | join_pending={account.get('join_pending', False)} | "
                        f"result_file={'success' if submit_succeeded else 'fail'}"
                    )
                    try:
                        self.root.after(0, self.refresh_add_link_status)
                    except RuntimeError:
                        pass
                finally:
                    with self.active_profiles_lock:
                        self.active_profiles.pop(account["user"], None)
                    self._close_standalone_process(standalone_process)
                    print(f"[CAPCUT][ADD LINK][BROWSER] Chromium 154 closed: {account['user']}")

    def write_add_link_results(self, lock_held=False):
        def write_file():
            self.add_link_export_path.parent.mkdir(exist_ok=True)
            # Keep every imported link, including untouched links with count 0,
            # so stopping early never loses invitations that can still be used.
            lines = [
                f"{link}|{count}"
                for link, count in self.add_link_counts.items()
            ]
            self.add_link_export_path.write_text("\n".join(lines), encoding="utf-8")
            remaining_path = getattr(
                self,
                "add_link_remaining_path",
                self.add_link_export_path.with_name("add_link_remaining.txt"),
            )
            remaining_lines = [
                f"{link}|{6 - min(6, count)}"
                for link, count in self.add_link_counts.items()
                if count < 6
            ]
            remaining_path.write_text("\n".join(remaining_lines), encoding="utf-8")
            error_lines = [
                f"{link}|{self.add_link_counts.get(link, 0)}"
                for link, errors in self.add_link_errors.items()
                if errors
            ]
            self.add_link_error_path.write_text("\n".join(error_lines), encoding="utf-8")

        if lock_held:
            write_file()
        else:
            with self.add_link_counts_lock:
                write_file()

    def append_add_link_account_result(self, account, success):
        path = self.add_link_success_path if success else self.add_link_fail_path
        path.parent.mkdir(exist_ok=True)
        with self.add_link_result_lock:
            with path.open("a", encoding="utf-8") as file:
                file.write(f'{account["user"]}|{account["passnew"]}\n')
                file.flush()

    def start(self):
        selected = [account for account in self.accounts if account["picked"]]
        if not selected:
            messagebox.showinfo("Info", "Import mail first")
            return
        passnew = self.passnew_entry.get().strip()
        if not passnew:
            messagebox.showinfo("Info", "Enter Pass CapCut")
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
            messagebox.showinfo("Info", "Reg CapCut is running")
            return
        try:
            self._resolve_chromium_154_browser()
        except Exception as exc:
            messagebox.showerror("Chromium 154", str(exc))
            print(f"[REG CAPCUT] Chromium 154 preflight failed: {exc}")
            return
        self.stop_event.clear()
        self.current_accounts = selected
        for account in selected:
            account["passnew"] = passnew
        self.refresh_status()
        self.task_thread = threading.Thread(target=self.run_accounts, args=(selected, max_threads, launch_delay), daemon=True)
        self.task_thread.start()

    def start_hold_mode(self):
        if not self.add_link_accounts:
            messagebox.showinfo("Thông báo", "Hãy nhập tài khoản vào Bảng acc thêm link trước")
            return
        selected = [
            account
            for account in self.add_link_accounts
            if account["user"].casefold() in self.add_link_account_selected
        ]
        if not selected:
            messagebox.showinfo("Thông báo", "Hãy tích ít nhất một tài khoản trong Bảng acc thêm link")
            return
        if self.task_thread and self.task_thread.is_alive():
            messagebox.showinfo("Thông báo", "Một tác vụ CapCut đang chạy")
            return
        try:
            resolve_installed_chrome()
        except RuntimeError as exc:
            messagebox.showerror("Google Chrome", str(exc))
            return
        try:
            threads = max(1, int(self.add_link_threads_entry.get().strip() or "1"))
        except ValueError:
            messagebox.showinfo("Thông báo", "Số luồng phải là một số")
            return
        self.hold_mode = True
        self.stop_event.clear()
        self.current_accounts = list(selected)
        self.task_thread = threading.Thread(target=self.run_hold_accounts, args=(selected, threads), daemon=True)
        self.task_thread.start()

    def run_hold_accounts(self, accounts, max_threads):
        index_lock = threading.Lock()
        next_index = [0]
        semaphore = threading.Semaphore(max_threads)
        def worker_loop():
            while not self.stop_event.is_set():
                with index_lock:
                    if next_index[0] >= len(accounts):
                        return
                    index = next_index[0]
                    next_index[0] += 1
                self.run_hold_login_worker(accounts[index], index, semaphore, max_threads)
        workers = [threading.Thread(target=worker_loop, daemon=True) for _ in range(max_threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.current_accounts = []
        self.task_thread = None
        self.hold_mode = False
        print("Đăng nhập treo CapCut đã hoàn tất")

    def run_hold_login_worker(self, account, index, semaphore, max_threads):
        with semaphore:
            session = None
            try:
                if self.stop_event.is_set():
                    return
                session, result = self._start_account_browser(
                    account, index, max_threads, startup_url=CAPCUT_LOGIN_URL,
                )
                run_capcut_login_hold_workflow(
                    account,
                    {
                        "start_result": result,
                        "stop_event": self.stop_event,
                        "on_logged_in": lambda: self._wait_for_hold_profile(result, account["user"]),
                    },
                )
            except CapCutWorkflowInterrupted as exc:
                print(f'[CAPCUT][LOGIN TREO] Đã dừng: {account["user"]} - {exc}')
            except Exception as exc:
                print(f'[CAPCUT][LOGIN TREO] Lỗi: {account["user"]} - {exc}')
            finally:
                self._close_standalone_process(session)

    def run_accounts(self, accounts, max_threads, launch_delay):
        group_name = self.selected_group_name()
        semaphore = threading.Semaphore(max_threads)
        workers = []
        total_accounts = len(accounts)
        if self.proxies:
            print(f"Using {len(self.proxies)} imported proxies for {max_threads} threads")
            self.reset_loaded_proxy_ips()
        # Run in batches so an ExpressVPN reset never interrupts active browsers.
        for batch_start in range(0, total_accounts, 10):
            batch = accounts[batch_start:batch_start + 10]
            workers = []
            self._expressvpn_login_blocked = threading.Event()
            self._expressvpn_login_fail_count = 0
            self._expressvpn_login_fail_lock = threading.Lock()
            for offset, account in enumerate(batch):
                index = batch_start + offset
                if self.stop_event.is_set() or self._expressvpn_login_blocked.is_set():
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
                if launch_delay > 0 and offset < len(batch) - 1:
                    waited = 0.0
                    while waited < launch_delay:
                        if self.stop_event.is_set():
                            break
                        sleep_for = min(0.1, launch_delay - waited)
                        time.sleep(sleep_for)
                        waited += sleep_for
            for worker in workers:
                worker.join()
            if self._expressvpn_login_blocked.is_set() or (not self.stop_event.is_set() and len(batch) == 10):
                self.reset_expressvpn_ip("10 tài khoản hoặc đủ 4 lỗi đăng nhập")
            if self.stop_event.is_set():
                break
        self.flush_sheet_rows(force=True)
        print("Reg CapCut workflow stopped" if self.stop_event.is_set() else "Reg CapCut workflow completed")
        self.current_accounts = []
        self.task_thread = None

    def run_worker(self, account, group_name, index, total, semaphore, max_threads):
        with semaphore:
            standalone_process = None
            try:
                if self.stop_event.is_set():
                    self.mark_account_status(account, "stopped")
                    return
                self.mark_account_status(account, "running")
                standalone_process, result = self._start_account_browser(account, index, max_threads)
                if self.stop_event.is_set():
                    self.mark_account_status(account, "stopped")
                    return
                success_recorded = False

                def on_registered():
                    nonlocal success_recorded
                    if success_recorded:
                        return
                    success_recorded = True
                    self.mark_account_status(account, "true")
                    write_result(account, True, self.export_lock)
                    self.queue_sheet_success(account)
                    print(f'Success: {account["user"]}')
                    if getattr(self, "hold_mode", False):
                        self._wait_for_hold_profile(result, account["user"])

                workflow_ok = bool(
                    run_capcut_workflow(
                        account,
                        {
                            "start_result": result,
                            "stop_event": self.stop_event,
                            "on_login_blocked": self._request_expressvpn_reset,
                            "on_registered": on_registered,
                        },
                    )
                )
                if self.stop_event.is_set():
                    self.mark_account_status(account, "stopped")
                    return
                if workflow_ok:
                    on_registered()
                else:
                    self.mark_account_status(account, "change fail")
                    write_result(account, False, self.export_lock)
                    print(f'Change failed: {account["user"]}')
            except CapCutWorkflowInterrupted as exc:
                self.mark_account_status(account, "stopped")
                print(f'Workflow stopped: {account["user"]} - {exc}')
            except Exception as exc:
                error_text = str(exc).lower()
                if any(marker in error_text for marker in ("login", "password", "email step", "sign up")):
                    self._request_expressvpn_reset()
                self.mark_account_status(account, f"change fail: {exc}")
                write_result(account, False, self.export_lock)
                print(f'Error: {account["user"]} - {exc}')
            finally:
                with self.active_profiles_lock:
                    self.active_profiles.pop(account["user"], None)
                self._close_standalone_process(standalone_process)

    def _resolve_chromium_154_browser(self):
        return resolve_chromium_154(self.app_settings.chromium_path)

    def _start_standalone_chromium(self, account, index, browser_override=None,
                                   profile_root=None, startup_url=CAPCUT_SIGN_UP_URL,
                                   raw_proxy="", max_threads=1, expected_major="154",
                                   reuse_startup_context=False):
        # profile_root is accepted for compatibility; every session gets a fresh
        # temporary directory, never a reused account/index directory.
        session = ChromiumSession(
            browser_override or self._resolve_chromium_154_browser(),
            stop_event=self.stop_event, raw_proxy=raw_proxy,
            window_settings=self.app_settings.browser_window,
            index=index % max_threads, total_windows=max_threads,
            expected_major=expected_major,
            reuse_startup_context=reuse_startup_context,
        )
        with self.active_profiles_lock:
            self.active_standalone_processes[str(session.profile_dir)] = session
        if self.stop_event.is_set():
            self._close_standalone_process(session)
            raise CapCutWorkflowInterrupted("Đã dừng Chromium 154")
        return session, session

    def _start_account_browser(self, account, index, max_threads, startup_url=CAPCUT_SIGN_UP_URL):
        proxy = self.proxy_for_worker(index, max_threads)
        raw_proxy = self.get_account_raw_proxy(account, proxy)
        if getattr(self, "hold_mode", False):
            return self._start_standalone_chromium(
                account,
                index,
                browser_override=resolve_installed_chrome(),
                raw_proxy=raw_proxy,
                max_threads=max_threads,
                expected_major=None,
                reuse_startup_context=True,
            )
        return self._start_standalone_chromium(
            account, index, raw_proxy=raw_proxy, max_threads=max_threads,
            startup_url=startup_url,
        )

    def _close_standalone_process(self, process):
        if process is None:
            return
        try:
            process.terminate()
        finally:
            with self.active_profiles_lock:
                active = getattr(self, "active_standalone_processes", {})
                for key in [key for key, value in active.items() if value is process]:
                    active.pop(key, None)

    def _request_expressvpn_reset(self):
        event = getattr(self, "_expressvpn_login_blocked", None)
        lock = getattr(self, "_expressvpn_login_fail_lock", None)
        if event is not None and lock is not None:
            with lock:
                self._expressvpn_login_fail_count += 1
                failures = self._expressvpn_login_fail_count
            print(f"[REG CAPCUT] Login failure {failures}/4")
            if failures < 4:
                return
            event.set()
            print("[REG CAPCUT] Four login failures detected; ExpressVPN reset queued")

    def reset_expressvpn_ip(self, reason=""):
        try:
            vpn = ExpressVPNController(binary_path=self.app_settings.expressvpn_path)
            current = vpn.get_status()
            if not current.connected or not current.alias:
                print("[REG CAPCUT][VPN] Reset skipped: ExpressVPN is not connected")
                return False
            print(f"[REG CAPCUT][VPN] Reset IP ({reason}) at {current.alias}")
            status = vpn.change_ip(alias=current.alias, cooldown_seconds=4.0)
            if not status.connected:
                raise ExpressVPNError("reconnect did not report connected")
            print(f"[REG CAPCUT][VPN] Reset complete at {status.alias}")
            return True
        except Exception as exc:
            print(f"[REG CAPCUT][VPN] Reset warning: {exc}")
            return False

    def _wait_for_hold_profile(self, start_result, user):
        # The owned process is authoritative. A transient CDP error or empty
        # target list must never trigger worker cleanup and kill a live window.
        if callable(getattr(start_result, "poll", None)):
            print(f"[REG TREO] Đang giữ cửa sổ cho {user}; đóng Chromium 154 bằng X để chạy tiếp")
            while not self.stop_event.is_set() and start_result.poll() is None:
                self.stop_event.wait(0.5)
            if not self.stop_event.is_set():
                print(f"[REG TREO] Chromium 154 đã thoát cho {user}; chờ 3 giây")
                self.stop_event.wait(3)
            return
        address = str(getattr(start_result, "remote_debugging_address", "") or getattr(start_result, "browser_location", "") or "")
        match = re.search(r"(?:localhost|127\.0\.0\.1|(?:\d{1,3}\.){3}\d{1,3}):(\d+)", address)
        if not match:
            print(f"[REG TREO] Không tìm thấy cổng Chromium 154 để theo dõi: {address}")
            return
        port = int(match.group(1))
        print(f"[REG TREO] Đang giữ cửa sổ cho {user}; đóng Chromium 154 bằng X để chạy tiếp")
        while not self.stop_event.is_set():
            try:
                with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as response:
                    if not any(target.get("type") == "page" for target in json.load(response)):
                        break
                self.stop_event.wait(0.5)
            except OSError:
                break
        if not self.stop_event.is_set():
            print(f"[REG TREO] Đã đóng cửa sổ {user}; chờ 3 giây")
            time.sleep(3)

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
        return "capcut"

    def proxy_for_worker(self, index, max_threads):
        if not self.proxies:
            return None
        active_slots = min(max_threads, max(1, len(self.current_accounts)))
        slot = index % active_slots
        proxy_index = min(len(self.proxies) - 1, (slot * len(self.proxies)) // active_slots)
        return self.proxies[proxy_index]

    def mask_proxy(self, raw_proxy):
        try:
            return (parse_browser_proxy(raw_proxy) or {}).get("server", "")
        except RuntimeError:
            return "<invalid proxy>"

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

    def mark_account_status(self, account, status):
        with self.account_lock:
            account["status"] = status
        self.root.after(0, self.refresh_status)

    def stop(self):
        if not self.task_thread or not self.task_thread.is_alive():
            print("No Reg CapCut workflow is running")
            return
        self.stop_event.set()
        with self.active_profiles_lock:
            standalone_to_close = list(
                getattr(self, "active_standalone_processes", {}).items()
            )
        for user, process in standalone_to_close:
            try:
                self._close_standalone_process(process)
                print(f"Stop closed Chromium 154: {user}")
            except Exception as exc:
                print(f"Stop Chromium 154 warning for {user}: {exc}")
        print("Stop requested")

    def drain(self):
        while not self.output_queue.empty():
            self.log.insert("end", self.output_queue.get_nowait())
            self.log.see("end")
        self.root.after(100, self.drain)


def main() -> None:
    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry("1100x700")
    app = RegCapCutApp(root)
    # Persist the Add Link workspace, including per-link usage counts, before exit.
    def close_app():
        try:
            app.stop()
            app._save_add_link_workspace()
        finally:
            root.destroy()
    root.protocol("WM_DELETE_WINDOW", close_app)
    root.mainloop()


if __name__ == "__main__":
    main()
