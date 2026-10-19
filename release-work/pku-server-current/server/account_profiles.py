"""Versioned, non-secret configuration shared by one product account."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from urllib.parse import urlsplit

MAX_PROFILE_BYTES = 16 * 1024
_PAGE_ID = re.compile(r"^[0-9a-fA-F-]{32,36}$")
_FORBIDDEN_KEYS = ("token", "password", "secret", "transcript", "video", "content")
SCHEMA = """
CREATE TABLE IF NOT EXISTS account_profiles (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id),
    revision INTEGER NOT NULL,
    profile_json TEXT NOT NULL,
    updated_at REAL NOT NULL
)
"""


class ProfileError(ValueError):
    pass


class ProfileConflict(ProfileError):
    def __init__(self, current: dict):
        super().__init__("profile revision conflict")
        self.current = current


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)


def _page_id(value: object, field: str, *, oauth_template: bool = False) -> str:
    if oauth_template and value == "oauth-template":
        return value
    if not isinstance(value, str) or not _PAGE_ID.fullmatch(value):
        raise ProfileError(f"invalid {field}")
    return value.lower()


def _notion_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 500:
        raise ProfileError("invalid notion home URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname not in {"notion.so", "www.notion.so", "app.notion.com"}:
        raise ProfileError("invalid notion home URL")
    return value


def validate(profile: object) -> dict:
    if not isinstance(profile, dict):
        raise ProfileError("profile must be an object")
    encoded = json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_PROFILE_BYTES:
        raise ProfileError("profile is too large")
    for key in profile:
        if not isinstance(key, str) or any(word in key.casefold() for word in _FORBIDDEN_KEYS):
            raise ProfileError("profile contains a forbidden field")
    allowed = {"notion_home", "notion_workspace_v2", "preferences"}
    if set(profile) - allowed:
        raise ProfileError("profile contains an unsupported field")

    result: dict = {}
    home = profile.get("notion_home")
    if home is not None:
        if not isinstance(home, dict) or set(home) - {"id", "url", "parent_id", "origin"}:
            raise ProfileError("invalid notion home")
        normalized_home = {
            "id": _page_id(home.get("id"), "notion home ID"),
            "url": _notion_url(home.get("url")),
            "parent_id": _page_id(home.get("parent_id"), "notion parent ID", oauth_template=True),
        }
        origin = home.get("origin")
        if origin is not None:
            if origin != "oauth-template":
                raise ProfileError("invalid notion home origin")
            normalized_home["origin"] = origin
        result["notion_home"] = normalized_home

    workspace = profile.get("notion_workspace_v2")
    if workspace is not None:
        if not isinstance(workspace, dict) or set(workspace) != {"id", "url", "parent_id"}:
            raise ProfileError("invalid version-two workspace")
        identifier = _page_id(workspace["id"], "workspace ID")
        url = _notion_url(workspace["url"])
        parsed = urlsplit(url)
        if parsed.query or parsed.fragment or identifier.replace("-", "") not in parsed.path.lower().replace("-", ""):
            raise ProfileError("workspace URL does not match its identity")
        result["notion_workspace_v2"] = {"id": identifier, "url": url,
                                          "parent_id": _page_id(workspace["parent_id"], "workspace parent ID")}

    preferences = profile.get("preferences")
    if preferences is not None:
        if not isinstance(preferences, dict) or set(preferences) - {"prefer_remote_ingest", "direct_oss"}:
            raise ProfileError("invalid preferences")
        if any(not isinstance(value, bool) for value in preferences.values()):
            raise ProfileError("preferences must be booleans")
        result["preferences"] = dict(preferences)
    return result


def get(conn: sqlite3.Connection, account_id: int) -> dict:
    row = conn.execute(
        "SELECT revision, profile_json, updated_at FROM account_profiles WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    if row is None:
        return {"revision": 0, "profile": {}, "updated_at": None}
    return {
        "revision": int(row["revision"]),
        "profile": json.loads(row["profile_json"]),
        "updated_at": float(row["updated_at"]),
    }


def put(conn: sqlite3.Connection, account_id: int, expected_revision: int, profile: object,
        *, replace_workspace_id: str | None = None) -> dict:
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
        raise ProfileError("invalid profile revision")
    clean = validate(profile)
    now = time.time()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        current = get(conn, account_id)
        if current["revision"] != expected_revision:
            raise ProfileConflict(current)
        existing_workspace = current["profile"].get("notion_workspace_v2")
        if replace_workspace_id is not None:
            requested = _page_id(replace_workspace_id, 'previous workspace ID')
            if (not existing_workspace or requested != existing_workspace['id']
                    or 'notion_workspace_v2' not in clean):
                raise ProfileConflict(current)
        if existing_workspace:
            if "notion_workspace_v2" not in clean:
                clean["notion_workspace_v2"] = existing_workspace
            elif (clean["notion_workspace_v2"]["id"] != existing_workspace["id"]
                  and replace_workspace_id is None):
                raise ProfileError("the canonical workspace identity cannot be replaced")
        encoded = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        revision = expected_revision + 1
        conn.execute(
            "INSERT INTO account_profiles(account_id, revision, profile_json, updated_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(account_id) DO UPDATE SET"
            " revision=excluded.revision, profile_json=excluded.profile_json,"
            " updated_at=excluded.updated_at",
            (account_id, revision, encoded, now),
        )
    return {"revision": revision, "profile": clean, "updated_at": now}
