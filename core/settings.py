"""Settings loader for external service integrations."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from dataclasses import asdict
from math import ceil, floor, sqrt
from pathlib import Path


SOURCE_SETTINGS_FILE = Path(__file__).with_name("settings.json")


def default_settings_file() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "core" / "settings.json"
    return SOURCE_SETTINGS_FILE


SETTINGS_FILE = default_settings_file()


def default_chromium_path() -> str:
    """Return the portable Chrome folder shipped beside the executable."""
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    return str(base / "chrome-152")


@dataclass(slots=True)
class GPMSettings:
    """GPM integration settings."""

    api_base_url: str
    profile_url_template: str
    default_group_id: int | None = None
    default_page: int = 1
    default_per_page: int = 50


@dataclass(slots=True)
class CapMonsterSettings:
    """CapMonster integration settings."""

    api_base_url: str
    api_key: str


@dataclass(slots=True)
class ThueCloudSettings:
    """ThueCloud proxy integration settings."""

    enabled: bool = False
    api_base_url: str = "https://api.thuecloud.com"
    access_token: str = ""
    proxy_endpoint: str = "/proxy/dan-cu-xoay-ip/list"
    proxy_method: str = "GET"
    proxy_api_key: str = ""


@dataclass(slots=True)
class BrowserWindowSettings:
    """Auto layout settings for browser windows."""

    mode: str = "auto_grid"
    scale: float = 1.0
    screen_width: int = 1920
    screen_height: int = 1080
    margin: int = 12
    gap: int = 12
    min_width: int = 480
    min_height: int = 360


@dataclass(slots=True)
class BrowserWindowLayout:
    """Resolved browser window layout for a single tab/profile."""

    scale: float
    position: str
    size: str


@dataclass(slots=True)
class AppSettings:
    """Top-level application integration settings."""

    gpm: GPMSettings
    capmonster: CapMonsterSettings
    thuecloud: ThueCloudSettings
    browser_window: BrowserWindowSettings
    expressvpn_path: str = r"C:\Program Files\ExpressVPN"
    chromium_path: str = default_chromium_path()


def _parse_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def load_settings(path: Path | None = None) -> AppSettings:
    """Load settings from the JSON settings file."""
    settings_path = path or default_settings_file()
    if path is None and not settings_path.exists() and getattr(sys, "frozen", False):
        bundled_path = Path(getattr(sys, "_MEIPASS", "")) / "core" / "settings.json"
        if bundled_path.exists():
            settings_path = bundled_path
    if settings_path.exists():
        raw = json.loads(settings_path.read_text(encoding="utf-8"))
    else:
        raw = {
            "gpm": {
                "api_base_url": os.getenv("REGGROK_GPM_API", "http://127.0.0.1:15288"),
                "profile_url_template": "",
                "default_group_id": None,
                "default_page": 1,
                "default_per_page": 50,
            },
            "capmonster": {
                "api_base_url": os.getenv("REGGROK_CAPMONSTER_API_BASE_URL", "https://api.capmonster.cloud"),
                "api_key": os.getenv("REGGROK_CAPMONSTER_API_KEY", ""),
            },
            "thuecloud": {
                "enabled": os.getenv("REGGROK_THUECLOUD_ENABLED", "false"),
                "api_base_url": os.getenv("REGGROK_THUECLOUD_API_BASE_URL", "https://api.thuecloud.com"),
                "access_token": os.getenv("REGGROK_THUECLOUD_ACCESS_TOKEN", ""),
                "proxy_endpoint": os.getenv("REGGROK_THUECLOUD_PROXY_ENDPOINT", "/proxy/dan-cu-xoay-ip/list"),
                "proxy_method": os.getenv("REGGROK_THUECLOUD_PROXY_METHOD", "GET"),
                "proxy_api_key": os.getenv("REGGROK_THUECLOUD_PROXY_API_KEY", ""),
            },
            "browser_window": {},
        }

    gpm_raw = raw.get("gpm", {})
    capmonster_raw = raw.get("capmonster", {})
    thuecloud_raw = raw.get("thuecloud", {})
    window_raw = raw.get("browser_window", {})

    gpm = GPMSettings(
        api_base_url=os.getenv("REGGROK_GPM_API", gpm_raw["api_base_url"]).rstrip("/"),
        profile_url_template=gpm_raw.get("profile_url_template") or f'{os.getenv("REGGROK_GPM_API", gpm_raw["api_base_url"]).rstrip("/")}/api/v3/profiles/{{profile_id}}',
        default_group_id=gpm_raw.get("default_group_id"),
        default_page=int(gpm_raw.get("default_page", 1)),
        default_per_page=int(gpm_raw.get("default_per_page", 50)),
    )
    capmonster = CapMonsterSettings(
        api_base_url=os.getenv("REGGROK_CAPMONSTER_API_BASE_URL", capmonster_raw.get("api_base_url", "https://api.capmonster.cloud")),
        api_key=os.getenv("REGGROK_CAPMONSTER_API_KEY", capmonster_raw.get("api_key", "")),
    )
    thuecloud = ThueCloudSettings(
        enabled=_parse_bool(os.getenv("REGGROK_THUECLOUD_ENABLED", thuecloud_raw.get("enabled")), False),
        api_base_url=os.getenv("REGGROK_THUECLOUD_API_BASE_URL", thuecloud_raw.get("api_base_url", "https://api.thuecloud.com")),
        access_token=os.getenv("REGGROK_THUECLOUD_ACCESS_TOKEN", thuecloud_raw.get("access_token", "")),
        proxy_endpoint=os.getenv("REGGROK_THUECLOUD_PROXY_ENDPOINT", thuecloud_raw.get("proxy_endpoint", "/proxy/dan-cu-xoay-ip/list")),
        proxy_method=os.getenv("REGGROK_THUECLOUD_PROXY_METHOD", thuecloud_raw.get("proxy_method", "GET")),
        proxy_api_key=os.getenv("REGGROK_THUECLOUD_PROXY_API_KEY", thuecloud_raw.get("proxy_api_key", "")),
    )
    browser_window = BrowserWindowSettings(
        mode=window_raw.get("mode", "auto_grid"),
        scale=float(window_raw.get("scale", 1.0)),
        screen_width=int(window_raw.get("screen_width", 1920)),
        screen_height=int(window_raw.get("screen_height", 1080)),
        margin=int(window_raw.get("margin", 12)),
        gap=int(window_raw.get("gap", 12)),
        min_width=int(window_raw.get("min_width", 480)),
        min_height=int(window_raw.get("min_height", 360)),
    )
    return AppSettings(
        gpm=gpm,
        capmonster=capmonster,
        thuecloud=thuecloud,
        browser_window=browser_window,
        expressvpn_path=str(raw.get("expressvpn_path", r"C:\Program Files\ExpressVPN")),
        chromium_path=str(raw.get("chromium_path") or default_chromium_path()),
    )


def save_settings(settings: AppSettings, path: Path | None = None) -> None:
    """Persist settings to the JSON settings file."""
    settings_path = path or default_settings_file()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    raw = {
        "gpm": asdict(settings.gpm),
        "capmonster": asdict(settings.capmonster),
        "thuecloud": asdict(settings.thuecloud),
        "browser_window": asdict(settings.browser_window),
        "expressvpn_path": settings.expressvpn_path,
        "chromium_path": settings.chromium_path,
    }
    settings_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")


def build_profile_url(template: str, profile_id: str) -> str:
    """Build a GPM profile URL from a template."""
    return template.format(profile_id=profile_id)


def resolve_browser_window_layout(
    settings: BrowserWindowSettings,
    window_index: int,
    total_windows: int,
) -> BrowserWindowLayout:
    """Calculate an auto-grid position and size for a browser window."""
    if total_windows < 1:
        raise ValueError("total_windows must be at least 1")
    if window_index < 0 or window_index >= total_windows:
        raise ValueError("window_index is out of range")

    screen_width = max(1, settings.screen_width)
    screen_height = max(1, settings.screen_height)
    margin = max(0, settings.margin)
    gap = max(0, settings.gap)
    min_width = max(1, settings.min_width)
    min_height = max(1, settings.min_height)

    max_columns_by_min_width = max(1, floor((screen_width - (margin * 2) + gap) / (min_width + gap)))
    max_rows_by_min_height = max(1, floor((screen_height - (margin * 2) + gap) / (min_height + gap)))
    max_visible_slots = max(1, max_columns_by_min_width * max_rows_by_min_height)
    visible_windows = min(total_windows, max_visible_slots)
    slot_index = window_index % visible_windows

    columns = max(
        1,
        min(
            max_columns_by_min_width,
            visible_windows,
            max(ceil(sqrt(visible_windows)), ceil(visible_windows / max_rows_by_min_height)),
        ),
    )
    rows = max(1, ceil(visible_windows / columns))

    usable_width = max(1, screen_width - (margin * 2) - (gap * (columns - 1)))
    usable_height = max(1, screen_height - (margin * 2) - (gap * (rows - 1)))

    width = max(1, usable_width // columns)
    height = max(1, usable_height // rows)

    column = slot_index % columns
    row = slot_index // columns
    x = margin + (column * (width + gap))
    y = margin + (row * (height + gap))
    x = min(x, max(0, screen_width - margin - width))
    y = min(y, max(0, screen_height - margin - height))

    return BrowserWindowLayout(
        scale=settings.scale,
        position=f"{x},{y}",
        size=f"{width},{height}",
    )
