"""Native Notion REST client for the sync chain, without MCP.

The daily chain (registration) and the batch lecture builder talk to Notion
through this module using an internal integration token (``NOTION_TOKEN`` in
.env), so the 06:00 run no longer depends on an agent session with MCP
tools. The MCP connection stays for interactive agent work only.

Surface, mapped from the runbooks:

- pages: retrieve / create / append blocks / replace body (``create_page``,
  ``append_blocks``, ``replace_page_content`` …)
- blocks: list children (the idempotency scans in the lecture runbooks)
- databases: schema fetch / query / create row (the registration step in
  ``daily_review_mcp.md``: query first, then create only missing rows)
- search
- file uploads: the singlepart flow for keyframe images, referenced in
  markdown as ``![caption](file-upload://<id>)`` exactly like the MCP syntax

The runbook red lines are enforced structurally: nothing here can archive a
page or a database row by itself, and ``replace_page_content`` refuses to run
on a page that still has child pages or databases -- overwrite mode is
body-only, original URL and title untouched.
"""

from __future__ import annotations

import mimetypes
import re
import time
import uuid as uuid_lib
from pathlib import Path

import httpx

API_HOST = "https://api.notion.com"
API_BASE = f"{API_HOST}/v1"
API_VERSION = "2022-06-28"

_RETRYABLE = (429, 500, 502, 503, 504)
_ATTEMPTS = 3
_BLOCK_BATCH = 100  # API cap for one children payload
_TEXT_LIMIT = 2000  # max chars in one rich-text text object
_SINGLEPART_LIMIT = 20 * 1024 * 1024

_IMAGE_SCHEME = "file-upload://"

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^[-+*]\s+(.+)$")
_NUMBERED_RE = re.compile(r"^\d+[.)]\s+(.+)$")
_IMAGE_RE = re.compile(r"^!\[(.*?)\]\((.+?)\)$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_HEX32_RE = re.compile(r"([0-9a-fA-F]{32})")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")


class NotionError(RuntimeError):
    """Notion API failure with the API status/code kept for triage."""

    def __init__(self, message: str, *, status: int | None = None, code: str = "", body: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body


class NotionClient:
    """Thin token-authed client for the subset of the REST API we use."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = API_HOST,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self._token = token
        self._base = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self._base,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": API_VERSION,
            },
            timeout=timeout,
            transport=transport,
        )

    # -- plumbing ---------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "NotionClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
        files: dict | None = None,
        headers: dict | None = None,
    ) -> dict:
        """One request with the shared retry policy (429 honors Retry-After)."""
        response: httpx.Response | None = None
        for attempt in range(1, _ATTEMPTS + 1):
            response = self._http.request(
                method, url, json=json_body, params=params, files=files, headers=headers
            )
            if response.status_code not in _RETRYABLE or attempt == _ATTEMPTS:
                break
            time.sleep(_backoff(response, attempt))
        assert response is not None
        if response.status_code >= 400:
            raise _api_error(response)
        return response.json() if response.content else {}

    # -- users ------------------------------------------------------------

    def whoami(self) -> dict:
        """Token check: the integration's own bot user."""
        return self._request("GET", "/v1/users/me")

    # -- pages ------------------------------------------------------------

    def get_page(self, page_id: str) -> dict:
        return self._request("GET", f"/v1/pages/{_norm_id(page_id)}")

    def create_page(
        self, parent_page_id: str, title: str, *, children: list[dict] | None = None
    ) -> dict:
        """Create a subpage. Extra blocks beyond the first 100 are appended."""
        payload: dict = {
            "parent": {"page_id": _norm_id(parent_page_id)},
            "properties": {"title": {"title": _plain_text(title)}},
        }
        if children:
            payload["children"] = children[:_BLOCK_BATCH]
        page = self._request("POST", "/v1/pages", json_body=payload)
        if children and len(children) > _BLOCK_BATCH:
            self.append_blocks(page["id"], children[_BLOCK_BATCH:])
        return page

    def create_database_row(self, database_id: str, properties: dict) -> dict:
        """Create one row in a database (registration step; never overwrite)."""
        return self._request(
            "POST",
            "/v1/pages",
            json_body={
                "parent": {"database_id": _norm_id(database_id)},
                "properties": properties,
            },
        )

    def update_page_properties(self, page_id: str, properties: dict) -> dict:
        return self._request(
            "PATCH", f"/v1/pages/{_norm_id(page_id)}", json_body={"properties": properties}
        )

    # -- blocks -----------------------------------------------------------

    def list_children(self, block_id: str) -> list[dict]:
        """All first-level children of a page/block, following pagination."""
        rows: list[dict] = []
        cursor: str | None = None
        while True:
            params: dict = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            body = self._request(
                "GET", f"/v1/blocks/{_norm_id(block_id)}/children", params=params
            )
            rows.extend(body.get("results") or [])
            if not body.get("has_more"):
                return rows
            cursor = body.get("next_cursor")

    def list_child_pages(self, page_id: str) -> list[dict]:
        """Direct subpages as [{"id", "title"}] -- the idempotency scan input."""
        found = []
        for block in self.list_children(page_id):
            if block.get("type") == "child_page":
                found.append(
                    {
                        "id": block["id"],
                        "title": (block.get("child_page") or {}).get("title", ""),
                    }
                )
        return found

    def append_blocks(self, page_id: str, blocks: list[dict]) -> None:
        """Append content blocks in API-sized batches (never touches existing)."""
        for chunk in _chunks(blocks, _BLOCK_BATCH):
            self._request(
                "PATCH",
                f"/v1/blocks/{_norm_id(page_id)}/children",
                json_body={"children": chunk},
            )

    def replace_page_content(self, page_id: str, blocks: list[dict]) -> None:
        """Overwrite mode: archive current body blocks, then append the new ones.

        Refuses when the page still has child pages or child databases:
        archiving such a block deletes a whole page, which the red lines
        forbid. URL and title are never touched by this operation.
        """
        existing = self.list_children(page_id)
        protected = [
            b for b in existing if b.get("type") in ("child_page", "child_database")
        ]
        if protected:
            names = ", ".join(_block_title(b) for b in protected[:5])
            raise NotionError(
                f"页面仍有 {len(protected)} 个子页面/子库（{names}），拒绝整体替换正文"
            )
        for block in existing:
            self._request("DELETE", f"/v1/blocks/{block['id']}")
        self.append_blocks(page_id, blocks)

    # -- search -----------------------------------------------------------

    def search(self, query: str, *, object_type: str | None = None) -> list[dict]:
        payload: dict = {"query": query, "page_size": 100}
        if object_type:
            payload["filter"] = {"property": "object", "value": object_type}
        rows: list[dict] = []
        cursor: str | None = None
        while True:
            body = dict(payload)
            if cursor:
                body["start_cursor"] = cursor
            data = self._request("POST", "/v1/search", json_body=body)
            rows.extend(data.get("results") or [])
            if not data.get("has_more"):
                return rows
            cursor = data.get("next_cursor")

    # -- databases ----------------------------------------------------------

    def get_database(self, database_id: str) -> dict:
        """Schema + property names (the runbooks fetch this before建行)."""
        return self._request("GET", f"/v1/databases/{_norm_id(database_id)}")

    def query_database(
        self, database_id: str, *, filter: dict | None = None, page_size: int = 100
    ) -> list[dict]:
        """Rows as raw page objects, following pagination."""
        rows: list[dict] = []
        cursor: str | None = None
        while True:
            payload: dict = {"page_size": page_size}
            if filter:
                payload["filter"] = filter
            if cursor:
                payload["start_cursor"] = cursor
            data = self._request(
                "POST", f"/v1/databases/{_norm_id(database_id)}/query", json_body=payload
            )
            rows.extend(data.get("results") or [])
            if not data.get("has_more"):
                return rows
            cursor = data.get("next_cursor")

    # -- file uploads -------------------------------------------------------

    def upload_file(self, path: str | Path) -> str:
        """Upload one small file (keyframes are far below 20 MB) → file-upload://<id>.

        Singlepart flow: register the upload, then POST the bytes to its
        upload_url as multipart/form-data with the file in the ``file`` field.
        The id must be attached to a page promptly; unattached uploads expire
        after about an hour.
        """
        file = Path(path)
        if not file.is_file():
            raise NotionError(f"文件不存在: {file}")
        if file.stat().st_size > _SINGLEPART_LIMIT:
            raise NotionError(
                f"{file.name} 超过单次上传 20 MB 上限（keyframes 不会到这个量级）"
            )
        content_type = _guess_type(file.name)
        created = self._request(
            "POST",
            "/v1/file_uploads",
            json_body={
                "filename": file.name,
                "content_type": content_type,
                "kind": "file",
                "mode": "singlepart_upload",
            },
        )
        upload_url = created.get("upload_url") or (
            f"{self._base}/v1/file_uploads/{created['id']}/send"
        )
        # The send endpoint must keep the multipart content type that httpx
        # generates, so only pass through non-content headers here.
        headers = {
            h["name"]: h["value"]
            for h in created.get("upload_headers") or []
            if h.get("name", "").lower() != "content-type"
        }
        headers.setdefault("Authorization", f"Bearer {self._token}")
        data = file.read_bytes()
        sent = self._request(
            "POST", upload_url, files={"file": (file.name, data, content_type)}, headers=headers
        )
        status = sent.get("status")
        if status != "uploaded":
            raise NotionError(f"上传后状态异常: {status!r}（期望 uploaded）")
        return f"{_IMAGE_SCHEME}{created['id']}"


def get_client(settings=None) -> NotionClient:
    """Client from settings; fails with setup instructions when unset."""
    if settings is None:
        from .config import settings as default_settings

        settings = default_settings
    if not settings.notion_token:
        raise NotionError(
            "NOTION_TOKEN 未设置：请在 notion.so/my-integrations 新建内部集成，"
            "把 Class Notes hub 分享给它，再把 token 写进 .env。"
        )
    return NotionClient(settings.notion_token)


# -- markdown → blocks ------------------------------------------------------


def markdown_to_blocks(markdown: str) -> list[dict]:
    """The Notion-flavored markdown subset used by the writer spec → blocks.

    Supported: ``#``–``###`` headings, ``-``/``+``/``*`` bullets, numbered
    items, ``>`` quotes, ``---`` dividers, fenced code, paragraphs (consecutive
    plain lines joined), standalone images ``![caption](file-upload://<id>)``
    or http(s), and ``**bold**`` inline. Indented (nested) bullets are not a
    thing in the writer format and are flattened.
    """
    blocks: list[dict] = []
    lines = markdown.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("```"):
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1  # closing fence
            blocks.append(
                {"object": "block", "type": "code", "code": {"rich_text": _rt("\n".join(code))}}
            )
            continue
        m = _HEADING_RE.match(stripped)
        if m:
            key = f"heading_{len(m.group(1))}"
            blocks.append({"object": "block", "type": key, key: {"rich_text": _rt(m.group(2))}})
            i += 1
            continue
        m = _IMAGE_RE.match(stripped)
        if m:
            blocks.append(_image_block(m.group(1), m.group(2)))
            i += 1
            continue
        if stripped in ("---", "***", "___"):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            i += 1
            continue
        m = _BULLET_RE.match(stripped)
        if m:
            blocks.append(
                {
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {"rich_text": _rt(m.group(1))},
                }
            )
            i += 1
            continue
        m = _NUMBERED_RE.match(stripped)
        if m:
            blocks.append(
                {
                    "object": "block",
                    "type": "numbered_list_item",
                    "numbered_list_item": {"rich_text": _rt(m.group(1))},
                }
            )
            i += 1
            continue
        if stripped.startswith(">"):
            blocks.append(
                {
                    "object": "block",
                    "type": "quote",
                    "quote": {"rich_text": _rt(stripped.lstrip("> ").strip())},
                }
            )
            i += 1
            continue
        paragraph = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if not nxt or _starts_special(nxt):
                break
            paragraph.append(nxt)
            i += 1
        blocks.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": _rt("\n".join(paragraph))},
            }
        )
    return blocks


def _image_block(caption: str, ref: str) -> dict:
    ref = ref.strip()
    if ref.startswith(_IMAGE_SCHEME):
        image: dict = {
            "type": "file_upload",
            "file_upload": {"id": ref[len(_IMAGE_SCHEME) :]},
        }
    elif ref.startswith(("http://", "https://")):
        image = {"type": "external", "external": {"url": ref}}
    else:
        raise NotionError(
            f"图片引用 {ref!r} 不是 file-upload://… 或 http(s) 链接："
            "本地文件请先用 `pku-sync notion upload-file` 上传再引用"
        )
    if caption:
        image["caption"] = _rt(caption)
    return {"object": "block", "type": "image", "image": image}


def _rt(text: str) -> list[dict]:
    """Text → rich text array: **bold** segments split out, chunks ≤ 2000 chars."""
    out: list[dict] = []
    for part, bold in _bold_segments(text):
        for chunk in _chunks_text(part):
            piece: dict = {"type": "text", "text": {"content": chunk}}
            if bold:
                piece["annotations"] = {"bold": True}
            out.append(piece)
    return out


def _bold_segments(text: str):
    pos = 0
    for m in _BOLD_RE.finditer(text):
        if m.start() > pos:
            yield text[pos : m.start()], False
        yield m.group(1), True
        pos = m.end()
    if pos < len(text):
        yield text[pos:], False


def _chunks_text(text: str) -> list[str]:
    if len(text) <= _TEXT_LIMIT:
        return [text]
    return [text[j : j + _TEXT_LIMIT] for j in range(0, len(text), _TEXT_LIMIT)]


def _starts_special(line: str) -> bool:
    return bool(
        _HEADING_RE.match(line)
        or _BULLET_RE.match(line)
        or _NUMBERED_RE.match(line)
        or _IMAGE_RE.match(line)
        or line.startswith(("```", ">"))
        or line in ("---", "***", "___")
    )


# -- property payload helpers (database rows) -------------------------------


def prop_title(text: str) -> dict:
    return {"title": _plain_text(text)}


def prop_text(text: str) -> dict:
    return {"rich_text": _plain_text(text)}


def prop_select(name: str) -> dict:
    return {"select": {"name": name}}


def prop_multi_select(*names: str) -> dict:
    return {"multi_select": [{"name": n} for n in names]}


def prop_number(value: float | int) -> dict:
    return {"number": value}


def prop_checkbox(value: bool) -> dict:
    return {"checkbox": bool(value)}


def prop_date(day: str, *, end: str | None = None) -> dict:
    d: dict = {"start": day}
    if end:
        d["end"] = end
    return {"date": d}


def prop_url(url: str) -> dict:
    return {"url": url}


def page_url(page: dict) -> str:
    """Canonical URL for a page response (used in the hub/学习中心 index lines)."""
    if page.get("url"):
        return page["url"]
    page_id = page.get("id", "")
    return f"https://www.notion.so/{page_id.replace('-', '')}"


def page_title(page: dict) -> str:
    """First title property's plain text (database rows and plain pages)."""
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop.get("title") or [])
    return ""


# -- internals ---------------------------------------------------------------


def _plain_text(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": text}}]


def _chunks(items: list, size: int) -> list[list]:
    return [items[j : j + size] for j in range(0, len(items), size)]


def _block_title(block: dict) -> str:
    inner = block.get(block.get("type", "")) or {}
    return inner.get("title") or inner.get("caption") or block["id"]


def _backoff(response: httpx.Response, attempt: int) -> float:
    header = response.headers.get("Retry-After")
    if header is not None:
        try:
            return min(float(header), 30.0)
        except ValueError:
            pass
    return 2.0 * attempt


def _api_error(response: httpx.Response) -> NotionError:
    try:
        body = response.json()
        code = str(body.get("code", ""))
        message = str(body.get("message", ""))
    except ValueError:
        code, message = "", (response.text or "")[:200]
    label = f"Notion API {response.status_code}"
    if code:
        label += f" {code}"
    return NotionError(
        f"{label}: {message}" if message else label,
        status=response.status_code,
        code=code,
        body=(response.text or "")[:500],
    )


def _guess_type(filename: str) -> str:
    guessed = mimetypes.guess_type(filename)[0]
    return guessed or "application/octet-stream"


def _norm_id(value: str) -> str:
    """Accept a 32-hex id, dashed UUID, notion.so URL or collection:// URL."""
    raw = value.strip()
    if raw.startswith("collection://"):
        raw = raw[len("collection://") :]
    m = _HEX32_RE.search(raw)
    if m:
        hex32 = m.group(1)
    elif _UUID_RE.match(raw):
        hex32 = raw.replace("-", "")
    else:
        raise NotionError(f"不是合法的 Notion 页面/数据库 id 或 URL: {value!r}")
    return str(uuid_lib.UUID(hex32))
