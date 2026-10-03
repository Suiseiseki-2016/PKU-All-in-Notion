"""A course handbook can contain coursework absent from the formal task tree."""
from __future__ import annotations

import json

from pku_sync.panel import handbook_requirements, notion_home
from pku_sync.panel.campus_catalog import campus_course, campus_courses


def _course(tmp_path):
    folder = tmp_path / "发展心理学"
    (folder / "materials" / "教学大纲").mkdir(parents=True)
    (folder / "assignments").mkdir()
    (folder / "course.json").write_text(json.dumps({
        "course_id": "_101545_1", "name": "发展心理学", "synced_at": "2026-09-28T10:00:00+08:00",
        "assignments_status": "complete",
    }), "utf-8")
    (folder / "assignments" / "index.json").write_text("[]", "utf-8")
    (folder / "materials" / "index.json").write_text(json.dumps([{
        "title": "课程手册及时间安排", "kind": "项目", "path": "教学大纲/课程手册及时间安排",
        "files": ["课程学生手册.pdf"],
    }]), "utf-8")
    (folder / "materials" / "教学大纲" / "课程学生手册.pdf").write_bytes(b"synthetic")
    return folder


def test_zero_formal_assignments_still_surfaces_handbook_pages(tmp_path, monkeypatch):
    _course(tmp_path)
    monkeypatch.setattr(handbook_requirements, "_pdf_pages", lambda path: [
        (1, "目录\n二、课程考核方式............................ 2\n期中成绩...... 3\n期末成绩....... 4"),
        (6, "二、课程考核方式。平时成绩包括出勤。小组作业需报告。" * 10 +
         "\n课堂出勤（3%）：包括集体授课和小班研讨的出勤。\n"
         "小测（4%×3）：集体授课前针对上一阶段进行小测。\n"
         "课堂展示：各组需围绕所选文献进行研读。"),
        (7, "所有组最晚于 10 月 31 日 24 点前，将所选文献发至本班助教邮箱。\n" * 8),
        (8, "文字报告电子版、科普视频成片请于 12 月 28 日 24 点前发至小班助教邮箱。期末成绩另计。\n" * 8 +
         "考试形式：闭卷考试"),
    ])
    overview = campus_courses(tmp_path)["courses"][0]
    course = campus_course(tmp_path, "_101545_1")
    assert overview["assignments"] == 0
    assert overview["handbook_reviews"] == 1
    assert course["assignments"] == []
    card = course["handbook_reviews"][0]
    assert [page["page"] for page in card["pages"]] == [6, 7, 8]
    preview = card["time_preview"]
    assert any(row["page"] == 7 and "10 月 31 日" in row["text"] for row in preview)
    assert any(row["page"] == 8 and "12 月 28 日" in row["text"] for row in preview)
    assert not any(row["text"].strip() == "提交内容及时间：" for row in preview)
    assert "12 月 28 日" in card["pages"][2]["excerpt"]
    assert [(row["label"], row["page"]) for row in card["presence_preview"]] == [
        ("出勤", 6), ("小测", 6), ("课堂展示", 6), ("考试", 8)
    ]
    assert all("核对" in row["note"] or "最新通知" in row["note"]
               for row in card["presence_preview"])
    assert "due_at" not in card and "content_id" not in card
    assert card["material_index"] == 0 and card["file_index"] == 0
    assert notion_home._source_items(course, "作业") == [card]
    blocks = notion_home._item_blocks(course["id"], "作业", card, "2026-09-28")
    text = str(blocks)
    assert "PDF 第 6 页" in text and "PDF 第 7 页" in text
    assert "手册原文中的时间／提交线索" in text
    assert "笔记无法代替的参与事项" in text
    assert "不是教学网正式作业" in text
    assert "尚未" not in notion_home._summary_line("作业", card)
    assert "打开教学网提交目标" not in text
    assert "当前教学网索引未提供这份 PDF 的安全直达链接" in text


def test_handbook_only_links_verified_blackboard_pdf_path(tmp_path, monkeypatch):
    folder = _course(tmp_path)
    index = folder / "materials" / "index.json"
    rows = json.loads(index.read_text("utf-8"))
    rows[0]["attachments"] = [
        {"filename": "课程学生手册.pdf", "path": "/bbcswebdav/pid-1/manual.pdf"},
        {"filename": "课程学生手册.pdf", "path": "/bbcswebdav/pid-1/manual.pdf?secret=x"},
    ]
    index.write_text(json.dumps(rows), "utf-8")
    monkeypatch.setattr(handbook_requirements, "_pdf_pages", lambda path: [])
    card = campus_course(tmp_path, "_101545_1")["handbook_reviews"][0]
    assert "secret=" not in json.dumps(card)
    blocks = notion_home._item_blocks("_101545_1", "作业", card, "")
    links = [part["text"]["link"]["url"] for block in blocks
             for part in (block.get("paragraph") or {}).get("rich_text", [])
             if part.get("text", {}).get("link")]
    assert "https://course.pku.edu.cn/bbcswebdav/pid-1/manual.pdf" in links
    assert not any("secret=" in link for link in links)


def test_missing_local_handbook_is_explicitly_unread(tmp_path):
    folder = _course(tmp_path)
    (folder / "materials" / "教学大纲" / "课程学生手册.pdf").unlink()
    card = campus_course(tmp_path, "_101545_1")["handbook_reviews"][0]
    assert card["pages"] == []
    assert "尚未取得" in card["status"]


def test_corrupt_downloaded_handbook_keeps_course_accessible(tmp_path):
    _course(tmp_path)
    card = campus_course(tmp_path, "_101545_1")["handbook_reviews"][0]
    assert card["pages"] == []
    assert "人工检查整份手册" in card["status"]


def test_handbook_cache_rejects_non_pdf_and_unsafe_paths(tmp_path):
    root = tmp_path / "materials"
    item = {"title": "课程手册", "path": "教学大纲/课程手册及时间安排"}
    assert not handbook_requirements.cache_handbook_pdf(root, item, "学生手册.pdf", b"<html>login</html>")
    assert not handbook_requirements.cache_handbook_pdf(root, item, "../学生手册.pdf", b"%PDF-test")
    assert not handbook_requirements.cache_handbook_pdf(
        root, {**item, "path": "../escape"}, "学生手册.pdf", b"%PDF-test")
    assert not root.exists()


def test_english_schedule_dates_keep_original_line_and_page():
    preview = handbook_requirements._time_preview([(1, """
6th. Oct 13 小测 1 + chap3 婴儿期
Oct 16 Literature Presentation 1
Nov 27 Experiment Presentation 2
17th Dec 29 期末考试
""")])
    assert preview
    assert all(row["page"] == 1 for row in preview)
    assert any(row["text"] == "Oct 16 Literature Presentation 1" for row in preview)
    assert any(row["text"] == "17th Dec 29 期末考试" for row in preview)
    assert not any("2026-" in row["text"] for row in preview)


def test_notion_dashboard_points_to_handbook_review_without_due_date(tmp_path, monkeypatch):
    _course(tmp_path)
    monkeypatch.setattr(handbook_requirements, "_pdf_pages", lambda path: [
        (6, "课程考核方式。小组作业与课堂展示需要本人参与。期末考试另行安排。" * 10 +
         "\n课堂出勤（3%）：包括集体授课和小班研讨的出勤。"),
    ])
    course = campus_course(tmp_path, "_101545_1")
    card = course["handbook_reviews"][0]
    key = notion_home._source_key(course["id"], "作业", card)
    blocks = notion_home._dashboard_blocks(
        tmp_path, {course["id"]: "发展心理学"}, {key: {"id": "ab" * 16}}
    )
    links = [part["text"]["link"]["url"] for block in blocks
             for part in (block.get("paragraph") or {}).get("rich_text", [])
             if part.get("text", {}).get("link")]
    assert "https://www.notion.so/" + "ab" * 16 in links
    rendered = str(blocks)
    assert "课程手册待核对" in rendered
    assert "需核对本人参与的事项" in rendered
    assert "出勤" in rendered and "课堂展示" in rendered and "考试" in rendered
    assert "PDF 第 6 页有考核线索" in rendered
    assert "正式作业和公告索引中未发现未来 14 天" in rendered


def test_notion_dashboard_shows_handbook_date_as_unverified_source_cue(tmp_path, monkeypatch):
    _course(tmp_path)
    monkeypatch.setattr(handbook_requirements, "_pdf_pages", lambda path: [
        (6, "课程考核方式。小组作业与课堂展示需要本人参与。" * 12 +
         "\n6th. Oct 13 小测 1 + chap3 婴儿期\n"
         "所有组最晚于 10 月 31 日 24 点前，将所选文献发至本班助教邮箱。"),
    ])
    course = campus_course(tmp_path, "_101545_1")
    card = course["handbook_reviews"][0]
    assert "due_at" not in card
    key = notion_home._source_key(course["id"], "作业", card)
    blocks = notion_home._dashboard_blocks(
        tmp_path, {course["id"]: "发展心理学"}, {key: {"id": "ab" * 16}}
    )
    rendered = str(blocks)
    assert "手册中的日期线索（节选，待核对）" in rendered
    assert "Oct 13 小测 1" in rendered
    assert "10 月 31 日 24 点前" in rendered
    assert "PDF 第 6 页" in rendered
    assert "不代表正式作业截止时间" in rendered
    assert "正式作业和公告索引中未发现未来 14 天" in rendered
    linked = [block for block in blocks if block.get("type") == "paragraph"
              and "Oct 13 小测" in str(block)]
    assert linked[0]["paragraph"]["rich_text"][0]["text"]["link"]["url"] == (
        "https://www.notion.so/" + "ab" * 16)
