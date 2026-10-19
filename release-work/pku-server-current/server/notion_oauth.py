"""Server-held Notion OAuth authorization-code exchange."""

from __future__ import annotations

import base64
import os

import httpx


class NotionOAuthError(RuntimeError):
    pass


class NotionOAuth:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret

    @classmethod
    def from_environment(cls) -> "NotionOAuth | None":
        client_id = os.environ.get("NOTION_OAUTH_CLIENT_ID")
        client_secret = os.environ.get("NOTION_OAUTH_CLIENT_SECRET")
        return cls(client_id, client_secret) if client_id and client_secret else None

    def exchange(self, code: str, redirect_uri: str) -> dict:
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        try:
            response = httpx.post(
                "https://api.notion.com/v1/oauth/token",
                headers={"Authorization": f"Basic {basic}"},
                json={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise NotionOAuthError("Notion authorization is temporarily unavailable") from exc
        if response.status_code != 200:
            raise NotionOAuthError("Notion authorization was rejected")
        payload = response.json()
        if not payload.get("access_token"):
            raise NotionOAuthError("Notion returned no access token")
        return payload
