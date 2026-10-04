"""Campus-first catalog and explicit Notion home behavior."""
from __future__ import annotations

import json
from itertools import count
from types import SimpleNamespace

import pytest

from pku_sync.models import Recording
from pku_sync.pipeline import collect_jobs
from pku_sync.panel.campus_catalog import (campus_course, campus_courses,
                                           teacher_deadline_quote)
from pku_sync.panel import notion_home


def test_teacher_written_lab_deadline_is_a_clue_not_a_system_timestamp():
    instruction = ("Lab 1 代码和 Session 打包提交。作业 DDL：2026/10/25（周日）23:59，"
                   "逾期提交按规则扣分。")
    assert teacher_deadline_quote(instruction) == "2026/10/25（周日）23:59"
    assert teacher_deadline_quote("开课日期：2026/10/25 23:59") == ""
    assert teacher_deadline_quote("作业 DDL：2026/02/30 23:59") == ""
    assert teacher_deadline_quote(
        "作业 DDL：2026/10/25 23:59；后改为截止：2026/10/26 23:59"
    ) == ""


def test_course_keeps_teacher_deadline_separate_from_missing_system_due(tmp_path):
    folder = _campus(tmp_path)
    (folder / "assignments").mkdir()
    (folder / "assignments" / "index.json").write_text(json.dumps([{
        "title": "Lab1发布", "source": "content-tree", "due_at": "",
        "instructions": "作业 DDL：2026/10/25（周日）23:59，逾期扣分",
    }], ensure_ascii=False), "utf-8")
    row = campus_course(tmp_path, "campus-1")["assignments"][0]
    assert row["due_at_label"] == ""
    assert row["teacher_deadline_quote"] == "2026/10/25（周日）23:59"


def test_notion_lecture_prefers_existing_note_and_retires_only_empty_duplicate():
    base = "2026-09-17 · 第7-8节"
    note_title = "笔记待验收 · " + base
    empty_title = "待整理 · " + base
    children = {note_title: {"id": "note", "title": note_title},
                empty_title: {"id": "empty", "title": empty_title}}

    class Client:
        def __init__(self):
            self.archived = []
            self.empty_text = notion_home.LECTURE_PLACEHOLDER

        def list_child_pages(self, page_id):
            return [{"title": "转写笔记 · 2026-09-17"}] if page_id == "note" else []

        def list_children(self, page_id):
            if page_id != "empty":
                return []
            return [{"type": "paragraph", "paragraph": {"rich_text": [
                {"plain_text": self.empty_text}]}}]

        def archive_page(self, page_id, *, retry):
            assert retry is False
            self.archived.append(page_id)

    client = Client()
    page, made = notion_home._ensure_status_lecture(client, "course", base, children)
    assert page["id"] == "note" and not made
    assert children[base]["id"] == "note"
    assert client.archived == ["empty"]
    assert empty_title not in children

    children[empty_title] = {"id": "empty", "title": empty_title}
    client.empty_text = "学生自己写的补充"
    notion_home._ensure_status_lecture(client, "course", base, children)
    assert client.archived == ["empty"]
    assert empty_title in children


def test_move_existing_notion_home_preserves_id_and_contents(tmp_path, monkeypatch):
    notion_home._save_home(tmp_path, {"id": "home", "parent_id": "oauth-template",
                                      "url": "https://notion.so/home", "origin": "oauth-template"})
    monkeypatch.setattr(notion_home, "available_parents", lambda token: [
        {"id": "new-parent", "title": "新位置"}])
    calls = []

    class Client:
        def __init__(self, token):
            assert token == "token"
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get_page(self, page_id):
            assert page_id == "home"
            return {"id": "home", "url": "https://notion.so/home",
                    "parent": {"page_id": "old-parent"}}
        def move_page(self, page_id, parent_id):
            calls.append((page_id, parent_id))
            return {"id": "home"}

    monkeypatch.setattr(notion_home, "NotionClient", Client)
    result = notion_home.move_home(tmp_path, "token", "new-parent")
    assert calls == [("home", "new-parent")]
    assert result == {"id": "home", "parent_id": "new-parent",
                      "url": "https://notion.so/home", "origin": "oauth-template"}
    assert notion_home.saved_home(tmp_path) == result


def test_failed_home_move_does_not_change_saved_parent(tmp_path, monkeypatch):
    original = {"id": "home", "parent_id": "old-parent", "url": "https://notion.so/home"}
    notion_home._save_home(tmp_path, original)
    monkeypatch.setattr(notion_home, "available_parents", lambda token: [
        {"id": "new-parent", "title": "新位置"}])

    class Client:
        def __init__(self, token):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get_page(self, page_id):
            return {"id": "home", "parent": {"page_id": "old-parent"}}
        def move_page(self, page_id, parent_id):
            raise RuntimeError("Notion rejected move")

    monkeypatch.setattr(notion_home, "NotionClient", Client)
    with pytest.raises(RuntimeError, match="Notion rejected"):
        notion_home.move_home(tmp_path, "token", "new-parent")
    assert notion_home.saved_home(tmp_path) == original


def _campus(root):
    folder = root / "计算机网络"
    (folder / "recordings").mkdir(parents=True)
    (folder / "materials").mkdir()
    (folder / "course.json").write_text(json.dumps({"course_id": "campus-1", "name": "计算机网络"}), "utf-8")
    recordings = [Recording(course_id="campus-1", title="第1讲", recorded_at="2026-09-01 08:00:00",
                            play_url="https://private.example/video?token=secret")]
    (folder / "recordings" / "index.json").write_text(json.dumps([row.model_dump() for row in recordings]), "utf-8")
    (folder / "materials" / "index.json").write_text(json.dumps([
        {"title": "第1讲课件", "kind": "文件", "path": "教学内容/第1讲", "files": ["lecture.pdf"],
         "private_url": "https://private.example/slide?token=secret"},
        {"title": "补充阅读", "kind": "文件", "path": "资料", "files": []},
    ]), "utf-8")
    return folder


def test_campus_catalog_prioritizes_recordings_and_never_returns_media_secrets(tmp_path):
    _campus(tmp_path)
    courses = campus_courses(tmp_path)
    assert courses["courses"][0]["recordings"] == 1
    detail = campus_course(tmp_path, "campus-1")
    assert detail["source_priority"] == ["recordings", "materials"]
    assert detail["lessons"][0]["materials"][0]["title"] == "第1讲课件"
    assert detail["unassigned_materials"][0]["match_state"] == "needs_review"
    assert "secret" not in json.dumps(detail)


def test_campus_catalog_marks_complete_local_results_ready_to_publish(tmp_path):
    _campus(tmp_path)
    job = collect_jobs(tmp_path, "campus-1")[0]
    job.directory.mkdir(parents=True)
    (job.directory / "transcript.json").write_text('{"segments": []}', "utf-8")
    (job.directory / "notes.md").write_text("# 已有笔记", "utf-8")
    (job.directory / "keyframes").mkdir()
    (job.directory / "keyframes" / "index.json").write_text('{"keyframes": []}', "utf-8")

    lesson = campus_course(tmp_path, "campus-1")["lessons"][0]
    assert lesson["publish_ready"] is True
    assert lesson["video_available"] is False


def test_notion_dashboard_exposes_source_age_after_the_page_becomes_stale(tmp_path):
    folder = _campus(tmp_path)
    metadata = json.loads((folder / "course.json").read_text("utf-8"))
    metadata["synced_at"] = "2026-09-26T11:27:00+00:00"
    (folder / "course.json").write_text(json.dumps(metadata), "utf-8")
    blocks = notion_home._dashboard_blocks(tmp_path, {"campus-1": "计算机网络"}, {})
    rendered = json.dumps(blocks, ensure_ascii=False)
    assert "2026-09-26 19:27 北京时间" in rendered
    assert "同步快照，不会实时刷新" in rendered
    assert "超过 24 小时" in rendered
    assert "起 14 天内的已知截止事项" in rendered
    assert notion_home.DASHBOARD_MARKER in json.dumps(blocks[0], ensure_ascii=False)


def test_formal_assignment_uses_only_matching_safe_teacher_attachment(tmp_path):
    folder = _campus(tmp_path)
    (folder / "assignments").mkdir()
    (folder / "assignments" / "index.json").write_text(json.dumps([
        {"title": "第一次书面作业", "content_id": "_1714169_1",
         "source": "content-tree", "files": ["书面作业1-2026.pdf"]},
        {"title": "同名作业", "content_id": "_999_1",
         "source": "content-tree", "files": ["另一份.pdf"]},
    ]), "utf-8")
    (folder / "materials" / "index.json").write_text(json.dumps([
        {"title": "第一次书面作业", "content_id": "_1714169_1", "kind": "作业",
         "path": "课程作业/第一次书面作业", "files": ["书面作业1-2026.pdf"],
         "attachments": [
             {"filename": "书面作业1-2026.pdf", "path": "/bbcswebdav/pid-1/xid-1"},
             {"filename": "private", "path": "/bbcswebdav/pid-1/xid-1?token=secret"},
         ]},
    ]), "utf-8")

    assignments = campus_course(tmp_path, "campus-1")["assignments"]
    assert assignments[0]["attachments"] == [
        {"filename": "书面作业1-2026.pdf", "path": "/bbcswebdav/pid-1/xid-1"}]
    assert assignments[1]["attachments"] == []
    assert "secret" not in json.dumps(assignments)


def test_notion_home_creates_once_and_lecture_only_when_requested(tmp_path, monkeypatch):
    folder = _campus(tmp_path)
    (folder / "announcements" ).mkdir()
    (folder / "assignments").mkdir()
    (folder / "announcements" / "index.json").write_text(json.dumps([{
        "id": "notice-1", "title": "课堂通知", "posted_at": "2026-09-25",
        "body_text": "下周停课", "files": []
    }]), "utf-8")
    (folder / "assignments" / "index.json").write_text(json.dumps([{
        "content_id": "_1714169_1", "title": "第一次书面作业",
        "due_at": "2026-10-01 23:30", "instructions": "答案与代码打包",
        "files": ["作业题.pdf"], "source": "content-tree"
    }]), "utf-8")
    calls = []
    child_pages = {"parent": [], "home": [], "course": []}
    blocks = {}
    block_numbers = count()
    pages = {"parent": {"id": "parent", "url": "https://notion.so/parent"}}

    class Client:
        def __init__(self, token):
            assert token == "token"
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def search(self, query, *, object_type=None):
            assert query == "" and object_type == "page"
            return [{"id": "parent", "url": "https://notion.so/parent", "properties": {
                "title": {"type": "title", "title": [{"plain_text": "我的页面"}]}}}]
        def list_child_pages(self, parent):
            return child_pages[parent]
        def list_children(self, parent):
            return blocks.get(parent, [])
        def append_blocks(self, page_id, values, **kwargs):
            saved = [{**value, "id": f"block-{next(block_numbers)}"}
                     for value in values]
            current = blocks.setdefault(page_id, [])
            after = kwargs.get("after")
            position = next((index + 1 for index, block in enumerate(current)
                             if block["id"] == after), len(current))
            current[position:position] = saved
            return saved
        def archive_block(self, block_id, **kwargs):
            for rows in blocks.values():
                for index, block in enumerate(rows):
                    if block["id"] == block_id:
                        return rows.pop(index)
            raise AssertionError("block to archive not found")
        def replace_page_content(self, page_id, values):
            blocks[page_id] = []
            self.append_blocks(page_id, values)
        def update_paragraph(self, block_id, text):
            for rows in blocks.values():
                for block in rows:
                    if block["id"] == block_id:
                        block["paragraph"] = {"rich_text": [{
                            "type": "text", "text": {"content": text}, "plain_text": text
                        }]}
                        return block
            raise AssertionError("status block not found")
        def get_page(self, page_id):
            return pages[page_id]
        def update_page_properties(self, page_id, properties):
            title = properties["title"]["title"][0]["text"]["content"]
            for rows in child_pages.values():
                for page in rows:
                    if page["id"] == page_id:
                        page["title"] = title
            return pages[page_id]
        def create_page(self, parent, title, *, children=None, retry=True):
            assert retry is False
            page_id = {"parent": "home", "home": "course"}.get(parent, f"page-{len(pages)}")
            calls.append((parent, title))
            child_pages[parent].append({"id": page_id, "title": title})
            child_pages.setdefault(page_id, [])
            pages[page_id] = {"id": page_id, "url": "https://notion.so/" + page_id}
            blocks[page_id] = [{**value, "id": f"block-{next(block_numbers)}"}
                               for value in children or []]
            return pages[page_id]

    monkeypatch.setattr(notion_home, "NotionClient", Client)
    assert notion_home.available_parents("token")[0]["title"] == "我的页面"
    assert notion_home.create_home(tmp_path, "token", "parent")["id"] == "home"
    assert notion_home.create_home(tmp_path, "token", "parent")["id"] == "home"
    assert len(calls) == 1
    progress = []
    items = []
    first = notion_home.sync_catalog(tmp_path, "token",
                                     progress=lambda *args: progress.append(args),
                                     item_progress=lambda *args: items.append(args))
    second = notion_home.sync_catalog(tmp_path, "token")
    assert first["created_courses"] == 1 and first["created_lectures"] == 1
    assert second["created_courses"] == 0 and second["created_lectures"] == 0
    course_sections = {page["title"]: page["id"] for page in child_pages["course"]}
    assert set(notion_home.SECTION_TITLES).issubset(course_sections)
    assert len(child_pages[course_sections["通知"]]) == 1
    assert len(child_pages[course_sections["作业"]]) == 1
    assert len(child_pages[course_sections["资料"]]) == 2
    lecture_id = next(page["id"] for page in child_pages["course"]
                      if page["title"].startswith("待整理 · 2026-09-01"))
    assert "目录占位" in json.dumps(blocks[lecture_id], ensure_ascii=False)
    assert "待整理" in json.dumps(blocks[course_sections["课堂记录"]], ensure_ascii=False)
    assignment_id = child_pages[course_sections["作业"]][0]["id"]
    assert "2026-10-01 23:30" in json.dumps(blocks[assignment_id], ensure_ascii=False)
    assert "要交什么" in json.dumps(blocks[assignment_id], ensure_ascii=False)
    changed = json.loads((folder / "assignments" / "index.json").read_text("utf-8"))
    changed[0]["due_at"] = "2026-10-02 23:30"
    (folder / "assignments" / "index.json").write_text(json.dumps(changed), "utf-8")
    notion_home.sync_catalog(tmp_path, "token")
    assert len(child_pages[course_sections["作业"]]) == 1
    assert "2026-10-02 23:30" in json.dumps(blocks[assignment_id], ensure_ascii=False)
    assert "2026-10-01 23:30" not in json.dumps(blocks[assignment_id], ensure_ascii=False)
    assert "通知尚未同步" in json.dumps(blocks[course_sections["通知"]], ensure_ascii=False)
    meta = json.loads((folder / "course.json").read_text("utf-8"))
    meta["existing_notion_url"] = "https://app.notion.com/p/" + "f" * 32
    (folder / "course.json").write_text(json.dumps(meta), "utf-8")
    notion_home.sync_catalog(tmp_path, "token")
    linked = next(page for page in child_pages["course"] if page["title"] == "已有课程笔记")
    assert "https://app.notion.com/p/" in json.dumps(blocks[linked["id"]], ensure_ascii=False)
    assert progress == [(1, 1, "courses", 0)]
    # Item progress must advance one planned step at a time and finish the plan,
    # so the panel can show real remaining work instead of a frozen bar.
    assert [done for done, _, _ in items] == list(range(1, len(items) + 1))
    assert items[-1][0] == items[-1][1] == notion_home.catalog_plan_total(
        campus_courses(tmp_path)["courses"])
    assert any(label == "计算机网络 · 建目录页：通知" for _, _, label in items)
    assert any(label.startswith("计算机网络 · 通知：") for _, _, label in items)
    assert items[-1][2] == "汇总近期待办与新通知"
    assert not list(tmp_path.rglob("video.mp4"))
    job = SimpleNamespace(recording=Recording(course_id="campus-1", title="第1讲",
                                              recorded_at="2026-09-01 08:00:00"), course_dir=tmp_path / "计算机网络")
    target = notion_home.ensure_recording_target(tmp_path, "token", job)
    assert target.startswith("page-")
    assert notion_home.ensure_recording_target(tmp_path, "token", job) == target
    assert sum(title.startswith("待整理 · 2026-09-01") for _, title in calls) == 1

def test_oauth_template_becomes_home_without_parent_picker(tmp_path, monkeypatch):
    class Client:
        def __init__(self, token):
            assert token == "token"
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get_page(self, page_id):
            assert page_id == "duplicated-page"
            return {"id": page_id, "url": "https://notion.so/duplicated-page"}
    monkeypatch.setattr(notion_home, "NotionClient", Client)
    home = notion_home.adopt_oauth_template(tmp_path, "token", "duplicated-page")
    assert home["id"] == "duplicated-page"
    assert home["origin"] == "oauth-template"
    assert notion_home.saved_home(tmp_path)["id"] == "duplicated-page"


def test_upgrade_recovers_previously_published_note_without_republishing(tmp_path, monkeypatch):
    from pku_sync.pipeline import collect_jobs
    from pku_sync.panel.recordings_api import recording_id

    _campus(tmp_path)
    job = collect_jobs(tmp_path, "campus-1")[0]
    job.directory.mkdir(parents=True)
    (job.directory / "notes.md").write_text("# 已有笔记", "utf-8")
    (tmp_path / "notion-learning-home.json").write_text(json.dumps({
        "id": "home", "parent_id": "parent", "url": "https://app.notion.com/home"
    }), "utf-8")
    detail = campus_course(tmp_path, "campus-1")
    lecture_title = notion_home._lecture_titles(detail)[recording_id(job)]
    child_pages = {
        "home": [{"id": "course", "title": "计算机网络"}],
        "course": [{"id": "a" * 32, "title": lecture_title}],
        "a" * 32: [],
    }
    blocks = {}
    created = []
    class Client:
        def __init__(self, token): assert token == "token"
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def get_page(self, page_id): return {"id": page_id}
        def update_page_properties(self, page_id, properties):
            title = properties["title"]["title"][0]["text"]["content"]
            for rows in child_pages.values():
                for page in rows:
                    if page["id"] == page_id:
                        page["title"] = title
            return {"id": page_id}
        def list_child_pages(self, page_id): return child_pages[page_id]
        def list_children(self, page_id): return blocks.get(page_id, [])
        def append_blocks(self, page_id, values, **kwargs):
            rows = [{**value, "id": f"block-{len(blocks.get(page_id, [])) + index}"}
                    for index, value in enumerate(values)]
            blocks.setdefault(page_id, []).extend(rows)
            return rows
        def replace_page_content(self, page_id, values):
            blocks[page_id] = []
            self.append_blocks(page_id, values)
        def update_paragraph(self, block_id, text):
            for rows in blocks.values():
                for block in rows:
                    if block["id"] == block_id:
                        block["paragraph"] = {"rich_text": [{"plain_text": text}]}
                        return block
            raise AssertionError("status block not found")
        def create_page(self, parent, title, *, children=None, **kwargs):
            created.append((parent, title))
            page_id = f"section-{len(created)}"
            return self._create(parent, title, page_id, children)
        def _create(self, parent, title, page_id, values):
            child_pages[parent].append({"id": page_id, "title": title})
            child_pages[page_id] = []
            blocks[page_id] = [{**value, "id": f"initial-{index}"}
                               for index, value in enumerate(values or [])]
            return {"id": page_id}
    monkeypatch.setattr(notion_home, "NotionClient", Client)
    assert notion_home.sync_catalog(tmp_path, "token")["recovered_notes"] == 0
    assert not (job.directory / "notion-publication.json").exists()
    next(page for page in child_pages["course"] if page["id"] == "a" * 32)["title"] = (
        "旧版笔记待验收 · " + lecture_title
    )
    child_pages["a" * 32].append({
        "id": "b" * 32,
        "title": f"转写笔记 · {job.recording.date} {job.recording.title}",
    })
    assert notion_home.sync_catalog(tmp_path, "token")["recovered_notes"] == 1
    assert notion_home.sync_catalog(tmp_path, "token")["recovered_notes"] == 0
    assert next(page for page in child_pages["course"] if page["id"] == "a" * 32)["title"] == (
        "笔记待验收 · " + lecture_title
    )
    record_page = next(page for page in child_pages["course"] if page["title"] == "课堂记录")
    record_text = json.dumps(blocks[record_page["id"]], ensure_ascii=False)
    assert "有转写笔记，待质量验收" in record_text
    assert "已整理，有转写笔记" not in record_text
    lesson = campus_course(tmp_path, "campus-1")["lessons"][0]
    assert lesson["notion_note_url"] == "https://www.notion.so/" + "b" * 32
    assert lesson["notion_lecture_url"] == "https://www.notion.so/" + "a" * 32
    assert not any(title == lecture_title for _, title in created)


def test_note_child_detection_includes_revisions_without_calling_them_accepted():
    assert notion_home._has_note_child([{"title": "更新笔记 · 2026-09-17"}])
    assert not notion_home._has_note_child([{"title": "资料 · 课件"}])
    assert notion_home._lecture_status_title("2026-09-17", True) == "笔记待验收 · 2026-09-17"


def test_campus_counts_materials_separately_and_keeps_download_index(tmp_path):
    folder = _campus(tmp_path)
    (folder / "materials" / "index.json").write_text(json.dumps([
        {"title": "讲义", "kind": "文件", "path": "课程/讲义", "files": ["slides.pdf"]},
        {"title": "第一次作业", "kind": "作业", "path": "课程/第一次作业", "files": []},
        {"title": "主讲教师", "kind": "项目", "path": "课程/主讲教师", "files": []},
    ]), "utf-8")
    row = campus_courses(tmp_path)["courses"][0]
    assert (row["materials"], row["assignments"], row["information"]) == (1, 1, 1)
    detail = campus_course(tmp_path, "campus-1")
    assert [item["index"] for item in detail["all_materials"]] == [0, 1, 2]
    assert detail["all_materials"][0]["files"] == ["slides.pdf"]


def test_duration_lookup_reads_only_player_metadata_and_caches_seconds(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from pku_sync.config import Settings
    from pku_sync.panel.connection import build_fake_connection_service
    from pku_sync.panel.fake_directory import build_fake_directory
    from pku_sync.panel.platform_bridge import FakePlatformBridge
    from pku_sync.panel.recordings_api import recording_id
    from pku_sync.panel.webapi import create_app
    from pku_sync.pipeline import collect_jobs
    from pku_sync import auth, recordings

    _campus(tmp_path)
    job = collect_jobs(tmp_path)[0]
    calls = []
    class Client:
        def close(self):
            calls.append("close")
    def session(**credentials):
        assert credentials == {"username": "student", "password": "secret"}
        calls.append("login")
        return Client()
    def resolve(client, recording):
        assert isinstance(client, Client)
        calls.append("metadata")
        recording.duration_seconds = 3600
    monkeypatch.setattr(auth, "get_session", session)
    monkeypatch.setattr(recordings, "resolve_media", resolve)
    settings = Settings(_env_file=None, data_dir=tmp_path, pku_username="student",
                        pku_password="secret", platform_token="session")
    directory = build_fake_directory()
    app = create_app(
        settings=settings, runner=SimpleNamespace(submit=lambda *_: None, current=lambda: None, last=lambda: None),
        directory_service=directory, connection_service=build_fake_connection_service(),
        platform_service=FakePlatformBridge(activated=True),
    )
    client = TestClient(app)
    url = f"/api/campus/courses/campus-1/recordings/{recording_id(job)}/duration"
    response = client.post(url)
    assert response.status_code == 200
    assert response.json() == {"duration_seconds": 3600}
    assert calls == ["login", "metadata", "close"]
    assert campus_course(tmp_path, "campus-1")["lessons"][0]["duration_seconds"] == 3600
    assert not list(tmp_path.rglob("video.mp4"))
    assert client.post(url.replace("/campus-1/", "/another-course/")).status_code == 404


def test_campus_utc_timestamp_is_labeled_as_beijing_time():
    from pku_sync.panel.campus_catalog import display_campus_time
    assert display_campus_time("2026-10-01T15:30:00.000Z") == "2026-10-01 23:30 北京时间"
    assert display_campus_time("2026-10-01 23:30") == "2026-10-01 23:30"
    assert display_campus_time("") == ""
