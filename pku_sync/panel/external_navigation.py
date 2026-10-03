"""Open user-selected Notion pages in the system browser.

The student panel is served from localhost inside a Tauri WebView. Notion
sign-in needs the user's normal browser, while the panel must remain at /app.
"""
from __future__ import annotations

import webbrowser
from collections.abc import Callable
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, HTTPException


def is_notion_url(url: str) -> bool:
    if not isinstance(url, str) or len(url) > 4096:
        return False
    if any(ord(char) <= 32 for char in url) or "\\" in url:
        return False
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or not host or not parsed.path:
        return False
    if parsed.username or parsed.password or port is not None:
        return False
    return host in {"notion.so", "notion.site", "app.notion.com"} or host.endswith((".notion.so", ".notion.site"))


def _open_system_browser(url: str) -> bool:
    return webbrowser.open(url, new=2)


def add_external_navigation_routes(
    app: FastAPI, opener: Callable[[str], bool] | None = None
) -> None:
    open_url = opener or _open_system_browser

    @app.post("/api/open-notion")
    def open_notion(payload: dict = Body(...)) -> dict[str, bool]:
        url = payload.get("url")
        if not is_notion_url(url):
            raise HTTPException(status_code=422, detail="Invalid Notion URL")
        try:
            opened = open_url(url)
        except Exception:
            opened = False
        if not opened:
            raise HTTPException(status_code=502, detail="Could not open browser")
        return {"opened": True}
