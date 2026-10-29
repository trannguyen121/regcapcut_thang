"""Run the original regcapcut v7.5 code with one hold-mode proxy patch."""

from __future__ import annotations

import importlib.abc
import importlib.util
import base64
import marshal
import os
import re
import select
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import types
import zlib
from pathlib import Path
from urllib.parse import unquote, urlsplit


LEGACY_ENTRY_NAME = "reg_capcut_tab.pyc"
LEGACY_PYZ_NAME = "PYZ.pyz"
MODERN_WORKFLOW_NAME = "capcut_workflow.py"
LEGACY_PATCH_VERSION = "7.5.507"
_PENDING_PROXY_KEY = "_v75_pending_proxy"
_WINDOW_SLOT_KEY = "_v75_window_slot"
_WINDOW_TOTAL_KEY = "_v75_window_total"
_BROWSER_CHOICE_BUNDLED = "chrome154"
_BROWSER_CHOICE_SYSTEM = "system"


class PauseAwareEvent:
    """A stop Event that cooperatively blocks workers while paused."""

    def __init__(self, stop_event=None, pause_event=None):
        self._stop_event = stop_event or threading.Event()
        self._pause_event = pause_event or threading.Event()

    def set(self):
        # Stop must always wake a paused worker.
        self._pause_event.clear()
        self._stop_event.set()

    def clear(self):
        self._stop_event.clear()
        self._pause_event.clear()

    def pause(self):
        if not self._stop_event.is_set():
            self._pause_event.set()

    def resume(self):
        self._pause_event.clear()

    def is_paused(self):
        return self._pause_event.is_set() and not self._stop_event.is_set()

    def stop_requested(self):
        return self._stop_event.is_set()

    def _wait_while_paused(self):
        while self._pause_event.is_set() and not self._stop_event.is_set():
            self._stop_event.wait(0.1)

    def is_set(self):
        self._wait_while_paused()
        return self._stop_event.is_set()

    def wait(self, timeout=None):
        if self._stop_event.is_set():
            return True
        remaining = None if timeout is None else max(0.0, float(timeout))
        while True:
            if self._stop_event.is_set():
                return True
            if self._pause_event.is_set():
                self._wait_while_paused()
                continue
            if remaining is not None and remaining <= 0:
                return False
            wait_slice = 0.1 if remaining is None else min(0.1, remaining)
            started = time.monotonic()
            if self._stop_event.wait(wait_slice):
                return True
            if remaining is not None:
                remaining -= time.monotonic() - started


_DISCOUNTED_22000_PRICE_PATTERN = re.compile(
    r"^\s*(?:₫\s*22[.,\s]?000|22[.,\s]?000\s*(?:₫|đ|d|vnd))\s*$",
    re.I,
)
_MONTHLY_BILLING_PATTERN = re.compile(
    r"(?:\bMonthly\b|Hàng\s+tháng|/\s*month\b|per\s+month\b|Mỗi\s+tháng)",
    re.I,
)
_CHECKOUT_DUE_22000_PATTERN = re.compile(
    r"(?:Due\s+today|Thanh\s+toán\s+hôm\s+nay)\s*:?[\s\S]{0,80}?"
    r"(?:₫\s*22[.,\s]?000|22[.,\s]?000\s*(?:₫|đ|d|vnd))(?!\w)",
    re.I,
)
_CHECKOUT_FIRST_MONTH_22000_PATTERN = re.compile(
    r"(?:First[-\s]*time\s+discount|Giảm\s+giá\s+lần\s+đầu)\s*:?[\s\S]{0,40}?"
    r"(?:₫\s*22[.,\s]?000|22[.,\s]?000\s*(?:₫|đ|d|vnd))\s*/?\s*month",
    re.I,
)
_CHECKOUT_URL_PATTERN = re.compile(
    r"(?:cashier|checkout|payin|pipopay|payment)",
    re.I,
)


def _has_discounted_22000_price(text) -> bool:
    """Accept only the explicit 22,000 VND offer, never 222,000 or 0 VND."""
    return bool(_DISCOUNTED_22000_PRICE_PATTERN.fullmatch(str(text or "")))


def _is_monthly_price_context(text) -> bool:
    """Recognize both the old ``Monthly`` label and the new ``/month`` UI."""
    return bool(_MONTHLY_BILLING_PATTERN.search(str(text or "")))


def _checkout_due_today_is_22000(text) -> bool:
    """Verify checkout price despite CapCut's animated Due-today digits."""
    value = str(text or "")
    return bool(
        _CHECKOUT_DUE_22000_PATTERN.search(value)
        or _CHECKOUT_FIRST_MONTH_22000_PATTERN.search(value)
    )


def _is_checkout_url(url) -> bool:
    """Distinguish the payment target from unrelated pages opened by CapCut."""
    return bool(_CHECKOUT_URL_PATTERN.search(str(url or "")))


def application_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_root() -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    return Path(bundle_root).resolve() if bundle_root else application_root()


def _browser_choice_path() -> Path:
    """Store the GUI browser choice per Windows user, including portable builds."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else application_root()
    return base / "RegCapCut" / "browser_choice.txt"


def _read_browser_choice() -> str:
    try:
        value = _browser_choice_path().read_text(encoding="utf-8").strip().lower()
    except OSError:
        value = ""
    return value if value in {_BROWSER_CHOICE_BUNDLED, _BROWSER_CHOICE_SYSTEM} else _BROWSER_CHOICE_BUNDLED


def _write_browser_choice(value: str) -> None:
    if value not in {_BROWSER_CHOICE_BUNDLED, _BROWSER_CHOICE_SYSTEM}:
        return
    try:
        path = _browser_choice_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    except OSError as exc:
        print(f"[BROWSER] Could not save Chrome choice: {exc}")


def _chrome_executable(candidate) -> Path | None:
    value = str(candidate or "").strip().strip('"')
    if not value:
        return None
    path = Path(value).expanduser()
    executable = path / "chrome.exe" if path.is_dir() else path
    return executable.resolve() if executable.is_file() else None


def _find_system_chrome(configured_path=None) -> Path | None:
    """Find the normal Google Chrome installed on the current Windows host."""
    candidates = []
    configured = _chrome_executable(configured_path)
    if configured is not None and "chrome-154" not in str(configured).lower():
        candidates.append(configured)
    path_chrome = shutil.which("chrome.exe") or shutil.which("chrome")
    if path_chrome:
        candidates.append(Path(path_chrome))
    for environment_name, relative_path in (
        ("PROGRAMFILES", "Google/Chrome/Application/chrome.exe"),
        ("PROGRAMFILES(X86)", "Google/Chrome/Application/chrome.exe"),
        ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
    ):
        base = os.environ.get(environment_name)
        if base:
            candidates.append(Path(base) / relative_path)
    if os.name == "nt":
        try:
            import winreg

            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(
                        hive,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                    ) as key:
                        candidates.append(Path(winreg.QueryValue(key, None)))
                except OSError:
                    continue
        except (ImportError, OSError):
            pass
    for candidate in candidates:
        executable = _chrome_executable(candidate)
        if executable is not None:
            return executable
    return None


def legacy_asset(name: str) -> Path:
    bundled = resource_root() / "legacy" / name
    if bundled.is_file():
        return bundled
    return application_root() / "analysis" / "regcapcut_v7_5" / name


def modern_workflow_asset() -> Path:
    bundled = resource_root() / "modern" / MODERN_WORKFLOW_NAME
    if bundled.is_file():
        return bundled
    return application_root() / "modules" / "actions" / "capcut_workflow.py"


def load_modern_registration_workflow():
    """Load the v10.3 registration workflow without replacing v7.5 modules."""
    path = modern_workflow_asset()
    if not path.is_file():
        raise RuntimeError(f"Khong tim thay workflow dang ky v10.3: {path}")
    module = types.ModuleType("regcapcut_v103_workflow")
    module.__file__ = str(path)
    module.__package__ = ""
    exec(compile(path.read_text(encoding="utf-8-sig"), str(path), "exec"), module.__dict__)
    return module.run_capcut_workflow


class LegacyPyzImporter(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Import the unmodified Python modules embedded in the v7.5 PYZ."""

    def __init__(self, archive: Path):
        self.archive = archive
        with archive.open("rb") as stream:
            if stream.read(4) != b"PYZ\0":
                raise RuntimeError("PYZ v7.5 khong hop le")
            stream.read(4)
            toc_offset = struct.unpack("!i", stream.read(4))[0]
            stream.seek(toc_offset)
            self.toc = dict(marshal.load(stream))

    def find_spec(self, fullname, path=None, target=None):
        entry = self.toc.get(fullname)
        if entry is None or entry[0] not in (0, 1, 3):
            return None
        return importlib.util.spec_from_loader(fullname, self, is_package=entry[0] in (1, 3))

    def create_module(self, spec):
        return None

    def _native_package(self, fullname: str) -> Path:
        native_root = Path(os.environ.get("REGCAPCUT_V75_INTERNAL_ROOT", resource_root()))
        relative_parts = fullname.split(".")
        bundled = native_root.joinpath(*relative_parts)
        if bundled.exists():
            return bundled
        # Source-mode smoke tests use native extensions from the active Python
        # environment. Frozen builds resolve the first bundled path above.
        for entry in sys.path:
            try:
                candidate = Path(entry).joinpath(*relative_parts)
            except (TypeError, ValueError):
                continue
            if candidate.exists():
                return candidate
        return bundled

    def get_resource_reader(self, fullname):
        package = self._native_package(fullname)
        return FilesystemResourceReader(package) if package.is_dir() else None

    def exec_module(self, module):
        typecode, offset, length = self.toc[module.__name__]
        if typecode in (1, 3):
            native_package = self._native_package(module.__name__)
            if native_package.is_dir():
                module.__path__.append(str(native_package))
        if typecode == 3:
            return
        with self.archive.open("rb") as stream:
            stream.seek(offset)
            compressed = stream.read(length)
        code = marshal.loads(zlib.decompress(compressed))
        native_module = self._native_package(module.__name__)
        module.__file__ = str(native_module / "__init__.py" if typecode == 1 else native_module.with_suffix(".py"))
        exec(code, module.__dict__)


class FilesystemResourceReader(importlib.abc.ResourceReader):
    def __init__(self, package: Path):
        self.package = package

    def open_resource(self, resource):
        return (self.package / resource).open("rb")

    def resource_path(self, resource):
        return str(self.package / resource)

    def is_resource(self, name):
        return (self.package / name).is_file()

    def contents(self):
        return (item.name for item in self.package.iterdir())


def install_legacy_importer() -> LegacyPyzImporter:
    archive = legacy_asset(LEGACY_PYZ_NAME)
    if not archive.is_file():
        raise RuntimeError(f"Khong tim thay PYZ goc v7.5: {archive}")
    importer = LegacyPyzImporter(archive)
    sys.meta_path.insert(0, importer)
    return importer


def load_legacy_namespace() -> dict:
    path = legacy_asset(LEGACY_ENTRY_NAME)
    if not path.is_file():
        raise RuntimeError(f"Khong tim thay giao dien goc v7.5: {path}")
    namespace = {
        "__name__": "legacy_reg_capcut_tab",
        "__file__": str(path.with_suffix(".py")),
        "__package__": "",
    }
    exec(marshal.loads(path.read_bytes()), namespace)
    return namespace


def parse_proxy(raw_proxy: str) -> dict | None:
    raw = str(raw_proxy or "").strip()
    if not raw:
        return None
    username = password = None
    if "://" not in raw and "@" not in raw:
        parts = raw.split(":", 3)
        if len(parts) == 4:
            host, port, username, password = parts
            raw = f"http://{host}:{port}"
    parsed = urlsplit(raw if "://" in raw else "http://" + raw)
    if parsed.scheme not in ("http", "https", "socks5") or not parsed.hostname or not parsed.port:
        raise RuntimeError("Proxy khong hop le")
    if parsed.username is not None:
        username = unquote(parsed.username)
        password = unquote(parsed.password or "")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    result = {"server": f"{parsed.scheme}://{host}:{parsed.port}"}
    if username is not None:
        if parsed.scheme == "socks5":
            raise RuntimeError("SOCKS5 co mat khau khong duoc Chrome ho tro")
        result.update(username=username, password=password or "")
    return result


class DeferredProxyRelay:
    """HTTP CONNECT relay that changes from DIRECT to an upstream proxy."""

    def __init__(self):
        self._upstream = None
        self._lock = threading.RLock()
        self._connections = set()
        self._closed = False
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(64)
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()

    @property
    def server(self):
        return f"http://127.0.0.1:{self.port}"

    def activate(self, upstream):
        with self._lock:
            self._upstream = upstream
            active = list(self._connections)
        # Force Chrome to create fresh tunnels through the newly selected proxy.
        for connection in active:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active = list(self._connections)
        try:
            self._listener.close()
        except OSError:
            pass
        for connection in active:
            try:
                connection.close()
            except OSError:
                pass

    def _accept_loop(self):
        while not self._closed:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()

    def _track(self, connection):
        with self._lock:
            self._connections.add(connection)

    def _untrack(self, connection):
        with self._lock:
            self._connections.discard(connection)

    @staticmethod
    def _read_headers(connection):
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 65536:
            chunk = connection.recv(8192)
            if not chunk:
                break
            data += chunk
        return data

    @staticmethod
    def _target(value, default_port):
        value = value.strip()
        if value.startswith("["):
            host, _, tail = value[1:].partition("]")
            return host, int(tail[1:]) if tail.startswith(":") else default_port
        if ":" in value:
            host, port = value.rsplit(":", 1)
            return host, int(port)
        return value, default_port

    @staticmethod
    def _basic_auth(upstream):
        if upstream.get("username") is None:
            return ""
        token = base64.b64encode(
            f"{upstream['username']}:{upstream.get('password', '')}".encode()
        ).decode("ascii")
        return f"Proxy-Authorization: Basic {token}\r\n"

    @staticmethod
    def _relay(left, right):
        sockets = [left, right]
        while True:
            readable, _, _ = select.select(sockets, [], [], 30)
            if not readable:
                continue
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                (right if source is left else left).sendall(data)

    def _connect_http_upstream(self, upstream):
        parsed = urlsplit(upstream["server"])
        connection = socket.create_connection((parsed.hostname, parsed.port), timeout=20)
        if parsed.scheme == "https":
            connection = ssl.create_default_context().wrap_socket(connection, server_hostname=parsed.hostname)
        return connection

    @staticmethod
    def _connect_socks5(upstream, host, port):
        parsed = urlsplit(upstream["server"])
        connection = socket.create_connection((parsed.hostname, parsed.port), timeout=20)
        connection.sendall(b"\x05\x01\x00")
        if connection.recv(2) != b"\x05\x00":
            raise RuntimeError("SOCKS5 proxy rejected authentication method")
        encoded_host = host.encode("idna")
        connection.sendall(b"\x05\x01\x00\x03" + bytes([len(encoded_host)]) + encoded_host + struct.pack("!H", port))
        response = connection.recv(4)
        if len(response) != 4 or response[1] != 0:
            raise RuntimeError("SOCKS5 proxy connection failed")
        address_type = response[3]
        if address_type == 1:
            connection.recv(4)
        elif address_type == 3:
            connection.recv(connection.recv(1)[0])
        elif address_type == 4:
            connection.recv(16)
        connection.recv(2)
        return connection

    def _open_target(self, upstream, host, port):
        if upstream and urlsplit(upstream["server"]).scheme == "socks5":
            return self._connect_socks5(upstream, host, port), True
        if upstream:
            return self._connect_http_upstream(upstream), False
        return socket.create_connection((host, port), timeout=20), True

    def _handle_client(self, client):
        remote = None
        self._track(client)
        try:
            request = self._read_headers(client)
            if not request:
                return
            header_end = request.find(b"\r\n\r\n") + 4
            headers, remainder = request[:header_end], request[header_end:]
            first_line = headers.split(b"\r\n", 1)[0].decode("latin-1")
            method, target, version = first_line.split(" ", 2)
            with self._lock:
                upstream = dict(self._upstream) if self._upstream else None

            if method.upper() == "CONNECT":
                host, port = self._target(target, 443)
                remote, connected = self._open_target(upstream, host, port)
                self._track(remote)
                if upstream and not connected:
                    auth = self._basic_auth(upstream)
                    remote.sendall(
                        f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n{auth}Connection: keep-alive\r\n\r\n".encode("latin-1")
                    )
                    response = self._read_headers(remote)
                    client.sendall(response)
                    if b" 200 " not in response.split(b"\r\n", 1)[0]:
                        return
                else:
                    client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if remainder:
                    remote.sendall(remainder)
                self._relay(client, remote)
                return

            parsed_target = urlsplit(target)
            host_header = next(
                (line.split(b":", 1)[1].strip().decode("latin-1") for line in headers.split(b"\r\n") if line.lower().startswith(b"host:")),
                "",
            )
            host, port = self._target(parsed_target.netloc or host_header, 80)
            remote, connected = self._open_target(upstream, host, port)
            self._track(remote)
            if upstream and not connected:
                auth = self._basic_auth(upstream).encode("latin-1")
                outbound = headers[:-2] + auth + b"\r\n" + remainder
            else:
                path = parsed_target.path or "/"
                if parsed_target.query:
                    path += "?" + parsed_target.query
                outbound = f"{method} {path} {version}\r\n".encode("latin-1") + headers.split(b"\r\n", 1)[1] + remainder
            remote.sendall(outbound)
            self._relay(client, remote)
        except (OSError, ValueError, RuntimeError):
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
        finally:
            for connection in (client, remote):
                if connection is not None:
                    self._untrack(connection)
                    try:
                        connection.close()
                    except OSError:
                        pass


class RelayProcess:
    def __init__(self, process, relay):
        self._process = process
        self._relay = relay

    def poll(self):
        code = self._process.poll()
        if code is not None:
            self._relay.close()
        return code

    def wait(self, timeout=None):
        try:
            return self._process.wait(timeout=timeout)
        finally:
            self._relay.close()

    def terminate(self):
        self._relay.close()
        return self._process.terminate()

    def kill(self):
        self._relay.close()
        return self._process.kill()

    @property
    def pid(self):
        return self._process.pid


def open_pro_trial_after_registration(account, context, original_workflow, checkout_attempt=1):
    """Reload the registered tab and open the supported promotional Pro checkout."""
    workflow_globals = original_workflow.__globals__
    sync_playwright = workflow_globals["sync_playwright"]
    connect_browser = workflow_globals["_connect_browser"]
    cdp_url = workflow_globals["_cdp_url"](context)
    stop_event = (context or {}).get("stop_event")
    user = account["user"]
    retry_checkout = False

    with sync_playwright() as playwright:
        browser = connect_browser(playwright, cdp_url, user, stop_event)
        browser_context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = browser_context.pages[0] if browser_context.pages else browser_context.new_page()
        newly_opened_pages = []
        browser_context.on("page", lambda opened_page: newly_opened_pages.append(opened_page))

        def dismiss_blocking_overlays():
            dismiss_labels = (
                "Skip",
                "Bỏ qua",
                "Not now",
                "Maybe later",
                "No thanks",
                "Got it",
                "Đã hiểu",
            )
            dismissed = 0
            attempts_by_label = {}
            for _ in range(8):
                clicked = False
                for label in dismiss_labels:
                    if attempts_by_label.get(label, 0) >= 2:
                        continue
                    label_pattern = re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)
                    groups = (
                        page.get_by_role("button", name=label_pattern),
                        page.get_by_text(label_pattern, exact=True),
                    )
                    for candidates in groups:
                        for index in range(candidates.count()):
                            candidate = candidates.nth(index)
                            if not candidate.is_visible():
                                continue
                            try:
                                candidate.click(timeout=2000)
                            except Exception:
                                try:
                                    candidate.click(timeout=2000, force=True)
                                except Exception:
                                    candidate.evaluate("element => element.click()")
                            attempts_by_label[label] = attempts_by_label.get(label, 0) + 1
                            clicked = True
                            dismissed += 1
                            page.wait_for_timeout(500)
                            break
                        if clicked:
                            break
                    if clicked:
                        break
                if not clicked:
                    break
            if dismissed:
                print(f"[REG TREO][PRO] Dismissed {dismissed} onboarding popup(s) for {user}")

        print(f"[REG TREO][PRO] Reloading registered page for {user}")
        last_reload_error = None
        for attempt in range(1, 4):
            try:
                page.reload(wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(2000)
                dismiss_blocking_overlays()
                print(f"[REG TREO][PRO] Reload {attempt}/3 completed for {user}")
            except Exception as exc:
                last_reload_error = exc
                print(f"[REG TREO][PRO] Reload {attempt}/3 failed for {user}: {exc}")
        if last_reload_error is not None and not str(getattr(page, "url", "")).startswith("http"):
            raise RuntimeError(f"Khong reload duoc trang CapCut: {last_reload_error}")

        upgrade_pattern = re.compile(r"^\s*(Upgrade|Nâng cấp)\s*$", re.I)
        top_candidates = (
            page.locator("button").filter(has_text=re.compile(r"Upgrade|Nâng cấp", re.I)),
            page.get_by_role("button", name=upgrade_pattern),
            page.get_by_text(upgrade_pattern, exact=True),
        )
        top_upgrade = None
        for candidates in top_candidates:
            for index in range(candidates.count()):
                candidate = candidates.nth(index)
                if candidate.is_visible() and candidate.is_enabled():
                    top_upgrade = candidate
                    break
            if top_upgrade is not None:
                break
        if top_upgrade is None:
            raise RuntimeError("Khong tim thay nut Upgrade tren thanh tren cung")
        if top_upgrade.evaluate("element => element.tagName") != "BUTTON":
            parent_button = top_upgrade.locator("xpath=ancestor::button[1]")
            if parent_button.count() and parent_button.is_visible():
                top_upgrade = parent_button
        try:
            top_upgrade.click(timeout=5000)
        except Exception:
            # CapCut frequently leaves promotional/onboarding overlays above
            # the header after reload. Dispatch directly to the known Upgrade
            # control so those visual layers cannot swallow the click.
            try:
                top_upgrade.click(timeout=5000, force=True)
            except Exception:
                top_upgrade.evaluate("element => element.click()")
        print(f"[REG TREO][PRO] Opened Upgrade plans for {user}")
        page.wait_for_timeout(3000)

        # Some accounts receive a campaign splash before the pricing table.
        # Continue through it instead of treating the splash title "Pro" as
        # the actual subscription card.
        get_offer = page.get_by_role(
            "button", name=re.compile(r"^\s*Get offer\s*$", re.I)
        )
        if get_offer.count() and get_offer.first.is_visible():
            try:
                get_offer.first.click(timeout=5000)
            except Exception:
                get_offer.first.click(timeout=5000, force=True)
            print(f"[REG TREO][PRO] Continued through campaign offer for {user}")
            page.wait_for_timeout(3000)

        def find_promotional_pro_button():
            prices = page.get_by_text(_DISCOUNTED_22000_PRICE_PATTERN, exact=True)
            for index in range(prices.count()):
                price = prices.nth(index)
                if not price.is_visible():
                    continue
                card = price.locator(
                    "xpath=ancestor::div["
                    ".//*[normalize-space()='Pro'] and "
                    ".//button[normalize-space()='Upgrade' or "
                    "normalize-space()='Nâng cấp']][1]"
                )
                if not card.count() or not card.is_visible():
                    continue
                buttons = card.get_by_role("button", name=upgrade_pattern)
                for button_index in range(buttons.count()):
                    button = buttons.nth(button_index)
                    if button.is_visible() and button.is_enabled():
                        return button
            return None

        def click_promotional_pro():
            button = find_promotional_pro_button()
            if button is None:
                raise RuntimeError("Khong tim thay goi Pro uu dai dung 22.000d/thang dau")
            try:
                button.click(timeout=5000)
            except Exception:
                try:
                    button.click(timeout=5000, force=True)
                except Exception:
                    button.evaluate("element => element.click()")

        existing_pages = [candidate for item in browser.contexts for candidate in item.pages]
        pages_before = len(existing_pages)
        click_promotional_pro()
        print(f"[REG TREO][PRO] Clicked promotional Pro Upgrade for {user}")
        deadline = time.time() + 15
        checkout_popup = None
        learn_more_handled = False
        while time.time() < deadline:
            current_pages = [candidate for item in browser.contexts for candidate in item.pages]
            pages_after = len(current_pages)
            if pages_after > pages_before:
                print(f"[REG TREO][PRO] Checkout popup opened for {user}")
                checkout_popup = next(
                    (candidate for candidate in newly_opened_pages if candidate not in existing_pages),
                    next((candidate for candidate in current_pages if candidate not in existing_pages), None),
                )
                break
            # The 22,000 VND first-month campaign can show an information
            # modal before checkout. Acknowledge it, then click the same
            # verified Pro offer once more.
            if not learn_more_handled:
                learn_more = page.get_by_role(
                    "button", name=re.compile(r"^\s*Learn more\s*$", re.I)
                )
                if learn_more.count() and learn_more.first.is_visible():
                    try:
                        learn_more.first.click(timeout=5000)
                    except Exception:
                        learn_more.first.click(timeout=5000, force=True)
                    learn_more_handled = True
                    page.wait_for_timeout(1000)
                    click_promotional_pro()
                    print(
                        f"[REG TREO][PRO] Acknowledged campaign notice and clicked "
                        f"Pro Upgrade again for {user}"
                    )
            page.wait_for_timeout(500)

        # The page event also records popups that open and close between two
        # polling ticks, which is the failure mode this recovery handles.
        if checkout_popup is None:
            checkout_popup = next(
                (candidate for candidate in newly_opened_pages if candidate not in existing_pages),
                None,
            )
        if checkout_popup is None:
            raise RuntimeError("Checkout popup did not open within 15 seconds")

        stability_deadline = time.time() + 10
        while time.time() < stability_deadline:
            if checkout_popup.is_closed():
                retry_checkout = True
                break
            page.wait_for_timeout(500)
        if retry_checkout:
            if checkout_attempt >= 3:
                raise RuntimeError("Checkout popup closed unexpectedly after 3 attempts")
            print(
                f"[REG TREO][PRO] Checkout popup closed unexpectedly for {user}; "
                f"waiting 2 seconds before retry {checkout_attempt + 1}/3"
            )
        else:
            print(f"[REG TREO][PRO] Checkout popup remained open for {user}; entering hold mode")

    if retry_checkout:
        time.sleep(2)
        return open_pro_trial_after_registration(
            account,
            context,
            original_workflow,
            checkout_attempt=checkout_attempt + 1,
        )


def open_pro_checkout_across_five_tabs(account, context, original_workflow):
    """Try the registered tab first and open fallback tabs only if no popup appears."""
    workflow_globals = original_workflow.__globals__
    sync_playwright = workflow_globals["sync_playwright"]
    connect_browser = workflow_globals["_connect_browser"]
    cdp_url = workflow_globals["_cdp_url"](context)
    stop_event = (context or {}).get("stop_event")
    user = account["user"]
    session = (context or {}).get("start_result")
    if not getattr(session, "incognito", False) or not getattr(
        session, "reuse_startup_context", False
    ):
        raise RuntimeError("Reg treo checkout requires Chrome's original incognito window")
    upgrade_pattern = re.compile(r"^\s*(Upgrade|Nâng cấp)\s*$", re.I)
    plan_upgrade_pattern = re.compile(r"^\s*(Upgrade|Nâng cấp)\s*$", re.I)

    with sync_playwright() as playwright:
        browser = connect_browser(playwright, cdp_url, user, stop_event)
        browser_context = browser.contexts[0] if browser.contexts else browser.new_context()
        opened_pages = []
        browser_context.on("page", lambda opened_page: opened_pages.append(opened_page))

        def is_account_workspace(url):
            value = str(url or "").lower()
            return "capcut.com/" in value and bool(
                re.search(r"/my-cloud(?:/[^/?#]+)?(?:[/?#]|$)", value)
            )

        # Registration normally lands on /my-cloud/<space-id>, but fresh
        # accounts can remain on authenticated /my-cloud?tab=all while CapCut
        # provisions the space. Both forms share the same signed-in context.
        workspace_page = None
        workspace_deadline = time.time() + 20
        while time.time() < workspace_deadline and workspace_page is None:
            workspace_page = next(
                (
                    candidate
                    for candidate in browser_context.pages
                    if not candidate.is_closed() and is_account_workspace(candidate.url)
                ),
                None,
            )
            if workspace_page is None:
                time.sleep(0.5)
        if workspace_page is None:
            workspace_page = browser_context.pages[0] if browser_context.pages else browser_context.new_page()
            try:
                workspace_page.goto(
                    "https://www.capcut.com/my-cloud?tab=all",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except Exception:
                try:
                    workspace_page.evaluate("window.stop()")
                except Exception:
                    pass
            redirect_deadline = time.time() + 30
            while time.time() < redirect_deadline:
                concrete_page = next(
                    (
                        candidate
                        for candidate in browser_context.pages
                        if not candidate.is_closed() and is_account_workspace(candidate.url)
                    ),
                    None,
                )
                if concrete_page is not None:
                    workspace_page = concrete_page
                    break
                time.sleep(0.5)
        workspace_url = str(getattr(workspace_page, "url", "") or "")
        if not is_account_workspace(workspace_url):
            raise RuntimeError(f"Authenticated CapCut workspace was not found: {workspace_url}")
        if context is not None:
            context["payment_workspace_url"] = workspace_url
        print(f"[REG TREO][PRO] Using authenticated workspace for {user}: {workspace_url}")

        def visible_enabled(candidates):
            try:
                count = min(candidates.count(), 30)
            except Exception:
                return None
            for index in range(count):
                candidate = candidates.nth(index)
                try:
                    if candidate.is_visible() and candidate.is_enabled():
                        return candidate
                except Exception:
                    continue
            return None

        def click_control(control):
            """Click hydrated CapCut controls without spending 10s per action."""
            try:
                control.click(timeout=1800)
            except Exception:
                try:
                    control.click(timeout=1800, force=True)
                except Exception:
                    control.evaluate("element => element.click()", timeout=1000)

        def dismiss_overlays(page):
            labels = ("Skip", "Bỏ qua", "Not now", "Maybe later", "No thanks", "Got it", "Đã hiểu")
            for _ in range(6):
                clicked = False
                for label in labels:
                    pattern = re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)
                    for candidates in (
                        page.get_by_role("button", name=pattern),
                        page.get_by_text(pattern, exact=True),
                    ):
                        candidate = visible_enabled(candidates)
                        if candidate is None:
                            continue
                        try:
                            click_control(candidate)
                        except Exception:
                            # Promotional/onboarding nodes are frequently
                            # replaced while CapCut hydrates. A stale Skip
                            # control must not abort the checkout attempt.
                            continue
                        time.sleep(0.3)
                        clicked = True
                        break
                    if clicked:
                        break
                if not clicked:
                    return

        def dismiss_discount_offer_popup(page, timeout=0):
            """Close the nested Get-offer promotion without closing pricing."""
            deadline = time.time() + max(0, timeout)
            while True:
                # Verified against the live CapCut DOM: the nested campaign is
                # an lv-modal and its X is a role=button span with aria-label
                # Close. Scope the selector by offer copy so the outer pricing
                # modal's own close control can never be selected.
                try:
                    promo_modals = page.locator("div.lv-modal").filter(
                        has_text=re.compile(r"Get\s+Pro\s+at\s+\d+%\s+OFF", re.I)
                    )
                    for modal_index in range(promo_modals.count() - 1, -1, -1):
                        promo_modal = promo_modals.nth(modal_index)
                        if not promo_modal.is_visible():
                            continue
                        close_control = promo_modal.locator(
                            '.lv-modal-close-icon[role="button"][aria-label="Close"], '
                            '[role="button"][aria-label="Close"]'
                        ).first
                        if close_control.count() and close_control.is_visible():
                            click_control(close_control)
                            print(
                                f"[REG TREO][PRO] Dismissed blocking Get offer "
                                f"popup for {user} via live DOM selector"
                            )
                            time.sleep(0.5)
                            return True
                except Exception:
                    pass
                try:
                    dismissed = bool(
                        page.evaluate(
                            r"""() => {
                                const visible = (element) => {
                                    const style = getComputedStyle(element);
                                    const box = element.getBoundingClientRect();
                                    return style.display !== 'none' &&
                                        style.visibility !== 'hidden' &&
                                        Number(style.opacity || 1) > 0 &&
                                        box.width > 0 && box.height > 0;
                                };
                                const offerTitle = /get\s+pro\s+at\s+\d+%\s+off/i;
                                const offerAction = /^get\s+offer$/i;
                                const roots = Array.from(document.querySelectorAll('body *'))
                                    .filter((element) => {
                                        if (!visible(element)) return false;
                                        const text = (element.innerText || '').trim();
                                        if (!offerTitle.test(text) || !/get\s+offer/i.test(text)) {
                                            return false;
                                        }
                                        const box = element.getBoundingClientRect();
                                        return box.width >= 280 && box.width <= 760 &&
                                            box.height >= 240 && box.height <= 760;
                                    })
                                    .sort((left, right) => {
                                        const a = left.getBoundingClientRect();
                                        const b = right.getBoundingClientRect();
                                        return a.width * a.height - b.width * b.height;
                                    });
                                for (const root of roots) {
                                    const rootBox = root.getBoundingClientRect();
                                    const candidates = Array.from(root.querySelectorAll(
                                        'button, [role="button"], [aria-label], '
                                        + '[class*="close" i], svg'
                                    ));
                                    for (const candidate of candidates) {
                                        if (!visible(candidate)) continue;
                                        const label = (
                                            candidate.getAttribute('aria-label') ||
                                            candidate.getAttribute('title') ||
                                            candidate.innerText || ''
                                        ).trim();
                                        if (offerAction.test(label)) continue;
                                        const box = candidate.getBoundingClientRect();
                                        const centerX = box.left + box.width / 2;
                                        const centerY = box.top + box.height / 2;
                                        const inTopRight = centerX >= rootBox.left + rootBox.width * 0.72 &&
                                            centerY <= rootBox.top + Math.min(120, rootBox.height * 0.35);
                                        const closeSized = box.width >= 8 && box.height >= 8 &&
                                            box.width <= 80 && box.height <= 80;
                                        if (!inTopRight || !closeSized) continue;
                                        const target = candidate.closest('button, [role="button"]') || candidate;
                                        target.click();
                                        return true;
                                    }
                                }
                                return false;
                            }"""
                        )
                    )
                except Exception:
                    dismissed = False
                if dismissed:
                    print(f"[REG TREO][PRO] Dismissed blocking Get offer popup for {user}")
                    time.sleep(0.3)
                    return True
                if time.time() >= deadline:
                    return False
                time.sleep(0.15)

        def wait_for_top_upgrade(page, timeout=10):
            deadline = time.time() + timeout
            while time.time() < deadline:
                dismiss_overlays(page)
                for candidates in (
                    page.get_by_role("button", name=upgrade_pattern),
                    page.locator("button").filter(has_text=re.compile(r"Upgrade|Nâng cấp", re.I)),
                    page.get_by_text(upgrade_pattern, exact=True),
                ):
                    candidate = visible_enabled(candidates)
                    if candidate is None:
                        continue
                    try:
                        if candidate.evaluate("element => element.tagName") != "BUTTON":
                            parent = candidate.locator("xpath=ancestor::button[1]")
                            if parent.count() and parent.is_visible():
                                candidate = parent
                    except Exception:
                        pass
                    return candidate
                time.sleep(0.15)
            return None

        def select_monthly_pricing(page):
            monthly_pattern = re.compile(
                r"^\s*(?:Monthly|Theo tháng)(?:\s+.*)?$",
                re.I,
            )
            for candidates in (
                page.get_by_role("tab", name=monthly_pattern),
                page.get_by_role("button", name=monthly_pattern),
                page.get_by_text(monthly_pattern),
            ):
                candidate = visible_enabled(candidates)
                if candidate is None:
                    continue
                try:
                    selected = str(candidate.get_attribute("aria-selected") or "").lower()
                    pressed = str(candidate.get_attribute("aria-pressed") or "").lower()
                    if selected == "true" or pressed == "true":
                        return True
                except Exception:
                    pass
                try:
                    clickable_parent = candidate.locator(
                        "xpath=ancestor-or-self::*[self::button or @role='tab'][1]"
                    )
                    if clickable_parent.count() and clickable_parent.is_visible():
                        candidate = clickable_parent
                except Exception:
                    pass
                try:
                    click_control(candidate)
                except Exception:
                    continue
                print(f"[REG TREO][PRO] Switched pricing from Yearly to Monthly for {user}")
                time.sleep(0.25)
                return True
            return False

        def find_pro_button(page, timeout=60):
            deadline = time.time() + timeout
            monthly_selected = False
            promotion_opened = False
            monthly_offer_pattern = _DISCOUNTED_22000_PRICE_PATTERN
            purchase_pattern = re.compile(
                r"^\s*(?:Purchase|Buy now|Mua|Thanh toán)\s*$",
                re.I,
            )
            while time.time() < deadline:
                if dismiss_discount_offer_popup(page):
                    monthly_selected = False
                if not monthly_selected:
                    monthly_selected = select_monthly_pricing(page)

                # New CapCut pricing UI renders Monthly and Yearly choices in
                # one sheet. Select the exact 22,000 VND monthly offer, then
                # use its shared Purchase button.
                monthly_prices = page.get_by_text(monthly_offer_pattern, exact=True)
                if not promotion_opened and visible_enabled(monthly_prices) is None:
                    learn_more = visible_enabled(
                        page.get_by_role(
                            "button",
                            name=re.compile(r"^\s*Learn more\s*$", re.I),
                        )
                    )
                    if learn_more is not None:
                        try:
                            click_control(learn_more)
                            promotion_opened = True
                            print(f"[REG TREO][PRO] Opened delayed pricing promotion for {user}")
                            time.sleep(0.5)
                            continue
                        except Exception:
                            pass
                for price_index in range(min(monthly_prices.count(), 10)):
                    monthly_price = monthly_prices.nth(price_index)
                    try:
                        if not monthly_price.is_visible():
                            continue
                        if not _has_discounted_22000_price(
                            monthly_price.inner_text(timeout=1500)
                        ):
                            continue
                    except Exception:
                        continue
                    try:
                        price_context = monthly_price.locator("xpath=ancestor::div[1]").inner_text(
                            timeout=1500
                        )
                    except Exception:
                        price_context = ""
                    if price_context and not _is_monthly_price_context(price_context):
                        # The price and billing label can be sibling nodes one
                        # level higher, so inspect a few compact ancestors.
                        for ancestor_level in range(2, 6):
                            try:
                                price_context = monthly_price.locator(
                                    f"xpath=ancestor::div[{ancestor_level}]"
                                ).inner_text(timeout=1500)
                            except Exception:
                                continue
                            if _is_monthly_price_context(price_context):
                                break
                    if price_context and not _is_monthly_price_context(price_context):
                        continue
                    try:
                        monthly_choice = monthly_price.locator(
                            "xpath=ancestor-or-self::*[self::button or self::label or @role='radio'][1]"
                        )
                        if monthly_choice.count() and monthly_choice.is_visible():
                            click_control(monthly_choice)
                        else:
                            click_control(monthly_price)
                    except Exception:
                        continue
                    print(f"[REG TREO][PRO] Selected 22,000d Monthly offer for {user}")
                    time.sleep(0.25)

                    # The pricing card exposes a black Upgrade control.
                    # control that can be a div rather than a real button.
                    # Resolve it from the exact visible 22,000 VND price node
                    # so an adjacent 222,000 VND card can never be selected.
                    card = monthly_price.locator(
                        "xpath=ancestor::div["
                        ".//*[normalize-space()='Pro'] and "
                        ".//*[normalize-space()='Upgrade' or "
                        "normalize-space()='Nâng cấp']][1]"
                    )
                    try:
                        card_is_visible = bool(card.count() and card.is_visible())
                    except Exception:
                        card_is_visible = False
                    if card_is_visible:
                        plan_control = visible_enabled(
                            card.get_by_role("button", name=plan_upgrade_pattern)
                        )
                        if plan_control is None:
                            plan_text = visible_enabled(
                                card.get_by_text(plan_upgrade_pattern, exact=True)
                            )
                            if plan_text is not None:
                                try:
                                    clickable = plan_text.locator(
                                        "xpath=ancestor-or-self::*[self::button or "
                                        "@role='button' or @tabindex][1]"
                                    )
                                    if clickable.count() and clickable.is_visible():
                                        plan_control = clickable
                                    else:
                                        plan_control = plan_text
                                except Exception:
                                    plan_control = plan_text
                        if plan_control is not None:
                            print(
                                f"[REG TREO][PRO] Found black Upgrade in exact "
                                f"22,000d Pro card for {user}"
                            )
                            return plan_control

                    # CapCut now splits the price and /month label across
                    # nested React nodes. In that DOM, locating an exact price
                    # node and climbing with XPath is unreliable. Scan visible
                    # Upgrade controls and accept one only when a compact
                    # ancestor is the Pro card and contains both 222,000 and
                    # the discounted 22,000 monthly price.
                    for upgrade_candidates in (
                        page.get_by_role("button", name=plan_upgrade_pattern),
                        page.get_by_text(plan_upgrade_pattern, exact=True),
                    ):
                        try:
                            candidate_count = min(upgrade_candidates.count(), 20)
                        except Exception:
                            candidate_count = 0
                        for candidate_index in range(candidate_count):
                            candidate = upgrade_candidates.nth(candidate_index)
                            try:
                                if not candidate.is_visible():
                                    continue
                                in_exact_pro_card = bool(
                                    candidate.evaluate(
                                        r"""(element) => {
                                            const visible = (node) => {
                                                const style = getComputedStyle(node);
                                                const box = node.getBoundingClientRect();
                                                return style.display !== 'none' &&
                                                    style.visibility !== 'hidden' &&
                                                    Number(style.opacity || 1) > 0 &&
                                                    box.width > 0 && box.height > 0;
                                            };
                                            const hasAmount = (text, amount) => {
                                                const compact = text.replace(/[.,\s\u00a0]/g, '');
                                                return new RegExp('(?:^|[^0-9])' + amount + '(?:[^0-9]|$)')
                                                    .test(compact);
                                            };
                                            for (let node = element; node && node !== document.body;
                                                 node = node.parentElement) {
                                                if (!visible(node)) continue;
                                                const box = node.getBoundingClientRect();
                                                if (box.width > innerWidth * 0.62) continue;
                                                const text = (node.innerText || '').trim();
                                                if (!/(^|\n)\s*Pro\s*(\n|$)/i.test(text)) continue;
                                                if (!hasAmount(text, '22000') || !hasAmount(text, '222000')) continue;
                                                if (!/(?:\bmonthly\b|\/\s*month\b|per\s+month\b|hàng\s+tháng|mỗi\s+tháng)/i.test(text)) continue;
                                                return true;
                                            }
                                            return false;
                                        }"""
                                    )
                                )
                            except Exception:
                                continue
                            if not in_exact_pro_card:
                                continue
                            try:
                                clickable = candidate.locator(
                                    "xpath=ancestor-or-self::*[self::button or "
                                    "@role='button' or @tabindex][1]"
                                )
                                if clickable.count() and clickable.is_visible():
                                    candidate = clickable
                            except Exception:
                                pass
                            print(
                                f"[REG TREO][PRO] Found black Upgrade in exact "
                                f"22,000d/month Pro card for {user}"
                            )
                            return candidate

                    # The newer pricing sheet uses one shared Purchase action
                    # after the exact Monthly 22,000 VND option is selected.
                    purchase = visible_enabled(
                        page.get_by_role("button", name=purchase_pattern)
                    )
                    if purchase is None:
                        purchase = visible_enabled(
                            page.locator("button").filter(has_text=purchase_pattern)
                        )
                    if purchase is None:
                        purchase_text = visible_enabled(
                            page.get_by_text(purchase_pattern, exact=True)
                        )
                        if purchase_text is not None:
                            try:
                                purchase_container = purchase_text.locator(
                                    "xpath=ancestor-or-self::*[contains(@class, 'purchase-new')][1]"
                                )
                                if purchase_container.count() and purchase_container.is_visible():
                                    purchase = purchase_container
                                else:
                                    purchase = purchase_text
                            except Exception:
                                purchase = purchase_text
                    if purchase is not None:
                        return purchase
                time.sleep(0.25)
            return None

        def monitor_checkout_while_holding(checkout_popup, location):
            """Wait for manual payment and return only after its result is known."""
            if bool((context or {}).get("e2e_return_on_stable_popup")):
                return True

            payment_success_pattern = re.compile(
                r"(?:payment|purchase|subscription|order)\s+(?:was\s+)?"
                r"(?:successful|succeeded|completed|confirmed)|"
                r"(?:successfully\s+(?:paid|subscribed|purchased))|"
                r"thank\s+you\s+for\s+your\s+(?:payment|purchase)|"
                r"thanh\s*to[aá]n\s+th[aà]nh\s+c[oô]ng|"
                r"(?:[đd]ăng\s+k[yý]|mua)\s+th[aà]nh\s+c[oô]ng",
                re.I,
            )
            payment_success_url_pattern = re.compile(
                r"(?:payment|checkout|cashier|payin|order)[^?#]*"
                r"(?:success|complete|result)|"
                r"(?:success|complete)[^?#]*(?:payment|checkout|order)",
                re.I,
            )

            def page_text(candidate):
                try:
                    return candidate.locator("body").inner_text(timeout=1200)
                except Exception:
                    return ""

            def paid_plan_visible(candidate):
                """Require explicit paid-plan state, never generic pricing copy."""
                text = page_text(candidate)
                normalized = text.casefold().replace("’", "'")
                if any(
                    marker in normalized
                    for marker in (
                        "pro: active",
                        "capcut pro: active",
                        "subscription active",
                        "you're enjoying pro benefits",
                    )
                ):
                    return True
                if re.search(
                    r"(?im)^\s*(?:pro|ultra|teams)\s*$\s*"
                    r"^(?:renews?|expires?|valid\s+until|next\s+billing\s+date)\b",
                    text,
                ):
                    return True
                try:
                    return bool(
                        candidate.evaluate(
                            r"""() => {
                                const visible = (node) => {
                                    const style = getComputedStyle(node);
                                    const box = node.getBoundingClientRect();
                                    return style.display !== 'none' &&
                                        style.visibility !== 'hidden' &&
                                        box.width > 0 && box.height > 0;
                                };
                                const titles = Array.from(document.querySelectorAll('body *'))
                                    .filter((node) => visible(node) &&
                                        /^\s*(?:Pro|Ultra|Teams)\s*$/i.test(node.textContent || ''));
                                for (const title of titles) {
                                    let card = title;
                                    for (let depth = 0; depth < 8 && card; depth++, card = card.parentElement) {
                                        const value = (card.innerText || '').trim();
                                        const planTitles = Array.from(card.querySelectorAll('*'))
                                            .filter((node) => visible(node) &&
                                                /^\s*(?:Free|Pro|Ultra|Teams)\s*$/i.test(node.textContent || ''));
                                        if (/\bCurrent\s+plan\b/i.test(value) &&
                                            /\b(?:Pro|Ultra|Teams)\b/i.test(value) &&
                                            planTitles.length === 1 &&
                                            !/^\s*Free\s*$/i.test(planTitles[0].textContent || '')) return true;
                                    }
                                }
                                return false;
                            }"""
                        )
                    )
                except Exception:
                    return False

            def reveal_account_plan(candidate):
                """Open CapCut's account menu so the paid-plan label is rendered."""
                try:
                    current_url = str(candidate.url or "")
                except Exception:
                    return
                if not re.search(r"https?://(?:[^/]+\.)?capcut\.com/", current_url, re.I):
                    return
                selectors = (
                    "[aria-label*='profile' i]",
                    "[aria-label*='account' i]",
                    "[data-testid*='avatar' i]",
                    "[class*='avatar']",
                )
                for selector in selectors:
                    try:
                        controls = candidate.locator(selector)
                        for control_index in range(min(controls.count(), 4)):
                            control = controls.nth(control_index)
                            if control.is_visible():
                                control.click(timeout=1200)
                                candidate.wait_for_timeout(500)
                                return
                    except Exception:
                        continue

            def payment_confirmed():
                try:
                    live_pages = [
                        candidate
                        for item in browser.contexts
                        for candidate in item.pages
                        if not candidate.is_closed()
                    ]
                except Exception:
                    live_pages = []
                for candidate in live_pages:
                    try:
                        current_url = str(candidate.url or "")
                    except Exception:
                        current_url = ""
                    text = page_text(candidate)
                    if payment_success_url_pattern.search(current_url) and not re.search(
                        r"(?:fail|failed|cancel|error|declin)", current_url, re.I
                    ):
                        return True, f"success URL: {current_url}"
                    if payment_success_pattern.search(text) and not re.search(
                        r"(?:payment|purchase|order).{0,30}(?:fail|failed|declined|cancelled)",
                        text,
                        re.I | re.S,
                    ):
                        return True, "checkout success message"
                    # The checkout can reuse the registered CapCut tab.  An
                    # explicit "Pro / Renews on ..." state is valid evidence
                    # there too; it cannot be confused with a pricing card.
                    if paid_plan_visible(candidate):
                        return True, "active paid plan visible on CapCut account"
                return False, ""

            popup_closed = threading.Event()
            try:
                checkout_popup.on("close", lambda *_args: popup_closed.set())
            except Exception:
                pass
            print(
                f"[REG TREO][PRO] Checkout detected on {location} for {user}; "
                "waiting for manual payment confirmation"
            )
            closed_confirmation_deadline = None
            account_plan_revealed = False
            while True:
                if stop_event is not None and stop_event.is_set():
                    raise RuntimeError(f"Stop requested for {user}")
                try:
                    if not browser.is_connected():
                        return False
                except Exception:
                    return False

                paid, evidence = payment_confirmed()
                if paid:
                    if context is not None:
                        context["payment_confirmed"] = True
                        context["payment_evidence"] = evidence
                    print(
                        f"[REG TREO][PAYMENT] Payment confirmed for {user}: {evidence}"
                    )
                    return True

                try:
                    checkout_is_closed = checkout_popup.is_closed()
                except Exception:
                    checkout_is_closed = True
                if popup_closed.is_set() or checkout_is_closed:
                    if closed_confirmation_deadline is None:
                        closed_confirmation_deadline = time.time() + 10
                        print(
                            f"[REG TREO][PAYMENT] Checkout closed on {location} for {user}; "
                            "checking the account for payment confirmation"
                        )
                        # CapCut only adds "Pro / Renews on ..." to the DOM
                        # after the avatar menu is opened. Reveal it once, then
                        # let the normal confirmation loop verify the text.
                        if not account_plan_revealed:
                            try:
                                account_pages = [
                                    candidate
                                    for item in browser.contexts
                                    for candidate in item.pages
                                    if not candidate.is_closed()
                                ]
                            except Exception:
                                account_pages = []
                            for candidate in account_pages:
                                reveal_account_plan(candidate)
                            account_plan_revealed = True
                    elif time.time() >= closed_confirmation_deadline:
                        if context is not None:
                            context["checkout_tab_closed"] = True
                            context["payment_confirmed"] = False
                        print(
                            f"[REG TREO][PAYMENT] Checkout closed without confirmed payment "
                            f"for {user}"
                        )
                        return False
                try:
                    live_pages = [
                        candidate
                        for item in browser.contexts
                        for candidate in item.pages
                        if not candidate.is_closed()
                    ]
                except Exception:
                    live_pages = []
                # This is intentionally passive. A short Playwright wait merely
                # pumps CDP events so the close event is received quickly.
                passive_page = next(
                    (candidate for candidate in live_pages if not candidate.is_closed()),
                    None,
                )
                try:
                    if passive_page is not None:
                        passive_page.wait_for_timeout(500)
                    else:
                        time.sleep(0.5)
                except Exception:
                    time.sleep(0.5)

        def attempt_checkout(
            page,
            location,
            progress=None,
            top_timeout=10,
            plan_timeout=6,
        ):
            """Click only the verified offer and require a stable checkout."""
            progress = progress if progress is not None else {}
            progress["top_upgrade_clicked"] = False
            progress["plan_action_clicked"] = False
            page.bring_to_front()
            dismiss_overlays(page)
            print(f"[REG TREO][PRO] Waiting for top Upgrade on {location} for {user}")
            top_upgrade = wait_for_top_upgrade(page, timeout=top_timeout)
            if top_upgrade is None:
                raise RuntimeError(
                    f"top Upgrade did not appear within {top_timeout:g} seconds"
                )
            click_control(top_upgrade)
            progress["top_upgrade_clicked"] = True
            print(f"[REG TREO][PRO] Clicked top Upgrade on {location} for {user}")
            time.sleep(0.2)
            # Probe briefly here. If the campaign overlay is injected later,
            # find_pro_button() keeps dismissing it on every fast polling pass,
            # so an absent overlay no longer adds four seconds to every tab.
            dismiss_discount_offer_popup(page, timeout=1.2)

            # Some accounts first receive a compact CapCut Pro promotion
            # instead of the pricing sheet. Open its Learn more action before
            # looking for Monthly/Yearly choices.
            learn_more = page.get_by_role(
                "button",
                name=re.compile(r"^\s*Learn more\s*$", re.I),
            )
            if learn_more.count() and learn_more.first.is_visible():
                click_control(learn_more.first)
                print(f"[REG TREO][PRO] Opened pricing from Learn more on {location} for {user}")
                time.sleep(0.25)

            pro_button = find_pro_button(page, timeout=plan_timeout)
            if pro_button is None:
                raise RuntimeError(
                    f"22,000 VND monthly Pro action did not appear within "
                    f"{plan_timeout:g} seconds"
                )

            pages_before = [candidate for item in browser.contexts for candidate in item.pages]
            event_offset = len(opened_pages)
            click_control(pro_button)
            progress["plan_action_clicked"] = True
            print(f"[REG TREO][PRO] Clicked 22,000d Monthly Upgrade/Purchase on {location} for {user}")

            checkout_popup = None
            learn_more_handled = False
            popup_closed_during_attempt = False
            checkout_target_seen = False
            attempt_pages = []
            close_events = {}

            def remember_attempt_page(candidate):
                nonlocal checkout_target_seen
                if candidate in attempt_pages:
                    return
                attempt_pages.append(candidate)
                closed_event = threading.Event()
                close_events[candidate] = closed_event
                try:
                    candidate.on("close", lambda *_args, event=closed_event: event.set())
                except Exception:
                    pass
                try:
                    checkout_target_seen = checkout_target_seen or _is_checkout_url(candidate.url)
                except Exception:
                    pass

            deadline = time.time() + 5
            while time.time() < deadline:
                current_pages = [candidate for item in browser.contexts for candidate in item.pages]
                for candidate in list(opened_pages[event_offset:]) + current_pages:
                    if candidate not in pages_before:
                        remember_attempt_page(candidate)

                live_attempt_pages = []
                checkout_attempt_pages = []
                for candidate in attempt_pages:
                    try:
                        if close_events[candidate].is_set() or candidate.is_closed():
                            continue
                        live_attempt_pages.append(candidate)
                        if _is_checkout_url(candidate.url):
                            checkout_target_seen = True
                            checkout_attempt_pages.append(candidate)
                    except Exception:
                        close_events[candidate].set()

                # A plan click can briefly create an unrelated target before
                # the actual PipoPay cashier. Prefer the confirmed checkout URL
                # so hold mode never watches the wrong page.
                checkout_popup = checkout_attempt_pages[-1] if checkout_attempt_pages else None
                if checkout_popup is not None:
                    # CapCut can create the checkout window and close it again
                    # immediately. Do not treat that transient page event as a
                    # successful popup, otherwise the workflow enters hold
                    # mode on the pricing sheet and never opens fallback tabs.
                    force_close_key = None
                    if (
                        str(location).startswith("registered tab")
                        and bool((context or {}).get("force_primary_popup_close"))
                        and not bool((context or {}).get("_forced_primary_popup_closed"))
                    ):
                        force_close_key = "_forced_primary_popup_closed"
                    elif (
                        str(location).startswith("fallback tab")
                        and bool((context or {}).get("force_first_fallback_popup_close"))
                        and not bool((context or {}).get("_forced_fallback_popup_closed"))
                    ):
                        force_close_key = "_forced_fallback_popup_closed"
                    if force_close_key is not None:
                        try:
                            checkout_popup.close()
                        finally:
                            context[force_close_key] = True
                        print(
                            f"[REG TREO][PRO] Forced checkout popup close for E2E "
                            f"on {location} for {user}"
                        )
                    else:
                        print(
                            f"[REG TREO][PRO] Checkout popup opened from {location} "
                            f"for {user}; stopping all reload/click activity"
                        )
                        if monitor_checkout_while_holding(checkout_popup, location):
                            return True
                        raise RuntimeError("checkout closed without confirmed payment")
                if not learn_more_handled:
                    learn_more = page.get_by_role(
                        "button", name=re.compile(r"^\s*Learn more\s*$", re.I)
                    )
                    if learn_more.count() and learn_more.first.is_visible():
                        click_control(learn_more.first)
                        learn_more_handled = True
                        time.sleep(0.25)
                        pro_button = find_pro_button(page, timeout=plan_timeout)
                        if pro_button is not None:
                            click_control(pro_button)
                time.sleep(0.15)

            live_attempt_pages = []
            checkout_attempt_pages = []
            for candidate in attempt_pages:
                try:
                    if close_events[candidate].is_set() or candidate.is_closed():
                        continue
                    live_attempt_pages.append(candidate)
                    if _is_checkout_url(candidate.url):
                        checkout_target_seen = True
                        checkout_attempt_pages.append(candidate)
                except Exception:
                    close_events[candidate].set()
            any_attempt_page_closed = any(event.is_set() for event in close_events.values())
            if not checkout_attempt_pages:
                popup_closed_during_attempt = checkout_target_seen or any_attempt_page_closed
                checkout_popup = None
            else:
                checkout_popup = checkout_attempt_pages[-1]
            if popup_closed_during_attempt:
                print(
                    f"[REG TREO][PRO] Checkout target disappeared within 5 seconds "
                    f"on {location} for {user}; continuing to fallback"
                )

            if popup_closed_during_attempt:
                raise RuntimeError("checkout popup opened but closed within 5 seconds")
            if checkout_popup is None or checkout_popup.is_closed():
                raise RuntimeError("checkout popup did not open within 5 seconds")
            print(
                f"[REG TREO][PRO] Checkout popup opened from {location} for {user}; "
                "stopping all reload/click activity"
            )
            if monitor_checkout_while_holding(checkout_popup, location):
                return True
            raise RuntimeError("checkout closed without confirmed payment")

        # Always inspect the just-registered incognito tab first, without
        # refreshing it. If the exact offer is already visible, click it
        # immediately. Otherwise continue with five freshly opened sibling
        # tabs. Failed fallback tabs receive one staggered reload round.
        force_first_failure = bool((context or {}).get("force_first_checkout_failure"))
        if force_first_failure:
            print(f"[REG TREO][PRO] Forced first checkout failure for E2E: {user}")
        else:
            primary_progress = {}
            try:
                return attempt_checkout(
                    workspace_page,
                    "registered tab",
                    progress=primary_progress,
                )
            except Exception as exc:
                safe_error = str(exc).encode("ascii", "replace").decode("ascii")
                if primary_progress.get("plan_action_clicked"):
                    print(
                        f"[REG TREO][PRO] Registered tab clicked 22,000d Upgrade "
                        f"but no checkout popup appeared for {user}: {safe_error}; "
                        f"opening 5 incognito fallback tabs without reloading"
                    )
                else:
                    print(
                        f"[REG TREO][PRO] Exact 22,000d offer was not available on "
                        f"the registered tab for {user}: {safe_error}; opening 5 "
                        "incognito fallback tabs without reloading"
                    )

        def open_adjacent_tab_from_workspace(tab_number):
            """Open a real sibling tab from the registered tab, never a new window."""
            link_id = f"reg-treo-adjacent-tab-{tab_number}"
            workspace_page.bring_to_front()
            workspace_page.evaluate(
                """({linkId}) => {
                    document.getElementById(linkId)?.remove();
                    const link = document.createElement('a');
                    link.id = linkId;
                    link.href = 'about:blank';
                    link.target = '_blank';
                    link.textContent = 'open tab';
                    Object.assign(link.style, {
                        position: 'fixed', left: '1px', top: '1px', width: '2px',
                        height: '2px', opacity: '0.01', zIndex: '2147483647'
                    });
                    document.body.appendChild(link);
                }""",
                {"linkId": link_id},
            )
            try:
                # A trusted click on target=_blank makes Chrome insert a normal
                # tab beside its opener. BrowserContext.new_page() is avoided
                # because CDP can represent that target as a separate window.
                with browser_context.expect_page(timeout=10000) as page_info:
                    workspace_page.locator(f"#{link_id}").click(
                        force=True,
                        timeout=3000,
                    )
                adjacent_page = page_info.value
            finally:
                try:
                    workspace_page.evaluate(
                        "linkId => document.getElementById(linkId)?.remove()",
                        link_id,
                    )
                except Exception:
                    pass
            return adjacent_page

        # Keep the registration/workspace page and create all five sibling tabs
        # first. Start their navigations together instead of waiting for each
        # page to finish before opening the next one.
        candidate_tabs = []
        for tab_number in range(1, 6):
            page = open_adjacent_tab_from_workspace(tab_number)
            candidate_tabs.append((page, None))
            print(f"[REG TREO][PRO] Opened adjacent workspace tab {tab_number}/5 for {user}")
            if tab_number < 5:
                time.sleep(1.0)

        print(f"[REG TREO][PRO] Starting all 5 fallback tab navigations for {user}")
        navigation_errors = {}
        navigation_sessions = {}
        for tab_number, (page, _navigation_error) in enumerate(candidate_tabs, start=1):
            try:
                # Dispatch browser-level navigations back-to-back. This keeps
                # all five tabs moving together and is not a page reload.
                cdp_session = browser_context.new_cdp_session(page)
                navigation_sessions[tab_number] = cdp_session
                result = cdp_session.send("Page.navigate", {"url": workspace_url})
                if result.get("errorText"):
                    raise RuntimeError(result["errorText"])
            except Exception as exc:
                navigation_errors[tab_number] = exc

        pending_tabs = set(range(1, 6))
        shared_navigation_deadline = time.time() + 20
        while pending_tabs and time.time() < shared_navigation_deadline:
            for tab_number in list(pending_tabs):
                page = candidate_tabs[tab_number - 1][0]
                try:
                    if page.is_closed():
                        navigation_errors[tab_number] = RuntimeError("tab closed during navigation")
                        pending_tabs.remove(tab_number)
                    elif is_account_workspace(page.url):
                        pending_tabs.remove(tab_number)
                except Exception as exc:
                    navigation_errors[tab_number] = exc
                    pending_tabs.remove(tab_number)
            if pending_tabs:
                time.sleep(0.15)
        for tab_number in pending_tabs:
            page = candidate_tabs[tab_number - 1][0]
            navigation_errors[tab_number] = RuntimeError(
                f"shared navigation timeout after 20 seconds; url={getattr(page, 'url', '')}"
            )
        for cdp_session in navigation_sessions.values():
            try:
                cdp_session.detach()
            except Exception:
                pass
        candidate_tabs = [
            (page, navigation_errors.get(tab_number))
            for tab_number, (page, _navigation_error) in enumerate(candidate_tabs, start=1)
        ]
        print(
            f"[REG TREO][PRO] All 5 fallback navigations dispatched for {user}; "
            f"ready={5 - len(navigation_errors)}/5"
        )

        failures = []
        retry_tabs = []
        print(f"[REG TREO][PRO] Fast first scan across 5 fallback tabs: {user}")
        for tab_number, (page, _navigation_error) in enumerate(candidate_tabs, start=1):
            location = f"fallback tab {tab_number}/5"
            try:
                if tab_number in navigation_errors:
                    raise navigation_errors[tab_number]
                return attempt_checkout(
                    page,
                    location,
                    top_timeout=5,
                    plan_timeout=4,
                )
            except Exception as exc:
                safe_error = str(exc).encode("ascii", "replace").decode("ascii")
                failures.append(f"tab {tab_number} first scan: {safe_error}")
                try:
                    if not page.is_closed():
                        retry_tabs.append((tab_number, page))
                except Exception:
                    pass
                print(
                    f"[REG TREO][PRO] Fast scan missed offer on fallback tab "
                    f"{tab_number}/5 for {user}: {safe_error}"
                )

        # CapCut does not expose the discounted offer on every response. Give
        # each surviving fallback tab exactly one fresh request. Reloads are
        # staggered to avoid a five-request burst, while CDP dispatch keeps
        # earlier tabs loading in parallel with the later ones.
        if retry_tabs:
            print(
                f"[REG TREO][PRO] Staggered reload for {len(retry_tabs)}/5 "
                f"fallback tabs (0.7s apart): {user}"
            )
            reload_sessions = {}
            reload_errors = {}
            for retry_index, (tab_number, page) in enumerate(retry_tabs):
                if stop_event is not None and stop_event.is_set():
                    raise RuntimeError(f"Stop requested for {user}")
                try:
                    session = browser_context.new_cdp_session(page)
                    reload_sessions[tab_number] = session
                    if is_account_workspace(getattr(page, "url", "")):
                        result = session.send("Page.reload", {"ignoreCache": True})
                    else:
                        result = session.send("Page.navigate", {"url": workspace_url})
                    if result and result.get("errorText"):
                        raise RuntimeError(result["errorText"])
                    print(
                        f"[REG TREO][PRO] Reload dispatched for fallback tab "
                        f"{tab_number}/5: {user}"
                    )
                except Exception as exc:
                    reload_errors[tab_number] = exc
                if retry_index < len(retry_tabs) - 1:
                    if stop_event is not None and callable(getattr(stop_event, "wait", None)):
                        if stop_event.wait(0.7):
                            raise RuntimeError(f"Stop requested for {user}")
                    else:
                        time.sleep(0.7)

            # The first tab has already loaded for several seconds while the
            # last reload is dispatched. A single shared settle delay is much
            # faster than waiting for each tab sequentially.
            if stop_event is not None and callable(getattr(stop_event, "wait", None)):
                if stop_event.wait(2.0):
                    raise RuntimeError(f"Stop requested for {user}")
            else:
                time.sleep(2.0)
            for session in reload_sessions.values():
                try:
                    session.detach()
                except Exception:
                    pass

            print(f"[REG TREO][PRO] Scanning reloaded fallback tabs: {user}")
            for tab_number, page in retry_tabs:
                location = f"reloaded fallback tab {tab_number}/5"
                try:
                    if tab_number in reload_errors:
                        raise reload_errors[tab_number]
                    return attempt_checkout(
                        page,
                        location,
                        top_timeout=8,
                        plan_timeout=6,
                    )
                except Exception as exc:
                    safe_error = str(exc).encode("ascii", "replace").decode("ascii")
                    failures.append(f"tab {tab_number} after reload: {safe_error}")
                    print(
                        f"[REG TREO][PRO] Reloaded fallback tab {tab_number}/5 "
                        f"did not produce checkout for {user}: {safe_error}"
                    )

        raise RuntimeError(
            "All 5 Upgrade tabs failed after one staggered reload: "
            + " | ".join(failures)
        )


def cancel_capcut_subscription(account, context, original_workflow):
    """Turn off auto-renewal in the already authenticated CapCut session."""
    workflow_globals = original_workflow.__globals__
    sync_playwright = workflow_globals["sync_playwright"]
    connect_browser = workflow_globals["_connect_browser"]
    cdp_url = workflow_globals["_cdp_url"](context)
    stop_event = (context or {}).get("stop_event")
    user = account["user"]

    manage_pattern = re.compile(
        r"manage\s+(?:auto[- ]?renewal|subscription)|"
        r"qu[aả]n\s+l[yý]\s+t[uù]y\s+ch[oọ]n\s+t[uự]\s+đ[oộ]ng\s+gia\s+h[aạ]n",
        re.I,
    )
    cancel_pattern = re.compile(
        r"cancel\s+(?:the\s+)?(?:subscription|plan)|"
        r"turn\s+off\s+auto[- ]?renewal|"
        r"h[uủỷy]\s+g[oó]i\s+(?:đ[aă]ng\s+k[yý]|thu[eê])|"
        r"t[aắ]t\s+(?:t[uự]\s+đ[oộ]ng\s+)?gia\s+h[aạ]n",
        re.I,
    )
    confirm_pattern = re.compile(
        r"confirm(?:\s+cancellation)?|continue\s+(?:to\s+)?cancel|"
        r"cancel\s+(?:anyway|subscription|plan|auto[- ]?renewal)|yes,?\s+cancel|"
        r"x[aá]c\s+nh[aậ]n\s+h[uủỷy]|ti[eế]p\s+t[uụ]c\s+h[uủỷy]|"
        r"v[aẫ]n\s+(?:mu[oố]n\s+)?h[uủỷy]|h[uủỷy]\s+g[oó]i",
        re.I,
    )
    cancelled_pattern = re.compile(
        r"auto[- ]?renewal\s+(?:is\s+|has\s+been\s+)?(?:off|cancelled|canceled)|"
        r"subscription\s+(?:is\s+|has\s+been\s+)?(?:cancelled|canceled)|"
        r"cancellation\s+(?:was\s+)?successful|will\s+not\s+renew|"
        r"^\s*(?:pro|ultra|teams)\s*$\s*^\s*expires?\s+on\b|"
        r"đ[aã]\s+h[uủỷy](?:\s+g[oó]i|\s+gia\s+h[aạ]n)?|"
        r"kh[oô]ng\s+(?:t[uự]\s+đ[oộ]ng\s+)?gia\s+h[aạ]n|"
        r"h[eế]t\s+h[aạ]n\s+(?:v[aà]o|ng[aà]y)",
        re.I | re.M,
    )
    resume_pattern = re.compile(
        r"resume\s+(?:subscription|plan)|renew\s+(?:subscription|plan)|"
        r"b[aậ]t\s+(?:l[aạ]i\s+)?gia\s+h[aạ]n|gia\s+h[aạ]n\s+l[aạ]i",
        re.I,
    )

    def body_text(page):
        try:
            return str(page.locator("body").inner_text(timeout=2000) or "")
        except Exception:
            return ""

    def click_first(locator, timeout=2500):
        try:
            count = min(locator.count(), 30)
        except Exception:
            return False
        for index in range(count):
            candidate = locator.nth(index)
            try:
                if not candidate.is_visible() or not candidate.is_enabled():
                    continue
                try:
                    candidate.click(timeout=timeout)
                except Exception:
                    candidate.click(timeout=timeout, force=True)
                return True
            except Exception:
                continue
        return False

    def click_matching_control(page, pattern, timeout=2500):
        """Click a visible control by its rendered text across CapCut variants."""
        controls = page.locator("button, [role='button'], a")
        try:
            count = min(controls.count(), 200)
        except Exception:
            count = 0
        for index in range(count):
            candidate = controls.nth(index)
            try:
                label = str(candidate.inner_text(timeout=500) or "").strip()
                if not pattern.search(label):
                    continue
                if not candidate.is_visible() or not candidate.is_enabled():
                    continue
                try:
                    candidate.click(timeout=timeout)
                except Exception:
                    candidate.click(timeout=timeout, force=True)
                return True
            except Exception:
                continue
        return click_first(page.get_by_text(pattern, exact=True), timeout=timeout)

    def click_exact_dom_text(page, labels):
        """DOM fallback for icon-styled controls lacking button semantics."""
        for label in labels:
            locators = (
                page.get_by_text(str(label), exact=True),
                page.locator(f"text={label}"),
                page.locator(
                    "xpath=//*[normalize-space(.)="
                    + repr(str(label))
                    + "]"
                ),
            )
            for locator in locators:
                if click_first(locator, timeout=1800):
                    return True
                try:
                    count = locator.count()
                    for index in range(count - 1, -1, -1):
                        candidate = locator.nth(index)
                        if not candidate.is_visible():
                            continue
                        candidate.click(timeout=1800, force=True)
                        return True
                except Exception:
                    continue
        try:
            return bool(
                page.evaluate(
                    r"""labels => {
                        const normalize = value => value.replace(/\s+/g, ' ').trim().toLocaleLowerCase();
                        const wanted = labels.map(normalize);
                        const visible = element => {
                            const style = getComputedStyle(element);
                            const box = element.getBoundingClientRect();
                            return style.display !== 'none' && style.visibility !== 'hidden' &&
                                box.width > 0 && box.height > 0;
                        };
                        const nodes = Array.from(document.querySelectorAll('body *'));
                        const node = nodes.find(element => {
                            const text = normalize(element.innerText || element.textContent || '');
                            return wanted.includes(text) && visible(element) &&
                                !Array.from(element.children).some(child =>
                                    wanted.includes(normalize(child.innerText || child.textContent || ''))
                                );
                        });
                        if (!node) return false;
                        const target = node.closest('button, [role="button"], a') || node;
                        target.click();
                        return true;
                    }""",
                    list(labels),
                )
            )
        except Exception:
            return False

    def cancellation_visible(page):
        text = body_text(page)
        if cancelled_pattern.search(text):
            return True, "subscription manager shows auto-renewal disabled"
        controls = page.get_by_role("button", name=resume_pattern).or_(
            page.get_by_text(resume_pattern, exact=False)
        )
        try:
            if any(controls.nth(index).is_visible() for index in range(min(controls.count(), 20))):
                return True, "subscription manager offers renewal/resume"
        except Exception:
            pass
        return False, ""

    def open_profile_menu(page):
        text = body_text(page)
        if manage_pattern.search(text) or re.search(r"(?im)^\s*(?:pro|ultra|teams)\s*$\s*^\s*(?:renews?|expires?)\s+on\b", text):
            return True
        selectors = (
            "[aria-label*='profile' i]",
            "[aria-label*='account' i]",
            "[data-testid*='avatar' i]",
            "[class*='avatar']",
        )
        for selector in selectors:
            if click_first(page.locator(selector), timeout=1500):
                page.wait_for_timeout(700)
                if re.search(r"(?im)^\s*(?:pro|ultra|teams)\s*$", body_text(page)):
                    return True
        # Current CapCut builds render the top-right account avatar as an
        # icon-only canvas control without an accessible name or stable class.
        # Probe only the far-right header area and verify the account menu;
        # the older width-55/y75 point hits the Space settings gear on the
        # current layout.
        try:
            viewport = page.evaluate("() => ({width: innerWidth, height: innerHeight})")
            width = int(viewport["width"])
            avatar_points = (
                (max(1, width - 24), 20),
                (max(1, width - 24), 48),
                (max(1, width - 24), 75),
                (max(1, width - 55), 20),
            )
            for avatar_x, avatar_y in avatar_points:
                page.mouse.click(avatar_x, avatar_y)
                page.wait_for_timeout(700)
                menu_text = body_text(page)
                menu_lowered = menu_text.casefold()
                if (
                    re.search(r"(?im)^\s*(?:pro|ultra|teams)\s*$", menu_text)
                    or ("view profile" in menu_lowered and "sign out" in menu_lowered)
                ):
                    print(
                        f"[REG TREO][CANCEL] Account menu opened at "
                        f"({avatar_x}, {avatar_y}) for {user}"
                    )
                    return True
                if "space settings" in menu_lowered:
                    try:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(300)
                    except Exception:
                        pass
        except Exception:
            pass
        return bool(re.search(r"(?im)^\s*(?:pro|ultra|teams)\s*$", body_text(page)))

    def open_plan_manager(page):
        if manage_pattern.search(body_text(page)):
            return True
        open_profile_menu(page)
        titles = page.get_by_text(
            re.compile(r"^\s*(?:Pro|Ultra|Teams)\s*$", re.I), exact=True
        )
        try:
            count = min(titles.count(), 30)
        except Exception:
            count = 0
        for index in range(count):
            title = titles.nth(index)
            try:
                if not title.is_visible():
                    continue
                target = title
                container = title.locator("xpath=ancestor::div[1]")
                for _ in range(7):
                    candidate_text = str(container.inner_text(timeout=400) or "")
                    if re.search(r"renews?|expires?|gia\s+h[aạ]n|h[eế]t\s+h[aạ]n", candidate_text, re.I):
                        target = container
                        break
                    container = container.locator("xpath=..")
                try:
                    target.click(timeout=2000)
                except Exception:
                    target.click(timeout=2000, force=True)
                deadline = time.time() + 6
                while time.time() < deadline:
                    if manage_pattern.search(body_text(page)):
                        return True
                    page.wait_for_timeout(250)
            except Exception:
                continue
        return manage_pattern.search(body_text(page)) is not None

    with sync_playwright() as playwright:
        browser = connect_browser(playwright, cdp_url, user, stop_event)
        pages = [
            candidate
            for browser_context in browser.contexts
            for candidate in browser_context.pages
            if not candidate.is_closed()
        ]
        page = next(
            (
                candidate
                for candidate in pages
                if re.search(r"https?://(?:[^/]+\.)?capcut\.com/", str(candidate.url or ""), re.I)
            ),
            pages[0] if pages else None,
        )
        if page is None:
            raise RuntimeError("No live CapCut page remained after payment")
        page.bring_to_front()
        # CapCut updates the paid-plan entitlement asynchronously after the
        # checkout closes. Give the backend/UI five seconds before refreshing
        # the authenticated workspace and looking for the plan manager.
        print(
            f"[REG TREO][CANCEL] Payment confirmed; waiting 5 seconds for "
            f"the Pro entitlement to update: {user}"
        )
        page.wait_for_timeout(5000)
        workspace_url = str((context or {}).get("payment_workspace_url") or "").strip()
        if workspace_url:
            try:
                # Payment is often completed from a fallback tab that still
                # has the pricing sheet open. Re-enter the known authenticated
                # workspace so the account menu can be opened reliably.
                page.goto(workspace_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
                print(f"[REG TREO][CANCEL] Reopened clean workspace for {user}")
            except Exception as exc:
                print(f"[REG TREO][CANCEL] Workspace reopen warning for {user}: {exc}")
        else:
            page.wait_for_timeout(500)

        # Reload and inspect up to three times. A successful payment can take
        # several seconds to expose both the Pro label and the cancellation
        # entry in the account menu. Keep checks two seconds apart.
        plan_manager_opened = False
        refresh_errors = []
        for refresh_attempt in range(1, 4):
            try:
                print(
                    f"[REG TREO][CANCEL] Reloading paid workspace "
                    f"({refresh_attempt}/3): {user}"
                )
                page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                refresh_errors.append(str(exc))
                print(
                    f"[REG TREO][CANCEL] Reload {refresh_attempt}/3 warning "
                    f"for {user}: {exc}"
                )
            page.wait_for_timeout(2000)

            already_cancelled, evidence = cancellation_visible(page)
            if already_cancelled:
                if context is not None:
                    context["subscription_cancelled"] = True
                    context["cancellation_evidence"] = evidence
                return True
            if open_plan_manager(page):
                plan_manager_opened = True
                print(
                    f"[REG TREO][CANCEL] Pro plan manager found after reload "
                    f"{refresh_attempt}/3: {user}"
                )
                break
            print(
                f"[REG TREO][CANCEL] Pro/cancellation control not ready after "
                f"reload {refresh_attempt}/3; retrying in 2 seconds: {user}"
            )

        if not plan_manager_opened:
            detail = f"; reload warnings: {' | '.join(refresh_errors)}" if refresh_errors else ""
            raise RuntimeError(
                f"Could not open CapCut auto-renewal manager after 3 reloads{detail}"
            )
        already_cancelled, evidence = cancellation_visible(page)
        if already_cancelled:
            if context is not None:
                context["subscription_cancelled"] = True
                context["cancellation_evidence"] = evidence
            return True

        cancel_clicked = (
            click_exact_dom_text(
                page,
                (
                    "Cancel subscription",
                    "Cancel plan",
                    "Turn off auto-renewal",
                    "Hủy gói đăng ký",
                    "Huỷ gói đăng ký",
                ),
            )
            or click_matching_control(page, cancel_pattern)
        )
        if not cancel_clicked:
            # The current subscription modal paints this small action through
            # a framework layer that exposes text in body.innerText but no
            # clickable DOM/accessibility node. Use its stable position only
            # after positively identifying the manager and its active fields.
            try:
                viewport = page.evaluate("() => ({width: innerWidth, height: innerHeight})")
                page.mouse.click(
                    int(viewport["width"] * 0.573),
                    int(viewport["height"] * 0.501),
                )
                cancel_clicked = True
            except Exception as exc:
                raise RuntimeError(f"Could not click subscription modal action: {exc}") from exc
        if not cancel_clicked:
            raise RuntimeError("Cancel subscription control was not visible")
        print(f"[REG TREO][CANCEL] Cancellation started for {user}")
        page.wait_for_timeout(700)

        reason_selected = False
        for _ in range(12):
            cancelled, evidence = cancellation_visible(page)
            if cancelled:
                if context is not None:
                    context["subscription_cancelled"] = True
                    context["cancellation_evidence"] = evidence
                print(f"[REG TREO][CANCEL] Subscription cancelled for {user}: {evidence}")
                return True

            if not reason_selected:
                reason_options = page.locator(
                    '[role="radio"], input[type="radio"], [class*="reason" i] [role="option"]'
                )
                if click_first(reason_options, timeout=1200):
                    reason_selected = True
                    page.wait_for_timeout(300)

            confirmation_clicked = click_exact_dom_text(
                page,
                (
                    "Cancel auto-renewal",
                    "Confirm cancellation",
                    "Continue to cancel",
                    "Cancel anyway",
                    "Yes, cancel",
                ),
            ) or click_matching_control(page, confirm_pattern)
            if not confirmation_clicked:
                confirmation_text = body_text(page).casefold()
                if (
                    "cancel auto-renewal?" in confirmation_text
                    and "not now" in confirmation_text
                    and "cancel auto-renewal" in confirmation_text
                ):
                    try:
                        viewport = page.evaluate("() => ({width: innerWidth, height: innerHeight})")
                        page.mouse.click(
                            int(viewport["width"] * 0.571),
                            int(viewport["height"] * 0.746),
                        )
                        confirmation_clicked = True
                    except Exception:
                        pass
            if confirmation_clicked:
                page.wait_for_timeout(800)
                continue
            page.wait_for_timeout(500)

        # Re-open the manager once; some CapCut versions close the confirmation
        # dialog and only expose "Expires on" in a freshly rendered card.
        open_profile_menu(page)
        open_plan_manager(page)
        cancelled, evidence = cancellation_visible(page)
        if cancelled:
            if context is not None:
                context["subscription_cancelled"] = True
                context["cancellation_evidence"] = evidence
            print(f"[REG TREO][CANCEL] Subscription cancelled for {user}: {evidence}")
            return True
        if context is not None:
            context["subscription_cancelled"] = False
        raise RuntimeError("CapCut did not confirm that auto-renewal was disabled")


def patch_hold_proxy(namespace, registration_workflow=None):
    """Patch proxy handling and use the modern registration flow in both modes."""
    app_class = namespace["RegCapCutApp"]
    original_init = app_class.__init__
    original_proxy_for_worker = app_class.proxy_for_worker
    original_run_worker = getattr(app_class, "run_worker", None)
    write_result = namespace.get("write_result", lambda *_args, **_kwargs: None)
    oauth_export_line = namespace.get(
        "oauth_export_line",
        lambda account: account.get("api")
        or f"{account.get('user', '')}|{account.get('passnew', '')}",
    )
    workflow_interrupted = namespace.get("CapCutWorkflowInterrupted", RuntimeError)
    signup_url = namespace["CAPCUT_SIGN_UP_URL"]
    resolve_window_layout = sys.modules["core.settings"].resolve_browser_window_layout

    def write_cancelled_result(account, export_lock, export_dir="export"):
        target = Path(export_dir) / "cancelled.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        with export_lock:
            with target.open("a", encoding="utf-8") as file:
                file.write(str(oauth_export_line(account)).rstrip("\r\n") + "\n")
                file.flush()
        return target

    def write_unpaid_result(account, export_lock, export_dir="export"):
        target = Path(export_dir) / "chua_pay.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        with export_lock:
            with target.open("a", encoding="utf-8") as file:
                file.write(str(oauth_export_line(account)).rstrip("\r\n") + "\n")
                file.flush()
        return target

    def wait_until_browser_closed(process, user, stop_event, reason="UNPAID"):
        """Wait only for the user's Chrome X, then immediately release the worker."""
        reason_label = str(reason or "HOLD").upper()
        print(
            f"[REG TREO][{reason_label}] Waiting for user to close Chrome for {user}; "
            "the next account will start immediately after the window closes"
        )
        while process is not None and process.poll() is None:
            if stop_event.is_set():
                try:
                    process.terminate()
                except Exception:
                    pass
                return False
            time.sleep(0.5)
        print(
            f"[REG TREO][{reason_label}] Chrome closed by user; "
            f"advancing queue: {user}"
        )
        return True

    def toggle_pause(self):
        task = getattr(self, "task_thread", None)
        if task is None or not task.is_alive():
            print("No Reg CapCut workflow is running")
            return False
        if self.stop_event.is_paused():
            self.stop_event.resume()
            button_text = "Tạm dừng"
            button_color = "#f59e0b"
            active_color = "#d97706"
            print("Reg CapCut workflow resumed")
        else:
            self.stop_event.pause()
            button_text = "Tiếp tục"
            button_color = "#2563eb"
            active_color = "#1d4ed8"
            print("Reg CapCut workflow paused")
        pause_button = getattr(self, "pause_button", None)
        if pause_button is not None:
            pause_button.configure(
                text=button_text,
                bg=button_color,
                activebackground=active_color,
            )
        return self.stop_event.is_paused()

    def versioned_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        if not hasattr(self, "active_standalone_processes"):
            self.active_standalone_processes = {}
        if not isinstance(getattr(self, "stop_event", None), PauseAwareEvent):
            self.pause_event = threading.Event()
            self.stop_event = PauseAwareEvent(
                getattr(self, "stop_event", None),
                self.pause_event,
            )
        else:
            self.pause_event = self.stop_event._pause_event
        # A packaged release carries Chrome 154 under PyInstaller's resource
        # directory. Prefer it so the portable folder does not depend on the
        # absolute path used on the build machine.
        bundled_chromium = resource_root() / "chrome-154"
        adjacent_chromium = application_root() / "chrome-154"
        collected_chromium = application_root() / "_internal" / "chrome-154"
        for chromium_candidate in (
            bundled_chromium,
            adjacent_chromium,
            collected_chromium,
        ):
            if (chromium_candidate / "chrome.exe").is_file():
                self.app_settings.chromium_path = str(chromium_candidate)
                break
        root = args[0] if args else kwargs.get("root", getattr(self, "root", None))
        self.browser_choice = _read_browser_choice()
        # "Chrome máy" must mean the installed Google Chrome, not another
        # Chromium path that happened to be stored in the legacy settings.
        self._machine_chrome_path = _find_system_chrome()
        try:
            if root is not None:
                root.title(f"Reg CapCut v{LEGACY_PATCH_VERSION}")
        except Exception:
            pass

        # Add one mutually-exclusive Chrome selector to the registration bar
        # shared by both Reg thuong and Reg treo.
        try:
            tk = namespace["tk"]

            def descendants(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from descendants(child)

            registration_bar = None
            stop_button = None
            for widget in descendants(root):
                children = widget.winfo_children()
                labels = set()
                candidate_stop_button = None
                for child in children:
                    try:
                        child_label = str(child.cget("text")).strip().lower()
                        labels.add(child_label)
                        if child_label == "stop":
                            candidate_stop_button = child
                    except Exception:
                        continue
                if "reg treo" in labels and any(label.startswith("reg th") for label in labels):
                    registration_bar = widget
                    stop_button = candidate_stop_button
                    break

            if registration_bar is not None:
                self.browser_choice_var = tk.StringVar(master=root, value=self.browser_choice)

                def save_browser_choice():
                    selected = self.browser_choice_var.get()
                    self.browser_choice = selected
                    _write_browser_choice(selected)
                    label = "Chrome 154" if selected == _BROWSER_CHOICE_BUNDLED else "Chrome máy"
                    print(f"[BROWSER] Selected {label} for Reg thường and Reg treo")

                radio_options = (
                    ("Chrome 154", _BROWSER_CHOICE_BUNDLED),
                    ("Chrome máy", _BROWSER_CHOICE_SYSTEM),
                )
                self.browser_choice_buttons = []
                for label, value in radio_options:
                    button = tk.Radiobutton(
                        registration_bar,
                        text=label,
                        variable=self.browser_choice_var,
                        value=value,
                        command=save_browser_choice,
                        bg="#ffffff",
                        activebackground="#ffffff",
                        selectcolor="#ffffff",
                        takefocus=False,
                        padx=2,
                    )
                    button.pack(side="left", padx=(5, 0), pady=6)
                    self.browser_choice_buttons.append(button)

                self.pause_button = tk.Button(
                    registration_bar,
                    text="Tạm dừng",
                    command=self.toggle_pause,
                    bg="#f59e0b",
                    fg="#ffffff",
                    activebackground="#d97706",
                    activeforeground="#ffffff",
                    relief="solid",
                    borderwidth=1,
                    takefocus=False,
                    padx=8,
                )
                pause_pack = {
                    "side": "left",
                    "padx": (5, 0),
                    "pady": 6,
                }
                if stop_button is not None:
                    pause_pack["before"] = stop_button
                self.pause_button.pack(**pause_pack)
        except Exception as exc:
            print(f"[BROWSER] Could not add Chrome selector: {exc}")

    def stop(self):
        """Stop workers and close every standalone registration browser now."""
        task = getattr(self, "task_thread", None)
        if task is None or not task.is_alive():
            print("No Reg CapCut workflow is running")
            return
        self.stop_event.set()
        pause_button = getattr(self, "pause_button", None)
        if pause_button is not None:
            try:
                pause_button.configure(
                    text="Tạm dừng",
                    bg="#f59e0b",
                    activebackground="#d97706",
                )
            except Exception:
                pass
        lock = getattr(self, "active_profiles_lock", None)
        if lock is None:
            standalone_to_close = list(
                getattr(self, "active_standalone_processes", {}).items()
            )
        else:
            with lock:
                standalone_to_close = list(
                    getattr(self, "active_standalone_processes", {}).items()
                )
        for user, process in standalone_to_close:
            try:
                if process is not None and process.poll() is None:
                    process.terminate()
                print(f"Stop closed registration Chrome: {user}")
            except Exception as exc:
                print(f"Stop Chrome warning for {user}: {exc}")
        print("Stop requested; waiting for workers to finish")

    def proxy_for_worker(self, index, max_threads):
        proxy = original_proxy_for_worker(self, index, max_threads)
        account = self.current_accounts[index]
        active_windows = max(1, min(max_threads, len(self.current_accounts)))
        account[_WINDOW_SLOT_KEY] = index % active_windows
        account[_WINDOW_TOTAL_KEY] = active_windows
        if not str(account.get("passnew", "") or "").strip():
            fallback_password = str(account.get("passmail", "") or account.get("pass", "") or "").strip()
            if fallback_password:
                account["passnew"] = fallback_password
                if getattr(self, "hold_mode", False):
                    print(f"[REG TREO] Using the supplied account password for CapCut registration: {account['user']}")
                else:
                    print(f"[REG THUONG] Using the supplied account password for CapCut registration: {account['user']}")
        # Reg treo activates this upstream only after registration. Reg thuong
        # activates it before launch, matching its original proxy behavior.
        account[_PENDING_PROXY_KEY] = proxy
        return proxy

    def start_standalone_chromium(self, account, index):
        browser_choice = getattr(self, "browser_choice", _BROWSER_CHOICE_BUNDLED)
        if browser_choice == _BROWSER_CHOICE_SYSTEM:
            browser = _find_system_chrome(getattr(self, "_machine_chrome_path", None))
            if browser is None:
                raise RuntimeError(
                    "Khong tim thay Google Chrome tren may. Hay cai Chrome hoac chon Chrome 154."
                )
            browser_label = "Chrome máy"
        else:
            browser = _chrome_executable(getattr(self.app_settings, "chromium_path", ""))
            if browser is None:
                for chromium_candidate in (
                    resource_root() / "chrome-154",
                    application_root() / "chrome-154",
                    application_root() / "_internal" / "chrome-154",
                ):
                    browser = _chrome_executable(chromium_candidate)
                    if browser is not None:
                        break
            if browser is None:
                raise RuntimeError("Chrome 154 khong ton tai trong thu muc chuong trinh")
            browser_label = "Chrome 154"
        print(f"[BROWSER] Launching {browser_label}: {browser}")

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()

        hold_mode = bool(getattr(self, "hold_mode", False))
        profile_root = "reg treo" if hold_mode else "reg thuong"
        profile_dir = Path(profile_root) / "chromium_profiles" / str(index + 1)
        profile_dir.mkdir(parents=True, exist_ok=True)
        pending_proxy = account.pop(_PENDING_PROXY_KEY, None)
        window_slot = int(account.pop(_WINDOW_SLOT_KEY, 0))
        total_windows = max(1, int(account.pop(_WINDOW_TOTAL_KEY, 1)))
        window_layout = resolve_window_layout(
            self.app_settings.browser_window,
            window_index=min(window_slot, total_windows - 1),
            total_windows=total_windows,
        )
        thuecloud_enabled = bool(getattr(getattr(self.app_settings, "thuecloud", None), "enabled", False))
        relay = DeferredProxyRelay() if pending_proxy is not None or thuecloud_enabled else None
        if relay is not None and not hold_mode:
            raw_proxy = self.get_account_raw_proxy(account, pending_proxy)
            upstream = parse_proxy(raw_proxy)
            if upstream is None:
                relay.close()
                raise RuntimeError("Proxy rong cho Reg thuong")
            relay.activate(upstream)
            print(f"[REG THUONG] Activated registration proxy for {account['user']}: {upstream['server']}")
        args = [
            str(browser),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir.resolve()}",
            "--incognito",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-component-update",
            "--disable-background-networking",
            "--disable-default-apps",
            "--disable-sync",
            "--disk-cache-size=104857600",
            f"--window-position={window_layout.position}",
            f"--window-size={window_layout.size}",
        ]
        if relay:
            # Registration traffic exits DIRECT through this relay.  The relay
            # switches upstream only after run_capcut_workflow returns success.
            args.append(f"--proxy-server={relay.server}")
        args.append(signup_url)
        try:
            process = subprocess.Popen(
                args,
                cwd=str(browser.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except BaseException:
            if relay:
                relay.close()
            raise
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.25)
        else:
            process.terminate()
            if relay:
                relay.close()
            raise RuntimeError("Chromium khong mo duoc cong dieu khien")

        address = f"127.0.0.1:{port}"

        def activate_proxy():
            if relay is None:
                return
            raw_proxy = self.get_account_raw_proxy(account, pending_proxy)
            upstream = parse_proxy(raw_proxy)
            if upstream is None:
                raise RuntimeError("Proxy rong sau khi dang ky thanh cong")
            relay.activate(upstream)
            print(f"[REG TREO] Activated proxy after registration for {account['user']}: {upstream['server']}")

        start_result = types.SimpleNamespace(
            success=True,
            remote_debugging_address=address,
            browser_location=address,
            incognito=True,
            reuse_startup_context=True,
            proxy=None,
            reg_treo=hold_mode,
            activate_proxy=activate_proxy if relay and hold_mode else None,
        )
        return RelayProcess(process, relay) if relay else process, start_result

    original_workflow = namespace["run_capcut_workflow"]
    modern_workflow = registration_workflow or original_workflow

    def run_capcut_workflow(account=None, context=None):
        start_result = (context or {}).get("start_result")
        is_reg_treo = bool(getattr(start_result, "reg_treo", False))
        # Reg thuong uses the same resilient birthday/OTP/login workflow as
        # Reg treo. Only Reg treo continues into proxy activation, checkout,
        # and browser hold; Reg thuong returns immediately after account ready.
        if context is not None:
            context["allow_existing_login"] = True
        result = modern_workflow(account, context)
        if result and is_reg_treo:
            activate_proxy = getattr(start_result, "activate_proxy", None)
            if activate_proxy:
                activate_proxy()
            try:
                checkout_paid = bool(
                    open_pro_checkout_across_five_tabs(account, context, modern_workflow)
                )
                if context is not None:
                    context["checkout_success"] = checkout_paid
                    if checkout_paid:
                        context.pop("checkout_error", None)
                    else:
                        context["checkout_error"] = "payment was not confirmed"
            except Exception as exc:
                # Registration is already complete.  Preserve the successful
                # result and hold Chrome so the user can finish manually.
                safe_error = str(exc).encode("ascii", "replace").decode("ascii")
                if context is not None:
                    context["checkout_success"] = False
                    context["checkout_error"] = safe_error
                print(f"[REG TREO][PRO] Automatic Pro checkout warning for {account['user']}: {safe_error}")
        elif result:
            print(
                f"[REG THUONG] Account ready for {account['user']}; "
                "finishing this task and continuing the queue"
            )
        return result

    run_capcut_workflow.__wrapped__ = original_workflow
    run_capcut_workflow.__reg_treo_workflow__ = modern_workflow

    def run_worker(self, account, group_name, index, total, semaphore, max_threads):
        """Run both buttons through standalone incognito Chromium 154."""
        with semaphore:
            standalone_process = None
            hold_mode = bool(getattr(self, "hold_mode", False))
            workflow_context = None
            try:
                if self.stop_event.is_set():
                    self.mark_account_status(account, "stopped")
                    return
                self.mark_account_status(account, "running")
                self.proxy_for_worker(index, max_threads)
                standalone_process, start_result = self._start_standalone_chromium(account, index)
                with self.active_profiles_lock:
                    if not hasattr(self, "active_standalone_processes"):
                        self.active_standalone_processes = {}
                    self.active_standalone_processes[account["user"]] = standalone_process
                mode_label = "REG TREO" if hold_mode else "REG THUONG"
                print(f"[{mode_label}] Started Chromium standalone for {account['user']}")
                if self.stop_event.is_set():
                    self.mark_account_status(account, "stopped")
                    return

                workflow_context = {
                    "start_result": start_result,
                    "stop_event": self.stop_event,
                    "on_login_blocked": self._request_expressvpn_reset,
                }
                workflow_ok = bool(run_capcut_workflow(account, workflow_context))
                if self.stop_event.is_set():
                    self.mark_account_status(account, "stopped")
                    return
                payment_confirmed = bool(
                    workflow_context and workflow_context.get("payment_confirmed")
                )
                account_success = workflow_ok and (not hold_mode or payment_confirmed)
                if account_success:
                    self.mark_account_status(account, "Đã pay" if hold_mode else "true")
                    write_result(account, True, self.export_lock)
                    self.queue_sheet_success(account)
                    if hold_mode:
                        print(
                            f"[REG TREO][PAYMENT] Paid account saved; starting "
                            f"subscription cancellation: {account['user']}"
                        )
                        try:
                            cancelled = bool(
                                cancel_capcut_subscription(
                                    account,
                                    workflow_context,
                                    modern_workflow,
                                )
                            )
                            if not cancelled:
                                raise RuntimeError(
                                    "CapCut cancellation returned without confirmation"
                                )
                            self.write_cancelled_result(account, self.export_lock)
                            self.mark_account_status(account, "Đã pay - Đã huỷ gói")
                            print(
                                f"[REG TREO][CANCEL] Cancelled account saved; "
                                f"advancing to next account: {account['user']}"
                            )
                        except Exception as exc:
                            safe_error = str(exc).encode("ascii", "replace").decode("ascii")
                            if workflow_context is not None:
                                workflow_context["subscription_cancelled"] = False
                                workflow_context["cancellation_error"] = safe_error
                                workflow_context["cancellation_pending"] = True
                            self.mark_account_status(account, "Đã pay - Chưa huỷ gói")
                            print(
                                f"[REG TREO][CANCEL] Paid account remains exported but "
                                f"cancellation was not confirmed for {account['user']}: "
                                f"{safe_error}"
                            )
                            wait_until_browser_closed(
                                standalone_process,
                                account["user"],
                                self.stop_event,
                                reason="CANCEL",
                            )
                    else:
                        print(f"Success: {account['user']}")
                elif workflow_ok and hold_mode:
                    checkout_tab_closed = bool(
                        workflow_context and workflow_context.get("checkout_tab_closed")
                    )
                    self.mark_account_status(account, "Chưa pay")
                    self.write_unpaid_result(account, self.export_lock)
                    print(
                        f"[REG TREO][PAYMENT] Account was registered but payment was "
                        f"not confirmed; unpaid account saved: {account['user']}"
                    )
                    if not checkout_tab_closed:
                        wait_until_browser_closed(
                            standalone_process,
                            account["user"],
                            self.stop_event,
                        )
                else:
                    self.mark_account_status(account, "change fail")
                    write_result(account, False, self.export_lock)
                    print(f"Change failed: {account['user']}")
            except workflow_interrupted as exc:
                self.mark_account_status(account, "stopped")
                print(f"Workflow stopped: {account['user']} - {exc}")
            except Exception as exc:
                if self.stop_event.is_set():
                    self.mark_account_status(account, "stopped")
                    print(f"Workflow stopped: {account['user']} - {exc}")
                    return
                error_text = str(exc).lower()
                if any(marker in error_text for marker in ("login", "password", "email step", "sign up")):
                    self._request_expressvpn_reset()
                self.mark_account_status(account, f"change fail: {exc}")
                write_result(account, False, self.export_lock)
                print(f"Error: {account['user']} - {exc}")
            finally:
                with self.active_profiles_lock:
                    self.active_profiles.pop(account["user"], None)
                    getattr(self, "active_standalone_processes", {}).pop(
                        account["user"], None
                    )
                if (
                    standalone_process is not None
                    and standalone_process.poll() is None
                    and (
                        self.stop_event.is_set()
                        or
                        not hold_mode
                        or bool(
                            workflow_context
                            and (
                                workflow_context.get("checkout_tab_closed")
                                or workflow_context.get("subscription_cancelled")
                                or (
                                    workflow_context.get("payment_confirmed")
                                    and not workflow_context.get("cancellation_pending")
                                )
                            )
                        )
                    )
                ):
                    standalone_process.terminate()
                    mode_label = "REG TREO" if hold_mode else "REG THUONG"
                    print(f"[{mode_label}] Closed Chromium after account task: {account['user']}")

    if original_run_worker is not None:
        run_worker.__wrapped__ = original_run_worker

    app_class.proxy_for_worker = proxy_for_worker
    app_class.__init__ = versioned_init
    app_class.toggle_pause = toggle_pause
    app_class.stop = stop
    app_class._start_standalone_chromium = start_standalone_chromium
    app_class.write_cancelled_result = staticmethod(write_cancelled_result)
    app_class.write_unpaid_result = staticmethod(write_unpaid_result)
    app_class.run_worker = run_worker
    namespace["run_capcut_workflow"] = run_capcut_workflow
    return app_class


def prepare_legacy_app():
    install_legacy_importer()
    namespace = load_legacy_namespace()
    modern_workflow = load_modern_registration_workflow()
    patch_hold_proxy(namespace, modern_workflow)
    return namespace


def main():
    namespace = prepare_legacy_app()
    if os.environ.get("REGCAPCUT_V75_PROXY_SMOKE") == "1":
        workflow = namespace["run_capcut_workflow"]
        legacy_source = getattr(workflow, "__wrapped__", workflow).__code__.co_filename
        modern_source = getattr(workflow, "__reg_treo_workflow__", workflow).__code__.co_filename
        print(
            f"regcapcut v{LEGACY_PATCH_VERSION} loaded; legacy={legacy_source}; "
            f"registration={modern_source}"
        )
        return
    namespace["main"]()


if __name__ == "__main__":
    main()
