"""GPM API integration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib import error, parse, request


@dataclass(slots=True)
class GPMProfileGroup:
    """Represent a GPM profile group."""

    id: int
    name: str
    sort: int | None = None
    created_by: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(slots=True)
class GPMProfileStartResult:
    """Represent the result of starting a GPM profile."""

    success: bool
    profile_id: str
    browser_location: str | None = None
    remote_debugging_address: str | None = None
    driver_path: str | None = None


@dataclass(slots=True)
class GPMPagination:
    """Represent paginated metadata from GPM."""

    total: int = 0
    page: int = 1
    page_size: int = 50
    total_page: int = 1


@dataclass(slots=True)
class GPMProfile:
    """Represent a GPM browser profile."""

    id: str
    name: str
    raw_proxy: str | None = None
    browser_type: str | None = None
    browser_version: str | None = None
    group_id: str | int | None = None
    profile_path: str | None = None
    note: str | None = None
    created_at: str | None = None


@dataclass(slots=True)
class GPMProfileListResult:
    """Represent the profile list response from GPM."""

    profiles: list[GPMProfile]
    pagination: GPMPagination


class GPMClient:
    """Small client wrapper for the local GPM API."""

    GROUPS_ENDPOINT = "/api/v3/groups"
    PROFILES_ENDPOINT = "/api/v3/profiles"
    CREATE_PROFILE_ENDPOINT = "/api/v3/profiles/create"
    START_PROFILE_ENDPOINT = "/api/v3/profiles/start"
    CLOSE_PROFILE_ENDPOINT = "/api/v3/profiles/close"
    DELETE_PROFILE_ENDPOINT = "/api/v3/profiles/delete"

    def __init__(self, base_url: str = "http://127.0.0.1:15288") -> None:
        self.base_url = base_url.rstrip("/")

    def connect(self) -> bool:
        """Check whether the GPM API is reachable."""
        try:
            self._request("GET", "/")
            return True
        except RuntimeError:
            return False

    def login(self, endpoint: str, payload: dict[str, Any]) -> dict:
        """Submit a login request to a specific GPM endpoint."""
        return self._request("POST", endpoint, payload)

    def list_profile_groups(self) -> list[GPMProfileGroup]:
        """Fetch profile groups from the GPM API."""
        response = self._request("GET", self.GROUPS_ENDPOINT)
        if response.get("success") is not True:
            message = response.get("message", "Unknown GPM error")
            raise RuntimeError(f"Failed to fetch profile groups: {message}")

        groups: list[GPMProfileGroup] = []
        for item in response.get("data", []):
            groups.append(
                GPMProfileGroup(
                    id=item["id"],
                    name=item["name"],
                    sort=item.get("sort"),
                    created_by=item.get("created_by"),
                    created_at=item.get("created_at"),
                    updated_at=item.get("updated_at"),
                )
            )
        return groups

    def list_profiles(
        self,
        group_id: str | int | None = None,
        page: int = 1,
        per_page: int = 50,
        sort: int | None = None,
        search: str | None = None,
    ) -> GPMProfileListResult:
        """Fetch browser profiles from the GPM API."""
        params: dict[str, str] = {
            "page": str(page),
            "per_page": str(per_page),
        }
        if group_id is not None:
            params["group_id"] = str(group_id)
        if sort is not None:
            params["sort"] = str(sort)
        if search:
            params["search"] = search

        endpoint = f"{self.PROFILES_ENDPOINT}?{parse.urlencode(params)}"
        response = self._request("GET", endpoint)
        if response.get("success") is not True:
            message = response.get("message", "Unknown GPM error")
            raise RuntimeError(f"Failed to fetch profiles: {message}")

        profiles: list[GPMProfile] = []
        for item in response.get("data", []):
            profiles.append(
                GPMProfile(
                    id=str(item["id"]),
                    name=item["name"],
                    raw_proxy=item.get("raw_proxy"),
                    browser_type=item.get("browser_type"),
                    browser_version=item.get("browser_version"),
                    group_id=item.get("group_id"),
                    profile_path=item.get("profile_path"),
                    note=item.get("note"),
                    created_at=item.get("created_at"),
                )
            )

        pagination_data = response.get("pagination") or {}
        pagination = GPMPagination(
            total=int(pagination_data.get("total", len(profiles))),
            page=int(pagination_data.get("page", page)),
            page_size=int(pagination_data.get("page_size", per_page)),
            total_page=int(pagination_data.get("total_page", 1)),
        )
        return GPMProfileListResult(profiles=profiles, pagination=pagination)

    def create_profile(self, payload: dict[str, Any]) -> GPMProfile:
        """Create a GPM profile."""
        response = self._request("POST", self.CREATE_PROFILE_ENDPOINT, payload)
        if response.get("success") is not True:
            message = response.get("message", "Unknown GPM error")
            raise RuntimeError(f"Failed to create profile: {message}")

        item = response.get("data") or {}
        return GPMProfile(
            id=str(item["id"]),
            name=item["name"],
            raw_proxy=item.get("raw_proxy"),
            browser_type=item.get("browser_type"),
            browser_version=item.get("browser_version"),
            group_id=item.get("group_id"),
            profile_path=item.get("profile_path"),
            note=item.get("note"),
            created_at=item.get("created_at"),
        )

    def start_profile(
        self,
        profile_id: str,
        addination_args: str | None = None,
        win_scale: float | None = None,
        win_pos: str | None = None,
        win_size: str | None = None,
    ) -> GPMProfileStartResult:
        """Start a GPM profile and return connection details."""
        params: dict[str, str] = {}
        if addination_args:
            params["addination_args"] = addination_args
        if win_scale is not None:
            params["win_scale"] = str(win_scale)
        if win_pos:
            params["win_pos"] = win_pos
        if win_size:
            params["win_size"] = win_size

        endpoint = f"{self.START_PROFILE_ENDPOINT}/{profile_id}"
        if params:
            endpoint = f"{endpoint}?{parse.urlencode(params)}"

        response = self._request("GET", endpoint)
        if response.get("success") is not True:
            message = response.get("message", "Unknown GPM error")
            raise RuntimeError(f"Failed to start profile: {message}")

        payload = response.get("data") or {}
        success = payload.get("success")
        if success is None:
            success = response.get("success", False)
        return GPMProfileStartResult(
            success=bool(success),
            profile_id=str(payload.get("profile_id") or payload.get("id") or profile_id),
            browser_location=payload.get("browser_location"),
            remote_debugging_address=payload.get("remote_debugging_address"),
            driver_path=payload.get("driver_path"),
        )

    def close_profile(self, profile_id: str) -> bool:
        """Close a GPM profile."""
        response = self._request("GET", f"{self.CLOSE_PROFILE_ENDPOINT}/{profile_id}")
        if response.get("success") is not True:
            message = response.get("message", "Unknown GPM error")
            raise RuntimeError(f"Failed to close profile: {message}")
        return True

    def delete_profile(self, profile_id: str, mode: int = 2) -> bool:
        """Delete a GPM profile."""
        endpoint = f"{self.DELETE_PROFILE_ENDPOINT}/{profile_id}?{parse.urlencode({'mode': str(mode)})}"
        response = self._request("GET", endpoint)
        if response.get("success") is not True:
            message = response.get("message", "Unknown GPM error")
            raise RuntimeError(f"Failed to delete profile: {message}")
        return True

    def _request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
    ) -> dict:
        """Send an HTTP request to the GPM API."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}" if endpoint else self.base_url
        data = None
        headers = {"Content-Type": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")

        http_request = request.Request(url=url, data=data, headers=headers, method=method)
        try:
            with request.urlopen(http_request, timeout=10) as response:
                raw_body = response.read().decode("utf-8").strip()
        except error.URLError as exc:
            raise RuntimeError(f"GPM API request failed: {exc}") from exc

        if not raw_body:
            return {}

        try:
            return json.loads(raw_body)
        except json.JSONDecodeError:
            return {"raw": raw_body}
