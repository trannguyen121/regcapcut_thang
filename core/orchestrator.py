"""Flow orchestration."""

from typing import Any

from core.settings import AppSettings, BrowserWindowLayout, load_settings, resolve_browser_window_layout
from modules.browser.gpm import (
    GPMClient,
    GPMProfileGroup,
    GPMProfileListResult,
    GPMProfile,
    GPMProfileStartResult,
)


class Orchestrator:
    """Coordinate the main automation flow."""

    def __init__(self, settings: AppSettings | None = None) -> None:
        self.settings = settings or load_settings()
        self.gpm = GPMClient(self.settings.gpm.api_base_url)

    def run(self) -> None:
        """Run the workflow."""
        if not self.gpm.connect():
            raise RuntimeError("Cannot connect to GPM API")

    def get_profile_groups(self) -> list[GPMProfileGroup]:
        """Return profile groups from GPM."""
        return self.gpm.list_profile_groups()

    def get_profiles(
        self,
        group_id: str | int | None = None,
        page: int | None = None,
        per_page: int | None = None,
        sort: int | None = None,
        search: str | None = None,
    ) -> GPMProfileListResult:
        """Return profiles from GPM."""
        return self.gpm.list_profiles(
            group_id=group_id if group_id is not None else self.settings.gpm.default_group_id,
            page=page if page is not None else self.settings.gpm.default_page,
            per_page=per_page if per_page is not None else self.settings.gpm.default_per_page,
            sort=sort,
            search=search,
        )

    def get_all_profiles(
        self,
        page: int | None = None,
        per_page: int | None = None,
        sort: int | None = None,
        search: str | None = None,
    ) -> GPMProfileListResult:
        """Return profiles from GPM without applying a default group filter."""
        return self.gpm.list_profiles(
            group_id=None,
            page=page if page is not None else self.settings.gpm.default_page,
            per_page=per_page if per_page is not None else self.settings.gpm.default_per_page,
            sort=sort,
            search=search,
        )

    def create_profile(self, payload: dict[str, Any]) -> GPMProfile:
        """Create a profile in GPM."""
        return self.gpm.create_profile(payload)

    def start_profile(
        self,
        profile_id: str,
        addination_args: str | None = None,
        win_scale: float | None = None,
        win_pos: str | None = None,
        win_size: str | None = None,
        window_index: int | None = None,
        total_windows: int | None = None,
    ) -> GPMProfileStartResult:
        """Start a GPM profile in an incognito browser session."""
        layout: BrowserWindowLayout | None = None
        if window_index is not None and total_windows is not None:
            layout = resolve_browser_window_layout(
                self.settings.browser_window,
                window_index=window_index,
                total_windows=total_windows,
            )

        extra_args = str(addination_args or "").strip()
        if "--incognito" not in extra_args.split():
            extra_args = f"{extra_args} --incognito".strip()

        return self.gpm.start_profile(
            profile_id=profile_id,
            addination_args=extra_args,
            win_scale=win_scale if win_scale is not None else (layout.scale if layout else None),
            win_pos=win_pos if win_pos is not None else (layout.position if layout else None),
            win_size=win_size if win_size is not None else (layout.size if layout else None),
        )

    def get_window_layout(self, window_index: int, total_windows: int) -> BrowserWindowLayout:
        """Return the auto-calculated browser window layout."""
        return resolve_browser_window_layout(
            self.settings.browser_window,
            window_index=window_index,
            total_windows=total_windows,
        )

    def close_profile(self, profile_id: str) -> bool:
        """Close a profile in GPM."""
        return self.gpm.close_profile(profile_id)

    def delete_profile(self, profile_id: str, mode: int = 2) -> bool:
        """Delete a profile in GPM."""
        return self.gpm.delete_profile(profile_id, mode=mode)
