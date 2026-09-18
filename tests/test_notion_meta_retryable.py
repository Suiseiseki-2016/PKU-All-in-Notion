"""Notion 429/usage-cap failures → distinct retryable error state (VAL-META-010).

The client's existing retry policy (429/5xx with Retry-After, 3 attempts —
pinned by the pre-existing ``test_notion.py`` cases) runs FIRST: the adapter
only ever sees the exhausted ``NotionError`` and wraps it in a distinct
retryable error state. A failed read never renders as an empty directory,
and the user-facing copy never echoes the provider body or any token.
"""

from __future__ import annotations

import httpx
import pytest

import notion_meta_fake as fake

from pku_sync import notion
from pku_sync.notion import NotionClient, NotionError
from pku_sync.notion_meta import NotionDirectory
from pku_sync.notion_meta.errors import (
    HubAmbiguityError,
    NotionMetaError,
    NotionRetryableError,
)


class FailingNotionClient:
    """Delegates to the fake workspace but raises for the configured method.

    The raised error is the SAME ``NotionError`` the real client raises AFTER
    its retry policy is exhausted — that is the only error shape that can
    reach the adapter.
    """

    def __init__(self, workspace, *, fail: str, error: NotionError, block_id: str | None = None):
        self._inner = workspace.client()
        self._fail = fail
        self._error = error
        self._block_id = block_id  # restrict list_children failures to one target
        self.calls: list[tuple] = []

    def search(self, query: str, *, object_type: str | None = None) -> list[dict]:
        self.calls.append(("search", query, object_type))
        if self._fail == "search":
            raise self._error
        return self._inner.search(query, object_type=object_type)

    def list_children(self, block_id: str) -> list[dict]:
        self.calls.append(("list_children", block_id))
        if self._fail == "list_children" and (
            self._block_id is None or block_id == self._block_id
        ):
            raise self._error
        return self._inner.list_children(block_id)

    def get_page(self, page_id: str) -> dict:
        self.calls.append(("get_page", page_id))
        if self._fail == "get_page":
            raise self._error
        return self._inner.get_page(page_id)

    def get_database(self, database_id: str) -> dict:
        self.calls.append(("get_database", database_id))
        if self._fail == "get_database":
            raise self._error
        return self._inner.get_database(database_id)

    def query_database(self, database_id: str, *, filter: dict | None = None, page_size: int = 100) -> list[dict]:
        self.calls.append(("query_database", database_id))
        if self._fail == "query_database":
            raise self._error
        return self._inner.query_database(database_id, filter=filter, page_size=page_size)


def exhausted_429() -> NotionError:
    """The post-retry 429 the real client raises after 3 attempts."""
    return NotionError(
        "Notion API 429 rate_limited: slow down",
        status=429,
        code="rate_limited",
        body='{"object":"error","code":"rate_limited","message":"slow down"}',
    )


# -- distinct retryable error state ------------------------------------------


def test_exhausted_429_surfaces_as_distinct_retryable_error():
    ws = fake.build_verified_workspace()
    client = FailingNotionClient(ws, fail="query_database", error=exhausted_429())
    directory = NotionDirectory(client, semester=fake.SEMESTER)
    with pytest.raises(NotionRetryableError) as err:
        directory.load()
    # a DISTINCT retryable error state, not a generic failure
    assert isinstance(err.value, NotionMetaError)
    assert err.value.retryable is True
    assert err.value.status == 429
    # user-safe copy: no provider body, no token, retry guidance present
    message = str(err.value)
    assert "slow down" not in message
    assert fake.FAKE_TOKEN not in message
    assert "重试" in message


def test_usage_cap_error_is_retryable():
    """Workspace usage caps are real (hit 2026-09-17) and retryable."""
    ws = fake.build_verified_workspace()
    usage_cap = NotionError(
        "Notion API 429 rate_limited: workspace usage limit reached",
        status=429,
        code="rate_limited",
    )
    client = FailingNotionClient(ws, fail="query_database", error=usage_cap)
    directory = NotionDirectory(client, semester=fake.SEMESTER)
    with pytest.raises(NotionRetryableError) as err:
        directory.load()
    assert err.value.retryable is True
    assert "usage limit" not in str(err.value)


def test_usage_cap_text_pattern_is_retryable_even_off_429():
    """A usage-cap rejection that does not arrive as 429 still reads as retryable."""
    ws = fake.build_verified_workspace()
    usage_cap = NotionError(
        "Notion API 403 usage_limit_reached: workspace usage limit reached",
        status=403,
        code="usage_limit_reached",
    )
    client = FailingNotionClient(ws, fail="query_database", error=usage_cap)
    directory = NotionDirectory(client, semester=fake.SEMESTER)
    with pytest.raises(NotionRetryableError):
        directory.load()


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_errors_are_retryable(status):
    ws = fake.build_verified_workspace()
    error = NotionError(f"Notion API {status} internal_server_error: bork", status=status)
    client = FailingNotionClient(ws, fail="get_database", error=error)
    directory = NotionDirectory(client, semester=fake.SEMESTER)
    with pytest.raises(NotionRetryableError) as err:
        directory.load()
    assert err.value.status == status


def test_non_retryable_client_errors_are_not_wrapped_as_retryable():
    """A 401 is a connection problem, not a retryable-read state."""
    ws = fake.build_verified_workspace()
    error = NotionError("Notion API 401 unauthorized: API token invalid", status=401)
    client = FailingNotionClient(ws, fail="query_database", error=error)
    directory = NotionDirectory(client, semester=fake.SEMESTER)
    with pytest.raises(NotionError) as err:
        directory.load()
    assert not isinstance(err.value, NotionRetryableError)


def test_adapter_errors_stay_unwrapped_and_not_retryable():
    """The adapter's own errors (hub ambiguity) pass through with retryable=False."""
    ws = fake.build_verified_workspace()
    ws.search_results.append(fake.search_page(fake.AMBIG_HUB, "Class Notes 2026 下半学期（备份）"))
    client = ws.client()
    directory = NotionDirectory(client, semester=fake.SEMESTER)
    with pytest.raises(HubAmbiguityError) as err:
        directory.load()
    assert err.value.retryable is False
    assert not isinstance(err.value, NotionRetryableError)


# -- retry policy ordering + no adapter-level retry ---------------------------


def test_adapter_adds_no_retry_retry_policy_lives_in_the_client():
    """The adapter calls the failing read exactly ONCE: retries belong to the
    client (which already ran before the error surfaced)."""
    ws = fake.build_verified_workspace()
    client = FailingNotionClient(ws, fail="query_database", error=exhausted_429())
    directory = NotionDirectory(client, semester=fake.SEMESTER)
    with pytest.raises(NotionRetryableError):
        directory.load()
    query_calls = [call for call in client.calls if call[0] == "query_database"]
    assert query_calls == [("query_database", fake.DB_INDEX)]


def test_retry_policy_runs_first_real_client_chain():
    """End-to-end with the REAL client: the transport serves 429 on every
    search call; the client's 3-attempt retry policy runs to exhaustion BEFORE
    the adapter surfaces its distinct retryable state."""
    attempts = {"search": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["search"] += 1
        return httpx.Response(
            429,
            json={"object": "error", "code": "rate_limited", "message": "slow down"},
            headers={"Retry-After": "0"},
        )

    client = NotionClient("secret_test_x", transport=httpx.MockTransport(handler))
    directory = NotionDirectory(client, semester=fake.SEMESTER)
    with pytest.raises(NotionRetryableError):
        directory.load()
    # 3 transport attempts = the existing retry policy ran first, then the
    # adapter wrapped the exhausted error
    assert attempts["search"] == 3


def test_transient_429s_absorbed_by_client_retry_never_reach_adapter(monkeypatch):
    """Ordering proof: transient 429s answered by the client's retry policy
    let the directory load succeed — the adapter never sees an error at all."""
    monkeypatch.setattr(notion.time, "sleep", lambda seconds: None)
    ws = fake.build_verified_workspace()
    search_attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/search":
            search_attempts["n"] += 1
            if search_attempts["n"] <= 2:
                return httpx.Response(
                    429,
                    json={"object": "error", "code": "rate_limited", "message": "slow down"},
                )
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "results": [ws.search_results[0]],  # the hub page only
                    "has_more": False,
                },
            )
        if path == f"/v1/blocks/{fake.HUB}/children":
            # minimal hub children: just the 课程资料索引 database
            db_block = ws.children[fake.HUB][6]
            return httpx.Response(200, json={"object": "list", "results": [db_block], "has_more": False})
        if path == f"/v1/databases/{fake.DB_INDEX}":
            return httpx.Response(200, json=ws.databases[fake.DB_INDEX])
        if path == f"/v1/databases/{fake.DB_INDEX}/query":
            return httpx.Response(200, json={"object": "list", "results": [], "has_more": False})
        raise AssertionError(f"unexpected request path: {path}")

    client = NotionClient("secret_test_x", transport=httpx.MockTransport(handler))
    directory = NotionDirectory(client, semester=fake.SEMESTER)
    data = directory.load()  # succeeds: both transient 429s were absorbed upstream
    assert data.hub.id == fake.HUB
    assert search_attempts["n"] == 3


# -- never an empty-success render -------------------------------------------


def test_failed_read_never_renders_as_an_empty_directory():
    """A failure at a LATE stage (course children, after hub/schema/rows were
    already read) raises; no partial or empty DirectoryData is ever returned."""
    ws = fake.build_verified_workspace()
    error = NotionError("Notion API 429 rate_limited: slow down", status=429, code="rate_limited")
    client = FailingNotionClient(
        ws, fail="list_children", error=error, block_id=fake.COURSE_NET
    )
    directory = NotionDirectory(client, semester=fake.SEMESTER)
    with pytest.raises(NotionRetryableError):
        directory.load()
    # the failure happened mid-read (hub, schema and rows had already been
    # fetched) and still surfaced as an error, never as a partial success
    kinds = [call[0] for call in client.calls]
    assert "query_database" in kinds  # rows were read...
    assert ("list_children", fake.COURSE_NET) in client.calls  # ...then a course read failed
