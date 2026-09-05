"""Google Sheets append helpers for Reg Grok outputs."""

from __future__ import annotations

import os
import sys
import json
from pathlib import Path
from urllib import error, parse, request

from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request


SPREADSHEET_ID = "1cC4co9IGG1nzrsCtWsXf2g6oedQODrCaTxx-5s2FjAM"
SHEET_NAME = "Sheet1"
SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def runtime_root() -> Path:
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        return Path(bundle_dir).resolve()
    return project_root()


def find_credentials_path() -> Path:
    candidates = []
    env_path = os.getenv("REGGROK_GOOGLE_CREDENTIALS", "").strip()
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend(
        (
            app_dir() / "cred.json",
            Path.cwd() / "cred.json",
            runtime_root() / "cred.json",
            project_root() / "cred.json",
        )
    )
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError("Google credentials file not found: cred.json")


def append_success_rows(rows: list[list[str]], credentials_path: Path | None = None) -> int:
    if not rows:
        return 0
    credentials = Credentials.from_service_account_file(
        str(credentials_path or find_credentials_path()),
        scopes=SCOPES,
    )
    credentials.refresh(Request())
    body = {"values": rows}
    range_name = parse.quote(f"{SHEET_NAME}!A:B", safe="")
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/"
        f"{range_name}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
    )
    http_request = request.Request(
        url=url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(http_request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(body).get("error", {}).get("message", body)
        except Exception:
            message = body
        raise RuntimeError(f"Google Sheets append failed: HTTP {exc.code}: {message}") from exc
    return int(result.get("updates", {}).get("updatedRows", 0))
