"""ExpressVPN command-line controller for changing the active VPN location.

ExpressVPN ships a CLI binary called ``expressvpn`` that exposes commands such as
``connect``, ``disconnect`` and ``list``. This module is a thin wrapper that lets
the automation tool drive the VPN tunnel, change locations, and read the current
state from inside Python.

The module never assumes a specific install location: it first looks at the
``EXPRESSVPN_BIN`` environment variable, then probes a list of common Windows /
macOS / Linux paths, and finally falls back to whatever ``shutil.which`` finds on
``PATH``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable


# Common install locations for the ExpressVPN CLI. The first match wins.
_DEFAULT_CANDIDATES = (
    # Windows: CLI binary in services folder (modern ExpressVPN)
    r"C:\Program Files (x86)\ExpressVPN\services\ExpressVPN.CLI.exe",
    r"C:\Program Files\ExpressVPN\services\ExpressVPN.CLI.exe",
    # Windows: GUI binary (older versions)
    r"C:\Program Files (x86)\ExpressVPN\expressvpn-ui\ExpressVPN.exe",
    r"C:\Program Files\ExpressVPN\expressvpn-ui\ExpressVPN.exe",
    r"C:\Program Files (x86)\ExpressVPN\ExpressVPN.exe",
    r"C:\Program Files\ExpressVPN\ExpressVPN.exe",
    # macOS / Linux
    "/usr/local/bin/expressvpn",
    "/usr/bin/expressvpn",
    "/opt/expressvpn/bin/expressvpn",
    "/Applications/ExpressVPN.app/Contents/Resources/expressvpn",
)


class ExpressVPNError(RuntimeError):
    """Raised when the ExpressVPN CLI returns an unexpected response."""


@dataclass
class ExpressVPNLocation:
    """A single entry returned by ``expressvpn list``."""

    alias: str
    country_code: str = ""
    country: str = ""
    recommended: bool = False
    location_id: str = ""

    @property
    def label(self) -> str:
        """Pretty label used for buttons / dropdowns."""
        parts = [self.alias]
        if self.country and self.country.casefold() not in self.alias.casefold():
            parts.append(self.country)
        if self.recommended:
            parts.append("(recommended)")
        return " - ".join(parts)


@dataclass
class ExpressVPNStatus:
    """Snapshot of the ExpressVPN tunnel state."""

    connected: bool
    alias: str = ""
    country: str = ""
    raw_output: str = ""
    extra: dict = field(default_factory=dict)


class ExpressVPNController:
    """Driver for the ExpressVPN CLI.

    Parameters
    ----------
    binary_path:
        Optional override for the CLI location. When omitted the controller
        auto-detects from :data:`_DEFAULT_CANDIDATES` and ``$PATH``.
    timeout:
        Per-command timeout in seconds.
    """

    def __init__(self, binary_path: str = "", timeout: float = 30.0) -> None:
        self.timeout = timeout
        configured = binary_path or os.environ.get("EXPRESSVPN_PATH", "")
        if configured and os.path.isdir(configured):
            # Install layouts differ between ExpressVPN releases. Prefer the
            # configured folder and search its descendants before falling back
            # to machine-wide defaults.
            preferred = os.path.join(configured, "services", "ExpressVPN.CLI.exe")
            if os.path.isfile(preferred):
                configured = preferred
            else:
                matches = []
                for root, _dirs, files in os.walk(configured):
                    for name in files:
                        if name.casefold() in {"expressvpn.cli.exe", "expressvpn.exe"}:
                            matches.append(os.path.join(root, name))
                configured = sorted(matches, key=lambda p: (".cli." not in p.casefold(), len(p)))[0] if matches else ""
        self.binary_path = configured if configured and os.path.isfile(configured) else self._resolve_binary()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ helpers

    def _resolve_binary(self) -> str:
        env_path = os.environ.get("EXPRESSVPN_BIN", "").strip()
        if env_path and os.path.isfile(env_path):
            return env_path
        for candidate in _DEFAULT_CANDIDATES:
            if os.path.isfile(candidate):
                return candidate
        discovered = shutil.which("expressvpn")
        if discovered:
            return discovered
        raise ExpressVPNError(
            "Could not locate the ExpressVPN CLI. Set the EXPRESSVPN_BIN env var "
            "to its full path (e.g. C:\\Program Files (x86)\\ExpressVPN\\expressvpn.exe)."
        )

    def _run(self, args: Iterable[str], timeout: float | None = None) -> subprocess.CompletedProcess:
        """Run ``expressvpn <args>`` and return the completed process."""
        if not self.binary_path:
            raise ExpressVPNError("ExpressVPN binary path is not configured")
        cmd = [self.binary_path, *args]
        effective_timeout = timeout if timeout is not None else self.timeout
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except FileNotFoundError as exc:
            raise ExpressVPNError(f"ExpressVPN binary not found at {self.binary_path}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ExpressVPNError(f"ExpressVPN command timed out after {effective_timeout}s") from exc

    @staticmethod
    def _strip_ascii_box(text: str) -> str:
        """The CLI prints an ASCII box around some commands; collapse it."""
        return "\n".join(line.rstrip() for line in text.splitlines()).strip()

    # ---------------------------------------------------------------- public API

    def is_available(self) -> bool:
        """Return ``True`` when the binary is reachable and responds to ``--version``."""
        if not self.binary_path:
            return False
        try:
            proc = self._run(["--version"], timeout=10)
        except ExpressVPNError:
            return False
        return proc.returncode == 0

    def get_status(self) -> ExpressVPNStatus:
        """Return the current tunnel status parsed from ``expressvpn status``."""
        proc = self._run(["status"], timeout=15)
        stdout = self._strip_ascii_box(proc.stdout or "")
        if proc.returncode != 0:
            raise ExpressVPNError(
                f"expressvpn status failed (exit {proc.returncode}): {stdout}"
            )
        return self._parse_status(stdout)

    def list_locations(self) -> list[ExpressVPNLocation]:
        """Return every available location parsed from ``expressvpn list``."""
        proc = self._run(["list"], timeout=20)
        stdout = self._strip_ascii_box(proc.stdout or "")
        if proc.returncode != 0:
            raise ExpressVPNError(
                f"expressvpn list failed (exit {proc.returncode}): {stdout}"
            )
        return self._parse_locations(stdout)

    def connect(self, alias: str = "", wait_seconds: float = 12.0) -> ExpressVPNStatus:
        """Connect to ``alias`` (or the smart-location if empty).

        ``wait_seconds`` is how long we wait for the tunnel to come up before
        returning. The ExpressVPN CLI does not block on ``connect``, so we poll
        ``status`` until it reports a connected tunnel.
        """
        if not alias:
            # Current Windows CLI uses Smart Location when the location
            # argument is omitted. ``--smart-location`` is interpreted as a
            # literal location name and fails with "Invalid location".
            args = ["connect"]
        else:
            args = ["connect", alias]
        proc = self._run(args, timeout=20)
        if proc.returncode != 0:
            raise ExpressVPNError(
                f"expressvpn connect failed (exit {proc.returncode}): "
                f"{self._strip_ascii_box(proc.stdout or '')}"
            )
        deadline = time.time() + max(0.0, wait_seconds)
        last_status: ExpressVPNStatus | None = None
        while time.time() <= deadline:
            try:
                status = self.get_status()
            except ExpressVPNError:
                status = None
            last_status = status
            if status and status.connected:
                return status
            time.sleep(0.5)
        return last_status or ExpressVPNStatus(connected=False)

    def disconnect(self, wait_seconds: float = 6.0) -> ExpressVPNStatus:
        """Disconnect the active tunnel and wait until ``status`` confirms it."""
        proc = self._run(["disconnect"], timeout=20)
        if proc.returncode != 0:
            raise ExpressVPNError(
                f"expressvpn disconnect failed (exit {proc.returncode}): "
                f"{self._strip_ascii_box(proc.stdout or '')}"
            )
        deadline = time.time() + max(0.0, wait_seconds)
        last_status: ExpressVPNStatus | None = None
        while time.time() <= deadline:
            try:
                status = self.get_status()
            except ExpressVPNError:
                status = None
            last_status = status
            if status and not status.connected:
                return status
            time.sleep(0.5)
        return last_status or ExpressVPNStatus(connected=False, raw_output="disconnect timed out")

    def change_ip(
        self,
        alias: str = "",
        cooldown_seconds: float = 2.0,
    ) -> ExpressVPNStatus:
        """Disconnect + reconnect to obtain a fresh IP.

        ``alias`` may be empty to use ExpressVPN's smart-location picker. The
        function waits ``cooldown_seconds`` after a successful reconnect so the
        tunnel has time to settle before the caller opens a browser.
        """
        with self._lock:
            try:
                self.disconnect(wait_seconds=5.0)
            except ExpressVPNError:
                # A failed disconnect is acceptable if the tunnel was already down.
                pass
            status = self.connect(alias=alias, wait_seconds=15.0)
            if cooldown_seconds > 0:
                time.sleep(cooldown_seconds)
            return status

    # --------------------------------------------------------------- parsing

    @staticmethod
    def _parse_status(stdout: str) -> ExpressVPNStatus:
        connected = False
        alias = ""
        country = ""
        extras: dict[str, str] = {}

        # Join all lines and look for key patterns
        full_text = "\n".join(
            line.strip() for line in stdout.splitlines()
        ).lower()

        if "not connected" in full_text:
            connected = False
        elif "connected" in full_text and "expressvpn" in full_text:
            connected = True

        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # Handle "Connected to\nExpressVPN\n<location>" format
            if stripped.lower() == "connected to":
                continue
            if stripped.lower() == "expressvpn":
                continue
            if stripped.lower().startswith("connected to"):
                # e.g. "Connected to Singapore - Jurong" or
                # "Connected to ExpressVPN - Japan - Tokyo".
                connected = True
                location = re.sub(r"^connected\s+to\s+", "", stripped, flags=re.I).strip()
                location = re.sub(r"^expressvpn\s*-?\s*", "", location, flags=re.I).strip()
                if location:
                    alias = location
                    if " - " in location:
                        country = location.split(" - ", 1)[0].strip()
                continue
            if ":" in stripped:
                key, _, value = stripped.partition(":")
                key = key.strip()
                value = value.strip()
                extras[key] = value
                if key.lower() in ("country", "location"):
                    country = value
                    if not alias:
                        alias = value

        return ExpressVPNStatus(
            connected=connected,
            alias=alias,
            country=country,
            raw_output=stdout,
            extra=extras,
        )

    @staticmethod
    def _parse_locations(stdout: str) -> list[ExpressVPNLocation]:
        """Parse expressvpn list output.

        Format:
            VPN Location Name            VPN Location Id
            ____________________________________________

            Asia Pacific:
              Australia - Adelaide        93
              Australia - Brisbane       208
        """
        locations: list[ExpressVPNLocation] = []
        seen_aliases: set[str] = set()
        current_region: str = ""

        for raw_line in stdout.splitlines():
            line = raw_line.rstrip()
            if not line:
                continue

            # Skip header and separator lines
            lowered = line.lower().strip()
            if lowered.startswith("vpn location name") or lowered.startswith("vpn location id"):
                continue
            if "____" in line or "=====" in line:
                continue

            # Region headers end with colon (not indented)
            if lowered.endswith(":") and not line[0].isspace():
                current_region = line.rstrip(":").strip()
                continue

            # Location lines are indented (start with space)
            if not line[0].isspace():
                continue

            # Extract location name (everything before trailing spaces + ID)
            cleaned = line.strip()
            if not cleaned:
                continue

            # Remove trailing ID number: "Australia - Adelaide        93"
            match = re.match(r"^(.+?)\s+(\d+)$", cleaned)
            if match:
                alias = match.group(1).strip()
                location_id = match.group(2)
            else:
                alias = cleaned
                location_id = ""

            if not alias or len(alias) < 2:
                continue

            # Parse "Country - City" format
            country = ""
            if " - " in alias:
                parts = alias.split(" - ", 1)
                country = parts[0].strip()

            # Skip duplicates
            if alias in seen_aliases:
                continue
            seen_aliases.add(alias)

            locations.append(
                ExpressVPNLocation(
                    alias=alias,
                    country_code="",
                    country=country or current_region or alias,
                    recommended=False,
                    location_id=location_id,
                )
            )

        return locations
