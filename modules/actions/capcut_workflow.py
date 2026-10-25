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
        if time.time() + 1.0 > deadline:
            break
        _wait(1.0, stop_event, user)
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
        if time.time() + 1.0 > deadline:
            break
        _wait(1.0, stop_event, user)
    if last_error is not None:
        raise RuntimeError(f"CapCut OTP polling failed for {user}: {last_error}") from last_error
    raise RuntimeError(f"CapCut verification code was not found for {user}")


def _resend_capcut_code(page, user: str, stop_event, timeout: float = 75.0) -> None:
    """Wait for CapCut's resend countdown and request a fresh verification code."""
    print(f"[CAPCUT][OTP] Waiting for Resend to become available for {user}")
    deadline = time.time() + timeout
    last_error = None
    resend_pattern = re.compile(
        r"(?:resend|send\s+(?:the\s+)?code\s+again|send\s+again|"
        r"get\s+(?:a\s+)?new\s+code|request\s+(?:a\s+)?new\s+code|"
        r"gá»­i\s+láº¡i|gá»­i\s+láº¡i\s+mÃ£)",
        re.IGNORECASE,
    )
    while time.time() <= deadline:
        _ensure_running(stop_event, user)
        candidates = (
            page.get_by_role("button", name=resend_pattern),
            page.get_by_role("link", name=resend_pattern),
            page.locator("button, [role='button'], a").filter(has_text=resend_pattern),
            page.get_by_text(resend_pattern),
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
                    try:
                        clickable = candidate.locator(
                            "xpath=ancestor-or-self::*[self::button or self::a or "
                            "@role='button' or @tabindex][1]"
                        )
                        if clickable.count() and clickable.is_visible():
                            candidate = clickable
                    except Exception:
                        pass
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
    initial_timeout: float = 25.0,
    max_resends: int = 3,
    resent_timeout: float = 45.0,
    previous_fingerprints: set[str] | None = None,
) -> str:
    """Read the first code, then retry Resend as soon as CapCut allows it."""
    user = account["user"]
    try:
        if previous_fingerprints is not None:
            return _wait_for_resent_capcut_code(
                account,
                stop_event,
                previous_fingerprints,
                timeout=initial_timeout,
            )
        return _wait_for_capcut_code(account, stop_event, excluded, timeout=initial_timeout)
    except CapCutWorkflowInterrupted:
        raise
    except RuntimeError as first_error:
        print(
            f"[CAPCUT][OTP] No verification code after {initial_timeout:.0f}s for {user}: "
            f"{first_error}"
        )
    last_error = None
    for resend_attempt in range(1, max_resends + 1):
        _ensure_running(stop_event, user)
        try:
            previous_fingerprints = _capcut_message_fingerprints(_get_messages(account))
            print(
                f"[CAPCUT][OTP] Resend attempt {resend_attempt}/{max_resends} "
                f"for {user}"
            )
            _resend_capcut_code(page, user, stop_event)
            return _wait_for_resent_capcut_code(
                account,
                stop_event,
                previous_fingerprints,
                timeout=resent_timeout,
            )
        except CapCutWorkflowInterrupted:
            raise
        except RuntimeError as exc:
            last_error = exc
            print(
                f"[CAPCUT][OTP] Resend attempt {resend_attempt}/{max_resends} "
                f"did not return a code for {user}: {exc}"
            )
    raise RuntimeError(
        f"CapCut OTP was not received after {max_resends} resend attempts for {user}: "
        f"{last_error}"
    ) from last_error


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


def _visible_locator(locator, description: str):
    """Return the first visible match instead of trusting a hidden DOM duplicate."""
    count = min(locator.count(), 50)
    for index in range(count):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible():
                return candidate
        except Exception:
            continue
    raise RuntimeError(f"CapCut {description} is not visible")


def _select_visible_option(page, label: str, fallback_index: int) -> None:
    """Choose an option from the currently open birthday dropdown."""
    option_sets = (
        page.get_by_role("option"),
        page.locator(
            '[role="option"], [class*="option" i], '
            '[class*="select-item" i], [class*="menu-item" i]'
        ),
    )
    normalized_label = label.strip().casefold()
    for options in option_sets:
        visible = []
        try:
            count = min(options.count(), 100)
        except Exception:
            continue
        for index in range(count):
            candidate = options.nth(index)
            try:
                if not candidate.is_visible():
                    continue
                text = candidate.inner_text(timeout=1000).strip()
            except Exception:
                continue
            visible.append(candidate)
            if text.casefold() == normalized_label:
                candidate.click(timeout=7000)
                return
        if 0 <= fallback_index < len(visible):
            visible[fallback_index].click(timeout=7000)
            return
    raise RuntimeError(f'CapCut birthday option "{label}" is not visible')


def _birthday_continue_ready(page, timeout: float = 0.0) -> bool:
    """Use the enabled Continue button as CapCut's source of truth for the form."""
    deadline = time.time() + max(0.0, timeout)
    while True:
        buttons = page.get_by_role("button", name="Continue", exact=True).or_(
            page.get_by_role("button", name="Tiếp tục", exact=True)
        )
        try:
            count = min(buttons.count(), 10)
        except Exception:
            count = 0
        for index in range(count):
            candidate = buttons.nth(index)
            try:
                if candidate.is_visible() and candidate.is_enabled():
                    return True
            except Exception:
                continue
        if time.time() >= deadline:
            return False
        time.sleep(0.15)


def _select_birthday(page, email: str) -> None:
    year, month, day = _birthday_for(email)
    month_index = MONTH_NAMES.index(month)
    for attempt in range(1, 4):
        try:
            accept_capcut_cookies(page, email, None, timeout=1.0)
            # CapCut removes the Month/Day placeholder attributes after a
            # value is selected. A retry must recognize a completed form
            # before trying to locate those placeholders again.
            if _birthday_continue_ready(page, timeout=0.25):
                print(f"[CAPCUT][SIGNUP] Birthday form already complete for {email}")
                return
            year_input = _visible_locator(
                page.locator('input[placeholder="Year"], input[placeholder="Năm"]'),
                "birthday year input",
            )
            year_input.fill(year)
            year_input.press("Tab")
            try:
                month_input = _visible_locator(
                    page.locator('input[placeholder="Month"], input[placeholder="Tháng"]'),
                    "birthday month input",
                )
            except RuntimeError:
                # A missing month placeholder can mean that the previous
                # attempt selected it successfully before the DOM re-rendered.
                _visible_locator(
                    page.locator('input[placeholder="Day"], input[placeholder="Ngày"]'),
                    "birthday day input",
                )
                month_input = None
            if month_input is not None:
                try:
                    month_input.locator("..").click(timeout=7000)
                except Exception:
                    month_input.click(timeout=7000)
                _select_visible_option(page, month, month_index)

            if _birthday_continue_ready(page, timeout=0.25):
                print(f"[CAPCUT][SIGNUP] Birthday selected for {email}: {year}-{month}-{day}")
                return

            # Selecting a month can re-render all three controls. Resolve the
            # day input again instead of using a detached locator.
            day_input = _visible_locator(
                page.locator('input[placeholder="Day"], input[placeholder="Ngày"]'),
                "birthday day input",
            )
            try:
                day_input.locator("..").click(timeout=7000)
            except Exception:
                day_input.click(timeout=7000)
            _select_visible_option(page, day, int(day) - 1)
            if not _birthday_continue_ready(page, timeout=3.0):
                raise RuntimeError("CapCut birthday Continue button is still disabled")
            print(f"[CAPCUT][SIGNUP] Birthday selected for {email}: {year}-{month}-{day}")
            return
        except Exception as exc:
            if attempt >= 3:
                raise
            print(
                f"[CAPCUT][SIGNUP] Birthday retry {attempt}/3 for {email}: "
                f"{type(exc).__name__}: {str(exc).splitlines()[0][:240]}"
            )
            accept_capcut_cookies(page, email, None, timeout=1.0)
            time.sleep(1.5)


def _otp_inputs(page):
    return page.locator(
        'input[maxlength="6"], input[autocomplete="one-time-code"], '
        'input[inputmode="numeric"][maxlength="1"]'
    )


def _otp_is_visible(page) -> bool:
    fields = _otp_inputs(page)
    for index in range(fields.count()):
        if fields.nth(index).is_visible():
            return True
    return False


def _fill_otp(page, code: str) -> None:
    fields = _otp_inputs(page)
    fields.first.wait_for(state="visible", timeout=15000)
    visible = [fields.nth(index) for index in range(fields.count()) if fields.nth(index).is_visible()]
    if len(visible) >= len(code) and visible[0].get_attribute("maxlength") == "1":
        for field, digit in zip(visible, code):
            field.fill(digit)
        return
    visible[0].fill(code)


def _wait_for_registration_success(page, user: str, stop_event, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    last_text = ""
    otp_absent_since = None
    while time.time() <= deadline:
        _ensure_running(stop_event, user)
        try:
            url = page.url.lower()
            text = page.locator("body").inner_text(timeout=2000)
            last_text = text[-500:]
            lowered = text.lower()
            otp_visible = _otp_is_visible(page)
            invalid = any(
                phrase in lowered
                for phrase in (
                    "invalid code",
                    "incorrect code",
                    "code has expired",
                    "mã không hợp lệ",
                    "mã không chính xác",
                    "mã đã hết hạn",
                    "mã xác minh không đúng",
                )
            )
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
            if otp_visible:
                otp_absent_since = None
            elif otp_absent_since is None:
                otp_absent_since = time.time()
            if not otp_visible and (
                "/signup" not in url or any(marker in lowered for marker in signed_in_markers)
            ):
                print(f"[CAPCUT][RESULT] Registration completed for {user}: {page.url}")
                return True
            if otp_absent_since is not None and time.time() - otp_absent_since >= 2.0:
                print(f"[CAPCUT][RESULT] OTP form closed; registration completed for {user}: {page.url}")
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


def _first_visible_control(candidates):
    """Return the first visible Playwright locator from a locator collection."""
    try:
        count = min(candidates.count(), 20)
    except Exception:
        return None
    for index in range(count):
        candidate = candidates.nth(index)
        try:
            if candidate.is_visible():
                return candidate
        except Exception:
            continue
    return None


def _capcut_account_area_visible(page) -> bool:
    url = str(getattr(page, "url", "") or "").lower()
    return any(
        marker in url
        for marker in (
            "/my-cloud",
            "/my-edit",
            "/editor",
            "/space/",
        )
    )


def _login_existing_capcut_account(
    page,
    account: dict,
    stop_event,
    step_timeout: int,
    click_timeout: int,
) -> None:
    """Switch an existing signup email to login and keep the same browser tab."""
    user = account["user"]
    password = account["passnew"]
    login_name = re.compile(r"^\s*(?:Log in|Sign in)\s*$", re.I)
    continue_name = re.compile(r"^\s*Continue\s*$", re.I)

    sign_in_wait_seconds = max(60.0, step_timeout / 1000.0)
    sign_in_click_timeout = max(45000, click_timeout)
    sign_in_deadline = time.time() + sign_in_wait_seconds
    login_control = None
    print(
        f"[CAPCUT][LOGIN] Waiting up to {int(sign_in_wait_seconds)}s "
        f"for Sign in: {user}"
    )
    while time.time() <= sign_in_deadline:
        _ensure_running(stop_event, user)
        for role in ("button", "link"):
            login_control = _first_visible_control(page.get_by_role(role, name=login_name))
            if login_control is not None:
                break
        if login_control is None:
            login_control = _first_visible_control(page.get_by_text(login_name, exact=True))
        if login_control is not None:
            break
        if _capcut_account_area_visible(page):
            print(f"[CAPCUT][LOGIN] Login completed for {user}: {page.url}")
            return
        try:
            login_form_visible = bool(
                page.locator('input[name="username"]').first.is_visible()
                or page.locator('input[name="password"]').first.is_visible()
            )
        except Exception:
            login_form_visible = False
        if login_form_visible:
            break
        _wait(0.75, stop_event, user)
    if login_control is not None:
        print(f"[CAPCUT][LOGIN] Sign in ready; waiting 2s before click: {user}")
        _wait(2.0, stop_event, user)
        try:
            login_control.click(timeout=sign_in_click_timeout)
        except Exception:
            login_control.click(timeout=sign_in_click_timeout, force=True)
        _wait(2.0, stop_event, user)

    print(f"[CAPCUT][LOGIN] Existing account detected; logging in normally: {user}")
    deadline = time.time() + max(90.0, (step_timeout / 1000.0) * 1.5)
    password_submitted = False
    while time.time() <= deadline:
        _ensure_running(stop_event, user)
        accept_capcut_cookies(page, user, stop_event, timeout=0.1)
        if _capcut_account_area_visible(page):
            print(f"[CAPCUT][LOGIN] Login completed for {user}: {page.url}")
            return

        email_input = page.locator('input[name="username"]').first
        password_input = page.locator('input[name="password"]').first
        email_visible = False
        password_visible = False
        try:
            email_visible = email_input.is_visible()
        except Exception:
            pass
        try:
            password_visible = password_input.is_visible()
        except Exception:
            pass
        if password_submitted and not password_visible and not any(
            marker in str(getattr(page, "url", "")).lower()
            for marker in ("/login", "/signup")
        ):
            print(f"[CAPCUT][LOGIN] Login completed for {user}: {page.url}")
            return

        if not email_visible and not password_visible:
            continue_email = _first_visible_control(
                page.get_by_text(
                    re.compile(r"^\s*Continue with email\s*$", re.I),
                    exact=True,
                )
            )
            if continue_email is None:
                continue_email = _first_visible_control(
                    page.get_by_role(
                        "button",
                        name=re.compile(r"continue with email", re.I),
                    )
                )
            if continue_email is not None:
                try:
                    continue_email.click(timeout=click_timeout)
                except Exception:
                    continue_email.click(timeout=click_timeout, force=True)
                _wait(0.75, stop_event, user)
                continue

        try:
            if email_visible:
                current_value = str(email_input.input_value(timeout=1000) or "").strip()
                if current_value.lower() != user.lower():
                    email_input.fill(user)
                continue_button = _first_visible_control(
                    page.get_by_role("button", name=continue_name)
                )
                if continue_button is None:
                    continue_button = _first_visible_control(
                        page.locator('button[type="submit"]')
                    )
                if continue_button is not None:
                    continue_button.click(timeout=click_timeout)
                    _wait(0.75, stop_event, user)
                    continue
        except Exception:
            pass

        try:
            if password_visible:
                if password_submitted:
                    body_text = page.locator("body").inner_text(timeout=1500).lower()
                    if any(
                        marker in body_text
                        for marker in (
                            "incorrect password",
                            "wrong password",
                            "invalid password",
                            "password is incorrect",
                        )
                    ):
                        raise RuntimeError(f"CapCut password was rejected for existing account {user}")
                else:
                    password_input.fill(password)
                    submit = _first_visible_control(
                        page.get_by_role(
                            "button",
                            name=re.compile(r"^\s*(?:Log in|Sign in|Continue)\s*$", re.I),
                        )
                    )
                    if submit is None:
                        submit = _first_visible_control(
                            page.locator('button[type="submit"]')
                        )
                    if submit is not None:
                        submit.click(timeout=click_timeout)
                        password_submitted = True
                        _wait(1.0, stop_event, user)
                        continue
                    password_input.press("Enter")
                    password_submitted = True
                    _wait(1.0, stop_event, user)
                    continue
        except RuntimeError:
            raise
        except Exception:
            pass
        _wait(0.5, stop_event, user)

    raise RuntimeError(f"CapCut existing-account login was not confirmed for {user}: {page.url}")


def run_capcut_workflow(account: dict | None = None, context: dict | None = None) -> bool:
    """Register one CapCut account, or log it in during Reg treo if it exists."""
    if not account or not account.get("user"):
        raise RuntimeError("Missing CapCut account email")
    if not account.get("passnew"):
        raise RuntimeError("Missing CapCut account password")

    user = account["user"]
    stop_event = (context or {}).get("stop_event")
    force_resend_test = bool((context or {}).get("force_resend_test")) or os.getenv(
        "REGCAPCUT_FORCE_RESEND_TEST", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    session = (context or {}).get("start_result")
    allow_existing_login = bool(getattr(session, "reg_treo", False))
    using_proxy = bool(getattr(session, "proxy", None))
    signup_step_timeout = 120000 if using_proxy else 30000
    signup_click_timeout = 30000 if using_proxy else 10000
    if using_proxy:
        print("[CAPCUT][NETWORK] Proxy detected; allowing up to 120 seconds per signup step")
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
        ).click(timeout=signup_click_timeout)

        password_input = page.locator('input[name="password"]')
        password_input.wait_for(state="visible", timeout=signup_step_timeout)
        password_input.fill(account["passnew"])
        accept_capcut_cookies(page, user, stop_event, timeout=0.1)
        signup_button = page.get_by_role("button", name="Sign up", exact=True).or_(
            page.get_by_role("button", name="Đăng ký", exact=True)
        )
        visible_signup = _first_visible_control(signup_button)
        signup_render_deadline = time.time() + min(
            15.0,
            max(5.0, signup_step_timeout / 1000.0),
        )
        while visible_signup is None and time.time() <= signup_render_deadline:
            _ensure_running(stop_event, user)
            visible_signup = _first_visible_control(signup_button)
            if visible_signup is None:
                _wait(0.25, stop_event, user)
        if visible_signup is not None:
            visible_signup.click(timeout=signup_click_timeout)
        elif "/signup" in str(getattr(page, "url", "")).lower():
            signup_submit = _first_visible_control(
                page.locator('button[type="submit"]')
            )
            if signup_submit is not None:
                signup_submit.click(timeout=signup_click_timeout)
            else:
                password_input.press("Enter")
        elif allow_existing_login:
            _login_existing_capcut_account(
                page,
                account,
                stop_event,
                signup_step_timeout,
                signup_click_timeout,
            )
            account["userID"] = extract_capcut_username(
                page,
                user,
                stop_event,
                dismiss_popups=dismiss_capcut_onboarding,
            )
            print(f"[CAPCUT][RESULT] Existing user for {user}: {account['userID'] or 'not found'}")
            on_registered = (context or {}).get("on_registered")
            if on_registered is not None:
                on_registered()
            return True
        else:
            signup_button.click(timeout=signup_click_timeout)

        birthday_year = page.locator(
            'input[placeholder="Year"], input[placeholder="Năm"]'
        ).first
        if allow_existing_login:
            existing_markers = (
                "account already exists",
                "email already exists",
                "email is already registered",
                "email address has already been registered",
            )
            route_deadline = time.time() + max(30.0, signup_step_timeout / 1000.0)
            existing_account = False
            while time.time() <= route_deadline:
                _ensure_running(stop_event, user)
                if birthday_year.is_visible():
                    break
                if _capcut_account_area_visible(page):
                    existing_account = True
                    break
                try:
                    body_text = page.locator("body").inner_text(timeout=1500).lower()
                except Exception:
                    body_text = ""
                login_control = _first_visible_control(
                    page.get_by_role(
                        "button",
                        name=re.compile(r"^\s*(?:Log in|Sign in)\s*$", re.I),
                    )
                )
                signup_still_visible = False
                try:
                    signup_still_visible = visible_signup.is_visible()
                except Exception:
                    pass
                current_url = str(getattr(page, "url", "")).lower()
                if any(marker in body_text for marker in existing_markers) or (
                    "/login" in current_url
                    and login_control is not None
                    and not signup_still_visible
                ):
                    existing_account = True
                    break
                _wait(0.5, stop_event, user)
            if existing_account:
                if not _capcut_account_area_visible(page):
                    _login_existing_capcut_account(
                        page,
                        account,
                        stop_event,
                        signup_step_timeout,
                        signup_click_timeout,
                    )
                account["userID"] = extract_capcut_username(
                    page,
                    user,
                    stop_event,
                    dismiss_popups=dismiss_capcut_onboarding,
                )
                print(f"[CAPCUT][RESULT] Existing user for {user}: {account['userID'] or 'not found'}")
                on_registered = (context or {}).get("on_registered")
                if on_registered is not None:
                    on_registered()
                return True
        birthday_year.wait_for(state="visible", timeout=signup_step_timeout)
        print(f"[CAPCUT][SIGNUP] Birthday form ready: {user} | url={page.url}")
        _select_birthday(page, user)
        continue_button = page.get_by_role("button", name="Continue", exact=True).or_(
            page.get_by_role("button", name="Tiếp tục", exact=True)
        )
        continue_button.wait_for(state="visible", timeout=5000)
        if not continue_button.is_enabled():
            raise RuntimeError(f"CapCut birthday form is incomplete for {user}")
        # Snapshot prior CapCut messages before requesting this registration's
        # OTP. This prevents an old six-digit code from an earlier run being
        # submitted immediately and rejected as expired.
        try:
            otp_message_fingerprints = _capcut_message_fingerprints(_get_messages(account))
        except Exception as exc:
            otp_message_fingerprints = None
            print(f"[CAPCUT][OTP] Could not snapshot existing mail for {user}: {exc}")
        continue_button.click(timeout=signup_click_timeout)

        _otp_inputs(page).first.wait_for(state="visible", timeout=signup_step_timeout)
        used_codes: set[str] = set()
        resent_code = None
        otp_fill_attempts = 5
        for attempt in range(otp_fill_attempts):
            if resent_code is not None:
                code = resent_code
                resent_code = None
            elif attempt == 0 and force_resend_test:
                code = _force_resend_test(
                    page,
                    account,
                    stop_event,
                    used_codes,
                    initial_timeout=25.0,
                )
            elif attempt == 0:
                code = _get_capcut_code_with_resend(
                    page,
                    account,
                    stop_event,
                    used_codes,
                    initial_timeout=25.0,
                    max_resends=3,
                    resent_timeout=45.0,
                    previous_fingerprints=otp_message_fingerprints,
                )
            else:
                code = _wait_for_capcut_code(
                    account,
                    stop_event,
                    used_codes,
                    timeout=120.0,
                )
            used_codes.add(code)
            print(
                f"[CAPCUT][OTP] Filling code for {user} "
                f"({attempt + 1}/{otp_fill_attempts})"
            )
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
                if attempt == otp_fill_attempts - 1:
                    return False
                print(f"[CAPCUT][OTP] Code was rejected for {user}; checking for a newer code")
                if _otp_is_visible(page):
                    _otp_inputs(page).first.fill("")
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
                if attempt == otp_fill_attempts - 1:
                    raise
                print(f"[CAPCUT][OTP] Code was not accepted for {user}; checking for a newer code")
                if _otp_is_visible(page):
                    _otp_inputs(page).first.fill("")
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
