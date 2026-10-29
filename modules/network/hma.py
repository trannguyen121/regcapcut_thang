"""Windows automation helpers for rotating the system-wide HMA VPN IP."""

from __future__ import annotations

import json
import os
import queue
import struct
import threading
import time
import unicodedata
import subprocess
import sys
import ctypes
from ctypes import wintypes
from pathlib import Path

import requests


# HMA's browser-extension native-messaging host. It speaks Chrome native
# messaging (4-byte little-endian length + UTF-8 JSON) on stdin/stdout and
# forwards {"action": ...} requests to the HMA VPN service.
HMA_NATIVE_HOST = Path(
    os.environ.get("ProgramFiles", r"C:\Program Files")
) / "Privax" / "HMA VPN" / "VpnNM.exe"
HMA_NATIVE_ORIGIN = "chrome-extension://poeojclicodamonabcabmapamjkkmnnk/"
HMA_STATE_EVENT = "Vpn_OnStateChanged_SvcNm"

HMA_WINDOW_TITLE = "HMA VPN"
HMA_SWITCH_AUTOMATION_ID = "dashboard_switch"
HMA_SWITCH_X_RATIO = 0.256
HMA_SWITCH_Y_RATIO = 0.500
PUBLIC_IP_ENDPOINTS = (
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
)


def get_public_ip(timeout: float = 10.0) -> str:
    """Return the current public IP using independent lightweight endpoints."""
    errors = []
    for endpoint in PUBLIC_IP_ENDPOINTS:
        try:
            response = requests.get(endpoint, timeout=timeout)
            response.raise_for_status()
            value = response.text.strip()
            if value:
                return value
        except requests.RequestException as exc:
            errors.append(f"{endpoint}: {exc}")
    raise RuntimeError("Could not determine public IP: " + " | ".join(errors))


def _normalize_label(label: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", label.strip().casefold())
        if not unicodedata.combining(character)
    )


def _is_disconnect_label(label: str) -> bool:
    normalized = _normalize_label(label)
    return "disconnect" in normalized or "ngat ket noi" in normalized


def _is_connect_label(label: str) -> bool:
    normalized = _normalize_label(label)
    return (
        ("connect" in normalized and "disconnect" not in normalized)
        or "ket noi" in normalized and "ngat ket noi" not in normalized
    )


def _hma_switch():
    try:
        from pywinauto import Desktop
    except ImportError as exc:
        raise RuntimeError("pywinauto is required to control HMA VPN") from exc

    window = Desktop(backend="uia").window(title=HMA_WINDOW_TITLE)
    try:
        window.wait("exists enabled", timeout=10)
    except Exception as exc:
        raise RuntimeError("HMA VPN window is not running") from exc
    switch = window.child_window(
        auto_id=HMA_SWITCH_AUTOMATION_ID,
        control_type="Button",
    )
    try:
        switch.wait("exists enabled", timeout=10)
    except Exception as exc:
        raise RuntimeError("Could not find the HMA connect/disconnect switch") from exc
    return switch


def _wait_for_switch_state(predicate, timeout: float):
    deadline = time.monotonic() + timeout
    last_label = ""
    while time.monotonic() < deadline:
        switch = _hma_switch()
        try:
            last_label = switch.window_text()
            if predicate(last_label):
                return switch
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f'HMA did not reach the expected state (last button label: "{last_label}")')


def _click_hma_switch() -> None:
    """Click the HMA dashboard switch using only non-blocking Win32 calls."""
    user32 = ctypes.windll.user32

    class Point(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class Rect(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    cursor = Point()
    user32.GetCursorPos(ctypes.byref(cursor))
    window_handle = user32.FindWindowW(None, HMA_WINDOW_TITLE)
    if not window_handle:
        raise RuntimeError("HMA VPN window is not running")
    rectangle = Rect()
    if not user32.GetWindowRect(window_handle, ctypes.byref(rectangle)):
        raise RuntimeError("Could not read the HMA VPN window position")
    width = rectangle.right - rectangle.left
    height = rectangle.bottom - rectangle.top
    x = rectangle.left + round(width * HMA_SWITCH_X_RATIO)
    y = rectangle.top + round(height * HMA_SWITCH_Y_RATIO)
    try:
        # Restore HMA from minimized state and put its CEF surface in front.
        user32.ShowWindow(window_handle, 9)
        user32.SetForegroundWindow(window_handle)
        time.sleep(0.5)
        user32.SetCursorPos(x, y)
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
    finally:
        user32.SetCursorPos(cursor.x, cursor.y)


def _reconnect_hma_once(disconnect_pause: float = 4.0) -> None:
    _click_hma_switch()
    print("[HMA] Clicked the HMA disconnect switch", flush=True)
    if disconnect_pause > 0:
        print(
            f"[HMA] Waiting {disconnect_pause:g}s before reconnecting",
            flush=True,
        )
        time.sleep(disconnect_pause)
    _click_hma_switch()
    print("[HMA] Clicked the HMA reconnect switch", flush=True)
    time.sleep(2)


class HmaNativeClient:
    """Minimal client for HMA's native-messaging host (VpnNM.exe)."""

    def __init__(self, executable: Path = HMA_NATIVE_HOST):
        if not executable.exists():
            raise RuntimeError(f"HMA native host not found: {executable}")
        self._process = subprocess.Popen(
            [str(executable), HMA_NATIVE_ORIGIN, "--parent-window=0"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._messages: queue.Queue = queue.Queue()
        self._next_request_id = 0
        threading.Thread(target=self._read_messages, daemon=True).start()

    def __enter__(self) -> "HmaNativeClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        if self._process.poll() is None:
            self._process.kill()
        self._process.wait(timeout=5)

    def _read_messages(self) -> None:
        stdout = self._process.stdout
        while True:
            header = stdout.read(4)
            if len(header) < 4:
                self._messages.put(None)
                return
            body = stdout.read(struct.unpack("<I", header)[0])
            try:
                self._messages.put(json.loads(body.decode("utf-8")))
            except ValueError:
                continue

    def next_message(self, timeout: float):
        """Return the next reply or event, or None when nothing arrived in time."""
        try:
            message = self._messages.get(timeout=max(timeout, 0.01))
        except queue.Empty:
            return None
        if message is None:
            raise RuntimeError("HMA native host exited unexpectedly")
        return message

    def request(self, action: str, timeout: float = 15.0, **params):
        """Send one action and return its reply data; state events seen meanwhile are dropped."""
        self._next_request_id += 1
        request_id = str(self._next_request_id)
        payload = json.dumps({"action": action, "requestId": request_id, **params}).encode("utf-8")
        self._process.stdin.write(struct.pack("<I", len(payload)) + payload)
        self._process.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self.next_message(deadline - time.monotonic())
            if message is None or message.get("requestId") != request_id:
                continue
            if message.get("error"):
                raise RuntimeError(f"HMA {action} failed: {message['error']}")
            return message.get("data")
        raise RuntimeError(f"HMA {action} did not reply within {timeout:g} seconds")


def _current_vpn_ip(public_ip_info) -> str:
    return ((public_ip_info or {}).get("current") or {}).get("ip") or ""


def reset_hma_ip_via_api(timeout: float = 45.0) -> str:
    """Rotate the HMA IP through the VPN service and return the new public IP.

    A connected VPN gets ShufflePublicIp (new IP, same server); a disconnected
    one is connected to the optimal server first.
    """
    with HmaNativeClient() as client:
        state = client.request("GetState")
        status = state.get("vpnStatus")
        old_ip = _current_vpn_ip(state.get("publicIpInfo")) if status == "connected" else ""
        if status == "connected":
            print(f"[HMA] Shuffling public IP (current {old_ip or 'unknown'})", flush=True)
            client.request("ShufflePublicIp")
        else:
            print(f'[HMA] VPN is "{status}", connecting to the optimal server', flush=True)
            client.request("ConnectToOptimal")

        deadline = time.monotonic() + timeout
        next_poll = time.monotonic() + 5
        status = ""
        while time.monotonic() < deadline:
            # State events are partial updates; poll as well in case one is missed.
            if time.monotonic() >= next_poll:
                state = client.request("GetState")
                next_poll = time.monotonic() + 5
            else:
                message = client.next_message(min(1.0, deadline - time.monotonic()))
                if not message or message.get("action") != HMA_STATE_EVENT:
                    continue
                state = (message.get("data") or {}).get("data") or {}
            status = state.get("vpnStatus") or status
            new_ip = _current_vpn_ip(state.get("publicIpInfo"))
            if status == "connected" and new_ip and new_ip != old_ip:
                print(f"[HMA] New public IP: {new_ip}", flush=True)
                return new_ip
        raise RuntimeError(
            f'HMA did not report a new IP within {timeout:g} seconds (last status: "{status}")'
        )


def reset_hma_ip_via_gui() -> None:
    """Disconnect HMA, wait four seconds, then reconnect once by clicking its window."""
    print("[HMA] Resetting VPN connection through the HMA window", flush=True)
    _reconnect_hma_once(disconnect_pause=4)
    print("[HMA] HMA reconnect command completed", flush=True)


def reset_hma_ip() -> None:
    """Rotate the HMA IP via the service API, falling back to clicking the window."""
    try:
        reset_hma_ip_via_api()
    except Exception as exc:
        print(f"[HMA] API reset failed ({exc}); falling back to the HMA window", flush=True)
        reset_hma_ip_via_gui()


def reset_hma_ip_safely(timeout: float = 60.0) -> None:
    """Rotate the HMA IP without letting a stuck UIA call freeze workers."""
    try:
        reset_hma_ip_via_api()
        return
    except Exception as exc:
        print(f"[HMA] API reset failed ({exc}); falling back to the HMA window", flush=True)

    if getattr(sys, "frozen", False):
        # The standalone source runner is the supported PaySafe entry point.
        # Keep a functional fallback for a future frozen build.
        reset_hma_ip_via_gui()
        return

    project_root = Path(__file__).resolve().parents[2]
    command = [sys.executable, "-m", "modules.network.hma", "--reset-worker"]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            command,
            cwd=project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"HMA reset timed out after {timeout:g} seconds") from exc

    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0:
        detail = result.stderr.strip() or f"worker exited with code {result.returncode}"
        raise RuntimeError(f"HMA reset worker failed: {detail}")


if __name__ == "__main__":
    if "--reset-worker" in sys.argv:
        reset_hma_ip_via_gui()
    elif "--reset" in sys.argv:
        reset_hma_ip()
    else:
        raise SystemExit("Use --reset or --reset-worker")
