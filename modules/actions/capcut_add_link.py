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
# Membership is eventually consistent after Submit. Real 5-slot spaces can
# take tens of seconds to expose the new collaborator through the invitation API.
JOIN_VERIFY_ATTEMPTS = 12
JOIN_VERIFY_DELAY_SECONDS = 3.0


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


def _workspace_ids_from_payload(payload) -> set[str]:
    """Return workspace IDs explicitly listed for the authenticated account."""
    if not isinstance(payload, dict) or str(payload.get("ret")) != "0":
        return set()
    data = payload.get("data")
    rows = data.get("workspace_infos") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return set()
    return {
        str(row["workspace_id"])
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("workspace_id"), (str, int))
        and not isinstance(row.get("workspace_id"), bool)
        and str(row["workspace_id"]).strip()
    }


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

    captured = {"payload": None, "error": None, "status": None, "workspace_ids": set()}

    def is_workspace_list_response(response):
        try:
            url = urlsplit(response.url)
            host = (url.hostname or "").casefold()
            return (
                url.scheme == "https"
                and (host == "capcut.com" or host.endswith(".capcut.com")
                     or host == "capcutapi.com" or host.endswith(".capcutapi.com"))
                and url.path == "/cc/v1/workspace/get_user_workspaces"
                and response.request.frame == page.main_frame
            )
        except Exception:
            return False

    def response_payload(response):
        try:
            return response.json()
        except Exception as body_error:
            request_body = response.request.post_data_json
            original_headers = response.request.all_headers()
            retry_headers = {
                name: value
                for name, value in original_headers.items()
                if not name.startswith(":") and name.casefold() not in {
                    "cookie", "host", "content-length", "connection", "accept-encoding",
                }
            }
            print(
                f"[CAPCUT][ADD LINK][VERIFY] Response body unavailable; "
                f"retrying read-only API: {user} | {type(body_error).__name__}: {body_error}"
            )
            api_response = page.context.request.post(
                response.url, data=request_body, headers=retry_headers, timeout=20000,
            )
            if not api_response.ok:
                raise JoinVerificationError(
                    f"CapCut membership retry returned HTTP {api_response.status}"
                )
            return api_response.json()

    def capture_membership(response):
        invitation_response = is_invitation_response(response)
        workspace_response = is_workspace_list_response(response)
        if not invitation_response and not workspace_response:
            return
        try:
            if workspace_response:
                captured["workspace_ids"].update(
                    _workspace_ids_from_payload(response_payload(response))
                )
                return
            if captured["payload"] is not None or captured["error"] is not None:
                return
            captured["status"] = response.status
            if not response.ok:
                raise JoinVerificationError(
                    f"CapCut membership request returned HTTP {response.status}"
                )
            # Read inside the callback because redirects can discard CDP bodies.
            captured["payload"] = response_payload(response)
            if str(captured["payload"].get("ret")) != "0":
                print(
                    f"[CAPCUT][ADD LINK][VERIFY] Membership API retry rejected: {user} | "
                    f"ret={captured['payload'].get('ret')} | "
                    f"message={captured['payload'].get('msg') or captured['payload'].get('message') or ''}"
                )
        except Exception as exc:
            if invitation_response:
                captured["error"] = exc
            else:
                print(
                    f"[CAPCUT][ADD LINK][VERIFY] Workspace-list evidence unavailable: "
                    f"{user} | {type(exc).__name__}: {exc}"
                )

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
        # The invitation endpoint can remain stale after a successful join. When
        # CapCut redirects to My Cloud, allow its authenticated workspace list a
        # short bounded window to provide authoritative membership evidence.
        if not is_member and "/my-cloud/" in str(page.url):
            for _ in range(30):
                if workspace_id in captured["workspace_ids"]:
                    break
                _ensure_running(stop_event, user)
                page.wait_for_timeout(100)
        if workspace_id in captured["workspace_ids"]:
            is_member = True
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
    "enter a valid email or password",
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
    """Confirm Pro only from an explicit active-membership indicator.

    Pricing pages contain generic text such as ``Pro plan``, ``Current plan``
    and ``Upgrade to Pro`` even for Free accounts.  Those strings must never
    be treated as proof of an active subscription.
    """
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
        # The authenticated profile card renders the active product directly
        # above its renewal/expiry date (for example: Pro / Renews on ...).
        or bool(
            re.search(
                r"(?im)^\s*(?:pro|ultra|teams)\s*$\s*"
                r"^(?:renews?|expires?|valid\s+until|next\s+billing\s+date)\b",
                text,
            )
        )
    )


def _billing_cycle_from_text(text: str) -> str | None:
    """Return the billing cycle shown in CapCut's read-only plan manager."""
    normalized = str(text or "").casefold()
    if not any(marker in normalized for marker in ("manage auto-renewal", "manage subscription")):
        return None
    yearly = bool(re.search(r"(?im)^\s*(?:yearly|annual|one year|hàng năm|theo năm)\s*$", normalized))
    monthly = bool(re.search(r"(?im)^\s*(?:monthly|one month|hàng tháng|theo tháng)\s*$", normalized))
    # A pricing dialog can contain both choices. Text alone cannot identify
    # which tab is selected, so leave that decision to the DOM-aware reader.
    if yearly and not monthly:
        return "yearly"
    if monthly and not yearly:
        return "monthly"
    return None


def _profile_plan_kind(page) -> str | None:
    """Read an active product from the authenticated profile card."""
    text = _body_text(page)
    lowered = text.casefold()
    if "pro benefits" in lowered and "teams member" in lowered:
        return "teams"
    match = re.search(
        r"(?im)^\s*(pro|ultra|teams)\s*$\s*"
        r"^(?:renews?|expires?|valid\s+until|next\s+billing\s+date)\b",
        text,
    )
    if match:
        return match.group(1).casefold()
    if any(marker in lowered for marker in ("pro: active", "capcut pro: active", "subscription active")):
        return "pro"
    return None


def _read_profile_billing_cycle(page, user: str, stop_event) -> str | None:
    """Open the active plan card and read Yearly/Monthly without changing it."""
    teams_member_profile = bool(
        re.search(r"pro benefits.{0,80}teams member", _body_text(page), re.I | re.S)
    )
    plan_titles = page.get_by_text(re.compile(r"^\s*(?:Pro|Ultra|Teams)\s*$", re.I), exact=True)
    print(f"[CAPCUT][CHECK PRO] Visible plan-title candidates: {plan_titles.count()} | {user}")
    for index in range(min(plan_titles.count(), 20)):
        title = plan_titles.nth(index)
        try:
            if not title.is_visible():
                continue
            title_text = title.inner_text(timeout=1000).strip()
            print(
                f"[CAPCUT][CHECK PRO] Opening visible plan title "
                f"{title_text!r} at index {index}: {user}"
            )
            card = title.locator("xpath=ancestor::div[1]")
            clickable = title
            card_matched = False
            try:
                for _ in range(8):
                    card_info = card.evaluate(
                        """element => ({
                            text: element.innerText || '',
                            className: String(element.className || '')
                        })""",
                        timeout=400,
                    )
                    card_text = str(card_info.get("text", ""))
                    card_class = str(card_info.get("className", ""))
                    is_plan_card = bool(
                        re.search(r"(?:vip|subscription|plan)[-_ ]?card", card_class, re.I)
                    )
                    if teams_member_profile:
                        if re.search(r"pro benefits.{0,200}teams member", card_text, re.I | re.S):
                            clickable = card
                            card_matched = True
                            break
                    else:
                        if (
                            len(card_text) <= 500
                            and re.search(r"(?im)^\s*(?:pro|ultra|teams)\s*$", card_text)
                            and re.search(r"(?im)^\s*(?:renews?|expires?|valid\s+until)\b", card_text)
                        ):
                            clickable = card
                            card_matched = True
                            break
                    if is_plan_card and len(card_text) <= 800:
                        clickable = card
                        card_matched = True
                        break
                    card = card.locator("xpath=..")
            except Exception:
                clickable = title
            if teams_member_profile and not card_matched:
                print(
                    f"[CAPCUT][CHECK PRO] Skipping unrelated visible plan title "
                    f"at index {index}: {user}"
                )
                continue
            try:
                clickable.click(timeout=1500)
            except Exception:
                try:
                    clickable.click(timeout=1500, force=True)
                except Exception:
                    title.evaluate("element => element.click()", timeout=500)
            print(f"[CAPCUT][CHECK PRO] Plan-title click dispatched: {user}")
            deadline = time.time() + 6.0
            retry_stage = 0
            details_logged = False
            while time.time() <= deadline:
                _ensure_running(stop_event, user)
                try:
                    body_text = str(page.evaluate("() => document.body ? document.body.innerText : ''") or "")
                except Exception:
                    body_text = ""
                details_visible = any(
                    marker in body_text.casefold()
                    for marker in ("manage auto-renewal", "manage subscription")
                )
                if not details_visible and _dismiss_promotion_popup(page):
                    print(f"[CAPCUT][POPUP] Dismissed promotion while opening plan details: {user}")
                    _wait(0.25, stop_event, user)
                    continue
                if details_visible and not details_logged:
                    print(f"[CAPCUT][CHECK PRO] Plan details became visible: {user}")
                    details_logged = True
                # CapCut occasionally swallows the first click while the menu
                # animation is finishing. Retry the title, then the outer VIP
                # card; both are pointer targets in current CapCut rollouts.
                if not details_visible and retry_stage == 0 and time.time() >= deadline - 4.0:
                    try:
                        clickable.evaluate("element => element.click()", timeout=500)
                    except Exception:
                        pass
                    retry_stage = 1
                    _wait(0.35, stop_event, user)
                    continue
                if not details_visible and retry_stage == 1 and time.time() >= deadline - 2.0:
                    try:
                        clickable.locator("xpath=..").evaluate(
                            "element => element.click()", timeout=500
                        )
                    except Exception:
                        pass
                    retry_stage = 2
                    _wait(0.35, stop_event, user)
                    continue
                try:
                    active_text = str(
                        page.evaluate(
                            """() => {
                                const tab = document.querySelector(
                                    'button.subscriptionModal-tabActive, '
                                    + 'button[aria-selected="true"], [role="tab"][aria-selected="true"]'
                                );
                                return tab ? (tab.innerText || tab.textContent || '') : '';
                            }"""
                        )
                        or ""
                    ).casefold()
                except Exception:
                    active_text = ""
                if re.search(r"\bmonthly\b|\bone month\b|hàng tháng|theo tháng", active_text):
                    print(f"[CAPCUT][CHECK PRO] Billing cycle confirmed: monthly | {user}")
                    return "monthly"
                if re.search(r"\byearly\b|\bannual\b|\bone year\b|hàng năm|theo năm", active_text):
                    print(f"[CAPCUT][CHECK PRO] Billing cycle confirmed: yearly | {user}")
                    return "yearly"
                cycle = _billing_cycle_from_text(body_text)
                if cycle:
                    print(f"[CAPCUT][CHECK PRO] Billing cycle confirmed: {cycle} | {user}")
                    return cycle
                _wait(0.25, stop_event, user)
            print(f"[CAPCUT][CHECK PRO] Billing cycle unavailable after opening plan details: {user}")
            return None
        except CapCutWorkflowInterrupted:
            raise
        except Exception as exc:
            print(
                f"[CAPCUT][CHECK PRO] Plan detail candidate failed for {user}: "
                f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
            )
            continue
    return None


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


def _dismiss_promotion_popup(page) -> bool:
    """Close a visible CapCut promotion by its top-right icon.

    Some promotion dialogs expose only an unlabelled SVG ``X``. Keep this
    fallback scoped to a small dialog containing known promotion copy so it
    cannot click a checkout window, the profile avatar, or an offer button.
    """
    try:
        return bool(
            page.evaluate(
                r"""() => {
                    const visible = (element) => {
                        const style = getComputedStyle(element);
                        const box = element.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' &&
                            Number(style.opacity || 1) > 0 && box.width > 0 && box.height > 0;
                    };
                    const promoCopy = /(capcut\s+pro|get\s+pro\s+at\s+\d+%\s+off|learn\s+more|get\s+offer)/i;
                    const actionCopy = /^(learn\s+more|get\s+offer|upgrade|subscribe|buy|pay)$/i;
                    const rootCandidates = Array.from(document.querySelectorAll(
                        '[role="dialog"], [aria-modal="true"], [class*="modal" i], '
                        + '[class*="dialog" i], [class*="popup" i]'
                    ));
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                    let textNode;
                    while ((textNode = walker.nextNode())) {
                        if (!promoCopy.test(textNode.nodeValue || '')) continue;
                        let ancestor = textNode.parentElement;
                        for (let depth = 0; ancestor && depth < 8; depth += 1) {
                            rootCandidates.push(ancestor);
                            ancestor = ancestor.parentElement;
                        }
                    }
                    const roots = Array.from(new Set(rootCandidates)).filter((root) => {
                        if (!visible(root) || !promoCopy.test(root.innerText || '')) return false;
                        const box = root.getBoundingClientRect();
                        return box.width >= 240 && box.width <= 900 && box.height >= 180 &&
                            box.height <= 760 && box.width * box.height < innerWidth * innerHeight * 0.8;
                    }).sort((left, right) => {
                        const a = left.getBoundingClientRect();
                        const b = right.getBoundingClientRect();
                        return a.width * a.height - b.width * b.height;
                    });
                    for (const root of roots) {
                        const rootBox = root.getBoundingClientRect();
                        const candidates = Array.from(root.querySelectorAll(
                            'button, [role="button"], [aria-label], [data-testid], '
                            + '[class*="close" i], svg'
                        )).sort((left, right) => {
                            const a = left.getBoundingClientRect();
                            const b = right.getBoundingClientRect();
                            const aDistance = Math.hypot(rootBox.right - (a.left + a.width / 2),
                                (a.top + a.height / 2) - rootBox.top);
                            const bDistance = Math.hypot(rootBox.right - (b.left + b.width / 2),
                                (b.top + b.height / 2) - rootBox.top);
                            return aDistance - bDistance;
                        });
                        for (const candidate of candidates) {
                            if (!visible(candidate)) continue;
                            const label = (
                                candidate.getAttribute('aria-label') ||
                                candidate.getAttribute('title') ||
                                candidate.innerText || ''
                            ).trim();
                            if (actionCopy.test(label)) continue;
                            const box = candidate.getBoundingClientRect();
                            const centerX = box.left + box.width / 2;
                            const centerY = box.top + box.height / 2;
                            const inTopRight = centerX >= rootBox.left + rootBox.width * 0.78 &&
                                centerY <= rootBox.top + Math.min(120, rootBox.height * 0.35);
                            const closeSized = box.width >= 8 && box.height >= 8 &&
                                box.width <= 80 && box.height <= 80;
                            const className = String(candidate.className?.baseVal || candidate.className || '');
                            const namedClose = /^(close|dismiss|x)$/i.test(label) || /close/i.test(className);
                            if (!closeSized || (!inTopRight && !namedClose)) continue;
                            const target = candidate.closest('button, [role="button"]') || candidate;
                            if (typeof target.click === 'function') {
                                target.click();
                            } else {
                                target.dispatchEvent(new MouseEvent('click', {
                                    bubbles: true, cancelable: true, view: window
                                }));
                            }
                            return true;
                        }
                    }
                    return false;
                }"""
            )
        )
    except Exception:
        return False


def _dismiss_post_submit_popups(page, user: str, stop_event, timeout: float = 15.0) -> None:
    """Dismiss CapCut onboarding/announcement layers that cover the profile avatar."""
    started = time.monotonic()
    deadline = started + max(0.0, timeout)
    quiet_since = started
    cycles = 0
    max_cycles = max(1, int(max(0.0, timeout) / 0.1) + 5)
    while time.monotonic() <= deadline and cycles < max_cycles:
        cycles += 1
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
        )
        for control in controls:
            if _click_visible_control(control):
                clicked = True
                print(f"[CAPCUT][POPUP] Dismissed a popup for {user}")
                quiet_since = time.monotonic()
                _wait(0.35, stop_event, user)
                break
        if not clicked and _dismiss_promotion_popup(page):
            clicked = True
            print(f"[CAPCUT][POPUP] Dismissed promotion popup for {user}")
            quiet_since = time.monotonic()
            _wait(0.35, stop_event, user)
        if not clicked:
            # Once the page stays quiet, continue immediately. If another layer
            # appears later, the profile retry path invokes this cleanup again.
            if time.monotonic() - quiet_since >= min(0.7, max(0.0, timeout)):
                return
            _wait(0.15, stop_event, user)


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

    if "/signup" in page.url.casefold():
        raise CapCutLoginError(
            f"CapCut redirected to signup after email for {user}; "
            "the account was not recognized as an existing login"
        )

    password_input.fill(password)
    print(f"[CAPCUT][ADD LINK][LOGIN] Password form ready; submitting login: {user}")
    accept_capcut_cookies(page, user, stop_event, timeout=0.1)
    try:
        _click_first_visible(
            (
                page.get_by_role(
                    "button",
                    name=re.compile(r"^(log in|sign in|continue)$", re.IGNORECASE),
                ),
                page.locator('button[type="submit"]'),
            ),
            timeout=4000,
        )
    except RuntimeError:
        # CapCut currently A/B tests a password form whose submit control has no
        # stable accessible name/type. On a real login URL, submitting the
        # focused password field is equivalent to pressing the visible button.
        password_input.press("Enter")
        print(f"[CAPCUT][LOGIN] Submitted password with Enter fallback: {user}")

    deadline = time.time() + 45.0
    last_text = ""
    while time.time() <= deadline:
        _ensure_running(stop_event, user)
        try:
            last_text = page.locator("body").inner_text(timeout=2000)[-1000:]
            login_error = _login_error_text(last_text)
            if login_error:
                raise CapCutLoginError(f"CapCut login failed for {user}: {login_error}")
            current_url = page.url.lower()
            if not password_input.is_visible() and not any(
                marker in current_url for marker in ("/login", "/signup")
            ):
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
        "enter a valid email or password",
        "wrong password",
        "account doesn't exist",
        "account does not exist",
        "account not found",
        "user doesn't exist",
        "user does not exist",
        "redirected to signup",
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
            rate_limited = "try again later" in lowered or "too many attempts" in lowered
            if rate_limited:
                # CapCut's login throttle does not clear during the old 1.5–3s
                # retry interval. Drop the rejected session cookies and use an
                # interruptible cooldown before submitting this account again.
                try:
                    page.context.clear_cookies()
                except Exception:
                    pass
                cooldown = min(60.0, 15.0 * attempt)
                print(f"[CAPCUT][LOGIN] Rate limited; cooling down {cooldown:g}s: {user}")
                _wait(cooldown, stop_event, user)
            else:
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
        if pending:
            print(f"[CAPCUT][ADD LINK] Previous Submit is now confirmed for {user}: {workspace_id}")
            return
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
    for attempt in range(JOIN_VERIFY_ATTEMPTS):
        _ensure_running(stop_event, user)
        print(
            f"[CAPCUT][ADD LINK][VERIFY] Post-submit check "
            f"{attempt + 1}/{JOIN_VERIFY_ATTEMPTS}: {user}"
        )
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
        if attempt < JOIN_VERIFY_ATTEMPTS - 1:
            _wait(JOIN_VERIFY_DELAY_SECONDS, stop_event, user)
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


def run_capcut_login_hold_workflow(account: dict, context: dict | None = None) -> bool:
    """Log in to an existing CapCut account and keep its browser open."""
    if not account.get("user") or not account.get("passnew"):
        raise RuntimeError("Missing CapCut email or password")

    user = account["user"]
    stop_event = (context or {}).get("stop_event")
    cdp_url = _cdp_url(context)
    with sync_playwright() as playwright:
        browser = _connect_browser(playwright, cdp_url, user, stop_event)
        page = open_workflow_page(browser, context)
        _login_with_email(page, account, stop_event)
        print(f"[CAPCUT][LOGIN TREO] Đăng nhập thành công: {user}")
        on_logged_in = (context or {}).get("on_logged_in")
        if on_logged_in is not None:
            on_logged_in()
        return True


def run_capcut_check_pro_workflow(account: dict, context: dict | None = None) -> str | bool:
    """Return yearly/monthly/teams/pro for paid accounts, otherwise False."""
    if not account.get("user") or not account.get("passnew"):
        raise RuntimeError("Missing CapCut email or password")

    user = account["user"]
    stop_event = (context or {}).get("stop_event")
    cdp_url = _cdp_url(context)
    with sync_playwright() as playwright:
        browser = _connect_browser(playwright, cdp_url, user, stop_event)
        page = open_workflow_page(browser, context)
        # A check batch already retries the whole account once at worker level.
        # Repeating the login three more times in the same browser only extends
        # IP throttling and can hold one slot for over a minute.
        _login_with_email(page, account, stop_event, max_attempts=1)
        print(f"[CAPCUT][CHECK PRO] Authenticated landing page: {page.url} | {user}")
        # Plan checking does not need the public username. Extracting it can
        # navigate through onboarding pages and adds 10-20 seconds per account.
        account.setdefault("userID", "")

        # Fresh accounts can remain on the role survey after login. In that
        # state every avatar click fails and the old nested retry loop looked
        # like a frozen form. Move to My Cloud once and clear that onboarding
        # before looking for the authenticated profile card.
        initial_text = _body_text(page).casefold()
        onboarding_prepared = False
        if "/my-edit" in str(page.url).casefold() or any(
            marker in initial_text
            for marker in (
                "which of the following roles best describes you",
                "get started with space",
                "templates & projects",
            )
        ):
            for navigation_attempt in range(2):
                _ensure_running(stop_event, user)
                try:
                    page.goto(CAPCUT_MY_CLOUD_URL, wait_until="domcontentloaded", timeout=20000)
                    _wait(1.5, stop_event, user)
                    _dismiss_post_submit_popups(page, user, stop_event, timeout=3.0)
                    if "/my-cloud" in str(page.url).casefold():
                        onboarding_prepared = True
                        break
                except CapCutWorkflowInterrupted:
                    raise
                except Exception:
                    if navigation_attempt >= 1:
                        break

        # Let the authenticated home page and its onboarding layers settle. Close
        # each Skip/Got it/X layer, then open the profile menu only once so a
        # non-Pro account cannot toggle the menu open and closed repeatedly.
        if not onboarding_prepared:
            _wait(1.0, stop_event, user)
            _dismiss_post_submit_popups(page, user, stop_event, timeout=1.5)

        # _open_profile_menu already performs bounded strategies. Do not
        # wrap it in another three-attempt loop, which could stall one account
        # for minutes when CapCut blocks the avatar behind onboarding.
        menu_opened = _open_profile_menu(page, user, stop_event, max_attempts=3)
        if menu_opened:
            deadline = time.time() + 12.0
            menu_visible_seen = False
            while time.time() <= deadline:
                _ensure_running(stop_event, user)
                plan_kind = _profile_plan_kind(page)
                if plan_kind:
                    print(
                        f"[CAPCUT][CHECK PRO] Active {plan_kind.title()} confirmed "
                        f"in profile menu: {user}"
                    )
                    billing_cycle = _read_profile_billing_cycle(page, user, stop_event)
                    if plan_kind == "teams" and billing_cycle:
                        return f"teams_{billing_cycle}"
                    return billing_cycle or plan_kind
                if _profile_menu_visible(page):
                    menu_visible_seen = True
                _wait(0.3, stop_event, user)
            if menu_visible_seen or _profile_menu_visible(page):
                print(f"[CAPCUT][CHECK PRO] Pro check timeout; assuming not Pro: {user}")
                return False

        # Backup method: try subscription/settings pages if profile menu failed
        if not menu_opened:
            print(f"[CAPCUT][CHECK PRO] Profile unavailable at {page.url} | {user}")
            print(f"[CAPCUT][CHECK PRO] Profile menu failed, trying subscription pages for {user}")
            result = _check_pro_via_subscription_page(page, user, stop_event)
            if result is not None:
                return result

        raise RuntimeError(f"CapCut profile menu could not be opened for {user}")
