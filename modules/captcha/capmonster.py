"""CapMonster integration."""

from __future__ import annotations

import json
from urllib import error, request

from core.settings import CapMonsterSettings, load_settings


class CapMonsterClient:
    """Small holder for CapMonster settings."""

    def __init__(self, settings: CapMonsterSettings | None = None) -> None:
        self.settings = settings or load_settings().capmonster

    @property
    def api_base_url(self) -> str:
        """Return CapMonster base URL."""
        return self.settings.api_base_url

    @property
    def api_key(self) -> str:
        """Return CapMonster API key."""
        return self.settings.api_key

    def get_balance(self) -> float:
        """Validate the API key and return the current account balance."""
        if not self.api_key:
            raise RuntimeError("CapMonster API key is empty")
        url = f"{self.api_base_url.rstrip('/')}/getBalance"
        body = json.dumps({"clientKey": self.api_key}).encode("utf-8")
        http_request = request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(http_request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"CapMonster API request failed: {exc}") from exc
        if payload.get("errorId") != 0:
            code = payload.get("errorCode", "UNKNOWN_ERROR")
            description = payload.get("errorDescription", "")
            raise RuntimeError(f"CapMonster API error: {code} {description}".strip())
        return float(payload.get("balance", 0.0))


def solve_captcha(payload: dict) -> str:
    """Solve a captcha and return the token."""
    raise NotImplementedError("Implement CapMonster solving")
