"""Notion preview stays in the desktop panel and uses only safe browser URLs."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from pku_sync.panel.connection import build_fake_connection_service
from pku_sync.panel.external_navigation import is_notion_url
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.platform_bridge import FakePlatformBridge
from pku_sync.panel.webapi import create_app


class _NoopRunner:
    def submit(self, kind):
        return None

    def current(self):
        return None

    def last(self):
        return None


def make_client(opener):
    directory = build_fake_directory()
    return TestClient(create_app(
        settings=SimpleNamespace(data_dir="data", platform_token=""),
        runner=_NoopRunner(),
        directory_service=directory,
        connection_service=build_fake_connection_service(directory_service=directory),
        platform_service=FakePlatformBridge(activated=True),
        notion_opener=opener,
    ))


def test_notions_only_are_opened_in_system_browser():
    opened = []
    client = make_client(lambda url: opened.append(url) or True)
    url = "https://app.notion.com/abc123?pvs=4"
    response = client.post("/api/open-notion", json={"url": url})
    assert response.status_code == 200
    assert response.json() == {"opened": True}
    assert opened == [url]


def test_external_or_unsafe_urls_cannot_open_a_browser():
    opened = []
    client = make_client(lambda url: opened.append(url) or True)
    bad_urls = [
        "https://accounts.google.com/signin",
        "https://notion.so.evil.example/a",
        "https://app.notion.com.evil.example/a",
        "https://evil.example@notion.so/a",
        "http://www.notion.so/a",
        "file:///C:/secret",
        "https://www.notion.so:444/a",
        "https://www.notion.so/\\evil",
        "https://www.notion.so/a\n",
    ]
    for url in bad_urls:
        assert client.post("/api/open-notion", json={"url": url}).status_code == 422
        assert not is_notion_url(url)
    assert opened == []


def test_browser_launch_failure_is_reported():
    client = make_client(lambda url: False)
    response = client.post("/api/open-notion", json={"url": "https://www.notion.so/abc"})
    assert response.status_code == 502
    assert response.json()["detail"] == "Could not open browser"
