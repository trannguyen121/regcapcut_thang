"""ThueCloud proxy API integration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib import error, parse, request

from core.settings import ThueCloudSettings


@dataclass(slots=True)
class StaticProxy:
    """Represent a static proxy line with an optional change-IP URL."""

    raw_proxy: str
    change_ip_url: str = ""


def parse_static_proxy_line(line: str) -> StaticProxy:
    """Parse host:port:user:pass[:change_ip_url] or raw_proxy|change_ip_url."""
    value = line.strip()
    if not value:
        raise ValueError("Proxy line is empty")

    if "|" in value:
        raw_proxy, change_ip_url = value.split("|", 1)
        raw_proxy = raw_proxy.strip()
        change_ip_url = change_ip_url.strip()
        if not raw_proxy:
            raise ValueError("Proxy line is missing raw proxy")
        return StaticProxy(raw_proxy=raw_proxy, change_ip_url=change_ip_url)

    parts = value.split(":", 4)
    if len(parts) < 2:
        raise ValueError("Proxy line must contain at least host:port")
    if len(parts) >= 4:
        raw_proxy = ":".join(parts[:4]).strip()
        change_ip_url = parts[4].strip() if len(parts) == 5 else ""
    else:
        raw_proxy = ":".join(parts).strip()
        change_ip_url = ""
    if not raw_proxy:
        raise ValueError("Proxy line is missing raw proxy")
    return StaticProxy(raw_proxy=raw_proxy, change_ip_url=change_ip_url)


def reset_change_ip_url(change_ip_url: str) -> str:
    """Call a static proxy change-IP URL and return the response body."""
    url = change_ip_url.strip()
    if not url:
        return ""
    http_request = request.Request(url=url, method="GET")
    try:
        with request.urlopen(http_request, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace").strip()
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace").strip()
        detail = f"HTTP {exc.code}"
        if body:
            detail = f"{detail}: {body[:300]}"
        raise RuntimeError(f"Change-IP request failed: {detail}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Change-IP request failed: {exc}") from exc


def _first_present(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def _find_proxy_data(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        for key in ("raw_proxy", "rawProxy", "proxy", "proxyString", "httpProxy", "socks5Proxy"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return {"raw_proxy": value.strip()}
        credentials = data.get("loginCredentials")
        if isinstance(credentials, list) and credentials:
            credential = str(credentials[0]).strip()
            if credential:
                return {"raw_proxy": credential}
        if any(key in data for key in ("host", "ip", "server", "hostIp", "ipHostname", "port", "portHttp")):
            return data
        for key in ("data", "proxy", "result", "item"):
            found = _find_proxy_data(data.get(key))
            if found:
                return found
        for key in ("proxies", "items", "list"):
            value = data.get(key)
            if isinstance(value, list) and value:
                found = _find_proxy_data(value[0])
                if found:
                    return found
    if isinstance(data, list) and data:
        return _find_proxy_data(data[0])
    return {}


def _format_raw_proxy(proxy_data: dict[str, Any]) -> str:
    raw_proxy = str(proxy_data.get("raw_proxy") or "").strip()
    if raw_proxy:
        return raw_proxy

    host = _first_present(proxy_data, ("host", "hostIp", "ipHostname", "ip", "server", "hostname"))
    port = _first_present(proxy_data, ("portHttp", "port", "proxy_port"))
    username = _first_present(proxy_data, ("username", "user", "login"))
    password = _first_present(proxy_data, ("password", "pass"))
    if not host or not port:
        raise RuntimeError("ThueCloud response did not include proxy host/port")
    if username and password:
        return f"{host}:{port}:{username}:{password}"
    return f"{host}:{port}"


class ThueCloudClient:
    """Small client for fetching a proxy to attach to GPM profiles."""

    def __init__(self, settings: ThueCloudSettings) -> None:
        self.settings = settings
        self.base_url = settings.api_base_url.rstrip("/")

    def get_raw_proxy(self) -> str:
        if not self.settings.enabled:
            return ""
        proxy_api_key = self.settings.proxy_api_key.strip()
        if proxy_api_key:
            payload = self._request(
                endpoint=f"/get-proxy?{parse.urlencode({'key': proxy_api_key})}",
                token="",
                method="GET",
            )
            proxy_data = _find_proxy_data(payload)
            if not proxy_data:
                raise RuntimeError("ThueCloud get-proxy response did not include proxy data")
            return _format_raw_proxy(proxy_data)

        token = self.settings.access_token.strip()
        if not token:
            raise RuntimeError("ThueCloud access token is empty")
        endpoint = self.settings.proxy_endpoint.strip()
        if not endpoint:
            raise RuntimeError("ThueCloud proxy endpoint is empty")

        payload = self._request(endpoint=endpoint, token=token, method=self.settings.proxy_method)
        proxy_data = _find_proxy_data(payload)
        if not proxy_data:
            raise RuntimeError("ThueCloud response did not include proxy data")
        return _format_raw_proxy(proxy_data)

    def _request(self, endpoint: str, token: str, method: str) -> dict[str, Any]:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        method = (method or "GET").strip().upper()
        http_request = request.Request(url=url, headers=headers, method=method)
        try:
            with request.urlopen(http_request, timeout=30) as response:
                raw_body = response.read().decode("utf-8")
        except error.URLError as exc:
            raise RuntimeError(f"ThueCloud proxy request failed: {exc}") from exc

        try:
            return json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("ThueCloud proxy response is not JSON") from exc
