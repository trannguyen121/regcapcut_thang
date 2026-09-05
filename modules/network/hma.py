"""Windows automation helpers for rotating the system-wide HMA VPN IP."""

from __future__ import annotations

import time
import unicodedata
import subprocess
import sys
import ctypes
from ctypes import wintypes
from pathlib import Path

import requests


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


def reset_hma_ip() -> None:
    """Disconnect HMA, wait four seconds, then reconnect once."""
    print("[HMA] Resetting VPN connection", flush=True)
    _reconnect_hma_once(disconnect_pause=4)
    print("[HMA] HMA reconnect command completed", flush=True)


def reset_hma_ip_safely(timeout: float = 60.0) -> None:
    """Run HMA automation out of process so a stuck UIA call cannot freeze workers."""
    if getattr(sys, "frozen", False):
        # The standalone source runner is the supported PaySafe entry point.
        # Keep a functional fallback for a future frozen build.
        reset_hma_ip()
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
    if "--reset-worker" not in sys.argv:
        raise SystemExit("Use --reset-worker")
    reset_hma_ip()
