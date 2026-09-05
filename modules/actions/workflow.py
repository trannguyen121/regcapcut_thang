"""Primary automation workflow."""

import json
import random
import re
import time
from datetime import datetime, timedelta, timezone
from urllib import error, request

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright


SIGN_IN_URL = "https://accounts.x.ai/sign-in"
SIGN_UP_URL = "https://accounts.x.ai/sign-up"
ACCOUNT_URL = "https://accounts.x.ai/account"
GROK_SUBSCRIPTIONS_URL = "https://grok.com/rest/subscriptions"
OAUTH2_CODE_URL = "https://tools.dongvanfb.net/api/get_code_oauth2"
OAUTH2_MESSAGES_URL = "https://tools.dongvanfb.net/api/get_messages_oauth2"
SUBSCRIPTION_TIMEZONE = timezone(timedelta(hours=6))
ACCEPTED_SUBSCRIPTION_TIERS = {
    "SUBSCRIPTION_TIER_GROK_PRO",
    "SUBSCRIPTION_TIER_SUPERGROK",
    "SUBSCRIPTION_TIER_SUPERGROK_HEAVY",
}
US_FIRST_NAMES = (
    "James", "John", "Robert", "Michael", "William", "David", "Richard", "Joseph", "Thomas", "Charles",
    "Christopher", "Daniel", "Matthew", "Anthony", "Mark", "Donald", "Steven", "Paul", "Andrew", "Joshua",
    "Mary", "Patricia", "Jennifer", "Linda", "Elizabeth", "Barbara", "Susan", "Jessica", "Sarah", "Karen",
)
US_LAST_NAMES = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
    "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
)


class WorkflowInterrupted(RuntimeError):
    """Raised when a workflow should stop without being treated as a failure."""


def _cdp_url(context: dict | None) -> str:
    if not context or "start_result" not in context:
        raise RuntimeError("Missing start_result in workflow context")
    address = context["start_result"].remote_debugging_address
    if not address:
        raise RuntimeError("Missing remote_debugging_address from started profile")
    if address.startswith("http://") or address.startswith("https://"):
        return address
    return f"http://{address}"


def wait_for_cdp_http_ready(cdp_url: str, user: str, timeout: float = 45.0, interval: float = 1.0) -> str:
    deadline = time.time() + timeout
    last_error = None
    attempt = 0
    for probe_url in (f"{cdp_url}/json/version", cdp_url):
        while time.time() <= deadline:
            attempt += 1
            try:
                with request.urlopen(probe_url, timeout=3) as response:
                    response.read()
                print(f"[CDP] Endpoint ready for {user}: {probe_url} (attempt {attempt})")
                return probe_url
            except Exception as exc:
                last_error = exc
                print(f"[CDP] Waiting for {user}: {probe_url} not ready yet (attempt {attempt}): {exc}")
                if time.time() + interval > deadline:
                    break
                time.sleep(interval)
    raise RuntimeError(f"CDP endpoint not ready for {user}: {last_error}") from last_error


def connect_browser_over_cdp(playwright, cdp_url: str, user: str, attempts: int = 8, interval: float = 1.5):
    last_error = None
    wait_for_cdp_http_ready(cdp_url, user)
    for attempt in range(1, attempts + 1):
        try:
            print(f"[CDP] Connecting for {user} via {cdp_url} (attempt {attempt}/{attempts})")
            browser = playwright.chromium.connect_over_cdp(cdp_url)
            print(f"[CDP] Connected for {user}")
            return browser
        except Exception as exc:
            last_error = exc
            print(f"[CDP] Connect failed for {user} (attempt {attempt}/{attempts}): {exc}")
            if attempt < attempts:
                time.sleep(interval)
    raise RuntimeError(f"Could not connect to CDP for {user}: {last_error}") from last_error


def _oauth2_payload(account: dict) -> dict[str, str]:
    raw_api = str(account.get("api", "")).strip()
    parts = raw_api.split("|")
    if len(parts) < 3:
        raise RuntimeError("Account api is missing oauth2 fields")
    if len(parts) >= 4:
        email = parts[0]
        refresh_token = parts[-2]
        client_id = parts[-1]
    else:
        email = parts[0]
        refresh_token = parts[1]
        client_id = parts[2]
    return {
        "email": email,
        "refresh_token": refresh_token,
        "client_id": client_id,
    }


def target_email_from_api(account: dict) -> str:
    return _oauth2_payload(account)["email"]


def _post_json(url: str, payload: dict) -> dict:
    http_request = request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0",
        },
        method="POST",
    )
    try:
        with request.urlopen(http_request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = f"HTTP Error {exc.code}: {exc.reason}"
        try:
            body = exc.read().decode("utf-8", "replace").strip()
        except Exception:
            body = ""
        if body:
            detail = f"{detail} ({body[:200]})"
        raise RuntimeError(f"OAuth2 request failed: {detail}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"OAuth2 request failed: {exc}") from exc
    return json.loads(body)


def get_messages_oauth2(account: dict) -> dict:
    payload = _oauth2_payload(account)
    payload["list_mail"] = "all"
    result = _post_json(OAUTH2_MESSAGES_URL, payload)
    if not result.get("status"):
        raise RuntimeError(f"OAuth2 messages request returned false status: {result}")
    return result


def _extract_reset_password_code_from_messages(messages: list[dict]) -> str | None:
    for message in messages:
        sender = str(message.get("from", "")).lower()
        subject = str(message.get("subject", ""))
        body = str(message.get("message", ""))
        haystack = f"{subject}\n{body}".lower()
        if "noreply@x.ai" not in sender:
            continue
        if "reset your password" not in haystack:
            continue
        match = re.search(r"\b[A-Z0-9]{3}-[A-Z0-9]{3}\b", subject)
        if not match:
            match = re.search(r"\b[A-Z0-9]{3}-[A-Z0-9]{3}\b", body)
        if match:
            return match.group(0)
    return None


def wait_for_reset_password_code(account: dict, initial_delay: float = 1.0, interval: float = 1.5, timeout: float = 8.0) -> str:
    user = account.get("user", "")
    print(f"[OTP] Waiting {initial_delay}s before checking mailbox for {user}")
    time.sleep(initial_delay)
    deadline = time.time() + timeout
    last_error = None
    attempt = 0
    while time.time() <= deadline:
        attempt += 1
        try:
            print(f"[OTP] Poll {attempt}: reading mailbox for {user}")
            result = get_messages_oauth2(account)
            code = _extract_reset_password_code_from_messages(result.get("messages") or [])
            if code:
                print(f"[OTP] Received code for {user}: {code}")
                return code
            print(f"[OTP] Poll {attempt}: no reset code yet for {user}")
        except Exception as exc:
            last_error = exc
            print(f"[OTP] Poll {attempt}: mailbox error for {user}: {exc}")
        if time.time() + interval > deadline:
            break
        time.sleep(interval)
    if last_error is not None:
        raise RuntimeError(f"Reset password code polling failed: {last_error}") from last_error
    raise RuntimeError("Reset password code not found within polling window")


def _extract_xai_code_from_messages(messages: list[dict], exclude_codes: set[str] | None = None) -> str | None:
    exclude_codes = exclude_codes or set()
    patterns = (
        r"\b[A-Z0-9]{3}-[A-Z0-9]{3}\b",
        r"\b\d{6}\b",
        r"\b[A-Z0-9]{6}\b",
    )
    for message in messages:
        sender = str(message.get("from", "")).lower()
        subject = str(message.get("subject", ""))
        body = str(message.get("message", ""))
        haystack = f"{subject}\n{body}"
        lowered = haystack.lower()
        if "noreply@x.ai" not in sender:
            continue
        if not any(word in lowered for word in ("code", "verify", "verification", "sign", "xai", "x.ai")):
            continue
        for pattern in patterns:
            match = re.search(pattern, haystack, flags=re.IGNORECASE)
            if match:
                code = match.group(0)
                if code not in exclude_codes:
                    return code
    return None


def wait_for_xai_code(account: dict, initial_delay: float = 1.0, interval: float = 1.5, timeout: float = 20.0, exclude_codes: set[str] | None = None) -> str:
    user = account.get("user", "")
    exclude_codes = exclude_codes or set()
    print(f"[OTP] Waiting {initial_delay}s before checking signup mailbox for {user}")
    time.sleep(initial_delay)
    deadline = time.time() + timeout
    last_error = None
    attempt = 0
    while time.time() <= deadline:
        attempt += 1
        try:
            print(f"[OTP] Signup poll {attempt}: reading mailbox for {user}")
            result = get_messages_oauth2(account)
            code = _extract_xai_code_from_messages(result.get("messages") or [], exclude_codes=exclude_codes)
            if code:
                print(f"[OTP] Received signup code for {user}: {code}")
                return code
            print(f"[OTP] Signup poll {attempt}: no code yet for {user}")
        except Exception as exc:
            last_error = exc
            print(f"[OTP] Signup poll {attempt}: mailbox error for {user}: {exc}")
        if time.time() + interval > deadline:
            break
        time.sleep(interval)
    if last_error is not None:
        raise RuntimeError(f"Signup code polling failed: {last_error}") from last_error
    raise RuntimeError("Signup code not found within polling window")


def extract_reset_password_code(account: dict) -> str:
    result = get_messages_oauth2(account)
    code = _extract_reset_password_code_from_messages(result.get("messages") or [])
    if code:
        return code
    raise RuntimeError("Reset password code not found in xAI messages")


def fill_verification_code(page, code: str) -> None:
    normalized = re.sub(r"[^A-Za-z0-9]", "", code)
    otp_inputs = page.locator('input[maxlength="1"]')
    try:
        otp_inputs.first.wait_for(state="visible", timeout=3000)
    except PlaywrightTimeoutError:
        otp_inputs = page.get_by_role("textbox")
        otp_inputs.first.wait_for(state="visible", timeout=4000)

    count = otp_inputs.count()
    if count >= len(normalized) and len(normalized) > 1:
        for i, char in enumerate(normalized):
            field = otp_inputs.nth(i)
            field.click()
            field.press("Control+a")
            field.press("Backspace")
            field.type(char, delay=10)
        return

    first_input = otp_inputs.first
    first_input.click()
    first_input.press("Control+a")
    first_input.press("Backspace")
    first_input.type(normalized, delay=10)


def clear_verification_code(page) -> None:
    otp_inputs = page.locator('input[maxlength="1"]')
    try:
        otp_inputs.first.wait_for(state="visible", timeout=3000)
    except PlaywrightTimeoutError:
        otp_inputs = page.get_by_role("textbox")
        otp_inputs.first.wait_for(state="visible", timeout=3000)

    count = otp_inputs.count()
    if count > 1:
        for i in range(count):
            field = otp_inputs.nth(i)
            field.click()
            field.fill("")
        return

    first_input = otp_inputs.first
    first_input.click()
    first_input.press("Control+a")
    first_input.press("Backspace")


def otp_invalid_visible(page) -> bool:
    try:
        page.get_by_text("Email validation code is invalid").wait_for(state="visible", timeout=3000)
        return True
    except PlaywrightTimeoutError:
        return False


def wait_until_enabled(locator, timeout: int = 10000, step: float = 0.2) -> None:
    deadline = time.time() + (timeout / 1000)
    while time.time() <= deadline:
        try:
            if locator.is_enabled():
                return
        except Exception:
            pass
        time.sleep(step)
    raise RuntimeError("Target button did not become enabled in time")


def cdp_endpoint_alive(cdp_url: str) -> bool:
    probe_url = f"{cdp_url.rstrip('/')}/json/version"
    try:
        with request.urlopen(probe_url, timeout=1) as response:
            response.read(1)
        return True
    except Exception:
        return False


def interruptible_wait(
    seconds: float,
    user: str,
    stop_event=None,
    page=None,
    browser=None,
    cdp_url: str | None = None,
    step: float = 0.5,
) -> bool:
    """Wait up to seconds, stopping when UI Stop is pressed or the tab/browser is closed."""
    deadline = time.time() + seconds
    cdp_failures = 0
    print(f"[WAIT] Holding workflow for {user}: {seconds}s")
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            print(f"[WAIT] Stop requested while holding workflow for {user}")
            return False
        try:
            if page is not None and page.is_closed():
                raise WorkflowInterrupted(f"Browser tab was closed for {user}")
        except WorkflowInterrupted:
            raise
        except Exception as exc:
            raise WorkflowInterrupted(f"Browser tab is no longer available for {user}: {exc}") from exc
        try:
            if browser is not None and not browser.is_connected():
                raise WorkflowInterrupted(f"Browser was closed for {user}")
        except WorkflowInterrupted:
            raise
        except Exception as exc:
            raise WorkflowInterrupted(f"Browser connection is no longer available for {user}: {exc}") from exc
        if cdp_url is not None:
            if cdp_endpoint_alive(cdp_url):
                cdp_failures = 0
            else:
                cdp_failures += 1
                print(f"[WAIT] CDP endpoint not responding for {user} ({cdp_failures}/3)")
                if cdp_failures >= 3:
                    raise WorkflowInterrupted(f"Browser CDP endpoint stopped responding for {user}")
        time.sleep(min(step, max(0, deadline - time.time())))
    return True


def try_continue_after_otp(page, button, user: str) -> None:
    time.sleep(1)
    try:
        enabled = button.is_enabled()
    except Exception:
        enabled = False
    print(f"[OTP] Continue enabled for {user}: {enabled}")
    try:
        print(f"[OTP] Clicking Continue for {user}")
        button.click(timeout=1000)
        time.sleep(1)
    except Exception as exc:
        print(f"[OTP] Continue click skipped/failed for {user}: {exc}")


def try_signup_continue_after_otp(page, user: str) -> None:
    time.sleep(0.8)
    for name in ("Continue", "Next"):
        try:
            button = page.get_by_role("button", name=name)
            button.wait_for(state="visible", timeout=1000)
            enabled = button.is_enabled()
            print(f"[OTP] Signup {name} enabled for {user}: {enabled}")
            if enabled:
                button.click(timeout=2000)
                time.sleep(1)
                return
        except Exception as exc:
            print(f"[OTP] Signup {name} click skipped/failed for {user}: {exc}")


def wait_for_reset_password_form(page, user: str, timeout: float = 3.0, step: float = 0.2) -> bool:
    deadline = time.time() + timeout
    check = 0
    while time.time() <= deadline:
        check += 1
        try:
            new_password_visible = page.get_by_text("New password").is_visible()
        except Exception:
            new_password_visible = False
        try:
            reset_button_visible = page.get_by_role("button", name="Reset password").is_visible()
        except Exception:
            reset_button_visible = False
        print(
            f"[PASS] Reset form check {check} for {user}: "
            f"new_password_visible={new_password_visible}, reset_button_visible={reset_button_visible}"
        )
        if new_password_visible or reset_button_visible:
            return True
        time.sleep(step)
    return False


def reset_password_form_visible(page) -> bool:
    try:
        if page.get_by_text("New password").is_visible():
            return True
    except Exception:
        pass
    try:
        if page.get_by_role("button", name="Reset password").is_visible():
            return True
    except Exception:
        pass
    return False


def wait_for_change_password_success(page, user: str, timeout: float = 8.0, step: float = 0.5) -> tuple[bool, str]:
    deadline = time.time() + timeout
    check = 0
    while time.time() <= deadline:
        check += 1
        current_url = page.url
        try:
            manage_text_visible = page.get_by_text("Manage your account information").is_visible()
        except Exception:
            manage_text_visible = False
        try:
            password_updated_visible = page.get_by_text("Password updated successfully").is_visible()
        except Exception:
            password_updated_visible = False
        try:
            account_url_ok = current_url.startswith("https://accounts.x.ai/account")
        except Exception:
            account_url_ok = False
        try:
            reset_form_still_visible = reset_password_form_visible(page)
        except Exception:
            reset_form_still_visible = False
        print(
            f"[PASS] Success check {check} for {user}: "
            f"url={current_url}, account_url_ok={account_url_ok}, "
            f"manage_text_visible={manage_text_visible}, password_updated_visible={password_updated_visible}, "
            f"reset_form_still_visible={reset_form_still_visible}"
        )
        if account_url_ok or manage_text_visible or password_updated_visible:
            return True, current_url
        if not reset_form_still_visible and "reset" not in current_url.lower():
            return True, current_url
        time.sleep(step)
    return False, page.url


def get_new_password_input(page):
    print("[PASS] Looking for new password input with data-testid=password")
    try:
        locator_group = page.locator('[data-testid="password"]')
        print(f"[PASS] data-testid=password count: {locator_group.count()}")
        locator = locator_group.first
        locator.wait_for(state="visible", timeout=3000)
        print("[PASS] Found visible password input via data-testid=password")
        return locator
    except PlaywrightTimeoutError:
        print("[PASS] data-testid=password not visible, trying fallback selectors")
    selectors = [
        'input[type="password"]',
        'input[autocomplete="new-password"]',
        'input[name*="password" i]',
        'input[placeholder*="password" i]',
    ]
    for selector in selectors:
        locator_group = page.locator(selector)
        print(f"[PASS] Trying selector {selector}, count={locator_group.count()}")
        locator = locator_group.first
        try:
            locator.wait_for(state="visible", timeout=3000)
            print(f"[PASS] Found visible password input via selector: {selector}")
            return locator
        except PlaywrightTimeoutError:
            print(f"[PASS] Selector not visible: {selector}")
            continue
    print("[PASS] Falling back to last textbox")
    textbox = page.get_by_role("textbox").last
    textbox.wait_for(state="visible", timeout=3000)
    print("[PASS] Using fallback last textbox as password input")
    return textbox


def get_email_input(page):
    try:
        email_input = page.get_by_label("Email")
        email_input.wait_for(state="visible", timeout=5000)
        return email_input
    except PlaywrightTimeoutError:
        pass
    try:
        email_input = page.locator('label:has-text("Email")').locator("xpath=following::input[1]")
        email_input.wait_for(state="visible", timeout=5000)
        return email_input
    except PlaywrightTimeoutError:
        pass
    email_input = page.get_by_role("textbox").first
    email_input.wait_for(state="visible", timeout=5000)
    return email_input


def fill_email_field(page, user: str, label: str = "email") -> None:
    email_input = get_email_input(page)
    print(f"[EMAIL] Clicking {label} input for {user}")
    email_input.click(timeout=3000)
    email_input.press("Control+a")
    email_input.fill(user, timeout=3000)


def fill_login_password_field(page, account: dict) -> None:
    user = account["user"]
    password_input = get_login_password_input(page)
    print(f"[LOGIN] Clicking password input for {user}")
    password_input.click(timeout=3000)
    password_input.press("Control+a")
    print(f"[LOGIN] Filling password for {user}")
    password_input.fill(account["pass"], timeout=3000)


def get_login_password_input(page):
    candidates = (
        page.get_by_label("Password"),
        page.locator('input[type="password"]').first,
        page.locator('input[name*="password" i]').first,
        page.locator('input[autocomplete*="password" i]').first,
    )
    last_error = None
    for locator in candidates:
        try:
            locator.wait_for(state="visible", timeout=4000)
            return locator
        except Exception as exc:
            last_error = exc
    raise RuntimeError("Could not find login password input") from last_error


def click_button_matching(page, pattern: str, label: str, timeout: int = 5000) -> None:
    button = page.get_by_role("button", name=re.compile(pattern, re.I)).first
    button.wait_for(state="visible", timeout=timeout)
    wait_until_enabled(button, timeout=timeout)
    button.click(timeout=timeout)
    print(f"[BUTTON] Clicked {label}")


def wait_for_account_url_or_open(page, user: str, timeout: float = 8.0, step: float = 0.5) -> None:
    deadline = time.time() + timeout
    print(f"[LOGIN] Waiting for account URL after login for {user}")
    try:
        page.wait_for_url(re.compile(r"^https://accounts\.x\.ai/account"), timeout=int(timeout * 1000))
        print(f"[LOGIN] Account URL reached for {user}: {page.url}")
        return
    except PlaywrightTimeoutError:
        pass
    while time.time() <= deadline:
        try:
            current_url = page.url
        except Exception:
            current_url = ""
        if current_url.startswith(ACCOUNT_URL):
            print(f"[LOGIN] Account URL reached for {user}: {current_url}")
            return
        time.sleep(step)
    try:
        current_url = page.url
    except Exception:
        current_url = ""
    if current_url.startswith(ACCOUNT_URL):
        print(f"[LOGIN] Account URL reached for {user}: {current_url}")
        return
    print(f"[LOGIN] Account URL not reached for {user}, opening {ACCOUNT_URL}")
    try:
        page.goto(ACCOUNT_URL, wait_until="domcontentloaded")
    except Exception as exc:
        try:
            current_url = page.url
        except Exception:
            current_url = ""
        if current_url.startswith(ACCOUNT_URL):
            print(f"[LOGIN] Account URL reached during fallback navigation for {user}: {current_url}")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except PlaywrightTimeoutError:
                print(f"[LOGIN] Account fallback domcontentloaded wait timed out for {user}, continuing")
            return
        raise RuntimeError(f"Could not open account page for {user}: {exc}") from exc


def login_existing_account(page, account: dict) -> None:
    user = account["user"]
    if not account.get("pass"):
        raise RuntimeError("Missing account password for login")
    try:
        click_button_matching(page, r"^Login with email$", "login with email", timeout=5000)
    except Exception as exc:
        print(f"[LOGIN] Exact Login with email button not found for {user}: {exc}")
        click_button_matching(page, r"(login|log in|sign in).*email|email.*(login|log in|sign in)", "login with email", timeout=3000)
    print(f"[EMAIL] Filling login email for {user}")
    fill_email_field(page, user, "login email")
    click_button_matching(page, r"^Next$", "login next")
    fill_login_password_field(page, account)
    print(f"[LOGIN] Waiting 5s before clicking Login for {user}")
    time.sleep(5)
    try:
        click_button_matching(page, r"^Login$", "login submit", timeout=5000)
    except Exception as exc:
        print(f"[LOGIN] Exact Login button not found for {user}: {exc}")
        click_button_matching(page, r"^(Log in|Login|Sign in|Sign In|Continue)$", "login submit")
    try:
        page.wait_for_load_state("domcontentloaded", timeout=8000)
    except PlaywrightTimeoutError:
        print(f"[LOGIN] Login page did not finish domcontentloaded quickly for {user}, continuing")


def open_account_page(page, user: str) -> None:
    print(f"[MAIL] Opening account page for {user}")
    page.goto(ACCOUNT_URL, wait_until="domcontentloaded")
    wait_for_account_page_success(page, user, timeout=20.0)


def click_change_email(page, user: str) -> None:
    print(f"[MAIL] Opening change email form for {user}")

    def edit_email_form_visible(timeout: float = 0.5) -> bool:
        try:
            if page.get_by_text(re.compile(r"Edit email", re.I)).first.is_visible(timeout=int(timeout * 1000)):
                return True
        except Exception:
            pass
        try:
            input_visible = page.locator('input[type="email"], input[name*="email" i]').last.is_visible(timeout=int(timeout * 1000))
            continue_visible = page.get_by_role("button", name=re.compile(r"^Continue$", re.I)).first.is_visible(timeout=int(timeout * 1000))
            return input_visible and continue_visible
        except Exception:
            return False

    def wait_for_edit_email_form(timeout: float = 3.0, step: float = 0.25) -> bool:
        deadline = time.time() + timeout
        while time.time() <= deadline:
            if edit_email_form_visible(timeout=0.2):
                print(f"[MAIL] Edit email form visible for {user}")
                return True
            time.sleep(step)
        return False

    def click_candidate(locator, label: str, timeout: int = 3000) -> bool:
        try:
            locator.wait_for(state="visible", timeout=timeout)
            try:
                locator.scroll_into_view_if_needed(timeout=1000)
            except Exception:
                pass
            locator.click(timeout=timeout)
            print(f"[MAIL] Clicked Update email via {label} for {user}")
            if wait_for_edit_email_form():
                return True
            print(f"[MAIL] Update email click via {label} did not open form for {user}, retrying force click")
            locator.click(timeout=timeout, force=True)
            print(f"[MAIL] Force clicked Update email via {label} for {user}")
            return wait_for_edit_email_form()
        except Exception as exc:
            print(f"[MAIL] Update email candidate failed via {label} for {user}: {exc}")
            return False

    def try_click_update_email(timeout: int = 3000) -> bool:
        candidates = (
            ("role-last", page.get_by_role("button", name=re.compile(r"^Update email$", re.I)).last),
            ("button-text-last", page.locator("button", has_text=re.compile(r"^Update email$", re.I)).last),
            (
                "email-row-button",
                page.get_by_text(re.compile(r"^Email$", re.I)).first.locator(
                    "xpath=ancestor::*[self::div or self::section][1]//button"
                ).last,
            ),
        )
        for label, locator in candidates:
            if click_candidate(locator, label, timeout=timeout):
                return True
        return False

    if try_click_update_email():
        return

    print(f"[MAIL] Waiting 5s before opening account page for {user}")
    time.sleep(5)
    try:
        page.goto(ACCOUNT_URL, wait_until="domcontentloaded")
    except Exception as exc:
        print(f"[MAIL] Account page navigation warning for {user}: {exc}")
    if try_click_update_email(timeout=6000):
        return
    raise RuntimeError(f"Could not open Edit email form for {user}")


def get_change_email_input(page, current_email: str, target_email: str):
    candidates = (
        page.get_by_text(re.compile(r"Edit email", re.I)).first.locator("xpath=following::input[1]"),
        page.get_by_label(re.compile(r"new email|email address|email", re.I)),
        page.locator('input[type="email"]').last,
        page.locator('input[name*="email" i]').last,
        page.get_by_role("textbox").last,
    )
    last_error = None
    for locator in candidates:
        try:
            locator.wait_for(state="visible", timeout=4000)
            return locator
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Could not find new email input for {target_email}") from last_error


def fill_password_if_present(page, account: dict, user: str) -> None:
    try:
        password_input = page.locator('input[type="password"]').first
        password_input.wait_for(state="visible", timeout=1200)
        password_input.fill(account["pass"])
        print(f"[MAIL] Filled password confirmation for {user}")
    except Exception:
        return


def submit_change_email_form(page, user: str) -> None:
    patterns = (
        r"^(Continue|Next|Save|Submit|Verify)$",
        r"(change|update|save).*(email|mail)",
        r"(email|mail).*(change|update|save)",
        r"send.*code",
    )
    last_error = None
    for pattern in patterns:
        try:
            click_button_matching(page, pattern, "change email submit", timeout=2500)
            time.sleep(1)
            return
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Could not submit change email form for {user}: {last_error}") from last_error


def change_email_success_visible(page, target_email: str) -> bool:
    try:
        if page.get_by_text(re.compile(r"email.*(updated|update|changed|verified).*success|email.*success.*(updated|update|changed|verified)", re.I)).first.is_visible():
            return True
    except Exception:
        pass
    try:
        inputs = page.locator('input[maxlength="1"]')
        if inputs.count() > 0 and inputs.first.is_visible(timeout=500):
            return False
    except Exception:
        pass
    return False


def wait_for_change_email_success(page, user: str, target_email: str, timeout: float = 10.0, step: float = 0.5) -> bool:
    deadline = time.time() + timeout
    check = 0
    while time.time() <= deadline:
        check += 1
        success = change_email_success_visible(page, target_email)
        invalid = otp_invalid_visible(page)
        print(f"[MAIL] Change email success check {check} for {user}: success={success}, invalid={invalid}")
        if success:
            return True
        if invalid:
            return False
        time.sleep(step)
    return False


def run_change_mail_workflow(page, account: dict) -> bool:
    user = account["user"]
    target_email = target_email_from_api(account)
    print(f"[MAIL] Target email for {user}: {target_email}")
    login_existing_account(page, account)
    click_change_email(page, user)
    fill_password_if_present(page, account, user)
    email_input = get_change_email_input(page, user, target_email)
    print(f"[MAIL] Filling new email for {user}: {target_email}")
    email_input.click(timeout=2000)
    email_input.press("Control+a")
    email_input.fill(target_email, timeout=3000)
    submit_change_email_form(page, user)

    used_codes = set()
    for attempt in range(3):
        code = wait_for_xai_code(
            account,
            initial_delay=1.0 if attempt == 0 else 0.5,
            interval=1.5,
            timeout=20.0,
            exclude_codes=used_codes,
        )
        used_codes.add(code)
        print(f"[MAIL] Filling change email OTP for {user} (attempt {attempt + 1})")
        clear_verification_code(page)
        fill_verification_code(page, code)
        submit_change_email_form(page, user)
        if wait_for_change_email_success(page, user, target_email, timeout=8.0):
            print(f"[MAIL] Change email success for {user}: {target_email}")
            return True
        print(f"[MAIL] OTP not accepted for {user}, retrying mailbox")
    raise RuntimeError(f"Change email OTP was not accepted for {user}")


def split_name_from_email(email: str) -> tuple[str, str]:
    return random.choice(US_FIRST_NAMES), random.choice(US_LAST_NAMES)


def get_input_after_label(page, label_text: str, timeout: int = 10000):
    try:
        locator = page.get_by_label(label_text)
        locator.wait_for(state="visible", timeout=timeout)
        return locator
    except Exception:
        pass
    candidates = [
        page.locator(f'input[aria-label="{label_text}"]'),
        page.locator(f'input[placeholder="{label_text}"]'),
        page.locator(f'label:has-text("{label_text}")').locator("xpath=following::input[1]"),
        page.get_by_text(label_text, exact=True).locator("xpath=following::input[1]"),
    ]
    lowered = label_text.lower()
    if "password" in lowered:
        candidates.extend(
            [
                page.locator('input[type="password"]').first,
                page.locator('input[name*="password" i]').first,
                page.locator('input[autocomplete*="password" i]').first,
            ]
        )
    elif "first" in lowered:
        candidates.extend(
            [
                page.locator('input[name*="first" i]').first,
                page.locator('input[autocomplete*="given" i]').first,
            ]
        )
    elif "last" in lowered:
        candidates.extend(
            [
                page.locator('input[name*="last" i]').first,
                page.locator('input[autocomplete*="family" i]').first,
            ]
        )
    last_error = None
    for locator in candidates:
        try:
            locator.wait_for(state="visible", timeout=timeout)
            return locator
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Could not find input after {label_text}") from last_error


def visible_enabled_inputs(page, selector: str, limit: int = 20):
    inputs = page.locator(selector)
    visible = []
    try:
        count = min(inputs.count(), limit)
    except Exception:
        count = 0
    for index in range(count):
        field = inputs.nth(index)
        try:
            if field.is_visible() and field.is_enabled():
                visible.append(field)
        except Exception:
            continue
    return visible


def get_signup_detail_inputs(page):
    password_inputs = visible_enabled_inputs(page, 'input[type="password"]')
    try:
        first_name_input = page.get_by_text("First name", exact=True).locator("xpath=following::input[1]")
        first_name_input.wait_for(state="visible", timeout=800)
        last_name_input = page.get_by_text("Last name", exact=True).locator("xpath=following::input[1]")
        last_name_input.wait_for(state="visible", timeout=800)
        password_input = password_inputs[0] if password_inputs else page.get_by_text("Password", exact=True).locator("xpath=following::input[1]")
        password_input.wait_for(state="visible", timeout=800)
        return first_name_input, last_name_input, password_input
    except Exception:
        pass
    first_name_input = get_input_after_label(page, "First name", timeout=800)
    last_name_input = get_input_after_label(page, "Last name", timeout=800)
    password_input = password_inputs[0] if password_inputs else get_input_after_label(page, "Password", timeout=800)
    return first_name_input, last_name_input, password_input


def wait_for_signup_details_form(page, user: str, timeout: float = 8.0, step: float = 0.2) -> None:
    deadline = time.time() + timeout
    last_error = None
    while time.time() <= deadline:
        try:
            get_signup_detail_inputs(page)
            print(f"[SIGNUP] Details form visible for {user}")
            return
        except Exception as exc:
            last_error = exc
        time.sleep(step)
    raise RuntimeError(f"Signup details form did not appear for {user}: {last_error}") from last_error


def signup_details_form_visible(page, user: str, timeout: float = 1.2) -> bool:
    try:
        wait_for_signup_details_form(page, user, timeout=timeout, step=0.3)
        return True
    except Exception as exc:
        print(f"[SIGNUP] Details form not visible yet for {user}: {exc}")
        return False


def fill_input_reliably(locator, value: str, label: str, user: str) -> None:
    try:
        locator.scroll_into_view_if_needed(timeout=1000)
    except Exception:
        pass
    locator.click(timeout=2000)
    locator.fill(value, timeout=3000)
    try:
        current = locator.input_value(timeout=1000)
    except Exception:
        current = value
    if current != value:
        print(f"[SIGNUP] {label} fill did not stick for {user}, retrying with typing")
        locator.click(timeout=2000)
        locator.press("Control+a")
        locator.type(value, delay=5, timeout=5000)


def fill_signup_details(page, account: dict) -> None:
    first_name, last_name = split_name_from_email(account["user"])
    print(f"[SIGNUP] Filling name for {account['user']}: {first_name} {last_name}")
    first_name_input, last_name_input, password_input = get_signup_detail_inputs(page)
    fill_input_reliably(first_name_input, first_name, "First name", account["user"])
    print(f"[SIGNUP] Filled First name for {account['user']}")
    time.sleep(1)
    fill_input_reliably(last_name_input, last_name, "Last name", account["user"])
    print(f"[SIGNUP] Filled Last name for {account['user']}")
    time.sleep(1)
    fill_input_reliably(password_input, account["passnew"], "Password", account["user"])
    print(f"[SIGNUP] Filled Password for {account['user']}")
    print(f"[SIGNUP] Waiting 1s after password for {account['user']}")
    time.sleep(1)
    click_verify_checkbox_if_present(page, account["user"], timeout=8.0)
    print(f"[SIGNUP] Waiting 2s after checkbox for {account['user']}")
    time.sleep(2)


def checkbox_checked(locator) -> bool:
    try:
        return bool(locator.is_checked(timeout=500))
    except Exception:
        pass
    try:
        aria_checked = str(locator.get_attribute("aria-checked") or "").lower()
        if aria_checked in {"true", "mixed"}:
            return True
    except Exception:
        pass
    try:
        class_name = str(locator.get_attribute("class") or "").lower()
        return any(word in class_name for word in ("checked", "success", "complete"))
    except Exception:
        return False


def click_checkbox_candidate(candidate, user: str, root_name: str, selector: str, index: int, count: int) -> bool:
    print(
        f"[B8][VERIFY] Clicking checkbox for {user}: "
        f"root={root_name}, selector={selector}, index={index + 1}/{count}"
    )
    try:
        if checkbox_checked(candidate):
            print(f"[B8][VERIFY] Checkbox already checked for {user}")
            return True
    except Exception:
        pass
    try:
        candidate.scroll_into_view_if_needed(timeout=1000)
    except Exception:
        pass
    try:
        label = candidate.locator("xpath=ancestor::label[1]")
        if label.count():
            try:
                label.scroll_into_view_if_needed(timeout=1000)
            except Exception:
                pass
            box = label.bounding_box(timeout=1000)
            if box:
                label.click(
                    position={"x": min(24, max(1, box["width"] / 2)), "y": max(1, box["height"] / 2)},
                    timeout=3000,
                    force=True,
                )
                time.sleep(0.8)
                return True
    except Exception as exc:
        print(f"[B8][VERIFY] Label click failed for {user}: {exc}")
    try:
        candidate.check(timeout=3000, force=True)
        time.sleep(0.8)
        return True
    except Exception as exc:
        print(f"[B8][VERIFY] Checkbox check failed for {user}: {exc}")
    try:
        candidate.click(timeout=3000, force=True)
        time.sleep(0.8)
        return True
    except Exception as exc:
        print(f"[B8][VERIFY] Checkbox force click failed for {user}: {exc}")
    try:
        candidate.evaluate("(element) => element.click()")
        time.sleep(0.8)
        return True
    except Exception as exc:
        print(f"[B8][VERIFY] Checkbox JS click failed for {user}: {exc}")
    return False


def click_checkbox_by_image(page, user: str) -> bool:
    try:
        import cv2
        import numpy as np
    except Exception as exc:
        print(f"[B8][VERIFY] Image click unavailable for {user}: {exc}")
        return False

    try:
        screenshot = page.screenshot(type="png", full_page=False)
        image = cv2.imdecode(np.frombuffer(screenshot, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return False
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        dark_mask = cv2.inRange(gray, 0, 105)
        contours, _ = cv2.findContours(dark_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    except Exception as exc:
        print(f"[B8][VERIFY] Screenshot analysis failed for {user}: {exc}")
        return False

    best = None
    best_score = -1.0
    height, width = gray.shape[:2]
    min_size = max(18, min(width, height) // 50)
    max_size = max(90, min(width, height) // 5)

    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w < min_size or h < min_size or w > max_size or h > max_size:
            continue
        aspect = w / max(h, 1)
        if aspect < 0.65 or aspect > 1.35:
            continue
        crop = dark_mask[y : y + h, x : x + w]
        if crop.size == 0:
            continue
        border = max(2, int(min(w, h) * 0.14))
        top = crop[:border, :].mean() / 255.0
        bottom = crop[-border:, :].mean() / 255.0
        left = crop[:, :border].mean() / 255.0
        right = crop[:, -border:].mean() / 255.0
        inner = crop[border:-border, border:-border].mean() / 255.0 if w > border * 2 and h > border * 2 else 0.0
        border_strength = min(top, bottom, left, right)
        if border_strength < 0.22 or inner > 0.18:
            continue
        size_score = min(w, h) / max_size
        aspect_score = 1.0 - abs(1.0 - aspect)
        left_bias = 1.0 - min(x / max(width, 1), 1.0)
        score = (border_strength * 3.0) + aspect_score + size_score + (left_bias * 0.25) - inner
        if score > best_score:
            best_score = score
            best = (x, y, w, h, border_strength, inner)

    if best is None:
        return False

    x, y, w, h, border_strength, inner = best
    click_x = x + (w / 2)
    click_y = y + (h / 2)
    print(
        f"[B8][VERIFY] Clicking checkbox by image for {user}: "
        f"x={click_x:.1f}, y={click_y:.1f}, size={w}x{h}, border={border_strength:.2f}, inner={inner:.2f}"
    )
    try:
        page.mouse.move(click_x, click_y)
        page.mouse.click(click_x, click_y)
        time.sleep(0.8)
        return True
    except Exception as exc:
        print(f"[B8][VERIFY] Image checkbox click failed for {user}: {exc}")
        return False


def click_verify_checkbox_if_present(page, user: str, timeout: float = 8.0, step: float = 0.5) -> bool:
    print(f"[B8][VERIFY] Looking for checkbox after password for {user}")
    deadline = time.time() + timeout
    last_error = None
    selectors = ('input[type="checkbox"]', '[role="checkbox"]', 'label:has(input[type="checkbox"])')
    while time.time() <= deadline:
        if cloudflare_success_visible(page):
            print(f"[B8][VERIFY] Verification already successful for {user}")
            return True
        if click_checkbox_by_image(page, user):
            return True
        roots = [("page", page)] + [(f"frame:{frame.url[:80]}", frame) for frame in page.frames if frame != page.main_frame]
        for root_name, root in roots:
            for selector in selectors:
                candidates = root.locator(selector)
                try:
                    count = min(candidates.count(), 10)
                except Exception as exc:
                    last_error = exc
                    continue
                for index in range(count):
                    candidate = candidates.nth(index)
                    try:
                        if click_checkbox_candidate(candidate, user, root_name, selector, index, count):
                            return True
                    except Exception as exc:
                        last_error = exc
                        print(f"[B8][VERIFY] Checkbox candidate failed for {user}: {exc}")
        time.sleep(step)
    print(f"[B8][VERIFY] Checkbox not clicked for {user}: {last_error}")
    return False


def wait_for_verification_or_timeout(page, user: str, timeout: float = 5.0, step: float = 1.0) -> bool:
    deadline = time.time() + timeout
    last_error = None
    print(f"[B8][VERIFY] Waiting {timeout}s before trying Complete sign up for {user}")
    check = 0
    challenge_logged = False
    while time.time() <= deadline:
        if cloudflare_success_visible(page):
            print(f"[B8][VERIFY] Verification success visible for {user}")
            return True
        check += 1
        if click_checkbox_by_image(page, user):
            return True
        roots = [("page", page)] + [(f"frame:{frame.url[:80]}", frame) for frame in page.frames if frame != page.main_frame]
        total_candidates = 0
        visible_candidates = 0
        for root_name, root in roots:
            for selector in ('input[type="checkbox"]', '[role="checkbox"]', 'label:has(input[type="checkbox"])'):
                candidates = root.locator(selector)
                try:
                    count = min(candidates.count(), 10)
                except Exception as exc:
                    last_error = exc
                    continue
                total_candidates += count
                for index in range(count):
                    candidate = candidates.nth(index)
                    try:
                        visible_candidates += 1
                        if click_checkbox_candidate(candidate, user, root_name, selector, index, count):
                            if cloudflare_success_visible(page) or checkbox_checked(candidate):
                                print(f"[B8][VERIFY] Checkbox accepted for {user}")
                                return True
                            return True
                    except Exception as exc:
                        last_error = exc
                        print(f"[B8][VERIFY] Checkbox candidate skipped for {user}: {exc}")
        if total_candidates and not challenge_logged:
            challenge_logged = True
            print(
                f"[B8][VERIFY] Challenge detected for {user}. "
                "Workflow will try Complete sign up after the short delay."
            )
        if check == 1 or check % 6 == 0:
            frame_count = max(0, len(page.frames) - 1)
            print(
                f"[B8][VERIFY] Still waiting for verification for {user}: "
                f"checks={check}, frames={frame_count}, candidates={total_candidates}, visible={visible_candidates}"
            )
        time.sleep(step)
    print(f"[B8][VERIFY] Verification not confirmed after {timeout}s for {user}: {last_error}")
    return False


def cloudflare_success_visible(page) -> bool:
    """Return True when the Cloudflare challenge displays Success."""
    text_selectors = (
        'text=/Success!?/i',
        'text=/Thanh cong!?/i',
    )
    for selector in text_selectors:
        try:
            if page.locator(selector).first.is_visible(timeout=500):
                return True
        except Exception:
            pass
    try:
        body_text = page.locator("body").inner_text(timeout=500)
        if "success" in body_text.lower():
            return True
    except Exception:
        pass
    for frame in page.frames:
        for selector in text_selectors:
            try:
                if frame.locator(selector).first.is_visible(timeout=500):
                    return True
            except Exception:
                pass
        try:
            frame_text = frame.locator("body").inner_text(timeout=500)
            if "success" in frame_text.lower():
                return True
        except Exception:
            pass
    return False


def complete_signup_after_verification(
    page,
    user: str,
    timeout: float = 60.0,
    step: float = 1.0,
    initial_delay: float = 0.0,
    click_delay: float = 2.0,
    stop_event=None,
    browser=None,
    cdp_url: str | None = None,
) -> None:
    deadline = time.time() + timeout
    last_error = None
    cdp_failures = 0
    if initial_delay > 0:
        print(f"[B9][COMPLETE] Waiting {initial_delay}s before trying Complete sign up for {user}")
        if not interruptible_wait(
            initial_delay,
            user,
            stop_event=stop_event,
            page=page,
            browser=browser,
            cdp_url=cdp_url,
        ):
            raise WorkflowInterrupted(f"Stop requested while waiting for signup verification for {user}")
    print(f"[B9][COMPLETE] Trying Complete sign up for {user}")
    while time.time() <= deadline:
        if stop_event is not None and stop_event.is_set():
            raise WorkflowInterrupted(f"Stop requested while waiting for signup verification for {user}")
        try:
            if page.is_closed():
                raise WorkflowInterrupted(f"Browser tab was closed for {user}")
        except WorkflowInterrupted:
            raise
        except Exception as exc:
            raise WorkflowInterrupted(f"Browser tab is no longer available for {user}: {exc}") from exc
        try:
            if browser is not None and not browser.is_connected():
                raise WorkflowInterrupted(f"Browser was closed for {user}")
        except WorkflowInterrupted:
            raise
        except Exception as exc:
            raise WorkflowInterrupted(f"Browser connection is no longer available for {user}: {exc}") from exc
        if cdp_url is not None:
            if cdp_endpoint_alive(cdp_url):
                cdp_failures = 0
            else:
                cdp_failures += 1
                print(f"[B9][COMPLETE] CDP endpoint not responding for {user} ({cdp_failures}/3)")
                if cdp_failures >= 3:
                    raise WorkflowInterrupted(f"Browser CDP endpoint stopped responding for {user}")
        try:
            button = page.get_by_role("button", name="Complete sign up")
            button.wait_for(state="visible", timeout=1000)
            success_visible = cloudflare_success_visible(page)
            button_enabled = button.is_enabled()
            print(
                f"[B9][COMPLETE] Verification state for {user}: "
                f"cloudflare_success={success_visible}, complete_enabled={button_enabled}"
            )
            if not button_enabled:
                raise RuntimeError(f"[B9][COMPLETE] Complete sign up button is disabled; captcha likely not passed for {user}")
            print(f"[B9][COMPLETE] Clicking Complete sign up for {user}")
            button.click(timeout=3000)
            time.sleep(click_delay)
            if cloudflare_success_visible(page):
                print(f"[B9][COMPLETE] Captcha still visible after click for {user}")
                raise RuntimeError(f"[B9][COMPLETE] Captcha was not passed for {user}")
            print(f"[B9][COMPLETE] Complete sign up click submitted for {user}")
            return
        except WorkflowInterrupted:
            raise
        except Exception as exc:
            last_error = exc
            print(f"[B9][COMPLETE] Complete sign up attempt failed for {user}: {exc}")
            raise
        time.sleep(step)
    raise RuntimeError(f"[B9][COMPLETE] Complete sign up was not attempted for {user}: {last_error}") from last_error


def accept_terms_of_service_if_present(page, user: str, timeout: float = 8.0) -> bool:
    try:
        page.get_by_text("Accept Terms of Service", exact=True).wait_for(state="visible", timeout=int(timeout * 1000))
    except PlaywrightTimeoutError:
        print(f"[B10][TERMS] Accept Terms of Service screen not shown for {user}")
        return False

    print(f"[B10][TERMS] Accept Terms of Service screen shown for {user}")
    checkboxes = page.locator('button[role="checkbox"]')
    try:
        checkbox_count = checkboxes.count()
    except Exception as exc:
        raise RuntimeError(f"[B10][TERMS] Could not locate terms checkboxes for {user}: {exc}") from exc
    if checkbox_count < 2:
        raise RuntimeError(f"[B10][TERMS] Expected 2 terms checkboxes for {user}, found {checkbox_count}")

    def checkbox_checked(locator) -> bool:
        try:
            if locator.get_attribute("aria-checked") == "true":
                return True
        except Exception:
            pass
        try:
            if locator.get_attribute("data-state") == "checked":
                return True
        except Exception:
            pass
        try:
            return locator.is_checked(timeout=500)
        except Exception:
            pass
        try:
            classes = (locator.get_attribute("class") or "").lower()
            return "checked" in classes or ("bg-" in classes and "black" in classes)
        except Exception:
            return False

    def click_checkbox_until_checked(locator, label: str) -> None:
        locator.wait_for(state="visible", timeout=3000)
        for attempt in range(1, 4):
            if checkbox_checked(locator):
                print(f"[B10][TERMS] {label} already checked for {user}")
                return
            box = locator.bounding_box(timeout=1000)
            if box:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            else:
                locator.click(timeout=3000)
            time.sleep(1)
            checked = checkbox_checked(locator)
            print(f"[B10][TERMS] {label} click attempt {attempt}/3 for {user}: checked={checked}")
            if checked:
                return
        raise RuntimeError(f"[B10][TERMS] {label} checkbox was not checked after retries for {user}")

    click_checkbox_until_checked(checkboxes.nth(0), "First confirmation")
    click_checkbox_until_checked(checkboxes.nth(1), "Age confirmation")

    continue_button = page.get_by_role("button", name="Continue")
    continue_button.wait_for(state="visible", timeout=3000)
    if not checkbox_checked(checkboxes.nth(0)) or not checkbox_checked(checkboxes.nth(1)):
        raise RuntimeError(f"[B10][TERMS] Terms checkboxes were not both checked before Continue for {user}")
    continue_button.click(timeout=3000)
    print(f"[B10][TERMS] Clicked Continue after accepting terms for {user}")
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except PlaywrightTimeoutError:
        print(f"[B10][TERMS] Account page did not finish domcontentloaded quickly for {user}, continuing to wait")
    time.sleep(3)
    return True


def wait_for_account_page_success(page, user: str, timeout: float = 20.0, step: float = 0.5) -> bool:
    deadline = time.time() + timeout
    check = 0
    last_url = page.url
    print(f"[B10][RESULT] Waiting for account page to fully render for {user}")
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except PlaywrightTimeoutError:
        print(f"[B10][RESULT] Account page domcontentloaded wait timed out for {user}, continuing to poll")
    time.sleep(1.5)
    print(f"[B10][RESULT] Waiting for account page success for {user}")
    while time.time() <= deadline:
        check += 1
        try:
            last_url = page.url
        except Exception:
            last_url = ""
        try:
            account_url_ok = last_url.startswith("https://accounts.x.ai/account")
        except Exception:
            account_url_ok = False
        try:
            your_account_visible = page.get_by_role("heading", name=re.compile(r"Your account", re.I)).is_visible()
        except Exception:
            try:
                your_account_visible = page.get_by_text(re.compile(r"Your account", re.I)).first.is_visible()
            except Exception:
                your_account_visible = False
        try:
            manage_visible = page.get_by_text(re.compile(r"Manage your account information\.?", re.I)).first.is_visible()
        except Exception:
            manage_visible = False
        if check == 1 or check % 6 == 0 or (your_account_visible and manage_visible):
            print(
                f"[B10][RESULT] Account page check {check} for {user}: "
                f"url={last_url}, account_url_ok={account_url_ok}, "
                f"your_account_visible={your_account_visible}, manage_visible={manage_visible}"
            )
        if account_url_ok and (your_account_visible or manage_visible):
            print(f"[B10][RESULT] Thanh cong: account page text confirmed for {user}")
            return True
        if account_url_ok and check >= 4:
            print(f"[B10][RESULT] Thanh cong: account URL confirmed for {user}")
            return True
        time.sleep(step)
    raise RuntimeError(f"[B10][RESULT] Account page success not confirmed for {user}: final_url={last_url}")


def wait_for_login_redirect_to_settle(page, user: str, timeout: float = 15.0) -> None:
    print(f"[CHECKDATE] Waiting for login redirect to settle for {user}")
    try:
        page.wait_for_url(re.compile(r"^https://accounts\.x\.ai/account"), timeout=int(timeout * 1000))
        print(f"[CHECKDATE] Login redirect reached account page for {user}: {page.url}")
    except PlaywrightTimeoutError:
        print(f"[CHECKDATE] Account redirect not confirmed for {user}, current URL: {page.url}")
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except PlaywrightTimeoutError:
        print(f"[CHECKDATE] Redirect domcontentloaded wait timed out for {user}, continuing")


def wait_for_subscriptions_json(page, user: str, timeout: float = 20.0, step: float = 0.5) -> dict:
    deadline = time.time() + timeout
    last_text = ""
    print(f"[CHECKDATE] Waiting for subscriptions JSON for {user}")
    while time.time() <= deadline:
        for locator in (page.locator("pre").first, page.locator("body").first):
            try:
                text = locator.inner_text(timeout=1000).strip()
            except Exception:
                continue
            if not text:
                continue
            last_text = text[:200]
            json_start = min(
                [index for index in (text.find("{"), text.find("[")) if index >= 0],
                default=-1,
            )
            if json_start < 0:
                continue
            payload_text = text[json_start:]
            if '"subscriptions"' not in payload_text:
                continue
            try:
                payload = json.loads(payload_text)
                print(f"[CHECKDATE] Subscriptions JSON visible for {user}")
                if isinstance(payload, dict):
                    return payload
            except Exception:
                continue
        time.sleep(step)
    raise RuntimeError(f"Subscriptions JSON did not appear for {user}: last_text={last_text}")


def parse_iso_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def subscription_expiry_utc(subscription: dict) -> datetime | None:
    billing_period_end = parse_iso_utc(subscription.get("billingPeriodEnd"))
    if billing_period_end is not None:
        return billing_period_end
    google = subscription.get("google") if isinstance(subscription.get("google"), dict) else {}
    return parse_iso_utc(google.get("expiryTime"))


def subscription_plan_name(tier: str) -> str:
    return {
        "SUBSCRIPTION_TIER_GROK_PRO": "Grok Pro",
        "SUBSCRIPTION_TIER_SUPERGROK": "SuperGrok",
        "SUBSCRIPTION_TIER_SUPERGROK_HEAVY": "SuperGrok Heavy",
    }.get(tier, tier)


def build_subscription_check_result(payload: dict, tz: timezone = SUBSCRIPTION_TIMEZONE) -> dict:
    subscriptions = payload.get("subscriptions") if isinstance(payload, dict) else None
    if not isinstance(subscriptions, list):
        subscriptions = []
    active = []
    for subscription in subscriptions:
        if not isinstance(subscription, dict):
            continue
        if subscription.get("status") != "SUBSCRIPTION_STATUS_ACTIVE":
            continue
        tier = subscription.get("tier")
        if tier not in ACCEPTED_SUBSCRIPTION_TIERS:
            continue
        expiry_utc = subscription_expiry_utc(subscription)
        if expiry_utc is None:
            continue
        active.append((expiry_utc, subscription))
    if not active:
        return {
            "active": False,
            "status": "false",
            "plan": "",
            "expiry": "",
            "date": "",
            "days": "0",
            "cancel": "",
        }
    expiry_utc, subscription = max(active, key=lambda item: item[0])
    now_utc = datetime.now(timezone.utc)
    remaining_seconds = (expiry_utc - now_utc).total_seconds()
    days_remaining = 0 if remaining_seconds <= 0 else int((remaining_seconds + 86399) // 86400)
    if days_remaining <= 0:
        expiry_local = expiry_utc.astimezone(tz)
        return {
            "active": False,
            "status": "false",
            "plan": subscription_plan_name(str(subscription.get("tier", ""))),
            "expiry": expiry_local.strftime("%Y-%m-%d %H:%M:%S UTC%z"),
            "date": expiry_local.strftime("%Y/%m/%d"),
            "days": "0",
            "cancel": str(bool(subscription.get("cancelAtPeriodEnd"))),
        }
    expiry_local = expiry_utc.astimezone(tz)
    return {
        "active": True,
        "status": "true",
        "plan": subscription_plan_name(str(subscription.get("tier", ""))),
        "expiry": expiry_local.strftime("%Y-%m-%d %H:%M:%S UTC%z"),
        "date": expiry_local.strftime("%Y/%m/%d"),
        "days": str(days_remaining),
        "cancel": str(bool(subscription.get("cancelAtPeriodEnd"))),
    }


def open_grok_subscriptions_after_login(page, user: str) -> dict:
    print(f"[CHECKDATE] Waiting 5s after login for {user}")
    time.sleep(5)
    for attempt in range(1, 4):
        try:
            print(f"[CHECKDATE] Opening subscriptions page for {user} (attempt {attempt}/3)")
            page.goto(GROK_SUBSCRIPTIONS_URL, wait_until="domcontentloaded")
            return wait_for_subscriptions_json(page, user)
        except Exception as exc:
            try:
                current_url = page.url
            except Exception:
                current_url = ""
            if current_url.startswith(GROK_SUBSCRIPTIONS_URL):
                print(f"[CHECKDATE] Subscriptions page reached for {user}: {current_url}")
                return wait_for_subscriptions_json(page, user)
            if attempt == 3:
                raise RuntimeError(f"Could not open subscriptions page for {user}: {exc}") from exc
            print(f"[CHECKDATE] Subscription navigation interrupted for {user}: {exc}")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except PlaywrightTimeoutError:
                pass
            time.sleep(2)


def run_workflow(account: dict | None = None, context: dict | None = None) -> None:
    """Execute the main user actions."""
    if not account or not account.get("user"):
        raise RuntimeError("Missing account user")
    workflow_name = (context or {}).get("workflow_name", "Change")
    if workflow_name not in ("Change Mail", "checkdate") and not account.get("passnew"):
        raise RuntimeError("Missing account passnew")
    with sync_playwright() as p:
        cdp_url = _cdp_url(context)
        start_url = (context or {}).get("start_url", SIGN_IN_URL)
        stop_event = (context or {}).get("stop_event")
        browser = connect_browser_over_cdp(p, cdp_url, account["user"])
        browser_context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = browser_context.pages[0] if browser_context.pages else browser_context.new_page()
        page.goto(start_url, wait_until="domcontentloaded")
        try:
            page.get_by_role("button", name="Accept All Cookies").click(timeout=6000)
        except PlaywrightTimeoutError:
            pass
        if workflow_name == "checkdate":
            login_existing_account(page, account)
            payload = open_grok_subscriptions_after_login(page, account["user"])
            result = build_subscription_check_result(payload)
            print(
                f"[CHECKDATE] {account['user']}: {result['status']} | "
                f"plan={result['plan']} | expiry={result['expiry']} | "
                f"days={result['days']} | cancelAtPeriodEnd={result['cancel']}"
            )
            print(f"[CHECKDATE] Waiting 5s on subscriptions page for {account['user']}")
            time.sleep(5)
            return result
        if workflow_name == "Change Mail":
            return run_change_mail_workflow(page, account)
        email_button_name = "Sign up with email" if start_url == SIGN_UP_URL else "Login with email"
        page.get_by_role("button", name=email_button_name).click(timeout=6000)
        print(f"[EMAIL] Filling email for {account['user']}")
        fill_email_field(page, account["user"])
        if start_url == SIGN_UP_URL:
            page.get_by_role("button", name="Sign up").click(timeout=6000)
            used_codes = set()
            for attempt in range(3):
                signup_code = wait_for_xai_code(
                    account,
                    initial_delay=1.0 if attempt == 0 else 0.5,
                    interval=1.5,
                    timeout=20.0,
                    exclude_codes=used_codes,
                )
                used_codes.add(signup_code)
                print(f"[OTP] Filling signup verification code for {account['user']} (attempt {attempt + 1})")
                clear_verification_code(page)
                fill_verification_code(page, signup_code)
                print(f"[SIGNUP] Waiting for details form after signup OTP for {account['user']}")
                if signup_details_form_visible(page, account["user"], timeout=2.5):
                    break
                try_signup_continue_after_otp(page, account["user"])
                if signup_details_form_visible(page, account["user"], timeout=3.0):
                    break
                print(f"[OTP] Signup OTP not accepted/transitioned for {account['user']}, retrying mailbox")
            else:
                wait_for_signup_details_form(page, account["user"], timeout=8.0)
            fill_signup_details(page, account)
            verification_ok = wait_for_verification_or_timeout(page, account["user"], timeout=5.0)
            if not verification_ok:
                print(f"[B8][VERIFY] Captcha not confirmed before Complete sign up attempt for {account['user']}")
            complete_signup_after_verification(
                page,
                account["user"],
                timeout=8.0,
                stop_event=stop_event,
                browser=browser,
                cdp_url=cdp_url,
            )
            accept_terms_of_service_if_present(page, account["user"])
            wait_for_account_page_success(page, account["user"])
            print(f"[B10][RESULT] Reg Grok workflow returned success for {account['user']}")
            return True
        else:
            page.get_by_role("button", name="Next").click(timeout=6000)
        page.get_by_role("link", name="Forgot your password?").wait_for(state="visible", timeout=5000)
        page.get_by_role("link", name="Forgot your password?").click(timeout=6000)
        continue_button = page.get_by_role("button", name="Continue")
        continue_button.wait_for(state="visible", timeout=3000)
        for attempt in range(3):
            reset_code = wait_for_reset_password_code(account, initial_delay=1.0 if attempt == 0 else 0.5, interval=1.5, timeout=12.0)
            print(f"[OTP] Filling verification code for {account['user']} (attempt {attempt + 1})")
            clear_verification_code(page)
            fill_verification_code(page, reset_code)
            try_continue_after_otp(page, continue_button, account["user"])
            form_visible = wait_for_reset_password_form(page, account["user"], timeout=2.0)
            invalid_visible = otp_invalid_visible(page)
            print(f"[OTP] Post-continue state for {account['user']}: form_visible={form_visible}, invalid_visible={invalid_visible}")
            if form_visible or reset_password_form_visible(page):
                break
            print(f"[OTP] OTP not accepted for {account['user']}, retrying mailbox")
        else:
            print(f"[OTP] Continue/reset transition not confirmed for {account['user']}, moving on")
        wait_for_reset_password_form(page, account["user"], timeout=2.0)
        print(f"[PASS] Reset password page is visible for {account['user']}")
        password_input = get_new_password_input(page)
        print(f"[PASS] Filling new password for {account['user']}")
        print(f"[PASS] Password input visible={password_input.is_visible()} enabled={password_input.is_enabled()}")
        try:
            box = password_input.bounding_box()
            print(f"[PASS] Password input bounding_box={box}")
        except Exception as exc:
            print(f"[PASS] Could not read bounding_box: {exc}")
        print(f"[PASS] Clicking password input for {account['user']}")
        password_input.click()
        print(f"[PASS] Clearing password input for {account['user']}")
        password_input.press("Control+a")
        password_input.press("Backspace")
        print(f"[PASS] Typing new password for {account['user']}")
        password_input.type(account["passnew"], delay=13)
        reset_button = page.get_by_role("button", name="Reset password")
        reset_button.wait_for(state="visible", timeout=3000)
        print(f"[PASS] Clicking Reset password for {account['user']}")
        reset_button.click(timeout=3000)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except PlaywrightTimeoutError:
            print(f"[PASS] Result page did not finish domcontentloaded quickly for {account['user']}, continuing to poll")
        success, current_url = wait_for_change_password_success(page, account["user"], timeout=20.0, step=0.5)
        print(f"[PASS] Final url for {account['user']}: {current_url}")
        print(f"[PASS] Reset password success for {account['user']}: {success}")
        return success
