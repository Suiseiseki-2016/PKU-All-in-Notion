from __future__ import annotations

from pku_sync.panel.campus_todos import announcement_tasks, announcement_source_url
from pku_sync.panel.notion_home import _item_blocks
from pku_sync.panel.recordings_api import _safe_attachment_path


def test_announcement_tasks_keep_two_teacher_deadlines_without_showcase():
    notices = [{
        "id": "_105648_1", "title": "期中作业要求公告",
        "posted_at": "2026-09-24T10:00:00+08:00",
        "body_text": (
            "请各组组长于9月30日24:00前将本组成员名单提交至课程公邮。\n"
            "请于【12月5日24:00】前由各组组长统一提交期中作业至课程公邮。\n"
            "12月10日和17日进行课堂展示。"
        ),
    }]
    tasks = announcement_tasks("_102156_1", notices)
    assert len(tasks) == 2
    assert [row["due_at_label"] for row in tasks] == [
        "9月30日24:00（老师公告原文）", "12月5日24:00（老师公告原文）"
    ]
    assert tasks[0]["due_at"].startswith("2026-10-01T00:00:00+08:00")
    assert all(row["source"] == "announcement" for row in tasks)
    assert all(row["source_url"].endswith("#_105648_1") for row in tasks)
    assert announcement_source_url("_102156_1", "bad#fragment").endswith("mode=view")


def test_attachment_paths_discard_signed_or_external_urls():
    assert _safe_attachment_path("/bbcswebdav/pid-1/file.pdf") == "/bbcswebdav/pid-1/file.pdf"
    for url in (
        "/bbcswebdav/pid-1/file.pdf?token=secret",
        "https://evil.example/bbcswebdav/file.pdf",
        "/bbcswebdav/../private/file.pdf",
        "/bbcswebdav\\file.pdf",
    ):
        assert _safe_attachment_path(url) == ""


def test_notion_cards_link_specific_sources_and_lead_with_action():
    notice = {"id": "_105648_1", "title": "作业布置情况", "body_text": "请核对要求"}
    blocks = _item_blocks("_103987_1", "通知", notice, "2026-09-26 10:00 北京时间")
    assert blocks[0]["paragraph"]["rich_text"][0]["text"]["content"].startswith("老师通知")
    assert blocks[-2]["paragraph"]["rich_text"][0]["text"]["link"]["url"].endswith("#_105648_1")

    material = {
        "title": "课程介绍", "path": "教学内容/课程介绍",
        "parent_content_id": "_123_1", "files": ["slides.pdf"],
        "attachments": [{"filename": "slides.pdf", "path": "/bbcswebdav/pid-1/file.pdf"}],
    }
    blocks = _item_blocks("_103987_1", "资料", material, "2026-09-26 10:00 北京时间")
    links = [part["text"]["link"]["url"] for block in blocks
             for part in (block.get("paragraph") or {}).get("rich_text", [])
             if part.get("text", {}).get("link")]
    assert "https://course.pku.edu.cn/bbcswebdav/pid-1/file.pdf" in links
    assert any("content_id=_123_1" in url for url in links)
    assert "北京时间" in str(blocks[-1])

    task = announcement_tasks("_102156_1", [{
        "id": "_1_1", "title": "期中作业", "posted_at": "2026-09-24",
        "body_text": "请于9月30日前提交小组名单。"
    }])[0]
    blocks = _item_blocks("_102156_1", "作业", task, "")
    assert "公告待办" in blocks[0]["paragraph"]["rich_text"][0]["text"]["content"]
    assert "不是独立的教学网提交入口" in str(blocks)

