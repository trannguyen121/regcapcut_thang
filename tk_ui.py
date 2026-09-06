from modules.browser.chromium import ChromiumSession, resolve_chromium_152
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from core.orchestrator import Orchestrator
from core.settings import AppSettings, BrowserWindowSettings, CapMonsterSettings, GPMSettings, ThueCloudSettings, load_settings, save_settings
from modules.actions.workflow import SIGN_IN_URL, SIGN_UP_URL, WorkflowInterrupted, run_workflow
from modules.actions.capcut_workflow import (
    CAPCUT_SIGN_UP_URL,
    CapCutWorkflowInterrupted,
    run_capcut_workflow,
)
from modules.proxy.thuecloud import ThueCloudClient, parse_static_proxy_line, reset_change_ip_url
from modules.ui import reg_grok_tab, reg_capcut_tab


class Writer:
    HIDDEN_UI_PATTERNS = (
        "[CDP] Waiting",
        "[CDP] Connecting",
        "[CDP] Endpoint ready",
        "[OTP] Poll",
        "[OTP] Signup poll",
        "[WAIT] Holding workflow",
        "[PASS] Looking for",
        "[PASS] data-testid",
        "[PASS] Trying selector",
        "[PASS] Selector not visible",
        "[PASS] Password input",
        "[SIGNUP] Details form not visible yet",
        "[B8][VERIFY] Visible checkbox candidate",
        "[B8][VERIFY] Still waiting",
        "[B9][COMPLETE] Verification state",
        "[B10][RESULT] Account page check",
    )

    def __init__(self, q):
        self.q = q
        self.lock = threading.Lock()
        self.current_date = None
        self.log_file = None
        self.ui_buffer = ""
        self.input_buffer = ""

    def write(self, s):
        if not s:
            return
        with self.lock:
            self.input_buffer += s
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
                    self.q.put(self.ui_buffer)
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
            self.ensure_log_file()
            self.log_file.write(text)
            self.log_file.flush()
        except Exception:
            pass

    def ensure_log_file(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if self.current_date == today and self.log_file:
            return
        if self.log_file:
            self.log_file.close()
        log_dir = Path("log") / today
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = (log_dir / "app.log").open("a", encoding="utf-8")
        self.current_date = today

    def write_ui(self, text):
        self.ui_buffer += text
        while "\n" in self.ui_buffer:
            line, self.ui_buffer = self.ui_buffer.split("\n", 1)
            line += "\n"
            if self.should_show_ui(line):
                self.q.put(line)

    def should_show_ui(self, text):
        stripped = text.strip()
        if not stripped:
            return True
        return not any(pattern in stripped for pattern in self.HIDDEN_UI_PATTERNS)


class App:
    UNCHECKED = "\u2610"
    CHECKED = "\u2611"
    STABLE_BROWSER_VERSION = "142.0.7444.163"
    WINDOWS_CHROME_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/{browser_version} Safari/537.36"
    )

    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.orch = Orchestrator()
        self.app_settings = self.orch.settings
        self.groups = []
        self.all_profiles = []
        self.profiles = []
        self.profile_vars = []
        self.task_thread = None
        self.accounts_path = Path("account.txt")
        self.accounts = []
        self.proxy_path = Path("proxy.txt")
        self.proxies = []
        self.account_tables = {}
        self.current_tab = "Home"
        self.menu_buttons = {}
        self.sort_state = {}
        self.editor = None
        self.settings_window = None
        self.settings_entries = {}
        self.drag_start_row = None
        self.account_lock = threading.Lock()
        self.export_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.active_change_profiles = {}
        self.active_change_profiles_lock = threading.Lock()
        self.current_change_accounts = []
        sys.stdout = sys.stderr = Writer(self.q)
        self.setup_style()
        self.build_shell()
        self.build_home_tab()
        self.build_change_tab()
        self.build_change_mail_tab()
        self.build_checkdate_tab()
        self.build_reg_grok_tab()
        self.build_reg_capcut_tab()
        self.show_tab("Home")
        self.root.after(100, self.drain)

    def setup_style(self):
        self.root.configure(bg="#f3f4f7")
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Tool.Treeview", background="#ffffff", fieldbackground="#ffffff", rowheight=26, bordercolor="#9aa3b2")
        style.configure("Tool.Treeview.Heading", background="#e7ebf3", foreground="#111827", relief="flat", padding=6)
        style.map("Tool.Treeview.Heading", background=[("active", "#d8deea")])

    def build_shell(self):
        top = tk.Frame(self.root, bg="#4057a7", height=42)
        top.pack(fill="x")
        top.pack_propagate(False)
        tk.Label(top, text="Nguyen Dang Cap", bg="#4057a7", fg="#ffffff", font=("Tahoma", 12, "bold")).pack(side="left", padx=12)
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

        menu = tk.Frame(self.root, bg="#eef1f6", height=38)
        menu.pack(fill="x")
        menu.pack_propagate(False)
        self.make_menu_button(menu, "Home")
        self.make_menu_button(menu, "Change")
        self.make_menu_button(menu, "Change Mail")
        self.make_menu_button(menu, "checkdate")
        self.make_menu_button(menu, "Reg Grok")
        self.make_menu_button(menu, "Reg CapCut")

        self.body = tk.Frame(self.root, bg="#f3f4f7")
        self.body.pack(fill="both", expand=True, padx=8, pady=8)

        self.tab_frames = {
            "Home": tk.Frame(self.body, bg="#f3f4f7"),
            "Change": tk.Frame(self.body, bg="#f3f4f7"),
            "Change Mail": tk.Frame(self.body, bg="#f3f4f7"),
            "checkdate": tk.Frame(self.body, bg="#f3f4f7"),
            "Reg Grok": tk.Frame(self.body, bg="#f3f4f7"),
            "Reg CapCut": tk.Frame(self.body, bg="#f3f4f7"),
        }

        self.log = tk.Text(self.root, height=10, bg="#f8fafc", fg="#111827", relief="solid", borderwidth=1)
        self.log.pack(fill="both", padx=8, pady=(0, 8))

    def make_menu_button(self, parent, name):
        btn = tk.Button(parent, text=name, bd=0, relief="flat", padx=14, pady=6, command=lambda n=name: self.show_tab(n))
        btn.pack(side="left", padx=(6, 0), pady=4)
        self.menu_buttons[name] = btn

    def show_tab(self, name):
        self.current_tab = name
        for key, frame in self.tab_frames.items():
            frame.pack_forget()
            self.menu_buttons[key].configure(bg="#eef1f6", fg="#1f2937")
        self.tab_frames[name].pack(fill="both", expand=True)
        self.menu_buttons[name].configure(bg="#ffffff", fg="#0f172a")
        if name in self.account_tables:
            self.account_table = self.account_tables[name]

    def tool_bar(self, parent):
        bar = tk.Frame(parent, bg="#ffffff", relief="solid", borderwidth=1)
        bar.pack(fill="x", pady=(0, 8))
        return bar

    def section(self, parent):
        frame = tk.Frame(parent, bg="#ffffff", relief="solid", borderwidth=1)
        return frame

    def tool_button(self, parent, text, command, color="#ffffff", fg="#111827"):
        return tk.Button(parent, text=text, command=command, bg=color, fg=fg, relief="solid", borderwidth=1, padx=10, pady=4)

    def reload_app_settings(self):
        self.app_settings = load_settings()
        self.orch = Orchestrator(self.app_settings)

    def open_settings_window(self):
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            self.settings_window.focus_force()
            return

        self.settings_window = tk.Toplevel(self.root)
        self.settings_window.title("Settings")
        self.settings_window.geometry("720x560")
        self.settings_window.configure(bg="#f3f4f7")
        self.settings_window.transient(self.root)
        self.settings_window.grab_set()
        self.settings_window.protocol("WM_DELETE_WINDOW", self.close_settings_window)

        wrap = tk.Frame(self.settings_window, bg="#f3f4f7")
        wrap.pack(fill="both", expand=True, padx=12, pady=12)

        canvas = tk.Canvas(wrap, bg="#f3f4f7", highlightthickness=0)
        canvas.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        scroll.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=scroll.set)

        body = tk.Frame(canvas, bg="#f3f4f7")
        body_window = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(body_window, width=e.width))

        self.settings_entries = {}
        self.build_settings_section(
            body,
            "GPM",
            [
                ("gpm.api_base_url", "API Base URL"),
                ("gpm.profile_url_template", "Profile URL Template"),
                ("gpm.default_group_id", "Default Group ID"),
                ("gpm.default_page", "Default Page"),
                ("gpm.default_per_page", "Default Per Page"),
            ],
        )
        self.build_settings_section(
            body,
            "CapMonster",
            [
                ("capmonster.api_base_url", "API Base URL"),
                ("capmonster.api_key", "API Key"),
            ],
        )
        self.build_settings_section(
            body,
            "ThueCloud Proxy",
            [
                ("thuecloud.enabled", "Enabled"),
                ("thuecloud.api_base_url", "API Base URL"),
                ("thuecloud.access_token", "Access Token"),
                ("thuecloud.proxy_endpoint", "Proxy Endpoint"),
                ("thuecloud.proxy_method", "Proxy Method"),
                ("thuecloud.proxy_api_key", "Proxy API Key"),
            ],
        )
        self.build_settings_section(
            body,
            "Browser Window",
            [
                ("browser_window.mode", "Mode"),
                ("browser_window.scale", "Scale"),
                ("browser_window.screen_width", "Screen Width"),
                ("browser_window.screen_height", "Screen Height"),
                ("browser_window.margin", "Margin"),
                ("browser_window.gap", "Gap"),
                ("browser_window.min_width", "Min Width"),
                ("browser_window.min_height", "Min Height"),
            ],
        )

        actions = tk.Frame(self.settings_window, bg="#f3f4f7")
        actions.pack(fill="x", padx=12, pady=(0, 12))
        self.tool_button(actions, "Reload", self.fill_settings_form, "#ffffff", "#111827").pack(side="left", padx=(0, 6))
        self.tool_button(actions, "Save", self.save_settings_from_ui, "#16a34a", "#ffffff").pack(side="right")

        self.fill_settings_form()

    def build_settings_section(self, parent, title, fields):
        section = self.section(parent)
        section.pack(fill="x", pady=(0, 10))
        tk.Label(section, text=title, bg="#ffffff", fg="#111827", font=("Tahoma", 10, "bold")).pack(anchor="w", padx=10, pady=(10, 6))
        for key, label in fields:
            row = tk.Frame(section, bg="#ffffff")
            row.pack(fill="x", padx=10, pady=4)
            tk.Label(row, text=label, bg="#ffffff", width=20, anchor="w").pack(side="left")
            entry = tk.Entry(row, relief="solid", borderwidth=1)
            entry.pack(side="left", fill="x", expand=True)
            self.settings_entries[key] = entry

    def fill_settings_form(self):
        self.app_settings = load_settings()
        values = {
            "gpm.api_base_url": self.app_settings.gpm.api_base_url,
            "gpm.profile_url_template": self.app_settings.gpm.profile_url_template,
            "gpm.default_group_id": "" if self.app_settings.gpm.default_group_id is None else str(self.app_settings.gpm.default_group_id),
            "gpm.default_page": str(self.app_settings.gpm.default_page),
            "gpm.default_per_page": str(self.app_settings.gpm.default_per_page),
            "capmonster.api_base_url": self.app_settings.capmonster.api_base_url,
            "capmonster.api_key": self.app_settings.capmonster.api_key,
            "thuecloud.enabled": str(self.app_settings.thuecloud.enabled),
            "thuecloud.api_base_url": self.app_settings.thuecloud.api_base_url,
            "thuecloud.access_token": self.app_settings.thuecloud.access_token,
            "thuecloud.proxy_endpoint": self.app_settings.thuecloud.proxy_endpoint,
            "thuecloud.proxy_method": self.app_settings.thuecloud.proxy_method,
            "thuecloud.proxy_api_key": self.app_settings.thuecloud.proxy_api_key,
            "browser_window.mode": self.app_settings.browser_window.mode,
            "browser_window.scale": str(self.app_settings.browser_window.scale),
            "browser_window.screen_width": str(self.app_settings.browser_window.screen_width),
            "browser_window.screen_height": str(self.app_settings.browser_window.screen_height),
            "browser_window.margin": str(self.app_settings.browser_window.margin),
            "browser_window.gap": str(self.app_settings.browser_window.gap),
            "browser_window.min_width": str(self.app_settings.browser_window.min_width),
            "browser_window.min_height": str(self.app_settings.browser_window.min_height),
        }
        for key, entry in self.settings_entries.items():
            entry.delete(0, "end")
            entry.insert(0, values.get(key, ""))

    def close_settings_window(self):
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.destroy()
        self.settings_window = None
        self.settings_entries = {}

    def save_settings_from_ui(self):
        try:
            gpm_default_group_id = self.settings_entries["gpm.default_group_id"].get().strip()
            settings = AppSettings(
                gpm=GPMSettings(
                    api_base_url=self.settings_entries["gpm.api_base_url"].get().strip(),
                    profile_url_template=self.settings_entries["gpm.profile_url_template"].get().strip(),
                    default_group_id=int(gpm_default_group_id) if gpm_default_group_id else None,
                    default_page=int(self.settings_entries["gpm.default_page"].get().strip()),
                    default_per_page=int(self.settings_entries["gpm.default_per_page"].get().strip()),
                ),
                capmonster=CapMonsterSettings(
                    api_base_url=self.settings_entries["capmonster.api_base_url"].get().strip(),
                    api_key=self.settings_entries["capmonster.api_key"].get().strip(),
                ),
                thuecloud=ThueCloudSettings(
                    enabled=self.settings_entries["thuecloud.enabled"].get().strip().lower() in {"1", "true", "yes", "y", "on"},
                    api_base_url=self.settings_entries["thuecloud.api_base_url"].get().strip(),
                    access_token=self.settings_entries["thuecloud.access_token"].get().strip(),
                    proxy_endpoint=self.settings_entries["thuecloud.proxy_endpoint"].get().strip(),
                    proxy_method=self.settings_entries["thuecloud.proxy_method"].get().strip() or "GET",
                    proxy_api_key=self.settings_entries["thuecloud.proxy_api_key"].get().strip(),
                ),
                browser_window=BrowserWindowSettings(
                    mode=self.settings_entries["browser_window.mode"].get().strip() or "auto_grid",
                    scale=float(self.settings_entries["browser_window.scale"].get().strip()),
                    screen_width=int(self.settings_entries["browser_window.screen_width"].get().strip()),
                    screen_height=int(self.settings_entries["browser_window.screen_height"].get().strip()),
                    margin=int(self.settings_entries["browser_window.margin"].get().strip()),
                    gap=int(self.settings_entries["browser_window.gap"].get().strip()),
                    min_width=int(self.settings_entries["browser_window.min_width"].get().strip()),
                    min_height=int(self.settings_entries["browser_window.min_height"].get().strip()),
                ),
            )
        except ValueError as exc:
            messagebox.showerror("Settings", f"Invalid value: {exc}")
            return

        if not settings.gpm.api_base_url or not settings.gpm.profile_url_template:
            messagebox.showerror("Settings", "GPM settings cannot be empty")
            return
        if not settings.capmonster.api_base_url:
            messagebox.showerror("Settings", "CapMonster API Base URL cannot be empty")
            return

        save_settings(settings)
        self.reload_app_settings()
        print("Settings saved and reloaded")
        messagebox.showinfo("Settings", "Saved settings successfully")

    def build_home_tab(self):
        parent = self.tab_frames["Home"]
        create_bar = self.tool_bar(parent)
        tk.Label(create_bar, text="Name", bg="#ffffff").pack(side="left", padx=(8, 4), pady=6)
        self.name_entry = tk.Entry(create_bar, width=20, relief="solid", borderwidth=1)
        self.name_entry.pack(side="left", pady=6)
        tk.Label(create_bar, text="Qty", bg="#ffffff").pack(side="left", padx=(10, 4), pady=6)
        self.qty_entry = tk.Entry(create_bar, width=6, relief="solid", borderwidth=1)
        self.qty_entry.insert(0, "1")
        self.qty_entry.pack(side="left", pady=6)
        self.tool_button(create_bar, "Create", self.create_profiles, "#4f46e5", "#ffffff").pack(side="left", padx=8, pady=6)
        self.tool_button(create_bar, "Load Groups", self.load_groups, "#0ea5e9", "#ffffff").pack(side="left", padx=4, pady=6)
        self.tool_button(create_bar, "Start", self.start, "#16a34a", "#ffffff").pack(side="left", padx=4, pady=6)
        self.tool_button(create_bar, "Delete Selected", self.delete_selected, "#dc2626", "#ffffff").pack(side="left", padx=4, pady=6)
        self.tool_button(create_bar, "Stop", self.stop, "#f59e0b", "#ffffff").pack(side="left", padx=4, pady=6)

        content = tk.Frame(parent, bg="#f3f4f7")
        content.pack(fill="both", expand=True)

        left = self.section(content)
        left.pack(side="left", fill="both", expand=False, padx=(0, 8))
        right = self.section(content)
        right.pack(side="left", fill="both", expand=True)

        tk.Label(left, text="Groups", bg="#ffffff", fg="#111827", font=("Tahoma", 10, "bold")).pack(anchor="w", padx=8, pady=8)
        self.group_list = tk.Listbox(left, exportselection=False, relief="flat", bg="#ffffff", activestyle="none")
        self.group_list.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.group_list.bind("<<ListboxSelect>>", self.on_group_select)

        head = tk.Frame(right, bg="#ffffff")
        head.pack(fill="x")
        tk.Label(head, text="Profiles", bg="#ffffff", fg="#111827", font=("Tahoma", 10, "bold")).pack(side="left", padx=8, pady=8)
        self.select_all_var = tk.BooleanVar()
        tk.Checkbutton(head, text="All", bg="#ffffff", variable=self.select_all_var, command=self.toggle_all).pack(side="right", padx=8)

        wrap = tk.Frame(right, bg="#ffffff")
        wrap.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.profile_canvas = tk.Canvas(wrap, bg="#ffffff", highlightthickness=0)
        self.profile_canvas.pack(side="left", fill="both", expand=True)
        scroll = tk.Scrollbar(wrap, orient="vertical", command=self.profile_canvas.yview)
        scroll.pack(side="right", fill="y")
        self.profile_canvas.configure(yscrollcommand=scroll.set)
        self.profile_frame = tk.Frame(self.profile_canvas, bg="#ffffff")
        self.profile_canvas.create_window((0, 0), window=self.profile_frame, anchor="nw")
        self.profile_frame.bind("<Configure>", lambda e: self.profile_canvas.configure(scrollregion=self.profile_canvas.bbox("all")))

    def build_change_tab(self):
        self.build_account_workflow_tab(
            "Change",
            "change",
            self.start_change,
            default_passnew="",
            default_threads="3",
            default_delay="1",
            import_hint="Format: email|passemail|refresh_token|client_id",
        )

    def build_reg_grok_tab(self):
        reg_grok_tab.build_tab(self)

    def build_reg_capcut_tab(self):
        reg_capcut_tab.build_tab(self)

    def build_change_mail_tab(self):
        self.build_account_workflow_tab(
            "Change Mail",
            "change_mail",
            self.start_change_mail,
            default_threads="3",
            default_delay="1",
            show_workflow_controls=True,
            show_passnew_input=False,
            table_columns=("pick", "user", "pass", "api", "status"),
            import_label="Import Mail",
            import_hint="Format: email|pass|email|passmail|refresh_token|client_id",
        )

    def build_checkdate_tab(self):
        self.build_account_workflow_tab(
            "checkdate",
            "checkdate",
            self.start_checkdate,
            default_threads="3",
            default_delay="1",
            show_workflow_controls=True,
            show_passnew_input=False,
            table_columns=("pick", "user", "pass", "status", "date", "plan", "days", "cancel"),
            import_label="Import Accounts",
            import_hint="Format: email|pass",
        )

    def build_account_workflow_tab(self, tab_name, attr_prefix, start_command, default_passnew="", default_threads="3", default_delay="1", show_table=True, pass_label="Pass New", show_load_export=True, import_label="Import Accounts", show_workflow_controls=True, table_columns=None, import_hint=None, show_passnew_input=True):
        parent = self.tab_frames[tab_name]
        bar = self.tool_bar(parent)
        self.tool_button(bar, import_label, self.import_accounts, "#0ea5e9", "#ffffff").pack(side="left", padx=8, pady=6)
        self.tool_button(bar, "Import Proxy", self.import_proxies, "#0891b2", "#ffffff").pack(side="left", padx=4, pady=6)
        if show_load_export:
            self.tool_button(bar, "Load Accounts", self.load_accounts, "#2563eb", "#ffffff").pack(side="left", padx=4, pady=6)
            self.tool_button(bar, "Export Acc", self.export_accounts, "#16a34a", "#ffffff").pack(side="left", padx=4, pady=6)
        if show_workflow_controls:
            if show_passnew_input:
                tk.Label(bar, text=pass_label, bg="#ffffff").pack(side="left", padx=(12, 4), pady=6)
                passnew_entry = tk.Entry(bar, width=24, relief="solid", borderwidth=1)
                passnew_entry.insert(0, default_passnew)
                passnew_entry.pack(side="left", pady=6)
                setattr(self, f"{attr_prefix}_passnew_entry", passnew_entry)
            tk.Label(bar, text="Threads", bg="#ffffff").pack(side="left", padx=(12, 4), pady=6)
            threads_entry = tk.Entry(bar, width=6, relief="solid", borderwidth=1)
            threads_entry.insert(0, default_threads)
            threads_entry.pack(side="left", pady=6)
            setattr(self, f"{attr_prefix}_threads_entry", threads_entry)
            tk.Label(bar, text="Delay", bg="#ffffff").pack(side="left", padx=(12, 4), pady=6)
            delay_entry = tk.Entry(bar, width=6, relief="solid", borderwidth=1)
            delay_entry.insert(0, default_delay)
            delay_entry.pack(side="left", pady=6)
            setattr(self, f"{attr_prefix}_delay_entry", delay_entry)
            self.tool_button(bar, "Start", start_command, "#4f46e5", "#ffffff").pack(side="left", padx=4, pady=6)
            self.tool_button(bar, "Stop", self.stop, "#dc2626", "#ffffff").pack(side="left", padx=4, pady=6)
        elif tab_name == "Change Mail":
            tk.Label(bar, text="Format: email|pass|email|passmail|refresh_token|client_id", bg="#ffffff", fg="#475569").pack(side="left", padx=12, pady=6)
        if import_hint:
            tk.Label(bar, text=import_hint, bg="#ffffff", fg="#475569").pack(side="left", padx=12, pady=6)

        if not show_table:
            status = tk.Label(parent, text="Loaded: 0 accounts | Created: 0 | Failed: 0 | Proxy: 0", bg="#f3f4f7", fg="#111827", font=("Tahoma", 10, "bold"))
            status.pack(anchor="w", padx=4, pady=(4, 0))
            setattr(self, f"{attr_prefix}_status_label", status)
            return

        table_wrap = self.section(parent)
        table_wrap.pack(fill="both", expand=True)
        table_wrap.grid_rowconfigure(0, weight=1)
        table_wrap.grid_columnconfigure(0, weight=1)
        cols = table_columns or ("pick", "user", "pass", "passmail", "passnew", "status", "api")
        account_table = ttk.Treeview(table_wrap, columns=cols, show="headings", style="Tool.Treeview", selectmode="extended")
        widths = {
            "pick": 60,
            "user": 180,
            "email": 220,
            "pass": 160,
            "passmail": 220,
            "passnew": 180,
            "status": 120,
            "api": 420,
            "plan": 160,
            "date": 120,
            "expiry": 220,
            "days": 90,
            "cancel": 120,
        }
        for col in cols:
            account_table.heading(col, text=col.upper(), command=lambda c=col: self.sort_accounts(c))
            account_table.column(col, width=widths.get(col, 160), anchor="w")
        account_table.tag_configure("fail", background="#fecdd3", foreground="#7f1d1d")
        account_table.tag_configure("true", background="#dcfce7", foreground="#166534")
        account_table.tag_configure("normal", background="#ffffff", foreground="#111827")
        account_table.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=(8, 0))
        v_scroll = ttk.Scrollbar(table_wrap, orient="vertical", command=account_table.yview)
        v_scroll.grid(row=0, column=1, sticky="ns", padx=(0, 8), pady=(8, 0))
        h_scroll = ttk.Scrollbar(table_wrap, orient="horizontal", command=account_table.xview)
        h_scroll.grid(row=1, column=0, sticky="ew", padx=(8, 0), pady=(0, 8))
        account_table.configure(yscrollcommand=v_scroll.set, xscrollcommand=h_scroll.set)
        account_table.bind("<<TreeviewSelect>>", self.on_account_select)
        account_table.bind("<Control-a>", self.select_all_accounts)
        account_table.bind("<Control-A>", self.select_all_accounts)
        account_table.bind("<space>", self.toggle_selected_accounts)
        account_table.bind("<Double-1>", self.begin_edit_account)
        account_table.bind("<Button-1>", self.on_account_click)
        account_table.bind("<ButtonPress-1>", self.start_drag_select, add="+")
        account_table.bind("<B1-Motion>", self.drag_select)
        account_table.bind("<MouseWheel>", self.on_account_mousewheel)
        account_table.bind("<Shift-MouseWheel>", self.on_account_shift_mousewheel)
        self.account_tables[tab_name] = account_table
        self.account_table = account_table

    def load_groups(self):
        threading.Thread(target=self.fetch_groups, daemon=True).start()

    def fetch_groups(self):
        try:
            self.groups = self.orch.get_profile_groups()
            self.all_profiles = self.orch.get_all_profiles().profiles
            self.root.after(0, self.render_groups)
            print(f"Loaded {len(self.groups)} groups")
            print(f"Loaded {len(self.all_profiles)} profiles")
        except Exception as e:
            print(f"Error: {e}")

    def render_groups(self):
        self.group_list.delete(0, "end")
        self.group_list.insert("end", "All")
        for g in self.groups:
            self.group_list.insert("end", f"{g.id} - {g.name}")
        self.group_list.selection_set(0)
        self.on_group_select()

    def on_group_select(self, _=None):
        sel = self.group_list.curselection()
        if not sel:
            return
        index = sel[0]
        if index == 0:
            self.profiles = list(self.all_profiles)
        else:
            group = self.groups[index - 1]
            self.profiles = [p for p in self.all_profiles if str(p.group_id) == str(group.id)]
        self.render_profiles()
        print(f"Loaded {len(self.profiles)} profiles")

    def render_profiles(self):
        for widget in self.profile_frame.winfo_children():
            widget.destroy()
        self.profile_vars = []
        self.select_all_var.set(False)
        for p in self.profiles:
            row = tk.Frame(self.profile_frame, bg="#ffffff")
            row.pack(fill="x", anchor="w")
            var = tk.BooleanVar()
            self.profile_vars.append(var)
            tk.Checkbutton(row, text=f"{p.id} - {p.name}", bg="#ffffff", variable=var, anchor="w").pack(fill="x", padx=4, pady=1)

    def toggle_all(self):
        value = self.select_all_var.get()
        for var in self.profile_vars:
            var.set(value)

    def selected_group_name(self):
        sel = self.group_list.curselection()
        if not sel or sel[0] == 0:
            return "All"
        return self.groups[sel[0] - 1].name

    def create_profiles(self):
        try:
            qty = max(1, int(self.qty_entry.get().strip() or "1"))
        except ValueError:
            messagebox.showinfo("Info", "Qty must be a number")
            return
        if not self.group_list.curselection() or self.group_list.curselection()[0] == 0:
            messagebox.showinfo("Info", "Select a group")
            return
        name = self.name_entry.get().strip() or "Profile"
        threading.Thread(target=self.do_create_profiles, args=(name, qty), daemon=True).start()

    def do_create_profiles(self, name, qty):
        try:
            group_name = self.selected_group_name()
            for i in range(qty):
                profile_name = f"{name} {i + 1}" if qty > 1 else name
                payload = self.build_gpm_profile_payload(profile_name, group_name)
                profile = self.orch.create_profile(payload)
                print(f"Created: {profile.id} - {profile.name}")
            self.fetch_groups()
        except Exception as e:
            print(f"Error: {e}")

    def start(self):
        if self.task_thread and self.task_thread.is_alive():
            return
        selected = [p for p, v in zip(self.profiles, self.profile_vars) if v.get()]
        if not selected:
            messagebox.showinfo("Info", "Tick profiles")
            return
        self.task_thread = threading.Thread(target=self.run, args=(selected,), daemon=True)
        self.task_thread.start()

    def run(self, profiles):
        total = len(profiles)
        for i, profile in enumerate(profiles):
            try:
                result = self.orch.start_profile(profile_id=profile.id, window_index=i, total_windows=total)
                print(f"Started: {profile.id} - {profile.name} - {result.success}")
            except Exception as e:
                print(f"Error: {profile.id} - {e}")

    def delete_selected(self):
        selected = [p for p, v in zip(self.profiles, self.profile_vars) if v.get()]
        if not selected:
            messagebox.showinfo("Info", "Tick profiles")
            return
        threading.Thread(target=self.do_delete_selected, args=(selected,), daemon=True).start()

    def do_delete_selected(self, profiles):
        for profile in profiles:
            try:
                self.orch.delete_profile(profile.id, mode=2)
                print(f"Deleted: {profile.id} - {profile.name}")
            except Exception as e:
                print(f"Error: {profile.id} - {e}")
        self.fetch_groups()

    def import_accounts(self):
        path = filedialog.askopenfilename(
            title="Open account.txt",
            initialdir=str(Path.cwd()),
            initialfile="account.txt",
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
        )
        if not path:
            return
        self.accounts_path = Path(path)
        self.load_accounts()

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
        self.render_accounts()
        print(f"Loaded {len(self.proxies)} proxies from {self.proxy_path.name}")
        if skipped:
            print(f"Skipped {skipped} invalid proxy lines")

    def load_accounts(self):
        if not self.accounts_path.exists():
            messagebox.showinfo("Info", f"File not found: {self.accounts_path}")
            return
        try:
            self.accounts = []
            seen = set()
            reg_grok_format = self.current_tab == "Reg Grok"
            reg_capcut_format = self.current_tab == "Reg CapCut"
            change_mail_format = self.current_tab == "Change Mail"
            checkdate_format = self.current_tab == "checkdate"
            for line in self.accounts_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split("|")
                if checkdate_format:
                    min_parts = 2
                elif change_mail_format:
                    min_parts = 6
                else:
                    min_parts = 4
                if len(parts) < min_parts:
                    continue
                if reg_grok_format:
                    account = reg_grok_tab.parse_mail_api_line(line)
                    if account is None:
                        continue
                    user = account["user"]
                    password = account["pass"]
                    passmail = account["passmail"]
                    api = account["api"]
                elif reg_capcut_format:
                    account = reg_capcut_tab.parse_mail_api_line(line)
                    if account is None:
                        continue
                    user = account["user"]
                    password = account["pass"]
                    passmail = account["passmail"]
                    api = account["api"]
                elif change_mail_format:
                    user = parts[0]
                    password = parts[1]
                    passmail = parts[3]
                    api = "|".join(parts[2:])
                elif checkdate_format:
                    user = parts[0]
                    password = parts[1]
                    passmail = ""
                    api = ""
                else:
                    user = parts[0]
                    password = ""
                    passmail = parts[1]
                    api = line
                key = (user, passmail)
                if key in seen:
                    continue
                seen.add(key)
                status = self.detect_status(api)
                self.accounts.append(
                    {
                        "picked": reg_grok_format or reg_capcut_format,
                        "user": user,
                        "email": user,
                        "pass": password,
                        "passmail": passmail,
                        "passnew": "",
                        "api": api,
                        "status": status,
                        "plan": "",
                        "expiry": "",
                        "date": "",
                        "days": "",
                        "cancel": "",
                    }
                )
            self.render_accounts()
            if reg_grok_format:
                format_name = "Reg Grok"
            elif reg_capcut_format:
                format_name = "Reg CapCut"
            elif change_mail_format:
                format_name = "Change Mail"
            elif checkdate_format:
                format_name = "checkdate"
            else:
                format_name = "Change"
            print(f"Loaded {len(self.accounts)} {format_name} accounts from {self.accounts_path.name}")
        except Exception as e:
            print(f"Error: {e}")

    def detect_status(self, value):
        text = str(value).strip().lower()
        if any(word in text for word in ("true", "success", "ok", "done")):
            return "true"
        if any(word in text for word in ("fail", "false", "error", "checkpoint")):
            return "fail"
        return ""

    def selected_workflow_group_name(self):
        sel = self.group_list.curselection()
        if sel and sel[0] != 0 and self.groups:
            return self.groups[sel[0] - 1].name
        for group in self.groups:
            if group.name.lower() == "grok":
                return group.name
        return "grok"

    def proxy_for_worker(self, index, max_threads):
        if not self.proxies:
            return None
        active_slots = min(max_threads, max(1, len(self.current_change_accounts)))
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

    def build_temp_profile_payload(self, account, group_name, index, start_url=SIGN_IN_URL, proxy=None):
        suffix = datetime.now().strftime("%H%M%S")
        raw_proxy = self.get_account_raw_proxy(account, proxy)
        profile_name = f'grok_{account["user"].split("@")[0]}_{index + 1}_{suffix}'
        return self.build_gpm_profile_payload(profile_name, group_name, raw_proxy=raw_proxy, startup_urls=start_url)

    def get_account_raw_proxy(self, account, proxy=None):
        if proxy is not None:
            print(f'Attached imported proxy for {account["user"]}: {self.mask_proxy(proxy.raw_proxy)}')
            return proxy.raw_proxy
        return self.get_thuecloud_raw_proxy(account)

    def get_thuecloud_raw_proxy(self, account):
        if not self.app_settings.thuecloud.enabled:
            return ""
        raw_proxy = ThueCloudClient(self.app_settings.thuecloud).get_raw_proxy()
        if not raw_proxy:
            raise RuntimeError("ThueCloud returned empty proxy")
        print(f'Attached ThueCloud proxy for {account["user"]}')
        return raw_proxy

    def mark_account_status(self, account, status, passnew=None):
        with self.account_lock:
            account["status"] = status
            if passnew is not None:
                account["passnew"] = passnew
        self.root.after(0, self.render_accounts)

    def render_accounts(self):
        self.close_editor()
        tables = list(self.account_tables.values()) or [self.account_table]
        for table in tables:
            for item in table.get_children():
                table.delete(item)
            cols = table["columns"]
            for i, acc in enumerate(self.accounts):
                tag = "normal"
                status = str(acc["status"]).lower()
                if "fail" in status:
                    tag = "fail"
                elif acc["status"] == "true":
                    tag = "true"
                elif str(acc["status"]) == "Active Subscription Found":
                    tag = "true"
                elif str(acc["status"]) in ("No Active Subscription", "false"):
                    tag = "fail"
                values_by_col = {
                    "pick": self.CHECKED if acc["picked"] else self.UNCHECKED,
                    "user": acc["user"],
                    "email": acc.get("email", acc["user"]),
                "pass": acc.get("pass", acc.get("passnew", "")),
                    "passmail": acc["passmail"],
                    "passnew": acc["passnew"],
                    "status": acc["status"],
                    "api": acc["api"],
                    "plan": acc.get("plan", ""),
                    "expiry": acc.get("expiry", ""),
                    "date": acc.get("date", ""),
                    "days": acc.get("days", ""),
                "cancel": acc.get("cancel", ""),
                    "userID": acc.get("userID", ""),
                }
                table.insert(
                    "",
                    "end",
                    iid=str(i),
                    values=tuple(values_by_col.get(col, "") for col in cols),
                    tags=(tag,),
                )
        if hasattr(self, "reg_grok_status_label"):
            self.reg_grok_status_label.configure(text=reg_grok_tab.status_text(self.accounts, len(self.proxies)))
        if hasattr(self, "reg_capcut_status_label"):
            self.reg_capcut_status_label.configure(text=reg_capcut_tab.status_text(self.accounts, len(self.proxies)))

    def on_account_select(self, _=None):
        return

    def select_all_accounts(self, _=None):
        if _ is not None and hasattr(_, "widget"):
            self.account_table = _.widget
        items = self.account_table.get_children()
        if items:
            self.account_table.selection_set(items)
            self.account_table.focus(items[0])
        return "break"

    def on_account_click(self, event):
        self.account_table = event.widget
        row_id = self.account_table.identify_row(event.y)
        column_id = self.account_table.identify_column(event.x)
        if row_id and column_id == "#1":
            account = self.accounts[int(row_id)]
            account["picked"] = not account["picked"]
            self.render_accounts()
            return "break"

    def start_drag_select(self, event):
        self.account_table = event.widget
        column_id = self.account_table.identify_column(event.x)
        if column_id == "#1":
            self.drag_start_row = None
            return
        row_id = self.account_table.identify_row(event.y)
        self.drag_start_row = row_id or None

    def drag_select(self, event):
        self.account_table = event.widget
        if self.drag_start_row is None:
            return
        row_id = self.account_table.identify_row(event.y)
        if not row_id:
            return
        start = int(self.drag_start_row)
        end = int(row_id)
        lo, hi = sorted((start, end))
        items = [str(i) for i in range(lo, hi + 1)]
        self.account_table.selection_set(items)

    def on_account_mousewheel(self, event):
        self.account_table = event.widget
        self.account_table.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def on_account_shift_mousewheel(self, event):
        self.account_table = event.widget
        self.account_table.xview_scroll(int(-event.delta / 120), "units")
        return "break"

    def toggle_selected_accounts(self, _=None):
        if _ is not None and hasattr(_, "widget"):
            self.account_table = _.widget
        for item in self.account_table.selection():
            account = self.accounts[int(item)]
            account["picked"] = not account["picked"]
        self.render_accounts()
        return "break"

    def begin_edit_account(self, event):
        self.account_table = event.widget
        row_id = self.account_table.identify_row(event.y)
        column_id = self.account_table.identify_column(event.x)
        if not row_id:
            return
        column_index = int(column_id.replace("#", "")) - 1
        columns = self.account_table["columns"]
        if column_index < 0 or column_index >= len(columns) or columns[column_index] != "passnew":
            return
        if self.editor is not None:
            self.editor.destroy()
        x, y, width, height = self.account_table.bbox(row_id, column_id)
        value = self.accounts[int(row_id)]["passnew"]
        self.editor = tk.Entry(self.account_table)
        self.editor.place(x=x, y=y, width=width, height=height)
        self.editor.insert(0, value)
        self.editor.focus_set()
        self.editor.bind("<Return>", lambda e, rid=row_id: self.save_passnew(rid))
        self.editor.bind("<Escape>", lambda e: self.close_editor())
        self.editor.bind("<FocusOut>", lambda e, rid=row_id: self.save_passnew(rid))

    def save_passnew(self, row_id):
        if self.editor is None:
            return
        self.accounts[int(row_id)]["passnew"] = self.editor.get().strip()
        self.close_editor()
        self.render_accounts()

    def close_editor(self):
        if self.editor is not None:
            self.editor.destroy()
            self.editor = None

    def sort_accounts(self, column):
        if column == "pick":
            self.accounts.sort(key=lambda acc: (not acc["picked"], acc["user"].lower()))
        else:
            reverse = not self.sort_state.get(column, False)
            self.sort_state[column] = reverse
            self.accounts.sort(key=lambda acc: str(acc.get(column, "")).lower(), reverse=reverse)
        self.render_accounts()

    def export_accounts(self):
        if not self.accounts:
            messagebox.showinfo("Info", "No accounts")
            return
        reg_grok_format = self.current_tab == "Reg Grok"
        reg_capcut_format = self.current_tab == "Reg CapCut"
        change_mail_format = self.current_tab == "Change Mail"
        checkdate_format = self.current_tab == "checkdate"
        selected = [acc for acc in self.accounts if acc.get("picked")] if (reg_grok_format or reg_capcut_format) else [acc for acc in self.accounts if acc["picked"]]
        if not selected:
            messagebox.showinfo("Info", "Tick accounts to export")
            return
        if reg_grok_format:
            result = reg_grok_tab.export_accounts(selected)
            print(f'Exported Reg Grok success: {result["success_count"]} to {result["success_path"]}')
            print(f'Exported Reg Grok BackupPass: {result["backup_pass_count"]} to {result["backup_pass_path"]}')
            print(f'Exported Reg Grok error: {result["error_count"]} to {result["error_path"]}')
            return
        if reg_capcut_format:
            if self.current_tab == "Reg CapCut":
                path = reg_capcut_tab.export_registered_accounts_file(selected)
                print(f'Exported Reg CapCut accounts: {len(selected)} to {path}')
                return
            result = reg_capcut_tab.export_accounts(selected)
            print(f'Exported Reg CapCut success: {result["success_count"]} to {result["success_path"]}')
            print(f'Exported Reg CapCut BackupPass: {result["backup_pass_count"]} to {result["backup_pass_path"]}')
            print(f'Exported Reg CapCut error: {result["error_count"]} to {result["error_path"]}')
            return
        if checkdate_format:
            base_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
            export_dir = base_dir / "checkdate"
            export_dir.mkdir(parents=True, exist_ok=True)
            true_accounts = [acc for acc in selected if str(acc.get("status", "")).lower() == "true" and acc.get("date")]
            false_accounts = [acc for acc in selected if str(acc.get("status", "")).lower() != "true"]
            true_path = export_dir / "true.txt"
            false_path = export_dir / "false.txt"
            true_lines = [f'{acc["user"]}|{acc["pass"]}|{acc["date"]}' for acc in true_accounts]
            false_lines = [f'{acc["user"]}|{acc["pass"]}' for acc in false_accounts]
            true_path.write_text("\n".join(true_lines), encoding="utf-8")
            false_path.write_text("\n".join(false_lines), encoding="utf-8")
            print(f"Exported checkdate true: {len(true_lines)} to {true_path.resolve()}")
            print(f"Exported checkdate false: {len(false_lines)} to {false_path.resolve()}")
            return
        if change_mail_format:
            base_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
            export_dir = base_dir / "changemail"
            export_dir.mkdir(parents=True, exist_ok=True)
            true_accounts = [acc for acc in selected if str(acc.get("status", "")).lower() == "true"]
            false_accounts = [acc for acc in selected if str(acc.get("status", "")).lower() != "true"]
            true_path = export_dir / "true.txt"
            false_path = export_dir / "false.txt"
            true_lines = [acc["api"] for acc in true_accounts]
            false_lines = [f'{acc["user"]}|{acc["pass"]}|{acc["api"]}' for acc in false_accounts]
            true_path.write_text("\n".join(true_lines), encoding="utf-8")
            false_path.write_text("\n".join(false_lines), encoding="utf-8")
            print(f"Exported changemail true: {len(true_lines)} to {true_path.resolve()}")
            print(f"Exported changemail false: {len(false_lines)} to {false_path.resolve()}")
            return
        path = filedialog.asksaveasfilename(
            title="Save accounts",
            initialdir=str(Path.cwd()),
            initialfile="acc_export.txt",
            defaultextension=".txt",
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
        )
        if not path:
            return
        lines = [f'{acc["user"]}|{acc["pass"]}|{acc["passnew"]}|{acc["passmail"]}' for acc in selected]
        Path(path).write_text("\n".join(lines), encoding="utf-8")
        print(f"Exported {len(selected)} accounts to {Path(path).name}")

    def reg_grok_mail_parts(self, acc):
        return reg_grok_tab.mail_parts(acc)

    def format_reg_grok_oauth_export_line(self, acc):
        return reg_grok_tab.oauth_export_line(acc)

    def format_reg_grok_backup_pass_export_line(self, acc):
        return reg_grok_tab.backup_pass_export_line(acc)

    def write_reg_grok_result(self, account, success):
        result = reg_grok_tab.write_result(account, success, self.export_lock)
        if success:
            print(f'Reg Grok saved success: {account["user"]} -> {result["target"]} and {result["backup_pass_path"]}')
        else:
            print(f'Reg Grok saved error: {account["user"]} -> {result["target"]}')

    def reg_capcut_mail_parts(self, acc):
        return reg_capcut_tab.mail_parts(acc)

    def format_reg_capcut_oauth_export_line(self, acc):
        return reg_capcut_tab.oauth_export_line(acc)

    def format_reg_capcut_backup_pass_export_line(self, acc):
        return reg_capcut_tab.backup_pass_export_line(acc)

    def write_reg_capcut_result(self, account, success):
        result = reg_capcut_tab.write_result(account, success, self.export_lock)
        if success:
            print(f'Reg CapCut saved success: {account["user"]} -> {result["target"]} and {result["backup_pass_path"]}')
        else:
            print(f'Reg CapCut saved error: {account["user"]} -> {result["target"]}')

    def start_change(self):
        self.start_account_workflow(
            passnew_entry=self.change_passnew_entry,
            threads_entry=self.change_threads_entry,
            delay_entry=self.change_delay_entry,
            workflow_name="Change",
        )

    def start_reg_grok(self):
        self.start_account_workflow(
            passnew_entry=self.reg_grok_passnew_entry,
            threads_entry=self.reg_grok_threads_entry,
            delay_entry=self.reg_grok_delay_entry,
            workflow_name="Reg Grok",
        )

    def start_reg_capcut(self):
        self.start_account_workflow(
            passnew_entry=self.reg_capcut_passnew_entry,
            threads_entry=self.reg_capcut_threads_entry,
            delay_entry=self.reg_capcut_delay_entry,
            workflow_name="Reg CapCut",
        )

    def start_change_mail(self):
        self.start_account_workflow(
            passnew_entry=None,
            threads_entry=self.change_mail_threads_entry,
            delay_entry=self.change_mail_delay_entry,
            workflow_name="Change Mail",
        )

    def start_checkdate(self):
        self.start_account_workflow(
            passnew_entry=None,
            threads_entry=self.checkdate_threads_entry,
            delay_entry=self.checkdate_delay_entry,
            workflow_name="checkdate",
        )


    def start_account_workflow(self, passnew_entry, threads_entry, delay_entry, workflow_name):
        selected = [acc for acc in self.accounts if acc["picked"]]
        if not selected:
            messagebox.showinfo("Info", "Tick accounts")
            return
        passnew = passnew_entry.get().strip() if passnew_entry is not None else ""
        if passnew_entry is not None and not passnew:
            messagebox.showinfo("Info", "Enter Pass New")
            return
        try:
            max_threads = max(1, int(threads_entry.get().strip() or "1"))
        except ValueError:
            messagebox.showinfo("Info", "Threads must be a number")
            return
        try:
            launch_delay = max(0.0, float(delay_entry.get().strip() or "0"))
        except ValueError:
            messagebox.showinfo("Info", "Delay must be a number")
            return
        if self.task_thread and self.task_thread.is_alive():
            messagebox.showinfo("Info", "Workflow is running")
            return
        self.stop_event.clear()
        self.current_change_accounts = selected
        if passnew_entry is not None:
            for account in selected:
                account["passnew"] = passnew
        self.render_accounts()
        self.task_thread = threading.Thread(target=self.run_change, args=(selected, max_threads, launch_delay, workflow_name), daemon=True)
        self.task_thread.start()

    def run_change(self, accounts, max_threads, launch_delay, workflow_name="Change"):
        group_name = "capcut" if workflow_name == "Reg CapCut" else self.selected_workflow_group_name()
        semaphore = threading.Semaphore(max_threads)
        workers = []
        total_accounts = len(accounts)
        if self.proxies:
            print(f"Using {len(self.proxies)} imported proxies for {max_threads} threads")
            self.reset_loaded_proxy_ips()
        for index, account in enumerate(accounts):
            if self.stop_event.is_set():
                if account["status"] != "running":
                    self.mark_account_status(account, "stopped")
                print("Stop requested, no longer starting new threads")
                break
            worker = threading.Thread(
                target=self.run_change_worker,
                args=(account, group_name, index, total_accounts, semaphore, workflow_name, max_threads),
                daemon=True,
            )
            workers.append(worker)
            worker.start()
            if launch_delay > 0 and index < total_accounts - 1:
                waited = 0.0
                step = 0.1
                while waited < launch_delay:
                    if self.stop_event.is_set():
                        break
                    sleep_for = min(step, launch_delay - waited)
                    time.sleep(sleep_for)
                    waited += sleep_for
        for worker in workers:
            worker.join()
        if self.stop_event.is_set():
            for account in accounts:
                if account["status"] == "running":
                    continue
                if "fail" not in str(account["status"]).lower() and account["status"] != "true":
                    self.mark_account_status(account, "stopped")
            print(f"{workflow_name} workflow stopped")
        else:
            print(f"{workflow_name} workflow completed")
        self.current_change_accounts = []
        self.task_thread = None

    def run_change_worker(self, account, group_name, index, total, semaphore, workflow_name="Change", max_threads=1):
        with semaphore:
            profile = None
            session = None
            try:
                if self.stop_event.is_set():
                    self.mark_account_status(account, "stopped")
                    return
                self.mark_account_status(account, "running")
                if workflow_name == "Reg Grok":
                    start_url = SIGN_UP_URL
                elif workflow_name == "Reg CapCut":
                    start_url = CAPCUT_SIGN_UP_URL
                else:
                    start_url = SIGN_IN_URL
                proxy = self.proxy_for_worker(index, max_threads)
                if workflow_name == "Reg CapCut":
                    session = ChromiumSession(
                        resolve_chromium_152(self.app_settings.chromium_path),
                        stop_event=self.stop_event, raw_proxy=self.get_account_raw_proxy(account, proxy),
                        window_settings=self.app_settings.browser_window,
                        index=index % min(max_threads, total), total_windows=min(max_threads, total),
                    )
                    result = session
                    with self.active_change_profiles_lock:
                        self.active_change_profiles[account["user"]] = session
                else:
                    payload = self.build_temp_profile_payload(account, group_name, index, start_url=start_url, proxy=proxy)
                    profile = self.orch.create_profile(payload)
                    with self.active_change_profiles_lock:
                        self.active_change_profiles[account["user"]] = profile.id
                    print(f'Created temp profile: {profile.id} - {profile.name} for {account["user"]}')
                if self.stop_event.is_set():
                    self.mark_account_status(account, "stopped")
                    return
                if session is None:
                    result = self.orch.start_profile(
                        profile_id=profile.id,
                        window_index=index % min(max_threads, total),
                        total_windows=min(max_threads, total),
                    )
                    print(f'Started temp profile: {profile.id} - {result.success} for {account["user"]}')
                if self.stop_event.is_set():
                    self.mark_account_status(account, "stopped")
                    return
                workflow_context = {
                    "profile": profile,
                    "start_result": result,
                    "stop_event": self.stop_event,
                }
                if workflow_name == "Reg CapCut":
                    workflow_result = run_capcut_workflow(account, workflow_context)
                else:
                    workflow_context.update({"start_url": start_url, "workflow_name": workflow_name})
                    workflow_result = run_workflow(account, workflow_context)
                workflow_ok = bool(workflow_result)
                if self.stop_event.is_set():
                    self.mark_account_status(account, "stopped")
                    return
                if workflow_ok:
                    if workflow_name == "checkdate" and isinstance(workflow_result, dict):
                        with self.account_lock:
                            account["status"] = workflow_result.get("status", "No Active Subscription")
                            account["plan"] = workflow_result.get("plan", "")
                            account["expiry"] = workflow_result.get("expiry", "")
                            account["date"] = workflow_result.get("date", "")
                            account["days"] = workflow_result.get("days", "")
                            account["cancel"] = workflow_result.get("cancel", "")
                        self.root.after(0, self.render_accounts)
                    else:
                        self.mark_account_status(account, "true")
                    if workflow_name == "Reg Grok":
                        self.write_reg_grok_result(account, True)
                    elif workflow_name == "Reg CapCut":
                        self.write_reg_capcut_result(account, True)
                    print(f'Success: {account["user"]}')
                else:
                    self.mark_account_status(account, "change fail")
                    if workflow_name == "Reg Grok":
                        self.write_reg_grok_result(account, False)
                    elif workflow_name == "Reg CapCut":
                        self.write_reg_capcut_result(account, False)
                    print(f'Change failed: {account["user"]}')
            except (WorkflowInterrupted, CapCutWorkflowInterrupted) as e:
                self.mark_account_status(account, "stopped")
                print(f'Workflow stopped: {account["user"]} - {e}')
            except Exception as e:
                self.mark_account_status(account, f"change fail: {e}")
                if workflow_name == "Reg Grok":
                    self.write_reg_grok_result(account, False)
                elif workflow_name == "Reg CapCut":
                    self.write_reg_capcut_result(account, False)
                print(f'Error: {account["user"]} - {e}')
            finally:
                with self.active_change_profiles_lock:
                    self.active_change_profiles.pop(account["user"], None)
                if session is not None:
                    session.close()
                if profile is not None:
                    try:
                        self.orch.close_profile(profile.id)
                        print(f'Closed temp profile: {profile.id} for {account["user"]}')
                    except Exception as e:
                        print(f'Close profile error: {profile.id} - {account["user"]} - {e}')

    def stop(self):
        if not self.task_thread or not self.task_thread.is_alive():
            print("No workflow is running")
            return
        if not self.current_change_accounts:
            print("Stop is only supported for account workflow")
            return
        self.stop_event.set()
        to_close = []
        with self.active_change_profiles_lock:
            to_close = list(self.active_change_profiles.items())
        for _, profile_id in to_close:
            try:
                if isinstance(profile_id, ChromiumSession):
                    profile_id.close()
                else:
                    self.orch.close_profile(profile_id)
                print("Stop closed browser")
            except Exception as e:
                print(f"Stop close profile error: {profile_id} - {e}")
        print("Stop requested")

    def drain(self):
        while not self.q.empty():
            self.log.insert("end", self.q.get_nowait())
            self.log.see("end")
        self.root.after(100, self.drain)


def main():
    root = tk.Tk()
    root.title("Nguyen Dang Cap")
    root.geometry("1100x700")
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
