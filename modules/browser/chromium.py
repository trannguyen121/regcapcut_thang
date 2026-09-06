"""Disposable Chromium 154 processes with an isolated incognito context."""

import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen

from core.settings import resolve_browser_window_layout


def resolve_chromium_154(configured=""):
    root = Path(__file__).resolve().parents[2]
    configured = Path(str(configured or "").strip().strip('"'))
    candidates = [
        configured / "chrome.exe" if configured.is_dir() else configured,
        Path(sys.executable).resolve().parent / "chrome-154" / "chrome.exe",
        root / "chrome-154" / "chrome.exe",
        root / "dist" / "chrome-win" / "chrome.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise RuntimeError("Không tìm thấy Chromium 154. Chọn chrome.exe hoặc thư mục chrome-154 trong Settings.")


def resolve_installed_chrome():
    """Return the Google Chrome installed on this Windows machine."""
    if sys.platform != "win32":
        raise RuntimeError("Reg treo dùng Chrome cài trên máy hiện chỉ hỗ trợ Windows")
    import os
    candidates = (
        Path(os.environ.get("PROGRAMFILES", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise RuntimeError("Không tìm thấy Google Chrome đã cài trên máy")


def parse_browser_proxy(raw):
    raw = str(raw or "").strip()
    if not raw:
        return None
    if "://" not in raw and "@" not in raw:
        parts = raw.split(":", 3)
        if len(parts) == 4:
            host, port, username, password = parts
            raw = f"http://{host}:{port}"
        else:
            username = password = None
    else:
        username = password = None
    try:
        parsed = urlsplit(raw if "://" in raw else "http://" + raw)
        if parsed.scheme not in ("http", "https", "socks5") or not parsed.hostname or not parsed.port:
            raise ValueError()
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError()
        username = unquote(parsed.username) if parsed.username is not None else username
        password = unquote(parsed.password or "") if parsed.username is not None else password
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        result = {"server": f"{parsed.scheme}://{host}:{parsed.port}"}
        if username is not None:
            if parsed.scheme == "socks5":
                raise ValueError()
            result.update(username=username, password=password or "")
        return result
    except ValueError:
        raise RuntimeError("Proxy không hợp lệ; dùng host:port, host:port:user:pass hoặc URL HTTP(S). SOCKS5 chỉ hỗ trợ không mật khẩu.") from None


class ChromiumSession:
    def __init__(self, browser, *, stop_event=None, raw_proxy="", window_settings=None,
                 index=0, total_windows=1, headless=False, expected_major="154",
                 reuse_startup_context=False):
        self.proxy = parse_browser_proxy(raw_proxy)
        self.incognito = True
        self.reuse_startup_context = bool(reuse_startup_context)
        self.success = False
        self._lock = threading.RLock()
        self._temp = tempfile.TemporaryDirectory(prefix="capcut-chromium-")
        self.profile_dir = Path(self._temp.name).resolve()
        self.process = None
        self._closed = False
        # Keep Chrome's normal defaults. Only isolation, incognito and the CDP
        # endpoint required by the workflow are always enabled.
        args = [
            str(browser),
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            f"--user-data-dir={self.profile_dir}",
            "--incognito",
        ]
        if headless:
            args.append("--headless=new")
        if self.proxy:
            args.append(f"--proxy-server={self.proxy['server']}")
        if window_settings is not None:
            layout = resolve_browser_window_layout(window_settings, window_index=index, total_windows=total_windows)
            args.extend([f"--window-position={layout.position}", f"--window-size={layout.size}"])
        args.append("about:blank")
        try:
            self.process = subprocess.Popen(args, cwd=str(Path(browser).parent),
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if stop_event is not None and stop_event.is_set():
                    raise RuntimeError("Đã dừng mở Chromium 154")
                if self.process.poll() is not None:
                    raise RuntimeError("Chromium 154 đã thoát trước khi sẵn sàng")
                try:
                    port = int((self.profile_dir / "DevToolsActivePort").read_text().splitlines()[0])
                    address = f"127.0.0.1:{port}"
                    with urlopen(f"http://{address}/json/version", timeout=0.5) as response:
                        version = json.load(response).get("Browser", "")
                except (OSError, ValueError, IndexError):
                    time.sleep(0.1)
                    continue
                actual_major = version.split("/")[-1].split(".")[0]
                if expected_major is not None and actual_major != str(expected_major):
                    raise RuntimeError(f"Cần Chromium {expected_major}, trình duyệt hiện tại là {version}")
                self.browser_version = version
                self.remote_debugging_address = self.browser_location = address
                self.success = True
                return
            raise RuntimeError("Chromium 154 không mở được cổng điều khiển")
        except BaseException:
            self.close()
            raise

    def poll(self):
        return self.process.poll() if self.process is not None else 0

    def wait(self, timeout=None):
        return self.process.wait(timeout=timeout) if self.process is not None else 0

    def close(self):
        with self._lock:
            if self._closed:
                return
            if self.process is not None and self.process.poll() is None:
                if sys.platform == "win32":
                    # This PID belongs to this session; never close other Chrome instances.
                    subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
                else:
                    self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            # Only remove the unique directory allocated by TemporaryDirectory.
            if Path(self._temp.name).resolve() != self.profile_dir or self.profile_dir.parent != Path(tempfile.gettempdir()).resolve():
                raise RuntimeError("Unexpected Chrome temporary directory")
            # Windows may hold cache handles briefly after the process tree exits.
            for attempt in range(30):
                try:
                    self._temp.cleanup()
                    break
                except PermissionError:
                    if attempt == 29:
                        raise
                    time.sleep(0.1)
            self._closed = True

    terminate = close
    kill = close


def open_workflow_page(browser, context):
    session = (context or {}).get("start_result")
    if getattr(session, "reuse_startup_context", False):
        if session.proxy and session.proxy.get("username"):
            raise RuntimeError(
                "Reg treo dùng Chrome hệ thống không hỗ trợ proxy có username/password"
            )
        browser_context = next(
            (candidate for candidate in browser.contexts if candidate.pages),
            browser.contexts[0] if browser.contexts else None,
        )
        if browser_context is None:
            raise RuntimeError("Không tìm thấy context ẩn danh khởi động của Chrome")
        return browser_context.pages[0] if browser_context.pages else browser_context.new_page()
    if getattr(session, "incognito", False):
        options = {"no_viewport": True}
        if session.proxy:
            options["proxy"] = session.proxy
        browser_context = browser.new_context(**options)
        page = browser_context.new_page()
        # Remove startup tabs so closing the workflow window also ends hold mode.
        for existing in list(browser.contexts):
            if existing != browser_context:
                for tab in list(existing.pages):
                    tab.close()
        return page
    browser_context = browser.contexts[0] if browser.contexts else browser.new_context()
    return browser_context.pages[0] if browser_context.pages else browser_context.new_page()
