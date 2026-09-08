"""Check Teams memberships and leave extra spaces through CapCut's own client."""

import re
import time

from playwright.sync_api import sync_playwright

from modules.actions.capcut_add_link import (
    CAPCUT_MY_CLOUD_URL,
    _dismiss_post_submit_popups,
    _login_with_email,
)
from modules.actions.capcut_workflow import (
    _cdp_url,
    _connect_browser,
    _ensure_running,
    _wait,
    extract_capcut_username,
)
from modules.browser.chromium import open_workflow_page


class SpaceCheckError(RuntimeError):
    pass


# Use the authenticated site's client so its request signing, region and cookies
# match the UI. Resolve by API signatures instead of relying on bundle module IDs.
_CLIENT_CALL = r"""async ({method, data}) => {
    if (location.protocol !== 'https:' || location.hostname !== 'www.capcut.com')
        throw Error('Not on CapCut');
    const allowed = ['getUserWorkSpace', 'getWorkspaceMember', 'quitWorkspace'];
    if (!allowed.includes(method)) throw Error('Unsupported space operation');
    if (!window.__regcapcutSpaceClient) {
        let require;
        const chunks = window.__LOADABLE_LOADED_CHUNKS__;
        if (!chunks) throw Error('CapCut client not loaded');
        chunks.push([['regcapcut-space-' + Date.now()], {}, r => { require = r; }]);
        if (!require || !require.m) throw Error('CapCut module loader unavailable');
        for (const [id, factory] of Object.entries(require.m)) {
            const source = Function.prototype.toString.call(factory);
            if (!source.includes('/get_user_workspaces') || !source.includes('/quit_user')) continue;
            const exports = require(id);
            for (const client of Object.values(exports)) {
                if (client && typeof client.getUserWorkSpace === 'function' &&
                    typeof client.getWorkspaceMember === 'function' &&
                    typeof client.quitWorkspace === 'function') {
                    window.__regcapcutSpaceClient = client;
                    break;
                }
            }
            if (window.__regcapcutSpaceClient) break;
        }
    }
    const client = window.__regcapcutSpaceClient;
    if (!client) throw Error('CapCut space client changed or is unavailable');
    let timer;
    try {
        return await Promise.race([
            client[method](data),
            new Promise((_, reject) => { timer = setTimeout(() => reject(Error('Space request timed out')), 20000); })
        ]);
    } finally { clearTimeout(timer); }
}"""


def _call(page, method, data, user, stop_event):
    _ensure_running(stop_event, user)
    try:
        result = page.evaluate(_CLIENT_CALL, {"method": method, "data": data})
    except Exception as exc:
        raise SpaceCheckError(f"{method}: {exc}") from exc
    if not isinstance(result, dict) or str(result.get("ret")) != "0":
        raise SpaceCheckError(f"CapCut did not confirm {method}")
    return result


def _all_pages(page, method, field, user, stop_event, **params):
    cursor = "0"
    seen = set()
    rows = []
    for _ in range(100):
        if cursor in seen:
            raise SpaceCheckError("CapCut repeated the pagination cursor")
        seen.add(cursor)
        result = _call(page, method, {**params, "cursor": cursor, "count": 100}, user, stop_event)
        data = result.get("data")
        if not isinstance(data, dict) or not isinstance(data.get(field), list):
            raise SpaceCheckError(f"CapCut did not return {field}")
        rows.extend(data[field])
        if data.get("has_more") is False:
            return rows
        if data.get("has_more") is not True or not data.get("next_cursor"):
            raise SpaceCheckError("CapCut did not confirm a complete list")
        cursor = str(data["next_cursor"])
    raise SpaceCheckError("Space list exceeded pagination limit")


def _teams_only(rows):
    """Teams subscriptions: active or expired, never infer from a space's name."""
    teams = {}
    for row in rows:
        if not isinstance(row, dict):
            raise SpaceCheckError("Invalid workspace record")
        wid = row.get("workspace_id")
        if not isinstance(wid, str) or not wid.strip():
            raise SpaceCheckError("Workspace ID missing")
        status = row.get("team_vip_status")
        end = row.get("team_vip_end")
        # CapCut's subscription UI uses status 1 for active Teams, and status 0
        # plus a positive end timestamp for expired Teams (public web client).
        if type(status) is not int or status not in (0, 1):
            raise SpaceCheckError(f"Teams status is unknown for workspace {wid}")
        if status == 0 and (type(end) not in (int, float) or end < 0):
            raise SpaceCheckError(f"Teams expiry is unknown for workspace {wid}")
        if status == 1 or end > 0:
            if row.get("role") not in ("owner", "admin", "collaborator"):
                raise SpaceCheckError(f"Membership role is unknown for workspace {wid}")
            if wid in teams and teams[wid] != row:
                raise SpaceCheckError(f"Conflicting workspace records: {wid}")
            teams[wid] = dict(row)
    return list(teams.values())


def _read_teams(page, user, stop_event):
    return _teams_only(_all_pages(page, "getUserWorkSpace", "workspace_infos", user,
                                 stop_event, need_convert_workspace=True))


def _verified_owner_username(owner):
    # The current application exports CapCut's userNNN handle. Do not substitute
    # an arbitrary display name or numeric UID for a username.
    for key in ("username", "unique_id", "nickname"):
        value = str(owner.get(key) or "").strip()
        if re.fullmatch(r"user\d{6,}", value, re.I):
            return value
    raise SpaceCheckError("Owner username could not be verified")


def _owner_username(page, space, user, stop_event):
    members = _all_pages(page, "getWorkspaceMember", "member_list", user, stop_event,
                         workspace_id=space["workspace_id"])
    owners = {str(m.get("uid")): m for m in members
              if isinstance(m, dict) and m.get("role") == "owner" and m.get("uid")}
    if len(owners) != 1:
        raise SpaceCheckError("Could not identify exactly one space owner")
    owner_id, owner = next(iter(owners.items()))
    if str(space.get("owner")) != owner_id:
        raise SpaceCheckError("Space owner changed or could not be matched")
    return _verified_owner_username(owner)


def _read_verified_members(page, space, user, stop_event):
    """Return unique, valid members after verifying the workspace owner."""
    members = _all_pages(page, "getWorkspaceMember", "member_list", user, stop_event,
                         workspace_id=space["workspace_id"])
    unique = {}
    for member in members:
        if not isinstance(member, dict):
            raise SpaceCheckError("Invalid workspace member record")
        uid = member.get("uid")
        if not isinstance(uid, (str, int)) or isinstance(uid, bool) or not str(uid).strip():
            raise SpaceCheckError("Workspace member ID missing")
        key = str(uid)
        if key in unique and unique[key] != member:
            raise SpaceCheckError(f"Conflicting workspace member records: {key}")
        unique[key] = dict(member)
    owners = [member for member in unique.values() if member.get("role") == "owner"]
    if len(owners) != 1 or str(owners[0].get("uid")) != str(space.get("owner")):
        raise SpaceCheckError("Space owner changed or could not be matched")
    return list(unique.values()), _verified_owner_username(owners[0])


def check_owned_fam_members(page, account, stop_event, record):
    """Count current members in owned Teams spaces, with owner totals explicit."""
    user = account["user"]
    spaces = _read_teams(page, user, stop_event)
    owned = [space for space in spaces if space["role"] == "owner"]
    results = []
    for space in owned:
        members, owner_username = _read_verified_members(page, space, user, stop_event)
        item = {
            "workspace_id": space["workspace_id"],
            "space_name": space.get("name", ""),
            "owner_username": owner_username,
            "member_count": sum(member.get("role") != "owner" for member in members),
            "account_count": len(members),
        }
        record("fam_checked", item)
        results.append(item)
    owner_usernames = {item["owner_username"].casefold() for item in results}
    if len(owner_usernames) > 1:
        raise SpaceCheckError("Owned spaces returned different owner usernames")
    return {
        "owned_spaces": len(results),
        "owner_username": results[0]["owner_username"] if results else "",
        "total_members": sum(item["member_count"] for item in results),
        "total_accounts": sum(item["account_count"] for item in results),
        "spaces": results,
        "status": "ok" if results else "no owned teams",
    }


def _leave_spaces(page, account, spaces, targets, stop_event, record, operation):
    user = account["user"]
    initial = len(spaces)
    member_username = str(account.get("userID") or "").strip()
    if targets and not re.fullmatch(r"user\d{6,}", member_username, re.I):
        raise SpaceCheckError("Current account username could not be verified; no space was left")
    plan = []
    for target in targets:
        item = {"workspace_id": target["workspace_id"], "space_name": target.get("name", ""),
                "member_username": member_username,
                "owner_id": str(target["owner"]),
                "owner_username": _owner_username(page, target, user, stop_event),
                "member_role": target["role"]}
        # Export the relationship even when this account owns the space and
        # therefore cannot leave it through quitWorkspace.
        record("owner_found", item)
        plan.append(item)
    if any(target["member_role"] == "owner" for target in plan):
        raise SpaceCheckError("Account owns a Teams space; owner exported but cannot leave as a member")
    left = []
    expected = {s["workspace_id"] for s in spaces}
    for target in plan:
        fresh = _read_teams(page, user, stop_event)
        if {s["workspace_id"] for s in fresh} != expected:
            raise SpaceCheckError(f"Membership changed during {operation}; run it again")
        current = next(s for s in fresh if s["workspace_id"] == target["workspace_id"])
        if current["role"] == "owner" or str(current.get("owner")) != target["owner_id"]:
            raise SpaceCheckError("Space ownership changed before leaving")
        _ensure_running(stop_event, user)
        record("leave_pending", target)  # Durable owner export BEFORE leaving.
        error = None
        try:
            _call(page, "quitWorkspace", {"workspace_id": target["workspace_id"]}, user, stop_event)
        except SpaceCheckError as exc:
            error = exc  # A timeout may follow a successful leave. Never retry it.
        expected.remove(target["workspace_id"])
        confirmed = False
        for attempt in range(3):
            _wait(1.0, stop_event, user)
            remaining = _read_teams(page, user, stop_event)
            if {s["workspace_id"] for s in remaining} == expected:
                confirmed = True
                break
        if not confirmed:
            record("leave_unverified", target)
            raise SpaceCheckError(f"Leave not verified; no repeat leave sent. {error or ''}")
        left.append(target)
        record("left_verified", target)
    return {"before": initial, "after": len(expected), "left": left, "status": "ok"}


def check_and_reduce_spaces(page, account, stop_event, record):
    user = account["user"]
    spaces = _read_teams(page, user, stop_event)
    initial = len(spaces)
    if initial <= 1:
        return {"before": initial, "after": initial, "left": [],
                "status": "ok" if initial == 1 else "no teams"}
    owned = [s for s in spaces if s["role"] == "owner"]
    if len(owned) > 1:
        raise SpaceCheckError("Account owns multiple Teams spaces; cannot leave as a member")
    # Prefer the assigned invitation when it can be matched directly. Otherwise
    # keep a deterministic space; the user permits leaving either Teams space.
    assigned = str(account.get("assigned_link") or "").rstrip("/")
    keep = (owned or [s for s in spaces if assigned and
                     str(s.get("invitation_link") or "").rstrip("/") == assigned]
            or sorted(spaces, key=lambda s: s["workspace_id"]))[0]
    targets = [s for s in spaces if s["workspace_id"] != keep["workspace_id"]]
    return _leave_spaces(page, account, spaces, targets, stop_event, record, "Check Space")


def out_all_spaces(page, account, stop_event, record):
    """Leave every Teams membership after durably exporting each owner."""
    user = account["user"]
    spaces = _read_teams(page, user, stop_event)
    if not spaces:
        return {"before": 0, "after": 0, "left": [], "status": "no teams"}
    return _leave_spaces(page, account, spaces, spaces, stop_event, record, "Out Fam")


def run_capcut_check_space_workflow(account, context, record):
    user = account["user"]
    stop_event = context.get("stop_event")
    with sync_playwright() as playwright:
        browser = _connect_browser(playwright, _cdp_url(context), user, stop_event)
        page = open_workflow_page(browser, context)
        # Check Space is read/maintenance work and can safely wait through a
        # temporary CapCut login throttle instead of failing the account after
        # three rapid submissions.
        _login_with_email(page, account, stop_event, max_attempts=5)
        account["userID"] = extract_capcut_username(
            page, user, stop_event, dismiss_popups=_dismiss_post_submit_popups
        )
        page.goto(CAPCUT_MY_CLOUD_URL, wait_until="domcontentloaded", timeout=45000)
        _wait(2, stop_event, user)
        return check_and_reduce_spaces(page, account, stop_event, record)


def run_capcut_out_fam_workflow(account, context, record):
    user = account["user"]
    stop_event = context.get("stop_event")
    with sync_playwright() as playwright:
        browser = _connect_browser(playwright, _cdp_url(context), user, stop_event)
        page = open_workflow_page(browser, context)
        _login_with_email(page, account, stop_event, max_attempts=5)
        account["userID"] = extract_capcut_username(
            page, user, stop_event, dismiss_popups=_dismiss_post_submit_popups
        )
        page.goto(CAPCUT_MY_CLOUD_URL, wait_until="domcontentloaded", timeout=45000)
        _wait(2, stop_event, user)
        return out_all_spaces(page, account, stop_event, record)


def run_capcut_check_fam_origin_workflow(account, context, record):
    """Log in as an owner and count members in every Teams space it owns."""
    user = account["user"]
    stop_event = context.get("stop_event")
    with sync_playwright() as playwright:
        browser = _connect_browser(playwright, _cdp_url(context), user, stop_event)
        page = open_workflow_page(browser, context)
        _login_with_email(page, account, stop_event, max_attempts=5)
        # Counting does not need the public userNNN handle. Skipping its profile
        # menu/reload loop makes owner checks faster and removes an unrelated
        # failure point; ownership is verified below from workspace/member UIDs.
        page.goto(CAPCUT_MY_CLOUD_URL, wait_until="domcontentloaded", timeout=45000)
        _wait(2, stop_event, user)
        return check_owned_fam_members(page, account, stop_event, record)
