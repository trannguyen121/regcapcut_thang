"""Log in to CapCut accounts and join spaces through invitation links."""

from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

from modules.browser.chromium import open_workflow_page
from playwright.sync_api import sync_playwright

from modules.actions.capcut_workflow import (
    CapCutWorkflowInterrupted,
    _cdp_url,
    _connect_browser,
    _ensure_running,
    _wait,
    accept_capcut_cookies,
    extract_capcut_username,
)


CAPCUT_LOGIN_URL = "https://www.capcut.com/login"
CAPCUT_MY_CLOUD_URL = "https://www.capcut.com/my-cloud"


class CannotJoinSpaceError(RuntimeError):
    """The invitation rejected this account without consuming a link use."""


class LinkFullError(RuntimeError):
    """CapCut reports that the invitation can no longer accept submissions."""


class CapCutLoginError(RuntimeError):
    """The account cannot log in and therefore must not be used to join a link."""


class AlreadyJoinedSpaceError(RuntimeError):
    """Membership was confirmed before Submit; do not count another link use."""


class JoinVerificationError(RuntimeError):
    """Membership is unknown; another Submit must not be sent automatically."""


def _parse_invitation_membership(payload) -> tuple[str, bool]:
    # CapCut's invitation page reads data.workspace_info.is_member and redirects
    # existing members itself. A successful join/application response alone is
    # insufficient: applications may still await approval.
    if not isinstance(payload, dict) or str(payload.get("ret")) != "0":
        raise JoinVerificationError("CapCut invitation membership request failed")
    data = payload.get("data")
    info = data.get("workspace_info") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        raise JoinVerificationError("CapCut did not return workspace membership")
    workspace_id = info.get("workspace_id")
    member = info.get("is_member")
    if not isinstance(workspace_id, (str, int)) or isinstance(workspace_id, bool) or not str(workspace_id).strip():
        raise JoinVerificationError("CapCut did not identify the invitation workspace")
    if not isinstance(member, bool):
        raise JoinVerificationError("CapCut did not explicitly confirm membership status")
    return str(workspace_id), member


def _read_invitation_membership(page, link: str, user: str, stop_event) -> tuple[str, bool]:
    """Reload this invitation and observe its own authenticated, read-only request."""
    _ensure_running(stop_event, user)
    print(f"[CAPCUT][ADD LINK][VERIFY] Reading invitation membership: {user} -> {link}")

    def is_invitation_response(response):
        try:
            url = urlsplit(response.url)
            host = (url.hostname or "").casefold()
            if url.scheme != "https" or not (
                host == "capcut.com" or host.endswith(".capcut.com")
                or host == "capcutapi.com" or host.endswith(".capcutapi.com")
            ):
                return False
            if url.path != "/cc/v1/workspace/get_workspace_info_by_invitation_link":
                return False
            request = response.request
            body = request.post_data_json
            return (
                request.frame == page.main_frame
                and isinstance(body, dict)
                and body.get("invitation_link") in (link, page.url)
            )
        except Exception:
            return False

    captured = {"payload": None, "error": None, "status": None}

    def capture_membership(response):
        if captured["payload"] is not None or captured["error"] is not None:
            return
        if not is_invitation_response(response):
            return
        try:
            captured["status"] = response.status
            if not response.ok:
                raise JoinVerificationError(
                    f"CapCut membership request returned HTTP {response.status}"
                )
            try:
                # Read inside the response callback. The invitation immediately
                # redirects members and Chromium may otherwise discard this body.
                captured["payload"] = response.json()
            except Exception as body_error:
                # Chromium 154 can still report Network.getResponseBody missing.
                # Replay only this read-only membership request through the same
                # authenticated browser context; this never calls the join API.
                request_body = response.request.post_data_json
                original_headers = response.request.all_headers()
                retry_headers = {
                    name: value
                    for name, value in original_headers.items()
                    if not name.startswith(":") and name.casefold() not in {
                        "cookie", "host", "content-length", "connection",
                        "accept-encoding",
                    }
                }
                print(
                    f"[CAPCUT][ADD LINK][VERIFY] Response body unavailable; "
                    f"retrying read-only membership API: {user} | "
                    f"{type(body_error).__name__}: {body_error}"
                )
                api_response = page.context.request.post(
                    response.url,
                    data=request_body,
                    headers=retry_headers,
                    timeout=20000,
                )
                captured["status"] = api_response.status
                if not api_response.ok:
                    raise JoinVerificationError(
                        f"CapCut membership retry returned HTTP {api_response.status}"
                    )
                captured["payload"] = api_response.json()
                if str(captured["payload"].get("ret")) != "0":
                    print(
                        f"[CAPCUT][ADD LINK][VERIFY] Membership API retry rejected: {user} | "
                        f"ret={captured['payload'].get('ret')} | "
                        f"message={captured['payload'].get('msg') or captured['payload'].get('message') or ''}"
                    )
        except Exception as exc:
            captured["error"] = exc

    page.on("response", capture_membership)
    try:
        try:
            page.goto(link, wait_until="commit", timeout=30000)
        except Exception as navigation_error:
            print(
                f"[CAPCUT][ADD LINK][VERIFY] Invitation navigation interrupted; "
                f"still waiting for membership API: {user} | "
                f"{type(navigation_error).__name__}: {navigation_error}"
            )
        deadline = time.time() + 20.0
        while captured["payload"] is None and captured["error"] is None and time.time() < deadline:
            _ensure_running(stop_event, user)
            page.wait_for_timeout(100)
        if captured["error"] is not None:
            raise captured["error"]
        if captured["payload"] is None:
            raise JoinVerificationError("Timed out waiting for CapCut membership response")
        workspace_id, is_member = _parse_invitation_membership(captured["payload"])
        print(
            f"[CAPCUT][ADD LINK][VERIFY] Invitation response: {user} | "
            f"workspace={workspace_id} | is_member={is_member}"
        )
        return workspace_id, is_member
    except CapCutWorkflowInterrupted:
        raise
    except JoinVerificationError:
        raise
    except Exception as exc:
        detail = str(exc).strip() or repr(exc)
        raise JoinVerificationError(
            f"Could not verify invitation membership for {user}: "
            f"{type(exc).__name__}: {detail}"
        ) from exc
    finally:
        page.remove_listener("response", capture_membership)


LOGIN_ERROR_MARKERS = (
    "incorrect password",
    "invalid password",
    "email or password is incorrect",
    "email address or password is incorrect",
    "wrong password",
    "account doesn't exist",
    "account does not exist",
    "account not found",
    "user doesn't exist",
    "user does not exist",
    "couldn't log in",
    "could not log in",
    "login failed",
    "log in failed",
    "too many attempts",
    "try again later",
)


def _login_error_text(text: str) -> str | None:
    """Return the matching CapCut login error marker, if one is visible."""
    lowered = str(text).casefold().replace("â€™", "'").replace("’", "'")
    return next((marker for marker in LOGIN_ERROR_MARKERS if marker in lowered), None)


def _is_my_cloud_url(url: str) -> bool:
    try:
        parsed = urlsplit(str(url))
    except Exception:
        return False
    return (
        parsed.scheme.casefold() == "https"
        and parsed.netloc.casefold() == "www.capcut.com"
        and (parsed.path == "/my-cloud" or parsed.path.startswith("/my-cloud/"))
    )


def _body_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=2000)
    except Exception:
        return ""


def _cant_join_space_visible(page) -> bool:
    text = _body_text(page).casefold().replace("’", "'")
    return "can't join space" in text or "cannot join space" in text


def _cant_submit_request_visible(page) -> bool:
    text = _body_text(page).casefold().replace("’", "'")
    return "can't submit request" in text or "cannot submit request" in text


def _pro_plan_visible(page) -> bool:
    """Confirm an active individual or Teams Pro membership indicator."""
    text = _body_text(page).casefold()
    active_statuses = (
        "pro: active",
        "capcut pro: active",
        "subscription active",
    )
    return (
        any(status in text for status in active_statuses)
        or "you're enjoying pro benefits" in text
        or "you’re enjoying pro benefits" in text
        or ("pro benefits" in text and ("teams member" in text or "pro member" in text))
        # Additional Pro indicators
        or "pro plan" in text
        or "upgrade to pro" in text and ("active" in text or "current" in text)
        or "current plan" in text and ("pro" in text or "premium" in text)
    )


def _check_pro_via_subscription_page(page, user: str, stop_event) -> bool | None:
    """Backup method: check Pro status via subscription/settings page. Returns None if cannot determine."""
    subscription_urls = [
        "https://www.capcut.com/subscription",
        "https://www.capcut.com/settings/subscription",
        "https://www.capcut.com/account",
    ]
    for url in subscription_urls:
        _ensure_running(stop_event, user)
        try:
            print(f"[CAPCUT][CHECK PRO] Trying subscription page: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            _wait(2.0, stop_event, user)
            if _pro_plan_visible(page):
                print(f"[CAPCUT][CHECK PRO] Pro confirmed via subscription page: {user}")
                return True
            # Also check for "no active subscription" or "free plan" text
            text = _body_text(page).casefold()
            if any(phrase in text for phrase in ["no active subscription", "free plan", "basic plan", "not a pro member"]):
                print(f"[CAPCUT][CHECK PRO] Not Pro (subscription page): {user}")
                return False
        except Exception as exc:
            print(f"[CAPCUT][CHECK PRO] Subscription page error: {exc}")
            continue
    return None


def _profile_menu_visible(page) -> bool:
    """Distinguish an opened account menu from an avatar click that was blocked."""
    text = _body_text(page).casefold()
    return "view profile" in text and "sign out" in text


def _click_visible_control(locator, timeout: int = 1500) -> bool:
    try:
        count = min(locator.count(), 20)
    except Exception:
        return False
    for index in range(count):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible() and candidate.is_enabled():
                try:
                    candidate.click(timeout=timeout)
                except Exception:
                    candidate.click(timeout=timeout, force=True)
                return True
        except Exception:
            continue
    return False


def _dismiss_post_submit_popups(page, user: str, stop_event, timeout: float = 15.0) -> None:
    """Dismiss CapCut onboarding/announcement layers that cover the profile avatar."""
    deadline = time.time() + timeout
    max_cycles = int(timeout / 0.3)  # Prevent infinite loops
    cycles = 0
    while time.time() <= deadline and cycles < max_cycles:
        _ensure_running(stop_event, user)
        clicked = False
        controls = (
            page.get_by_role(
                "button",
                name=re.compile(r"^(skip|skip for now|not now|maybe later|got it|bỏ qua|để sau|tắt)$", re.I),
            ),
            page.get_by_text(
                re.compile(r"^(skip|skip for now|not now|maybe later|got it|bỏ qua|để sau|tắt)$", re.I),
                exact=True,
            ),
            page.locator("text=Skip"),
            page.get_by_role("button", name=re.compile(r"^(close|dismiss|đóng|tắt)$", re.I)),
            page.locator('[aria-label*="close" i], [role="button"][aria-label*="close" i]'),
            page.locator('button:has-text("×"), button:has-text("✕")'),
            # Additional CapCut-specific dismiss buttons
            page.locator('button:has-text("OK")'),
            page.locator('button:has-text("Confirm")'),
            page.locator('button:has-text("Continue")'),
        )
        for control in controls:
            if _click_visible_control(control):
                clicked = True
                print(f"[CAPCUT][POPUP] Dismissed a popup for {user}")
                _wait(0.7, stop_event, user)
                break
        if not clicked:
            # Some onboarding layers appear several seconds after the home page.
            # Keep polling for the promised timeout instead of returning early.
            _wait(0.25, stop_event, user)
        cycles += 1


def _open_profile_menu(page, user: str, stop_event, max_attempts: int = 5) -> bool:
    """Open only a top-right profile/avatar control, avoiding upgrade cards."""
    viewport = page.viewport_size
    if not viewport:
        try:
            viewport = page.evaluate("() => ({width: window.innerWidth, height: window.innerHeight})")
        except Exception:
            viewport = {"width": 1920, "height": 1080}

    # Try multiple fallback strategies
    for attempt in range(max_attempts):
        _ensure_running(stop_event, user)
        print(f"[CAPCUT][PROFILE] Attempt {attempt + 1}/{max_attempts} to open profile menu for {user}")

        # Strategy 1: Find avatar by selectors in top-right region
        candidates = page.locator(
            '[data-id="TitleBarUser"], .user-avatar, [data-testid*="avatar" i], [data-testid*="profile" i], '
            'button[aria-label*="profile" i], button[aria-label*="account" i], '
            '[class*="avatar" i], img[alt*="avatar" i]'
        )
        matches = []
        try:
            for index in range(min(candidates.count(), 100)):
                candidate = candidates.nth(index)
                try:
                    if not candidate.is_visible():
                        continue
                    box = candidate.bounding_box()
                    if not box:
                        continue
                    center_x = box["x"] + box["width"] / 2
                    center_y = box["y"] + box["height"] / 2
                    # Relaxed region: 50% width (was 65%), 250px height (was 180px)
                    if center_x >= viewport["width"] * 0.50 and center_y <= 250:
                        matches.append((center_x, candidate))
                except Exception:
                    continue
        except Exception:
            pass

        for _, candidate in sorted(matches, key=lambda item: item[0], reverse=True):
            for click_method in (
                lambda: candidate.click(timeout=2000),
                lambda: candidate.evaluate("element => element.click()"),
            ):
                try:
                    click_method()
                    _wait(0.8, stop_event, user)
                    if _profile_menu_visible(page) or _pro_plan_visible(page):
                        print(f"[CAPCUT][PROFILE] Opened profile menu for {user}")
                        return True
                except Exception:
                    continue

        # Strategy 2: Fixed position click (right edge, 76px from top)
        try:
            page.mouse.click(max(1, viewport["width"] - 56), 76)
            _wait(0.8, stop_event, user)
            if _profile_menu_visible(page):
                print(f"[CAPCUT][PROFILE] Opened profile menu by position for {user}")
                return True
        except Exception:
            pass

        # Strategy 3: Try clicking on settings icon if available
        try:
            settings_btn = page.locator(
                'button[aria-label*="setting" i], button[aria-label*="cài đặt" i], '
                '[data-testid*="setting" i], [class*="setting" i]'
            ).first
            if settings_btn.is_visible():
                settings_btn.click(timeout=2000)
                _wait(0.8, stop_event, user)
                if _profile_menu_visible(page):
                    print(f"[CAPCUT][PROFILE] Opened profile menu via settings for {user}")
                    return True
        except Exception:
            pass

        # Strategy 4: Try URL navigation to profile page
        try:
            # Navigate to a page where profile menu is more reliably accessible
            page.goto("https://www.capcut.com/my-cloud", wait_until="domcontentloaded", timeout=10000)
            _wait(2.0, stop_event, user)
            _dismiss_post_submit_popups(page, user, stop_event, timeout=3.0)
        except Exception:
            pass

        # Brief pause between attempts
        if attempt < max_attempts - 1:
            _wait(0.5, stop_event, user)

    print(f"[CAPCUT][PROFILE] Failed to open profile menu after {max_attempts} attempts for {user}")
    return False


def _click_first_visible(locators, timeout: int = 5000) -> None:
    last_error = None
    for locator in locators:
        try:
            count = locator.count()
        except Exception as exc:
            last_error = exc
            continue
        for index in range(count):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible() and candidate.is_enabled():
                    candidate.click(timeout=timeout)
                    return
            except Exception as exc:
                last_error = exc
    raise RuntimeError(f"Could not find a clickable CapCut control: {last_error}")


def _login_with_email_once(page, account: dict, stop_event) -> None:
    user = account["user"]
    password = account["passnew"]
    _ensure_running(stop_event, user)
    print(f"[CAPCUT][LOGIN] Opening login for {user}")
    page.goto(CAPCUT_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    print(f"[CAPCUT][ADD LINK][LOGIN] Login page loaded: {user} | url={page.url}")
    accept_capcut_cookies(page, user, stop_event, timeout=2.0)

    # CapCut can show either the provider chooser or the email form directly.
    email_input = page.locator('input[name="username"]')
    if not email_input.is_visible():
        accept_capcut_cookies(page, user, stop_event, timeout=0.25)
        _click_first_visible(
            (
                page.get_by_text("Continue with email", exact=True),
                page.get_by_role("button", name=re.compile(r"continue with email", re.IGNORECASE)),
            ),
            timeout=15000,
        )
    accept_capcut_cookies(page, user, stop_event, timeout=0.25)
    email_input.wait_for(state="visible", timeout=15000)
    email_input.fill(user)
    print(f"[CAPCUT][ADD LINK][LOGIN] Email filled; submitting email step: {user}")
    accept_capcut_cookies(page, user, stop_event, timeout=0.1)
    _click_first_visible(
        (
            page.get_by_role("button", name="Continue", exact=True),
            page.locator('button[type="submit"]'),
        ),
        timeout=10000,
    )

    password_input = page.locator('input[name="password"]')
    password_deadline = time.time() + 15.0
    continue_retried = False
    while time.time() <= password_deadline:
        _ensure_running(stop_event, user)
        body_text = _body_text(page)
        login_error = _login_error_text(body_text)
        if login_error:
            raise CapCutLoginError(f"CapCut login failed for {user}: {login_error}")
        try:
            if password_input.is_visible():
                break
        except Exception:
            pass
        # CapCut sometimes accepts the click visually but leaves the account on
        # the email step without an error. Clear a delayed cookie banner and
        # retry Continue once before classifying the account as login-blocked.
        if not continue_retried and time.time() >= password_deadline - 10.0:
            try:
                if email_input.is_visible():
                    accept_capcut_cookies(page, user, stop_event, timeout=0.25)
                    _click_first_visible(
                        (
                            page.get_by_role("button", name="Continue", exact=True),
                            page.locator('button[type="submit"]'),
                        ),
                        timeout=3000,
                    )
                    continue_retried = True
                    print(f"[CAPCUT][LOGIN] Retried Continue after email step was stuck: {user}")
            except Exception:
                continue_retried = True
        _wait(0.25, stop_event, user)
    else:
        raise CapCutLoginError(
            f"CapCut login blocked after email for {user}: password form did not appear"
        )

    password_input.fill(password)
    print(f"[CAPCUT][ADD LINK][LOGIN] Password form ready; submitting login: {user}")
    accept_capcut_cookies(page, user, stop_event, timeout=0.1)
    _click_first_visible(
        (
            page.get_by_role("button", name=re.compile(r"^(log in|sign in)$", re.IGNORECASE)),
            page.locator('button[type="submit"]'),
        ),
        timeout=10000,
    )

    deadline = time.time() + 45.0
    last_text = ""
    while time.time() <= deadline:
        _ensure_running(stop_event, user)
        try:
            last_text = page.locator("body").inner_text(timeout=2000)[-1000:]
            login_error = _login_error_text(last_text)
            if login_error:
                raise CapCutLoginError(f"CapCut login failed for {user}: {login_error}")
            if not password_input.is_visible() and "/login" not in page.url.lower():
                print(f"[CAPCUT][LOGIN] Logged in: {user}")
                return
        except CapCutWorkflowInterrupted:
            raise
        except CapCutLoginError:
            raise
        except Exception:
            pass
        _wait(0.5, stop_event, user)
    raise CapCutLoginError(f"CapCut login was not confirmed for {user}: {last_text}")


def _login_with_email(page, account: dict, stop_event, max_attempts: int = 3) -> None:
    """Log in with retries for transient navigation and delayed-form failures."""
    user = account["user"]
    last_error = None
    attempts = max(1, int(max_attempts))
    permanent_markers = (
        "incorrect password",
        "invalid password",
        "email or password is incorrect",
        "wrong password",
        "account doesn't exist",
        "account does not exist",
        "account not found",
        "user doesn't exist",
        "user does not exist",
    )
    for attempt in range(1, attempts + 1):
        _ensure_running(stop_event, user)
        print(f"[CAPCUT][ADD LINK][LOGIN] Attempt {attempt}/{attempts}: {user}")
        try:
            _login_with_email_once(page, account, stop_event)
            return
        except CapCutWorkflowInterrupted:
            raise
        except Exception as exc:
            last_error = exc
            lowered = str(exc).casefold()
            if any(marker in lowered for marker in permanent_markers):
                raise
            if attempt >= attempts:
                break
            print(
                f"[CAPCUT][LOGIN] Retry {attempt}/{attempts - 1} for {user} "
                f"after transient error: {exc}"
            )
            try:
                page.goto("about:blank", wait_until="domcontentloaded", timeout=10000)
            except Exception:
                pass
            _wait(min(5.0, 1.5 * attempt), stop_event, user)
    raise CapCutLoginError(
        f"CapCut login failed after {attempts} attempts for {user}: {last_error}"
    ) from last_error


def _join_space(page, link: str, user: str, stop_event, *, before_submit=None, pending=False) -> None:
    _ensure_running(stop_event, user)
    print(f"[CAPCUT][ADD LINK] Opening space link for {user}: {link}")
    workspace_id, is_member = _read_invitation_membership(page, link, user, stop_event)
    print(
        f"[CAPCUT][ADD LINK][JOIN] Initial state: {user} | workspace={workspace_id} | "
        f"is_member={is_member} | previous_submit_pending={pending}"
    )
    if is_member:
        raise AlreadyJoinedSpaceError(f"Already a member of workspace {workspace_id}: {user}")
    if pending:
        raise JoinVerificationError(f"Previous Submit is still unconfirmed for {user}; not submitting again")
    accept_capcut_cookies(page, user, stop_event, timeout=0.5)

    submit_locators = (
        page.get_by_role("button", name="Submit", exact=True),
        page.get_by_role("button", name=re.compile(r"^(join|join space|accept)$", re.IGNORECASE)),
        page.locator('button:has-text("Submit")'),
    )
    deadline = time.time() + 30.0
    submit_button = None
    while time.time() <= deadline:
        _ensure_running(stop_event, user)
        if _cant_submit_request_visible(page):
            raise LinkFullError(f"Can't submit request: {user}")
        if _cant_join_space_visible(page):
            raise CannotJoinSpaceError(f"Can't join space: {user}")
        # Locate first, then click exactly once. A click timeout can occur after
        # the request was sent, so trying another selector would risk a repeat.
        for locator in submit_locators:
            for index in range(locator.count()):
                candidate = locator.nth(index)
                if candidate.is_visible() and candidate.is_enabled():
                    submit_button = candidate
                    break
            if submit_button is not None:
                break
        if submit_button is not None:
            print(f"[CAPCUT][ADD LINK][JOIN] Submit control found and enabled: {user}")
            break
        _wait(0.5, stop_event, user)
    else:
        raise RuntimeError(f"CapCut Submit button was not found for {user}")

    _ensure_running(stop_event, user)
    if before_submit is not None:
        print(f"[CAPCUT][ADD LINK][JOIN] Persisting pending state before one Submit: {user}")
        before_submit()
    try:
        submit_button.click(timeout=5000)
        print(f"[CAPCUT][ADD LINK][JOIN] Submit click completed: {user} | workspace={workspace_id}")
    except Exception:
        print(f"[CAPCUT][ADD LINK] Click result uncertain; checking membership without another click: {user}")
    _wait(3.0, stop_event, user)
    if _cant_submit_request_visible(page):
        raise LinkFullError(f"Can't submit request: {user}")
    if _cant_join_space_visible(page):
        raise CannotJoinSpaceError(f"Can't join space: {user}")
    # Fresh invitation reads, never a second Submit. Match the exact workspace
    # identified before joining; neither My Cloud nor a pending request counts.
    for attempt in range(3):
        _ensure_running(stop_event, user)
        print(f"[CAPCUT][ADD LINK][VERIFY] Post-submit check {attempt + 1}/3: {user}")
        try:
            confirmed_id, confirmed_member = _read_invitation_membership(page, link, user, stop_event)
        except JoinVerificationError as exc:
            print(f"[CAPCUT][ADD LINK][VERIFY] Check {attempt + 1}/3 failed: {user} | {exc}")
            confirmed_id, confirmed_member = "", False
        if confirmed_id and confirmed_id != workspace_id:
            raise JoinVerificationError(f"Invitation workspace changed during verification for {user}")
        if confirmed_id == workspace_id and confirmed_member:
            print(f"[CAPCUT][ADD LINK] Membership confirmed for {user}: {workspace_id}")
            return
        if attempt < 2:
            _wait(2.0, stop_event, user)
    raise JoinVerificationError(f"Submit sent but membership is not confirmed for {user}; not submitting again")


def run_capcut_add_link_workflow(account: dict, link: str, context: dict | None = None) -> bool:
    """Log in with an existing CapCut account and submit one space invitation."""
    if not account.get("user") or not account.get("passnew"):
        raise RuntimeError("Missing CapCut email or password")
    if not str(link).strip():
        raise RuntimeError("Missing CapCut space link")

    user = account["user"]
    stop_event = (context or {}).get("stop_event")
    cdp_url = _cdp_url(context)
    print(f"[CAPCUT][ADD LINK][FLOW] Connecting browser: {user} | cdp={cdp_url}")
    with sync_playwright() as playwright:
        browser = _connect_browser(playwright, cdp_url, user, stop_event)
        page = open_workflow_page(browser, context)
        print(f"[CAPCUT][ADD LINK][FLOW] Browser connected; starting login: {user}")
        _login_with_email(page, account, stop_event)
        # Capture the authenticated CapCut username for the shared Add Link table.
        try:
            account["userID"] = extract_capcut_username(
                page, user, stop_event, dismiss_popups=_dismiss_post_submit_popups
            )
        except Exception:
            account.setdefault("userID", "")
            print(f"[CAPCUT][ADD LINK][FLOW] Username could not be read; continuing: {user}")
        else:
            print(f"[CAPCUT][ADD LINK][FLOW] Username: {user} | {account.get('userID') or 'not found'}")
        def before_submit():
            account["join_pending"] = True
            persist = (context or {}).get("before_join_submit")
            if persist is not None:
                persist()

        _join_space(
            page, str(link).strip(), user, stop_event,
            before_submit=before_submit, pending=bool(account.get("join_pending")),
        )
        account["join_pending"] = False
        print(f"[CAPCUT][ADD LINK][FLOW] Join workflow verified successfully: {user} -> {link}")
        return True


def run_capcut_check_user_workflow(account: dict, context: dict | None = None) -> str:
    """Log in and return the authenticated account's public CapCut username."""
    if not account.get("user") or not account.get("passnew"):
        raise RuntimeError("Missing CapCut email or password")

    user = account["user"]
    stop_event = (context or {}).get("stop_event")
    cdp_url = _cdp_url(context)
    with sync_playwright() as playwright:
        browser = _connect_browser(playwright, cdp_url, user, stop_event)
        page = open_workflow_page(browser, context)
        _login_with_email(page, account, stop_event)
        username = extract_capcut_username(
            page, user, stop_event, dismiss_popups=_dismiss_post_submit_popups
        )
        account["userID"] = username
        if not username:
            raise RuntimeError(f"CapCut username was not found for {user}")
        return username


def run_capcut_check_pro_workflow(account: dict, context: dict | None = None) -> bool:
    """Log in, dismiss CapCut popups, and check the Pro card in the profile menu."""
    if not account.get("user") or not account.get("passnew"):
        raise RuntimeError("Missing CapCut email or password")

    user = account["user"]
    stop_event = (context or {}).get("stop_event")
    cdp_url = _cdp_url(context)
    with sync_playwright() as playwright:
        browser = _connect_browser(playwright, cdp_url, user, stop_event)
        page = open_workflow_page(browser, context)
        _login_with_email(page, account, stop_event)
        try:
            account["userID"] = extract_capcut_username(
                page, user, stop_event, dismiss_popups=_dismiss_post_submit_popups
            )
        except Exception:
            account.setdefault("userID", "")

        # Let the authenticated home page and its onboarding layers settle. Close
        # each Skip/Got it/X layer, then open the profile menu only once so a
        # non-Pro account cannot toggle the menu open and closed repeatedly.
        _wait(4.0, stop_event, user)
        _dismiss_post_submit_popups(page, user, stop_event, timeout=15.0)

        # Try to open profile menu with multiple strategies (up to 5 attempts)
        menu_opened = False
        for attempt in range(3):
            _ensure_running(stop_event, user)
            if _open_profile_menu(page, user, stop_event, max_attempts=5):
                menu_opened = True
                # Increased timeout from 5s to 12s for Pro check
                deadline = time.time() + 12.0
                menu_visible_seen = False
                while time.time() <= deadline:
                    _ensure_running(stop_event, user)
                    if _pro_plan_visible(page):
                        print(f"[CAPCUT][CHECK PRO] Pro confirmed in profile menu: {user}")
                        return True
                    if _profile_menu_visible(page):
                        # Keep waiting for delayed subscription text instead of
                        # declaring Not Pro after a single 300 ms render cycle.
                        menu_visible_seen = True
                    _wait(0.3, stop_event, user)
                # Only treat the account as Not Pro after the full confirmation
                # window elapsed while an authenticated profile menu was visible.
                if menu_visible_seen or _profile_menu_visible(page):
                    print(f"[CAPCUT][CHECK PRO] Pro check timeout; assuming not Pro: {user}")
                    return False
            if attempt < 2:
                _dismiss_post_submit_popups(page, user, stop_event, timeout=5.0)
                _wait(1.0, stop_event, user)

        # Backup method: try subscription/settings pages if profile menu failed
        if not menu_opened:
            print(f"[CAPCUT][CHECK PRO] Profile menu failed, trying subscription pages for {user}")
            result = _check_pro_via_subscription_page(page, user, stop_event)
            if result is not None:
                return result

        raise RuntimeError(f"CapCut profile menu could not be opened for {user}")
