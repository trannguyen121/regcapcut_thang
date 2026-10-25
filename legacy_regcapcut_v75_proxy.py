"""Run the original regcapcut v7.5 code with one hold-mode proxy patch."""

from __future__ import annotations

import importlib.abc
import importlib.util
import base64
import marshal
import os
import re
import select
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
LEGACY_PATCH_VERSION = "7.5.200"
_PENDING_PROXY_KEY = "_v75_pending_proxy"
_WINDOW_SLOT_KEY = "_v75_window_slot"
_WINDOW_TOTAL_KEY = "_v75_window_total"


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


def application_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_root() -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    return Path(bundle_root).resolve() if bundle_root else application_root()


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
            try:
                control.click(timeout=5000)
            except Exception:
                try:
                    control.click(timeout=5000, force=True)
                except Exception:
                    control.evaluate("element => element.click()", timeout=2000)

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
                    time.sleep(0.5)
                    return True
                if time.time() >= deadline:
                    return False
                time.sleep(0.25)

        def wait_for_top_upgrade(page, timeout=60):
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
                time.sleep(0.5)
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
                time.sleep(1.5)
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
                            time.sleep(1.5)
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
                    time.sleep(0.75)

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
                time.sleep(0.5)
            return None

        def reload_before_upgrade(page, location):
            """Refresh one authenticated workspace and clear blocking overlays."""
            page.bring_to_front()
            print(f"[REG TREO][PRO] Reloading {location} before Upgrade for {user}")
            try:
                page.reload(wait_until="domcontentloaded", timeout=60000)
            except Exception as exc:
                try:
                    page.evaluate("window.stop()")
                except Exception:
                    pass
                raise RuntimeError(f"reload before Upgrade failed: {exc}") from exc
            time.sleep(2.5)
            dismiss_overlays(page)
            if not is_account_workspace(page.url):
                raise RuntimeError(f"reload left authenticated workspace: {page.url}")
            print(f"[REG TREO][PRO] Reloaded {location}; waiting for Upgrade for {user}")

        def attempt_checkout(
            page,
            location,
            reload_before=True,
            stop_when_popup_opens=False,
            progress=None,
        ):
            """Click only the verified offer and require a stable checkout."""
            progress = progress if progress is not None else {}
            progress["top_upgrade_clicked"] = False
            progress["plan_action_clicked"] = False
            if reload_before:
                reload_before_upgrade(page, location)
            else:
                page.bring_to_front()
            print(f"[REG TREO][PRO] Waiting for top Upgrade on {location} for {user}")
            top_upgrade = wait_for_top_upgrade(page)
            if top_upgrade is None:
                raise RuntimeError("top Upgrade did not appear within 60 seconds")
            click_control(top_upgrade)
            progress["top_upgrade_clicked"] = True
            print(f"[REG TREO][PRO] Clicked top Upgrade on {location} for {user}")
            time.sleep(1.5)
            dismiss_discount_offer_popup(page, timeout=6)

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
                time.sleep(1.5)

            pro_button = find_pro_button(page, timeout=10)
            if pro_button is None:
                raise RuntimeError("22,000 VND monthly Pro action did not appear within 10 seconds")

            pages_before = [candidate for item in browser.contexts for candidate in item.pages]
            event_offset = len(opened_pages)
            click_control(pro_button)
            progress["plan_action_clicked"] = True
            print(f"[REG TREO][PRO] Clicked 22,000d Monthly Upgrade/Purchase on {location} for {user}")

            checkout_popup = None
            learn_more_handled = False
            deadline = time.time() + 5
            while time.time() < deadline:
                checkout_popup = next(
                    (
                        candidate
                        for candidate in opened_pages[event_offset:]
                        if candidate not in pages_before
                    ),
                    None,
                )
                if checkout_popup is None:
                    current_pages = [candidate for item in browser.contexts for candidate in item.pages]
                    checkout_popup = next(
                        (candidate for candidate in current_pages if candidate not in pages_before),
                        None,
                    )
                if checkout_popup is not None:
                    break
                if not learn_more_handled:
                    learn_more = page.get_by_role(
                        "button", name=re.compile(r"^\s*Learn more\s*$", re.I)
                    )
                    if learn_more.count() and learn_more.first.is_visible():
                        click_control(learn_more.first)
                        learn_more_handled = True
                        time.sleep(0.75)
                        pro_button = find_pro_button(page, timeout=10)
                        if pro_button is not None:
                            click_control(pro_button)
                time.sleep(0.5)

            if checkout_popup is None:
                raise RuntimeError("checkout popup did not open within 5 seconds")

            # The registered tab is always the primary attempt. Once its exact
            # 22,000 VND Upgrade/Purchase action opens checkout, leave that
            # popup untouched and stop the automation immediately. In
            # particular, do not let slow checkout rendering or a later popup
            # close trigger creation of the five fallback tabs.
            if stop_when_popup_opens:
                print(
                    f"[REG TREO][PRO] Checkout popup opened from {location} "
                    f"for {user}; skipping all 5 fallback tabs"
                )
                return

            verified_checkout = False
            checkout_text = ""
            amount_deadline = time.time() + 30
            while time.time() < amount_deadline:
                if checkout_popup.is_closed():
                    raise RuntimeError("checkout popup closed before price verification")
                try:
                    checkout_text = checkout_popup.locator("body").inner_text(timeout=2000)
                except Exception:
                    checkout_text = ""
                if _checkout_due_today_is_22000(checkout_text):
                    verified_checkout = True
                    break
                time.sleep(0.5)
            if not verified_checkout:
                try:
                    checkout_popup.close()
                except Exception:
                    pass
                compact_checkout = re.sub(r"\s+", " ", checkout_text).strip()[:240]
                checkout_url = str(getattr(checkout_popup, "url", "") or "")
                raise RuntimeError(
                    "checkout Due today is not 22,000 VND; rejected popup: "
                    f"url={checkout_url} | {compact_checkout}"
                )
            print(
                f"[REG TREO][PRO] Verified checkout Due today 22,000d "
                f"from {location} for {user}"
            )

            stability_deadline = time.time() + 8
            while time.time() < stability_deadline:
                if checkout_popup.is_closed():
                    raise RuntimeError("checkout popup closed unexpectedly")
                time.sleep(0.5)
            print(
                f"[REG TREO][PRO] Checkout popup stable from {location} "
                f"for {user}; entering hold mode"
            )

        # Always use the just-registered incognito tab first. Scan the pricing
        # sheet for only 10 seconds per reload and try the registered tab twice.
        # If the exact offer still does not appear, continue with the five
        # sibling tabs; never click a different price just to make progress.
        force_first_failure = bool((context or {}).get("force_first_checkout_failure"))
        if force_first_failure:
            print(f"[REG TREO][PRO] Forced first checkout failure for E2E: {user}")
        else:
            for primary_attempt in range(1, 3):
                primary_progress = {}
                try:
                    attempt_checkout(
                        workspace_page,
                        f"registered tab attempt {primary_attempt}/2",
                        stop_when_popup_opens=True,
                        progress=primary_progress,
                    )
                    return
                except Exception as exc:
                    safe_error = str(exc).encode("ascii", "replace").decode("ascii")
                    if primary_progress.get("plan_action_clicked"):
                        print(
                            f"[REG TREO][PRO] Registered tab clicked 22,000d Upgrade "
                            f"but no checkout popup appeared for {user}: {safe_error}; "
                            f"opening 5 incognito fallback tabs"
                        )
                        break
                    print(
                        f"[REG TREO][PRO] Registered tab has not clicked 22,000d "
                        f"Upgrade for {user} (attempt {primary_attempt}/2): {safe_error}"
                    )
                    if primary_attempt < 2:
                        print(
                            f"[REG TREO][PRO] Retrying the registered tab before "
                            f"any fallback tab is allowed for {user}"
                        )
                        time.sleep(1)
                        continue
                    print(
                        f"[REG TREO][PRO] Exact 22,000d offer was not found on "
                        f"the registered tab after 2 reloads for {user}; "
                        f"opening 5 incognito fallback tabs"
                    )
                    break

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
                with browser_context.expect_page(timeout=15000) as page_info:
                    workspace_page.locator(f"#{link_id}").click(
                        force=True,
                        timeout=5000,
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

        # Keep the registration/workspace page and open five additional tabs of
        # that exact signed-in space. Load them sequentially with a short gap;
        # opening five navigations at once can trigger CapCut's Access Denied page.
        candidate_tabs = []
        for tab_number in range(1, 6):
            page = open_adjacent_tab_from_workspace(tab_number)
            navigation_error = None
            for navigation_attempt in range(1, 3):
                try:
                    page.goto(workspace_url, wait_until="domcontentloaded", timeout=30000)
                    if is_account_workspace(page.url):
                        navigation_error = None
                        break
                    navigation_error = RuntimeError(f"redirected outside workspace: {page.url}")
                except Exception as exc:
                    navigation_error = exc
                if navigation_attempt < 2:
                    time.sleep(2)
            if navigation_error is not None:
                safe_error = str(navigation_error).encode("ascii", "replace").decode("ascii")
                print(f"[REG TREO][PRO] Tab {tab_number}/5 navigation warning for {user}: {safe_error}")
            candidate_tabs.append((page, navigation_error))
            print(f"[REG TREO][PRO] Opened adjacent workspace tab {tab_number}/5 for {user}")
            time.sleep(1.2)

        failures = []
        for reload_round in range(1, 3):
            print(
                f"[REG TREO][PRO] Reloading all 5 fallback tabs "
                f"round {reload_round}/2 for {user}"
            )
            reload_results = []
            # Finish refreshing every tab before clicking Upgrade on any tab.
            for tab_number, (page, _navigation_error) in enumerate(candidate_tabs, start=1):
                location = f"fallback tab {tab_number}/5 round {reload_round}/2"
                try:
                    reload_before_upgrade(page, location)
                    reload_results.append((page, None))
                except Exception as exc:
                    reload_results.append((page, exc))

            for tab_number, (page, reload_error) in enumerate(reload_results, start=1):
                location = f"fallback tab {tab_number}/5 round {reload_round}/2"
                try:
                    if reload_error is not None:
                        raise RuntimeError(f"workspace reload failed: {reload_error}")
                    attempt_checkout(page, location, reload_before=False)
                    return
                except Exception as exc:
                    safe_error = str(exc).encode("ascii", "replace").decode("ascii")
                    failures.append(f"round {reload_round} tab {tab_number}: {safe_error}")
                    print(
                        f"[REG TREO][PRO] Round {reload_round}/2 tab {tab_number}/5 "
                        f"did not produce a stable popup for {user}: {safe_error}"
                    )
                    if tab_number < len(reload_results):
                        # This tab may have crashed or closed. Sleep outside
                        # Playwright so one dead page cannot abort the rest of
                        # the five-tab scan.
                        time.sleep(1)

        raise RuntimeError(
            "All 5 Upgrade tabs failed after 2 reload rounds: " + " | ".join(failures)
        )


def patch_hold_proxy(namespace, registration_workflow=None):
    """Patch proxy handling and use the modern workflow only in hold mode."""
    app_class = namespace["RegCapCutApp"]
    original_init = app_class.__init__
    original_proxy_for_worker = app_class.proxy_for_worker
    signup_url = namespace["CAPCUT_SIGN_UP_URL"]
    resolve_window_layout = sys.modules["core.settings"].resolve_browser_window_layout

    def versioned_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        # A packaged release carries Chrome 154 under PyInstaller's resource
        # directory. Prefer it so the portable folder does not depend on the
        # absolute path used on the build machine.
        bundled_chromium = resource_root() / "chrome-154"
        adjacent_chromium = application_root() / "chrome-154"
        for chromium_candidate in (bundled_chromium, adjacent_chromium):
            if (chromium_candidate / "chrome.exe").is_file():
                self.app_settings.chromium_path = str(chromium_candidate)
                break
        root = args[0] if args else kwargs.get("root", getattr(self, "root", None))
        try:
            if root is not None:
                root.title(f"Reg CapCut v{LEGACY_PATCH_VERSION}")
        except Exception:
            pass

    def proxy_for_worker(self, index, max_threads):
        proxy = original_proxy_for_worker(self, index, max_threads)
        if getattr(self, "hold_mode", False):
            account = self.current_accounts[index]
            active_windows = max(1, min(max_threads, len(self.current_accounts)))
            account[_WINDOW_SLOT_KEY] = index % active_windows
            account[_WINDOW_TOTAL_KEY] = active_windows
            if not str(account.get("passnew", "") or "").strip():
                fallback_password = str(account.get("passmail", "") or account.get("pass", "") or "").strip()
                if fallback_password:
                    account["passnew"] = fallback_password
                    print(f"[REG TREO] Using the supplied account password for CapCut registration: {account['user']}")
            # Keep the selection pending.  Do not request/activate the proxy
            # until the original registration workflow reports success.
            account[_PENDING_PROXY_KEY] = proxy
        return proxy

    def start_standalone_chromium(self, account, index):
        configured = Path(str(self.app_settings.chromium_path).strip().strip('"'))
        browser = configured / "chrome.exe" if configured.is_dir() else configured
        if not browser.is_file():
            raise RuntimeError(f"Chromium khong ton tai: {browser}")

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()

        profile_dir = Path("reg treo") / "chromium_profiles" / str(index + 1)
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
            reg_treo=True,
            activate_proxy=activate_proxy if relay else None,
        )
        return RelayProcess(process, relay) if relay else process, start_result

    original_workflow = namespace["run_capcut_workflow"]
    modern_workflow = registration_workflow or original_workflow

    def run_capcut_workflow(account=None, context=None):
        start_result = (context or {}).get("start_result")
        selected_workflow = (
            modern_workflow
            if getattr(start_result, "reg_treo", False)
            else original_workflow
        )
        result = selected_workflow(account, context)
        if result:
            activate_proxy = getattr(start_result, "activate_proxy", None)
            if activate_proxy:
                activate_proxy()
            try:
                open_pro_checkout_across_five_tabs(account, context, selected_workflow)
                if context is not None:
                    context["checkout_success"] = True
                    context.pop("checkout_error", None)
            except Exception as exc:
                # Registration is already complete.  Preserve the successful
                # result and hold Chrome so the user can finish manually.
                safe_error = str(exc).encode("ascii", "replace").decode("ascii")
                if context is not None:
                    context["checkout_success"] = False
                    context["checkout_error"] = safe_error
                print(f"[REG TREO][PRO] Automatic Pro checkout warning for {account['user']}: {safe_error}")
        return result

    run_capcut_workflow.__wrapped__ = original_workflow
    run_capcut_workflow.__reg_treo_workflow__ = modern_workflow

    app_class.proxy_for_worker = proxy_for_worker
    app_class.__init__ = versioned_init
    app_class._start_standalone_chromium = start_standalone_chromium
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
            f"reg_treo={modern_source}"
        )
        return
    namespace["main"]()


if __name__ == "__main__":
    main()
