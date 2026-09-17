"""Unit tests for the native Notion REST client (MockTransport, no network)."""

from __future__ import annotations

import json as json_lib

import httpx
import pytest

from pku_sync import notion
from pku_sync.notion import (
    NotionClient,
    NotionError,
    markdown_to_blocks,
    page_title,
    page_url,
    prop_checkbox,
    prop_date,
    prop_multi_select,
    prop_number,
    prop_select,
    prop_text,
    prop_title,
    prop_url,
)

PAGE_ID = "3d491b6f53e1802e8759d6a5e38ec783"
DASHED = "3d491b6f-53e1-802e-8759-d6a5e38ec783"


def make_client(handler) -> NotionClient:
    return NotionClient("secret_test", transport=httpx.MockTransport(handler))


# -- plumbing -----------------------------------------------------------------


def test_whoami_sends_auth_and_version_headers():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("Authorization")
        seen["version"] = request.headers.get("Notion-Version")
        return httpx.Response(200, json={"object": "user", "name": "pku-course-sync", "type": "bot", "bot": {}})

    with make_client(handler) as client:
        me = client.whoami()
    assert seen == {"path": "/v1/users/me", "auth": "Bearer secret_test", "version": notion.API_VERSION}
    assert me["name"] == "pku-course-sync"


def test_retries_429_honoring_retry_after(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(notion.time, "sleep", sleeps.append)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if len(seen) == 1:
            return httpx.Response(
                429,
                json={"object": "error", "code": "rate_limited", "message": "slow down"},
                headers={"Retry-After": "7"},
            )
        return httpx.Response(200, json={"object": "user", "name": "ok"})

    with make_client(handler) as client:
        assert client.whoami()["name"] == "ok"
    assert sleeps == [7.0]


def test_429_exhausts_into_error(monkeypatch):
    monkeypatch.setattr(notion.time, "sleep", lambda seconds: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={"object": "error", "code": "rate_limited", "message": "busy"})

    with make_client(handler) as client:
        with pytest.raises(NotionError) as err:
            client.whoami()
    assert calls["n"] == 3
    assert err.value.status == 429
    assert "rate_limited" in str(err.value)


def test_5xx_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(notion.time, "sleep", lambda seconds: None)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if len(seen) == 1:
            return httpx.Response(503, json={})
        return httpx.Response(200, json={"ok": True})

    with make_client(handler) as client:
        assert client.get_page(PAGE_ID) == {"ok": True}


def test_client_error_raises_without_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            404,
            json={"object": "error", "status": 404, "code": "object_not_found", "message": "Could not find page"},
        )

    with make_client(handler) as client:
        with pytest.raises(NotionError, match="object_not_found"):
            client.get_page("deadbeef" * 4)
    assert calls["n"] == 1


def test_api_error_with_non_json_body(monkeypatch):
    monkeypatch.setattr(notion.time, "sleep", lambda seconds: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    with make_client(handler) as client:
        with pytest.raises(NotionError, match="502"):
            client.whoami()


def test_backoff_honors_retry_after_with_cap():
    assert notion._backoff(httpx.Response(429, headers={"Retry-After": "600"}), 1) == 30.0
    assert notion._backoff(httpx.Response(429, headers={"Retry-After": "3"}), 1) == 3.0
    assert notion._backoff(httpx.Response(429), 2) == 4.0


@pytest.mark.parametrize(
    "value",
    [
        PAGE_ID,
        DASHED,
        f"https://www.notion.so/Class-Notes-{PAGE_ID}",
        f"collection://{PAGE_ID}",
        f"https://www.notion.so/xxx?v=1#{PAGE_ID}",
    ],
)
def test_norm_id_accepts_common_forms(value):
    assert notion._norm_id(value) == DASHED


def test_norm_id_rejects_garbage():
    with pytest.raises(NotionError, match="id"):
        notion._norm_id("not a uuid")


# -- pages & blocks ------------------------------------------------------------


def test_create_page_payload():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json_lib.loads(request.read())
        return httpx.Response(200, json={"object": "page", "id": DASHED, "url": "https://www.notion.so/b"})

    blocks = markdown_to_blocks("## 一、开头\n- 第一条\n")
    with make_client(handler) as client:
        page = client.create_page(PAGE_ID, "《第一讲 · 测试》", children=blocks)
    assert seen["method"] == "POST" and seen["path"] == "/v1/pages"
    assert seen["body"]["parent"] == {"page_id": DASHED}
    assert seen["body"]["properties"]["title"]["title"][0]["text"]["content"] == "《第一讲 · 测试》"
    assert len(seen["body"]["children"]) == 2
    assert page["url"] == "https://www.notion.so/b"


def test_create_page_appends_children_overflow():
    calls: list[tuple[str, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json_lib.loads(request.read())
        if request.method == "POST" and request.url.path == "/v1/pages":
            calls.append(("create", len(body["children"])))
            return httpx.Response(200, json={"object": "page", "id": "c" * 32, "url": "https://www.notion.so/c"})
        calls.append(("append", len(body["children"])))
        return httpx.Response(200, json={"results": []})

    blocks = markdown_to_blocks("\n".join(f"- 条目{i}" for i in range(250)))
    with make_client(handler) as client:
        client.create_page("a" * 32, "标题", children=blocks)
    assert calls == [("create", 100), ("append", 100), ("append", 50)]


def test_append_blocks_batches():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(len(json_lib.loads(request.read())["children"]))
        return httpx.Response(200, json={"results": []})

    blocks = markdown_to_blocks("\n".join(f"- {i}" for i in range(150)))
    with make_client(handler) as client:
        client.append_blocks("a" * 32, blocks)
    assert calls == [100, 50]


def test_list_children_follows_pagination():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        if len(calls) == 1:
            return httpx.Response(
                200, json={"results": [{"id": "1"}], "has_more": True, "next_cursor": "cur1"}
            )
        return httpx.Response(200, json={"results": [{"id": "2"}], "has_more": False})

    with make_client(handler) as client:
        rows = client.list_children("a" * 32)
    assert [r["id"] for r in rows] == ["1", "2"]
    assert calls[1].get("start_cursor") == "cur1"


def test_list_child_pages_only_returns_subpages():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"id": "p1", "type": "child_page", "child_page": {"title": "《第一讲 · 计算机网络（2026-09-07 第5-6节）》"}},
                    {"id": "b1", "type": "paragraph", "paragraph": {"rich_text": []}},
                    {"id": "p2", "type": "child_page", "child_page": {"title": "其他页"}},
                ],
                "has_more": False,
            },
        )

    with make_client(handler) as client:
        pages = client.list_child_pages("a" * 32)
    assert pages == [
        {"id": "p1", "title": "《第一讲 · 计算机网络（2026-09-07 第5-6节）》"},
        {"id": "p2", "title": "其他页"},
    ]


def test_replace_refuses_pages_with_subpages():
    deletes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            deletes.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "results": [{"id": "sub", "type": "child_page", "child_page": {"title": "重要子页"}}],
                "has_more": False,
            },
        )

    with make_client(handler) as client:
        with pytest.raises(NotionError, match="拒绝整体替换正文"):
            client.replace_page_content("a" * 32, markdown_to_blocks("- 新内容"))
    assert deletes == []


def test_replace_archives_existing_then_appends():
    events: list[tuple] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            events.append(("delete", request.url.path.rsplit("/", 1)[1]))
            return httpx.Response(200, json={"id": "x", "archived": True})
        if request.method == "PATCH":
            events.append(("append", len(json_lib.loads(request.read())["children"])))
            return httpx.Response(200, json={"results": []})
        return httpx.Response(
            200,
            json={
                "results": [
                    {"id": "old1", "type": "paragraph", "paragraph": {}},
                    {"id": "old2", "type": "image", "image": {}},
                ],
                "has_more": False,
            },
        )

    with make_client(handler) as client:
        client.replace_page_content("a" * 32, markdown_to_blocks("## 新标题\n- 新内容"))
    assert events == [("delete", "old1"), ("delete", "old2"), ("append", 2)]


def test_query_database_sends_filter_and_paginates():
    bodies: list[dict] = []
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json_lib.loads(request.read()))
        paths.append(request.url.path)
        if len(bodies) == 1:
            return httpx.Response(200, json={"results": [{"id": "r1"}], "has_more": True, "next_cursor": "c2"})
        return httpx.Response(200, json={"results": [{"id": "r2"}], "has_more": False})

    flt = {"property": "标题", "title": {"contains": "作业1"}}
    with make_client(handler) as client:
        rows = client.query_database("a" * 32, filter=flt)
    assert [r["id"] for r in rows] == ["r1", "r2"]
    assert paths[0] == "/v1/databases/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/query"
    assert bodies[0]["filter"] == flt
    assert bodies[1]["start_cursor"] == "c2"


def test_search_filter_and_pagination():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json_lib.loads(request.read()))
        if len(bodies) == 1:
            return httpx.Response(
                200, json={"results": [{"object": "page", "id": "s1"}], "has_more": True, "next_cursor": "c3"}
            )
        return httpx.Response(200, json={"results": [{"object": "page", "id": "s2"}], "has_more": False})

    with make_client(handler) as client:
        rows = client.search("学习任务", object_type="page")
    assert len(rows) == 2
    assert bodies[0]["query"] == "学习任务"
    assert bodies[0]["filter"] == {"property": "object", "value": "page"}
    assert bodies[1]["start_cursor"] == "c3"


def test_get_database_returns_schema():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "object": "database",
                "id": "a" * 32,
                "title": [{"plain_text": "2026秋季学习任务"}],
                "properties": {"任务类型": {"type": "select", "select": {"options": [{"name": "作业"}]}}},
            },
        )

    with make_client(handler) as client:
        db = client.get_database("a" * 32)
    assert db["title"][0]["plain_text"] == "2026秋季学习任务"
    assert db["properties"]["任务类型"]["type"] == "select"


# -- file uploads ----------------------------------------------------------------


def test_upload_file_singlepart_flow(tmp_path):
    jpg = tmp_path / "frame_12m34s.jpg"
    jpg.write_bytes(b"\xff\xd8fakejpg")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/file_uploads":
            seen["register"] = json_lib.loads(request.read())
            return httpx.Response(
                200,
                json={
                    "object": "file_upload",
                    "id": "up1",
                    "status": "pending",
                    "upload_url": "https://api.notion.com/v1/file_uploads/up1/send",
                },
            )
        seen["send_path"] = request.url.path
        seen["send_headers"] = dict(request.headers)
        seen["send_body"] = request.read()
        return httpx.Response(200, json={"object": "file_upload", "id": "up1", "status": "uploaded"})

    with make_client(handler) as client:
        ref = client.upload_file(jpg)
    assert ref == "file-upload://up1"
    assert seen["register"] == {
        "filename": "frame_12m34s.jpg",
        "content_type": "image/jpeg",
        "kind": "file",
        "mode": "singlepart_upload",
    }
    assert seen["send_path"] == "/v1/file_uploads/up1/send"
    assert seen["send_headers"]["authorization"] == "Bearer secret_test"
    assert "multipart/form-data" in seen["send_headers"]["content-type"]
    assert b"frame_12m34s.jpg" in seen["send_body"]
    assert b"\xff\xd8fakejpg" in seen["send_body"]


def test_upload_file_missing_raises(tmp_path):
    with make_client(lambda request: httpx.Response(200, json={})) as client:
        with pytest.raises(NotionError, match="文件不存在"):
            client.upload_file(tmp_path / "nope.jpg")


def test_upload_file_rejects_oversize(tmp_path, monkeypatch):
    monkeypatch.setattr(notion, "_SINGLEPART_LIMIT", 4)
    big = tmp_path / "big.jpg"
    big.write_bytes(b"12345")
    with make_client(lambda request: httpx.Response(200, json={})) as client:
        with pytest.raises(NotionError, match="20 MB"):
            client.upload_file(big)


def test_upload_file_rejects_bad_final_status(tmp_path):
    frame = tmp_path / "a.jpg"
    frame.write_bytes(b"x")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/file_uploads":
            return httpx.Response(
                200,
                json={
                    "id": "u2",
                    "status": "pending",
                    "upload_url": "https://api.notion.com/v1/file_uploads/u2/send",
                },
            )
        return httpx.Response(200, json={"id": "u2", "status": "pending"})

    with make_client(handler) as client:
        with pytest.raises(NotionError, match="状态异常"):
            client.upload_file(frame)


# -- markdown → blocks ---------------------------------------------------------


def test_markdown_headings_lists_quote_divider():
    blocks = markdown_to_blocks(
        "# 一级\n## 二级\n### 三级\n- 短横线\n+ 加号线\n* 星号线\n1. 第一\n2) 第二\n> 引用\n---\n"
    )
    kinds = [b["type"] for b in blocks]
    assert kinds == [
        "heading_1",
        "heading_2",
        "heading_3",
        "bulleted_list_item",
        "bulleted_list_item",
        "bulleted_list_item",
        "numbered_list_item",
        "numbered_list_item",
        "quote",
        "divider",
    ]
    assert blocks[1]["heading_2"]["rich_text"][0]["text"]["content"] == "二级"
    assert blocks[8]["quote"]["rich_text"][0]["text"]["content"] == "引用"


def test_markdown_paragraph_joins_consecutive_lines():
    blocks = markdown_to_blocks("第一行\n第二行\n\n- 分隔后的条目\n")
    assert [b["type"] for b in blocks] == ["paragraph", "bulleted_list_item"]
    assert blocks[0]["paragraph"]["rich_text"][0]["text"]["content"] == "第一行\n第二行"


def test_markdown_code_fence():
    blocks = markdown_to_blocks("```\n- 不是列表\n缩进保留\n```")
    assert blocks[0]["type"] == "code"
    assert blocks[0]["code"]["rich_text"][0]["text"]["content"] == "- 不是列表\n缩进保留"


def test_markdown_image_file_upload_with_caption():
    blocks = markdown_to_blocks("![关键帧：存储转发示意](file-upload://abc123)")
    image = blocks[0]["image"]
    assert image["type"] == "file_upload"
    assert image["file_upload"] == {"id": "abc123"}
    assert image["caption"][0]["text"]["content"] == "关键帧：存储转发示意"


def test_markdown_image_external_without_caption():
    blocks = markdown_to_blocks("![](https://example.com/a.png)")
    image = blocks[0]["image"]
    assert image["type"] == "external"
    assert image["external"]["url"] == "https://example.com/a.png"
    assert "caption" not in image


def test_markdown_image_local_path_rejected():
    with pytest.raises(NotionError, match="upload-file"):
        markdown_to_blocks("![x](/Users/wudi/Desktop/frame.jpg)")


def test_markdown_bold_segments():
    blocks = markdown_to_blocks("- **重点**：存储转发延迟")
    rt = blocks[0]["bulleted_list_item"]["rich_text"]
    assert rt[0] == {"type": "text", "text": {"content": "重点"}, "annotations": {"bold": True}}
    assert rt[1]["text"]["content"] == "：存储转发延迟"
    assert "annotations" not in rt[1]


def test_rich_text_split_at_2000_chars():
    rt = notion._rt("字" * 4500)
    assert [len(piece["text"]["content"]) for piece in rt] == [2000, 2000, 500]


def test_writer_format_sample_shape():
    md = (
        "第二讲（9/9 周三第 1-2 节）：先以案例讲传输与治理问题，随后进入正题。下节课继续对比两种交换方式。\n"
        "\n"
        "## 一、网络分类与结构\n"
        "\n"
        "- 网络按**传输介质**分有线与无线。\n"
        "- ISP 层级决定转发路径。\n"
        "\n"
        "![关键帧：网络分类树](file-upload://frame01)\n"
        "\n"
        "## 📌 需要自行核实的点\n"
        "\n"
        "- 光纤普及率排名 <待核：工信部年报>\n"
        "\n"
        "## 资料来源\n"
        "\n"
        "- 录像转写：E:\\pku-course-data\\计算机网络\\recordings\\x\\notes.md\n"
    )
    blocks = markdown_to_blocks(md)
    kinds = [b["type"] for b in blocks]
    assert kinds == [
        "paragraph",
        "heading_2",
        "bulleted_list_item",
        "bulleted_list_item",
        "image",
        "heading_2",
        "bulleted_list_item",
        "heading_2",
        "bulleted_list_item",
    ]
    pieces = blocks[2]["bulleted_list_item"]["rich_text"]
    assert [p.get("annotations", {}).get("bold") for p in pieces] == [None, True, None]
    assert blocks[4]["image"]["file_upload"] == {"id": "frame01"}


# -- helpers --------------------------------------------------------------------


def test_property_helpers():
    assert prop_title("作业1") == {"title": [{"type": "text", "text": {"content": "作业1"}}]}
    assert prop_text("备注") == {"rich_text": [{"type": "text", "text": {"content": "备注"}}]}
    assert prop_select("未开始") == {"select": {"name": "未开始"}}
    assert prop_multi_select("a", "b") == {"multi_select": [{"name": "a"}, {"name": "b"}]}
    assert prop_number(3) == {"number": 3}
    assert prop_checkbox(True) == {"checkbox": True}
    assert prop_date("2026-09-15") == {"date": {"start": "2026-09-15"}}
    assert prop_date("2026-09-15", end="2026-09-17") == {
        "date": {"start": "2026-09-15", "end": "2026-09-17"}
    }
    assert prop_url("https://example.com") == {"url": "https://example.com"}


def test_page_title_and_url_helpers():
    page = {
        "id": DASHED,
        "url": "https://www.notion.so/x",
        "properties": {"名称": {"type": "title", "title": [{"plain_text": "作业一"}]}},
    }
    assert page_title(page) == "作业一"
    assert page_url(page) == "https://www.notion.so/x"
    assert page_url({"id": DASHED}) == f"https://www.notion.so/{PAGE_ID}"


def test_get_client_requires_token():
    from pku_sync.config import Settings

    with pytest.raises(NotionError, match="NOTION_TOKEN"):
        notion.get_client(Settings(_env_file=None, notion_token=""))
