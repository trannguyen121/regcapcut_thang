"""Standalone CapCut email registration workflow."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from urllib import error, request

from modules.browser.chromium import open_workflow_page
from playwright.sync_api import sync_playwright


CAPCUT_SIGN_UP_URL = "https://www.capcut.com/signup"
OAUTH2_MESSAGES_URL = "https://tools.dongvanfb.net/api/get_messages_oauth2"
MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


class CapCutWorkflowInterrupted(RuntimeError):
    """Raised when the CapCut workflow is stopped by the user."""


def _cdp_url(context: dict | None) -> str:
    if not context or "start_result" not in context:
        raise RuntimeError("Missing start_result in CapCut workflow context")
    address = context["start_result"].remote_debugging_address
    if not address:
        raise RuntimeError("Missing remote_debugging_address from CapCut profile")
    if address.startswith(("http://", "https://")):
        return address.rstrip("/")
    return f"http://{address}".rstrip("/")


def _ensure_running(stop_event, user: str) -> None:
    if stop_event is not None and stop_event.is_set():
        raise CapCutWorkflowInterrupted(f"Stop requested for {user}")


def _wait(seconds: float, stop_event, user: str, step: float = 0.25) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        _ensure_running(stop_event, user)
        time.sleep(min(step, max(0.0, deadline - time.time())))


def accept_capcut_cookies(page, user: str = "", stop_event=None, timeout: float = 5.0) -> bool:
    """Accept CapCut's cookie banner when it appears, including delayed banners."""
    deadline = time.time() + max(0.0, timeout)
    while time.time() <= deadline:
        if user:
            _ensure_running(stop_event, user)
        controls = (
            page.get_by_role(
                "button",
                name=re.compile(r"^accept all(?: cookies)?$", re.IGNORECASE),
            ),
            page.get_by_text(
                re.compile(r"^accept all(?: cookies)?$", re.IGNORECASE),
                exact=True,
            ),
            page.locator(
                'button:has-text("Accept all"), button:has-text("Accept All"), '
                '#onetrust-accept-btn-handler, [data-testid*="accept" i]'
            ),
        )
        for control in controls:
            try:
                count = min(control.count(), 20)
            except Exception:
                continue
            for index in range(count):
                candidate = control.nth(index)
                try:
                    if candidate.is_visible() and candidate.is_enabled():
                        try:
                            candidate.click(timeout=2000)
                        except Exception:
                            candidate.click(timeout=2000, force=True)
                        print(f"[CAPCUT][COOKIE] Accepted CapCut cookies{f' for {user}' if user else ''}")
                        return True
                except Exception:
                    continue
        if timeout <= 0:
            break
        if user:
            _wait(0.25, stop_event, user)
        else:
            time.sleep(0.25)
    return False


def dismiss_capcut_onboarding(page, user: str = "", stop_event=None, timeout: float = 8.0) -> None:
    """Dismiss first-login surveys/popups while staying in the current browser profile."""
    deadline = time.time() + max(0.0, timeout)
    while time.time() <= deadline:
        if user:
            _ensure_running(stop_event, user)
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
            page.locator('[aria-label*="close" i], [role="button"][aria-label*="close" i]'),
        )
        clicked = False
        for control in controls:
            try:
                count = min(control.count(), 20)
            except Exception:
                continue
            for index in range(count):
                candidate = control.nth(index)
                try:
                    if candidate.is_visible() and candidate.is_enabled():
                        try:
                            candidate.click(timeout=2000)
                        except Exception:
                            candidate.click(timeout=2000, force=True)
                        clicked = True
                        print(f"[CAPCUT][ONBOARDING] Skipped popup for {user}")
                        break
                except Exception:
                    continue
            if clicked:
                break
        if clicked:
            _wait(0.8, stop_event, user)
        else:
            _wait(0.25, stop_event, user)


def _wait_for_cdp(cdp_url: str, user: str, stop_event, timeout: float = 45.0) -> None:
    deadline = time.time() + timeout
    last_error = None
    while time.time() <= deadline:
        _ensure_running(stop_event, user)
        try:
            with request.urlopen(f"{cdp_url}/json/version", timeout=3) as response:
                response.read(1)
            return
        except Exception as exc:
            last_error = exc
            _wait(1.0, stop_event, user)
    raise RuntimeError(f"CapCut CDP endpoint not ready for {user}: {last_error}") from last_error


def _connect_browser(playwright, cdp_url: str, user: str, stop_event):
    _wait_for_cdp(cdp_url, user, stop_event)
    last_error = None
    for attempt in range(1, 9):
        _ensure_running(stop_event, user)
        try:
            print(f"[CAPCUT][CDP] Connecting for {user} ({attempt}/8)")
            return playwright.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:
            last_error = exc
            if attempt < 8:
                _wait(1.5, stop_event, user)
    raise RuntimeError(f"Could not connect CapCut browser for {user}: {last_error}") from last_error


def _oauth2_payload(account: dict) -> dict[str, str]:
    parts = str(account.get("api", "")).strip().split("|")
    if len(parts) < 3:
        raise RuntimeError("CapCut account is missing OAuth2 mailbox fields")
    if len(parts) >= 4:
        email, refresh_token, client_id = parts[0], parts[-2], parts[-1]
    else:
        email, refresh_token, client_id = parts
    return {
        "email": email,
        "refresh_token": refresh_token,
        "client_id": client_id,
        "list_mail": "all",
    }


def _get_messages(account: dict) -> list[dict]:
    payload = json.dumps(_oauth2_payload(account)).encode("utf-8")
    http_request = request.Request(
        OAUTH2_MESSAGES_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0",
        },
        method="POST",
    )
    try:
        with request.urlopen(http_request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise RuntimeError(f"CapCut mailbox request failed: HTTP {exc.code}") from exc
    except (error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"CapCut mailbox request failed: {exc}") from exc
    if not result.get("status"):
        raise RuntimeError("CapCut mailbox API returned false status")
    return result.get("messages") or []


def _extract_capcut_code(messages: list[dict], excluded: set[str]) -> str | None:
    for message in messages:
        sender = str(message.get("from", ""))
        subject = str(message.get("subject", ""))
        body = str(message.get("message", ""))
        direct_code = str(message.get("code", ""))
        haystack = f"{sender}\n{subject}\n{body}\n{direct_code}"
        lowered = haystack.lower()
        if "capcut" not in lowered:
            continue
        if not any(word in lowered for word in ("code", "verify", "verification", "confirm")):
            continue
        for match in re.finditer(r"(?<!\d)(\d{6})(?!\d)", haystack):
            code = match.group(1)
            if code not in excluded:
                return code
    return None


def _capcut_message_fingerprints(messages: list[dict]) -> set[str]:
    fingerprints = set()
    for message in messages:
        haystack = " ".join(
            str(message.get(key, ""))
            for key in ("from", "subject", "message")
        ).lower()
        if "capcut" not in haystack:
            continue
        serialized = json.dumps(message, sort_keys=True, ensure_ascii=False, default=str)
        fingerprints.add(hashlib.sha256(serialized.encode("utf-8")).hexdigest())
    return fingerprints


def _wait_for_resent_capcut_code(
    account: dict,
    stop_event,
    previous_fingerprints: set[str],
    timeout: float = 120.0,
) -> str:
    """Wait for a newly delivered message, accepting a resent code even when unchanged."""
    user = account["user"]
    deadline = time.time() + timeout
    last_error = None
    attempt = 0
    while time.time() <= deadline:
        _ensure_running(stop_event, user)
        attempt += 1
        try:
            print(f"[CAPCUT][OTP] Resend poll {attempt} for {user}")
            messages = _get_messages(account)
            new_messages = []
            for message in messages:
                serialized = json.dumps(message, sort_keys=True, ensure_ascii=False, default=str)
                fingerprint = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
                if fingerprint not in previous_fingerprints:
                    new_messages.append(message)
            code = _extract_capcut_code(new_messages, set())
            if code:
                print(f"[CAPCUT][OTP] Received the resent verification email for {user}")
                return code
        except Exception as exc:
            last_error = exc
            print(f"[CAPCUT][OTP] Resend mailbox warning for {user}: {exc}")
        if time.time() + 2.0 > deadline:
            break
        _wait(2.0, stop_event, user)
    if last_error is not None:
        raise RuntimeError(f"CapCut resent OTP polling failed for {user}: {last_error}") from last_error
    raise RuntimeError(f"CapCut resent verification email was not found for {user}")


def _wait_for_capcut_code(
    account: dict,
    stop_event,
    excluded: set[str],
    timeout: float = 120.0,
) -> str:
    user = account["user"]
    deadline = time.time() + timeout
    last_error = None
    attempt = 0
    while time.time() <= deadline:
        _ensure_running(stop_event, user)
        attempt += 1
        try:
            print(f"[CAPCUT][OTP] Poll {attempt} for {user}")
            code = _extract_capcut_code(_get_messages(account), excluded)
            if code:
                print(f"[CAPCUT][OTP] Received verification code for {user}")
                return code
        except Exception as exc:
            last_error = exc
            print(f"[CAPCUT][OTP] Mailbox warning for {user}: {exc}")
        if time.time() + 2.0 > deadline:
            break
        _wait(2.0, stop_event, user)
    if last_error is not None:
        raise RuntimeError(f"CapCut OTP polling failed for {user}: {last_error}") from last_error
    raise RuntimeError(f"CapCut verification code was not found for {user}")


def _resend_capcut_code(page, user: str, stop_event, timeout: float = 75.0) -> None:
    """Wait for CapCut's resend countdown and request a fresh verification code."""
    print(f"[CAPCUT][OTP] Waiting for Resend to become available for {user}")
    deadline = time.time() + timeout
    last_error = None
    while time.time() <= deadline:
        _ensure_running(stop_event, user)
        candidates = (
            page.get_by_role("button", name=re.compile(r"resend", re.IGNORECASE)),
            page.locator('button:has-text("Resend")'),
            page.locator('[role="button"]:has-text("Resend")'),
            page.get_by_text(re.compile(r"^resend", re.IGNORECASE)),
        )
        for candidates_group in candidates:
            try:
                count = candidates_group.count()
            except Exception as exc:
                last_error = exc
                continue
            for index in range(count - 1, -1, -1):
                candidate = candidates_group.nth(index)
                try:
                    if not candidate.is_visible():
                        continue
                    if candidate.get_attribute("disabled") is not None:
                        continue
                    aria_disabled = str(candidate.get_attribute("aria-disabled") or "").lower()
                    if aria_disabled == "true":
                        continue
                    if hasattr(candidate, "is_enabled") and not candidate.is_enabled():
                        continue
                    candidate.click(timeout=3000)
                    print(f"[CAPCUT][OTP] Requested a new verification code for {user}")
                    return
                except Exception as exc:
                    last_error = exc
        _wait(0.5, stop_event, user)
    raise RuntimeError(f"CapCut Resend did not become available for {user}: {last_error}") from last_error


def _get_capcut_code_with_resend(
    page,
    account: dict,
    stop_event,
    excluded: set[str],
    initial_timeout: float = 60.0,
) -> str:
    """Read the first code, or resend once after a full initial wait."""
    user = account["user"]
    try:
        return _wait_for_capcut_code(
            account,
            stop_event,
            excluded,
            timeout=initial_timeout,
        )
    except CapCutWorkflowInterrupted:
        raise
    except RuntimeError as first_error:
        print(
            f"[CAPCUT][OTP] No verification code after {initial_timeout:.0f}s for {user}: "
            f"{first_error}"
        )
    previous_fingerprints = _capcut_message_fingerprints(_get_messages(account))
    _resend_capcut_code(page, user, stop_event)
    return _wait_for_resent_capcut_code(
        account,
        stop_event,
        previous_fingerprints,
        timeout=120.0,
    )


def _force_resend_test(
    page,
    account: dict,
    stop_event,
    excluded: set[str],
    initial_timeout: float = 60.0,
) -> str:
    """Exercise the no-first-OTP path by waiting, excluding the old code, and resending."""
    user = account["user"]
    print(
        f"[CAPCUT][OTP][TEST] Ignoring the first delivery for {initial_timeout:.0f}s "
        f"to exercise Resend for {user}"
    )
    _wait(initial_timeout, stop_event, user)
    try:
        first_messages = _get_messages(account)
        previous_fingerprints = _capcut_message_fingerprints(first_messages)
        old_code = _extract_capcut_code(first_messages, set())
        if old_code:
            print(f"[CAPCUT][OTP][TEST] Detected the first code but will only use the resent email for {user}")
    except Exception as exc:
        print(f"[CAPCUT][OTP][TEST] Could not snapshot the first code for {user}: {exc}")
        previous_fingerprints = set()
    _resend_capcut_code(page, user, stop_event)
    return _wait_for_resent_capcut_code(
        account,
        stop_event,
        previous_fingerprints,
        timeout=120.0,
    )


def _birthday_for(email: str) -> tuple[str, str, str]:
    digest = hashlib.sha256(email.casefold().encode("utf-8")).digest()
    year = 1988 + (digest[0] % 12)
    month_index = digest[1] % 12
    day = 1 + (digest[2] % 28)
    return str(year), MONTH_NAMES[month_index], str(day)


def _select_birthday(page, email: str) -> None:
    year, month, day = _birthday_for(email)
    month_index = MONTH_NAMES.index(month)
    year_input = page.locator('input[placeholder="Year"], input[placeholder="Năm"]').first
    month_input = page.locator('input[placeholder="Month"], input[placeholder="Tháng"]').first
    day_input = page.locator('input[placeholder="Day"], input[placeholder="Ngày"]').first
    for attempt in range(1, 4):
        try:
            accept_capcut_cookies(page, email, None, timeout=1.0)
            year_input.fill(year)
            month_input.locator("..").click(timeout=7000)
            page.get_by_role("option").nth(month_index).click(timeout=7000)
            day_input.locator("..").click(timeout=7000)
            page.get_by_role("option", name=day, exact=True).last.click(timeout=7000)
            return
        except Exception:
            if attempt >= 3:
                raise
            accept_capcut_cookies(page, email, None, timeout=1.0)
            time.sleep(3.0)


def _fill_otp(page, code: str) -> None:
    otp = page.locator('input[maxlength="6"]').first
    otp.wait_for(state="visible", timeout=15000)
    otp.fill(code)


def _wait_for_registration_success(page, user: str, stop_event, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    last_text = ""
    while time.time() <= deadline:
        _ensure_running(stop_event, user)
        try:
            url = page.url.lower()
            text = page.locator("body").inner_text(timeout=2000)
            last_text = text[-500:]
            lowered = text.lower()
            otp_visible = page.locator('input[maxlength="6"]').is_visible()
            invalid = any(phrase in lowered for phrase in ("invalid code", "incorrect code", "code has expired"))
            signed_in_markers = (
                "which of the following roles best describes you?",
                "get started with space",
                "open capcut",
                "templates & projects",
                "create new space",
                "create with ai",
            )
            if invalid:
                return False
            if not otp_visible and (
                "/signup" not in url or any(marker in lowered for marker in signed_in_markers)
            ):
                print(f"[CAPCUT][RESULT] Registration completed for {user}: {page.url}")
                return True
        except Exception:
            pass
        _wait(0.5, stop_event, user)
    raise RuntimeError(f"CapCut registration was not confirmed for {user}: {last_text}")


def extract_capcut_user_id(page) -> str:
    """Read the numeric id from browser storage, if available."""
    try:
        value = page.evaluate("""() => {
          const text = JSON.stringify(Object.fromEntries(Object.entries(localStorage)));
          const m = text.match(/(?:user[_-]?id|userid|uid)[^0-9a-zA-Z]{0,8}([0-9]{5,})/i);
          return m ? m[1] : '';
        }""")
        return str(value or "").strip()
    except Exception:
        return ""


def extract_capcut_username(page, user: str = "", stop_event=None, dismiss_popups=None) -> str:
    """Load My Cloud and read CapCut's public ``user123...`` handle."""

    def find_username(value) -> str:
        match = re.search(r"\buser\d{6,}\b", str(value or ""), re.IGNORECASE)
        return match.group(0) if match else ""

    def clear_onboarding(timeout: float = 6.0) -> None:
        if not callable(dismiss_popups):
            return
        try:
            dismiss_popups(page, user, stop_event, timeout=timeout)
        except CapCutWorkflowInterrupted:
            raise
        except Exception:
            pass

    # Login can finish on the homepage, where the username is not rendered.
    # My Cloud consistently has the account avatar/profile data available.
    if "/my-cloud" not in str(getattr(page, "url", "")).lower():
        for navigation_attempt in range(3):
            try:
                page.goto(
                    "https://www.capcut.com/my-cloud",
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                break
            except Exception:
                if navigation_attempt >= 2:
                    return ""
                _wait(2.0 + navigation_attempt, stop_event, user)
    _wait(3.0, stop_event, user)
    clear_onboarding()

    # Some first-login accounts are redirected to /my-edit for the three-step
    # "Which role describes you?" survey. Skip it, then request My Cloud again.
    if "/my-cloud" not in str(getattr(page, "url", "")).lower():
        try:
            page.goto(
                "https://www.capcut.com/my-cloud",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            _wait(3.0, stop_event, user)
            clear_onboarding(timeout=4.0)
        except CapCutWorkflowInterrupted:
            raise
        except Exception:
            pass

    for attempt in range(10):
        _ensure_running(stop_event, user)

        # Search visible text first, then the full DOM and browser storage. The
        # latter two cover profile data rendered outside the current viewport.
        snapshots = []
        try:
            snapshots.append(page.locator("body").inner_text(timeout=4000))
        except Exception:
            pass
        try:
            snapshots.append(page.locator("html").inner_html(timeout=4000))
        except Exception:
            pass
        try:
            snapshots.append(
                page.evaluate(
                    """() => JSON.stringify({
                      local: Object.fromEntries(Object.entries(localStorage)),
                      session: Object.fromEntries(Object.entries(sessionStorage))
                    })"""
                )
            )
        except Exception:
            pass
        for snapshot in snapshots:
            username = find_username(snapshot)
            if username:
                return username

        # Onboarding sometimes exposes the handle only in an input value.
        for selector in ("input", "textarea", "[title]", "[aria-label]"):
            try:
                fields = page.locator(selector)
                for index in range(min(fields.count(), 40)):
                    field = fields.nth(index)
                    values = []
                    for attribute in ("value", "title", "aria-label"):
                        try:
                            values.append(field.get_attribute(attribute))
                        except Exception:
                            pass
                    for value in values:
                        username = find_username(value)
                        if username:
                            return username
            except Exception:
                pass

        # Re-open the profile menu; delayed overlays commonly swallow the first
        # click. Every third miss, reload My Cloud and let its data settle again.
        try:
            avatar = page.locator(
                '[data-id="TitleBarUser"], [class*="avatar" i], '
                '[data-testid*="profile" i], [aria-label*="profile" i]'
            ).first
            if avatar.is_visible():
                avatar.click(timeout=5000)
            else:
                viewport = page.viewport_size or {"width": 1280, "height": 720}
                page.mouse.click(max(1, viewport["width"] - 56), 76)
        except Exception:
            pass
        _wait(2.0, stop_event, user)

        if attempt in (2, 5, 8):
            try:
                page.reload(wait_until="domcontentloaded", timeout=45000)
                _wait(3.0, stop_event, user)
                clear_onboarding(timeout=4.0)
                if "/my-cloud" not in str(getattr(page, "url", "")).lower():
                    page.goto(
                        "https://www.capcut.com/my-cloud",
                        wait_until="domcontentloaded",
                        timeout=45000,
                    )
                    _wait(3.0, stop_event, user)
                    clear_onboarding(timeout=4.0)
            except Exception:
                pass
    return ""


def run_capcut_workflow(account: dict | None = None, context: dict | None = None) -> bool:
    """Register one CapCut account by email."""
    if not account or not account.get("user"):
        raise RuntimeError("Missing CapCut account email")
    if not account.get("passnew"):
        raise RuntimeError("Missing CapCut account password")

    user = account["user"]
    stop_event = (context or {}).get("stop_event")
    force_resend_test = bool((context or {}).get("force_resend_test")) or os.getenv(
        "REGCAPCUT_FORCE_RESEND_TEST", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    cdp_url = _cdp_url(context)
    with sync_playwright() as playwright:
        browser = _connect_browser(playwright, cdp_url, user, stop_event)
        page = open_workflow_page(browser, context)
        _ensure_running(stop_event, user)
        print(f"[CAPCUT][START] Opening CapCut signup for {user}")
        last_navigation_error = None
        for navigation_attempt in range(1, 4):
            try:
                page.goto(CAPCUT_SIGN_UP_URL, wait_until="domcontentloaded", timeout=90000)
                break
            except Exception as exc:
                last_navigation_error = exc
                print(f"[CAPCUT][SIGNUP] Retry {navigation_attempt}/3 opening signup for {user}")
                time.sleep(3.0)
        else:
            raise RuntimeError(f"CapCut signup page did not load after 3 retries for {user}: {last_navigation_error}")
        accept_capcut_cookies(page, user, stop_event, timeout=2.0)
        accept_capcut_cookies(page, user, stop_event, timeout=0.25)
        email_input = page.locator('input[name="username"]')
        last_error = None
        for attempt in range(1, 5):
            try:
                accept_capcut_cookies(page, user, stop_event, timeout=1.0)
                if email_input.is_visible():
                    break
                # CapCut first renders an English login component, then may
                # replace it with a localized component using different CSS.
                # Resolve the current element for every attempt so Playwright
                # never clicks a detached element from the first render.
                continue_email = page.get_by_text(
                    re.compile(r"^(Continue with email|Tiếp tục bằng email)$", re.I),
                    exact=True,
                ).first
                continue_email.wait_for(state="visible", timeout=12000)
                continue_email.locator("..").click(timeout=12000)
                email_input.wait_for(state="visible", timeout=20000)
                break
            except Exception as exc:
                last_error = exc
                error_text = str(exc).replace("\n", " ")[:500]
                print(
                    f"[CAPCUT][SIGNUP] Retry {attempt}/4 waiting for email form: {user}"
                    f" | url={page.url} | {type(exc).__name__}: {error_text}"
                )
                if attempt < 4:
                    _wait(5.0, stop_event, user)
        else:
            raise RuntimeError(f"CapCut email form did not appear after 4 retries for {user}: {last_error}")
        email_input.fill(user)
        accept_capcut_cookies(page, user, stop_event, timeout=0.1)
        page.get_by_role("button", name="Continue", exact=True).or_(
            page.get_by_role("button", name="Tiếp tục", exact=True)
        ).click(timeout=10000)

        password_input = page.locator('input[name="password"]')
        password_input.wait_for(state="visible", timeout=30000)
        password_input.fill(account["passnew"])
        accept_capcut_cookies(page, user, stop_event, timeout=0.1)
        page.get_by_role("button", name="Sign up", exact=True).or_(
            page.get_by_role("button", name="Đăng ký", exact=True)
        ).click(timeout=10000)

        birthday_year = page.locator(
            'input[placeholder="Year"], input[placeholder="Năm"]'
        ).first
        birthday_year.wait_for(state="visible", timeout=30000)
        print(f"[CAPCUT][SIGNUP] Birthday form ready: {user} | url={page.url}")
        _select_birthday(page, user)
        continue_button = page.get_by_role("button", name="Continue", exact=True).or_(
            page.get_by_role("button", name="Tiếp tục", exact=True)
        )
        continue_button.wait_for(state="visible", timeout=5000)
        if not continue_button.is_enabled():
            raise RuntimeError(f"CapCut birthday form is incomplete for {user}")
        continue_button.click(timeout=10000)

        page.locator('input[maxlength="6"]').wait_for(state="visible", timeout=30000)
        used_codes: set[str] = set()
        resent_code = None
        for attempt in range(3):
            if resent_code is not None:
                code = resent_code
                resent_code = None
            elif attempt == 0 and force_resend_test:
                code = _force_resend_test(
                    page,
                    account,
                    stop_event,
                    used_codes,
                    initial_timeout=60.0,
                )
            elif attempt == 0:
                code = _get_capcut_code_with_resend(
                    page,
                    account,
                    stop_event,
                    used_codes,
                    initial_timeout=60.0,
                )
            else:
                code = _wait_for_capcut_code(
                    account,
                    stop_event,
                    used_codes,
                    timeout=120.0,
                )
            used_codes.add(code)
            print(f"[CAPCUT][OTP] Filling code for {user} ({attempt + 1}/3)")
            _fill_otp(page, code)
            try:
                accepted = _wait_for_registration_success(page, user, stop_event, timeout=15.0)
                if accepted:
                    account["userID"] = extract_capcut_username(
                        page,
                        user,
                        stop_event,
                        dismiss_popups=dismiss_capcut_onboarding,
                    )
                    print(f"[CAPCUT][RESULT] User for {user}: {account['userID'] or 'not found'}")
                    break
                if attempt == 2:
                    return False
                print(f"[CAPCUT][OTP] Code was rejected for {user}; checking for a newer code")
                page.locator('input[maxlength="6"]').first.fill("")
                previous_fingerprints = _capcut_message_fingerprints(_get_messages(account))
                _resend_capcut_code(page, user, stop_event)
                resent_code = _wait_for_resent_capcut_code(
                    account,
                    stop_event,
                    previous_fingerprints,
                    timeout=120.0,
                )
            except CapCutWorkflowInterrupted:
                raise
            except RuntimeError:
                if attempt == 2:
                    raise
                print(f"[CAPCUT][OTP] Code was not accepted for {user}; checking for a newer code")
                page.locator('input[maxlength="6"]').first.fill("")
                previous_fingerprints = _capcut_message_fingerprints(_get_messages(account))
                _resend_capcut_code(page, user, stop_event)
                resent_code = _wait_for_resent_capcut_code(
                    account,
                    stop_event,
                    previous_fingerprints,
                    timeout=120.0,
                )
        else:
            return False
        # Keep the CDP connection alive while hold mode waits. Disconnecting
        # disposes Playwright-owned incognito contexts. Keep persistence/hold
        # errors outside the OTP retry handler.
        on_registered = (context or {}).get("on_registered")
        if on_registered is not None:
            on_registered()
        return True
