"""Campus-first catalog and explicit Notion home behavior."""
from __future__ import annotations
from tests.retired_data_support import seed_old_home

import json
from itertools import count
from types import SimpleNamespace

import pytest
from contextlib import nullcontext

from pku_sync.models import Recording
from pku_sync.pipeline import collect_jobs
from pku_sync.panel.campus_catalog import (campus_course, campus_courses,
                                           teacher_deadline_quote)
from pku_sync.panel import notion_home


@pytest.fixture(autouse=True)
def verified_account_projection(monkeypatch):
    # These existing tests exercise Notion layout with a fake client. The
    # account-service gate is independently covered by test_account_catalog.
    def authority(*args, tracked_mutations=False):
        assert tracked_mutations is True
        return nullcontext()
    monkeypatch.setattr(notion_home, "_catalog_authority", authority)


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


def test_campus_sections_distinguish_unread_empty_and_failed_cached(tmp_path):
    folder = _campus(tmp_path)
    detail = campus_course(tmp_path, 'campus-1')
    assert detail['section_states']['recordings']['state'] == 'available'
    assert detail['section_states']['announcements']['state'] == 'not_synced'
    assert detail['section_states']['assignments']['state'] == 'not_synced'
    (folder / 'announcements').mkdir()
    (folder / 'announcements/index.json').write_text('[]', 'utf8')
    metadata = json.loads((folder / 'course.json').read_text('utf8'))
    metadata['announcements_status'] = 'available'
    (folder / 'course.json').write_text(json.dumps(metadata), 'utf8')
    assert campus_course(tmp_path, 'campus-1')['section_states']['announcements']['state'] == 'empty'
    (folder / 'announcements/index.json').write_text('[{"title":"保留通知"}]', 'utf8')
    metadata['announcements_status'] = 'error'
    (folder / 'course.json').write_text(json.dumps(metadata), 'utf8')
    state = campus_course(tmp_path, 'campus-1')['section_states']['announcements']
    assert state['state'] == 'cached'
    assert '失败' in state['message'] and state['count'] == 1


def test_recordings_failed_current_account_read_never_labels_cached_index_fresh(tmp_path):
    from pku_sync.catalog_snapshots import cache_path
    _campus(tmp_path)
    (tmp_path / 'portal-read-status.json').write_text(json.dumps({
        'complete':False, 'reason':'partial',
        'account_namespace':cache_path(tmp_path, 'campus-1').parent.name,
        'courses':{'campus-1':{'complete':False,'reason':'recordings_unavailable'}},
    }), 'utf8')
    course = campus_course(tmp_path, 'campus-1')
    assert course['read_only'] is True
    assert len(course['lessons']) == 1
    assert course['section_states']['recordings']['state'] == 'cached'
    assert '失败' in course['section_states']['recordings']['message']
    assert '已读取' not in course['section_states']['recordings']['message']




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
    directory = None
    app = create_app(settings=settings, connection_service=build_fake_connection_service(), platform_service=FakePlatformBridge(activated=True))
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


def test_course_summary_exists_only_with_saved_summary(tmp_path):
    folder = _campus(tmp_path)
    assert campus_course(tmp_path, "campus-1")["has_summary"] is False
    (folder / "course-summary.md").write_text("# 课程总结\n已整理的内容", "utf-8")
    assert campus_course(tmp_path, "campus-1")["has_summary"] is True
    (folder / "course-summary.md").unlink()
    (folder / "summary.json").write_text(json.dumps({"title": "已发布课程总结", "url": "https://notion.example/summary"}), "utf-8")
    assert campus_course(tmp_path, "campus-1")["has_summary"] is True
