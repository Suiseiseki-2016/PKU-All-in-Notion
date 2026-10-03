"""Course-summary snapshots: honest content, idempotent Notion writes."""
from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from pku_sync.models import Recording
from pku_sync.panel import course_summary, notion_home
from pku_sync.pipeline import collect_jobs

NOTE = """# 计算机网络 · 第1讲 绪论

> 本笔记由自动转写整理，术语与老师口头通知可能有误。

## 本讲来源

- 课堂录像自动转写。

## 作业与考试口头线索（自动转写原文，待核对）

- 老师提到期中考试范围到第 5 章。

## 课堂内容

#### 协议分层

**分层的好处**

* **解耦**：每层只关心自己的职责。
* **替换**：实现可以独立替换。
* **顺序**：发送方和接收方顺序不同。

* [12:30] 老师说的「三次握手」细节未听清，需核对原音。
"""


def _campus(root):
    folder = root / "计算机网络"
    (folder / "recordings").mkdir(parents=True)
    (folder / "materials").mkdir()
    (folder / "assignments").mkdir()
    (folder / "course.json").write_text(json.dumps({
        "course_id": "campus-1", "name": "计算机网络", "code": "04830010",
        "term": "26-27学年第1学期", "assignments_status": "available",
        "synced_at": "2026-09-26T11:27:00+00:00",
    }), "utf-8")
    recordings = [
        Recording(course_id="campus-1", title="第1讲 绪论",
                  recorded_at="2026-09-01 08:00:00",
                  play_url="https://private.example/video?token=secret"),
        Recording(course_id="campus-1", title="第2讲 协议",
                  recorded_at="2026-09-08 08:00:00",
                  play_url="https://private.example/video?token=secret"),
    ]
    (folder / "recordings" / "index.json").write_text(
        json.dumps([row.model_dump() for row in recordings]), "utf-8")
    (folder / "materials" / "index.json").write_text("[]", "utf-8")
    (folder / "assignments" / "index.json").write_text(json.dumps([{
        "title": "第一次书面作业", "content_id": "_1714169_1", "source": "content-tree",
        "due_at": "2026-10-01 23:30", "instructions": "答案与代码打包", "files": [],
    }], ensure_ascii=False), "utf-8")
    return folder


def _write_note(root, index, markdown):
    job = collect_jobs(root)[index]
    job.directory.mkdir(parents=True, exist_ok=True)
    (job.directory / "notes.md").write_text(markdown, "utf-8")
    return job


def _publish(job, *, note_url="https://notion.so/note-1",
             lecture_url="https://notion.so/lecture-1", home_id="home"):
    from pku_sync.panel.recordings_api import recording_id

    (job.directory / "notion-publication.json").write_text(json.dumps({
        "recording_id": recording_id(job), "home_id": home_id,
        "lecture_id": "lecture-1", "lecture_url": lecture_url,
        "note_url": note_url, "published_at": "2026-09-26T00:00:00+00:00",
    }, ensure_ascii=False), "utf-8")


class _FakeNotion:
    """Minimal in-memory Notion tree for the summary writer."""

    def __init__(self, children=None, pages=None):
        self.children = children if children is not None else {}
        self.pages = pages if pages is not None else {}
        self.created = []
        self.blocks = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def list_child_pages(self, parent):
        return list(self.children.get(parent, []))

    def create_page(self, parent, title, *, children=None, retry=True):
        assert retry is False
        page_id = f"page-{len(self.pages) + 1}"
        self.created.append((parent, title))
        self.children.setdefault(parent, []).append(
            {"id": page_id, "title": title, "url": "https://notion.so/" + page_id})
        self.pages[page_id] = {"id": page_id, "url": "https://notion.so/" + page_id}
        self.blocks[page_id] = list(children or [])
        return self.pages[page_id]

    def list_children(self, page_id):
        return list(self.blocks.get(page_id, []))


def _home(root):
    notion_home._save_home(root, {"id": "home", "parent_id": "parent",
                                  "url": "https://notion.so/home"})


def _tree():
    return {"home": [{"id": "course-page", "title": "计算机网络",
                      "url": "https://notion.so/course-page"}]}


def test_summary_uses_only_local_material_and_discloses_gaps(tmp_path):
    _campus(tmp_path)
    _write_note(tmp_path, 0, NOTE)
    text = course_summary.summary_markdown(tmp_path, "campus-1", day=date(2026, 9, 30))
    assert "# 计算机网络｜课程总结（截至 2026-09-30）" in text
    assert "不新造知识点" in text
    # Topics and points come from the local note, not from the model's memory.
    assert "### 《2026-09-01 · 第1讲 绪论》" in text
    # A sub-heading that opens a topic keeps its parent's label, and each
    # point's indented sub-bullets stay folded into that point.
    assert ("- 协议分层 › 分层的好处：解耦：每层只关心自己的职责。；"
            "替换：实现可以独立替换。；顺序：发送方和接收方顺序不同。") in text
    # Teacher's spoken clues and open questions stay verbatim with their source.
    assert "- 《2026-09-01 · 第1讲 绪论》 · 老师提到期中考试范围到第 5 章。" in text
    assert "- 《2026-09-01 · 第1讲 绪论》 [12:30] 老师说的「三次握手」细节未听清，需核对原音。" in text
    # Gaps are stated, never filled in with invented content.
    assert "已有笔记 1 节；待整理 1 节" in text
    assert "缺口：以下讲次尚无笔记" in text
    assert "《2026-09-08 · 第2讲 协议》" in text
    assert "正式作业 · 第一次书面作业 · 截止 2026-10-01 23:30" in text
    assert "token=secret" not in text


def test_summary_without_any_note_says_so_instead_of_summarizing(tmp_path):
    _campus(tmp_path)
    text = course_summary.summary_markdown(tmp_path, "campus-1")
    assert "本课程还没有已发布笔记，无法汇总主题要点" in text
    assert "已发布笔记中没有登记待确认条目" in text


def test_outline_folds_sub_bullets_and_never_invents_a_topic():
    markdown = (
        "## 课堂内容\n\n"
        "#### 主题一\n\n"
        "**要点组**\n\n"
        "* **父要点**：说明。\n"
        "    * 子要点一。\n"
        "    * 子要点二。\n"
        "* 无子项要点。\n"
    )
    outline = course_summary.note_outline(markdown)
    assert outline["topics"] == [
        ("主题一 › 要点组", [("父要点：说明。", ["子要点一。", "子要点二。"]),
                            ("无子项要点。", [])]),
    ]
    assert course_summary._point_text("父要点：说明。", ["子要点一。"]) == "父要点：说明。；子要点一。"
    assert course_summary._point_text("早产存活临界点：", ["22 周后：可存活。"]) == "早产存活临界点：22 周后：可存活。"
    assert course_summary._point_text("父要点", ["子要点一。"]) == "父要点；子要点一。"


def test_summary_page_links_lectures_and_reuses_the_same_day(tmp_path):
    _campus(tmp_path)
    job = _write_note(tmp_path, 0, NOTE)
    _publish(job)
    _home(tmp_path)
    client = _FakeNotion(_tree())
    first = course_summary.publish_course_summary(client, tmp_path, "campus-1",
                                                  day=date(2026, 9, 30))
    assert first["created"] is True and first["url"] == "https://notion.so/page-1"
    assert client.created == [("course-page", "计算机网络｜课程总结（截至 2026-09-30）")]
    linked = [block for block in client.blocks["page-1"]
              if block["type"] == "paragraph"
              and (block["paragraph"]["rich_text"][0]["text"].get("link") or {})]
    assert any(block["paragraph"]["rich_text"][0]["text"]["link"]["url"] == "https://notion.so/note-1"
               for block in linked)
    assert any("打开讲次页" in block["paragraph"]["rich_text"][0]["text"]["content"]
               for block in linked)

    again = course_summary.publish_course_summary(client, tmp_path, "campus-1",
                                                  day=date(2026, 9, 30))
    assert again["created"] is False and again["url"] == first["url"]
    assert len(client.created) == 1  # no duplicate page for the same day

    next_day = course_summary.publish_course_summary(
        client, tmp_path, "campus-1", day=date(2026, 9, 30) + timedelta(days=1))
    assert next_day["created"] is True
    assert [title for _, title in client.created] == [
        "计算机网络｜课程总结（截至 2026-09-30）",
        "计算机网络｜课程总结（截至 2026-10-01）",
    ]
    # The earlier snapshot keeps its own content.
    assert client.blocks["page-1"]
    assert json.loads((tmp_path / "计算机网络" / "summary.json").read_text("utf-8"))[
        "url"] == next_day["url"]


def test_summary_refuses_to_guess_when_the_course_page_is_missing(tmp_path):
    _campus(tmp_path)
    _home(tmp_path)
    client = _FakeNotion({"home": []})
    with pytest.raises(ValueError, match="还没有 Notion 课程页"):
        course_summary.publish_course_summary(client, tmp_path, "campus-1")
    assert client.created == []


def test_summary_batch_keeps_going_when_one_course_fails(tmp_path, monkeypatch):
    _campus(tmp_path)
    _home(tmp_path)

    def fake_publish(client, data_dir, course_id, *, day=None):
        if course_id == "broken":
            raise ValueError("Notion 拒绝写入")
        return {"course_id": course_id, "title": "t", "created": True,
                "url": "https://notion.so/ok"}

    monkeypatch.setattr(course_summary, "publish_course_summary", fake_publish)
    monkeypatch.setattr(course_summary, "campus_course",
                        lambda data_dir, course_id: {"title": course_id})
    monkeypatch.setattr(course_summary, "NotionClient", lambda token: _FakeNotion())
    result = course_summary.publish_course_summaries(
        tmp_path, "token", ["campus-1", "broken"], on_progress=lambda *args: None)
    assert result["created"] == 1 and result["urls"] == ["https://notion.so/ok"]
    assert result["failures"] == [("broken", "Notion 拒绝写入")]


def _settings(root, *, notion_token="token"):
    return SimpleNamespace(data_dir=str(root), notion_token=notion_token,
                           platform_token="platform", pku_username="u", pku_password="p")


def _manager(tmp_path, monkeypatch, *, notion_token="token"):
    from fastapi.testclient import TestClient
    from pku_sync.panel.fake_directory import build_fake_directory
    from pku_sync.panel.recordings_api import RecordingWorkManager, add_recording_routes
    from fastapi import FastAPI

    client_tree = _FakeNotion(_tree())
    monkeypatch.setattr(course_summary, "NotionClient", lambda token: client_tree)
    manager = RecordingWorkManager(_settings(tmp_path, notion_token=notion_token),
                                   build_fake_directory())
    app = FastAPI()
    add_recording_routes(app, manager)
    return TestClient(app), manager, client_tree


def _wait(manager, tries=200):
    import time
    for _ in range(tries):
        state = manager.state()
        if state.get("state") != "running":
            return state
        time.sleep(0.01)
    raise AssertionError("task did not finish")


def test_summary_api_creates_the_page_and_reports_progress(tmp_path, monkeypatch):
    _campus(tmp_path)
    _write_note(tmp_path, 0, NOTE)
    _home(tmp_path)
    client, manager, tree = _manager(tmp_path, monkeypatch)
    started = client.post("/api/campus/courses/campus-1/summary")
    assert started.status_code == 200
    assert started.json()["kind"] == "summary"
    done = _wait(manager)
    assert done["state"] == "done"
    assert done["result_url"] == "https://notion.so/page-1"
    assert done["result_urls"] == ["https://notion.so/page-1"]
    assert done["summary_created"] == 1 and done["summary_existing"] == 0
    assert done["stage_total"] == 2 and done["stage_index"] == 2
    assert done["progress_unit"] == "courses"
    assert done["progress_done"] == 1 and done["progress_remaining"] == 0
    assert done["progress_percent"] == 100
    assert done["current_item"] == "计算机网络"
    assert "课程总结已生成" in done["stage"]
    assert tree.created == [("course-page", "计算机网络｜课程总结（截至 "
                             + course_summary.beijing_today().isoformat() + "）")]
    # The course page can link straight to the newest snapshot.
    marker = json.loads((tmp_path / "计算机网络" / "summary.json").read_text("utf-8"))
    assert marker["url"] == done["result_url"]


def test_summary_api_needs_notion_and_a_course(tmp_path, monkeypatch):
    _campus(tmp_path)
    _home(tmp_path)
    client, _manager_instance, _tree = _manager(tmp_path, monkeypatch, notion_token="")
    assert client.post("/api/campus/courses/campus-1/summary").status_code == 409
    empty = tmp_path / "empty"
    empty.mkdir()
    notion_home._save_home(empty, {"id": "home", "parent_id": "parent",
                                   "url": "https://notion.so/home"})
    client2, _m2, _t2 = _manager(empty, monkeypatch)
    assert client2.post("/api/campus/notion/summaries").status_code == 409
    assert client2.post("/api/campus/courses/missing/summary").status_code == 409
