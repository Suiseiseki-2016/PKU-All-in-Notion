from tests.retired_data_support import seed_old_home
"""Windows cross-repository contract acceptance, with local fake HTTP only."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import time

import pytest
from fastapi import FastAPI, HTTPException, Header, Body
from fastapi.testclient import TestClient

from pku_sync import catalog_snapshots as catalog, platform
from pku_sync.models import Course, Recording
from pku_sync.panel import notion_home, recordings_api
from pku_sync.panel.campus_catalog import campus_course

SERVER_ROOT = Path(__file__).resolve().parents[2] / "pku-server"
pytestmark = pytest.mark.skipif(not (SERVER_ROOT / "server" / "catalog.py").is_file(),
                                reason="separate account-service checkout required")


@pytest.fixture
def account_service(tmp_path, monkeypatch):
    if not (SERVER_ROOT / "server" / "catalog.py").is_file():
        pytest.skip("separate account-service checkout required")
    sys.path.insert(0, str(SERVER_ROOT))
    from server import catalog_routes
    app = FastAPI()
    app.state.db_path = tmp_path / "catalog.db"
    def connect(path):
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn
    def account_for(authorization, db):
        if authorization not in {"Bearer mock-account", "Bearer mock-second-device"}:
            raise HTTPException(401)
        return {"id": 1}
    catalog_routes.register(app, connect, account_for, lambda account: None)
    from server import account_profiles
    @app.get("/v1/profile")
    def profile_get(authorization: str = Header()):
        conn = connect(app.state.db_path)
        try:
            account_for(authorization, conn)
            account_profiles.ensure_schema(conn)
            conn.commit()
            return account_profiles.get(conn, 1)
        finally:
            conn.close()
    @app.put("/v1/profile")
    def profile_put(body: dict = Body(), authorization: str = Header()):
        conn = connect(app.state.db_path)
        try:
            account_for(authorization, conn)
            account_profiles.ensure_schema(conn)
            conn.commit()
            options = {'replace_workspace_id': body['replace_workspace_id']} if 'replace_workspace_id' in body else {}
            return account_profiles.put(conn, 1, body["revision"], body["profile"], **options)
        except account_profiles.ProfileConflict as exc:
            raise HTTPException(409) from exc
        except account_profiles.ProfileError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            conn.close()
    http = TestClient(app)
    for name, method in (("_get", "get"), ("_put", "put"), ("_post", "post")):
        def call(url, _method=method, **kwargs):
            kwargs.pop("timeout", None)
            return getattr(http, _method)(url.replace("https://relay.example", ""), **kwargs)
        monkeypatch.setattr(platform, name, call)
    yield http
    sys.path.remove(str(SERVER_ROOT))


class Notion:
    def __init__(self):
        self.pages = {"home": {"id": "home", "title": "旧主页保留", "url": "https://notion.so/home", "parent": None}}
        self.blocks = {"home": []}
        self.sequence = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get_page(self, page_id):
        page = deepcopy(self.pages[page_id])
        if isinstance(page.get("parent"), str):
            page["parent"] = {"page_id": page["parent"]}
        return page

    def move_page(self, page_id, parent_id):
        self.pages[page_id]["parent"] = parent_id
        return self.get_page(page_id)

    def list_child_pages(self, page_id):
        return [deepcopy(page) for page in self.pages.values() if page["parent"] == page_id]

    def list_children(self, page_id):
        rows = deepcopy(self.blocks[page_id])
        rows.extend({"id": page["id"], "type": "child_page", "child_page": {"title": page["title"]}}
                    for page in self.pages.values() if page.get("parent") == page_id)
        return rows

    def append_blocks(self, page_id, blocks, *, after=None, **kwargs):
        added = []
        for block in blocks:
            self.sequence += 1
            added.append({**deepcopy(block), "id": f"b{self.sequence}"})
        rows = self.blocks[page_id]
        index = next((i + 1 for i, row in enumerate(rows) if row["id"] == after), len(rows))
        rows[index:index] = added
        return deepcopy(added)

    def create_page(self, parent, title, *, children=None, **kwargs):
        from uuid import UUID
        identifier = str(UUID(int=len(self.pages) + 1))
        self.pages[identifier] = {"id": identifier, "title": title, "url": "https://notion.so/" + identifier, "parent": parent}
        self.blocks[identifier] = []
        self.append_blocks(identifier, children or [])
        return deepcopy(self.pages[identifier])

    def update_page_properties(self, page_id, properties):
        self.pages[page_id]["title"] = properties["title"]["title"][0]["text"]["content"]

    def update_paragraph(self, block_id, text):
        for rows in self.blocks.values():
            for row in rows:
                if row["id"] == block_id:
                    row["paragraph"]["rich_text"] = [{"type": "text", "text": {"content": text}}]
                    return deepcopy(row)
        raise AssertionError(block_id)

    def archive_block(self, block_id, **kwargs):
        for rows in self.blocks.values():
            for i, row in enumerate(rows):
                if row["id"] == block_id:
                    return rows.pop(i)
        raise AssertionError(block_id)

    def replace_page_content(self, *args):
        raise AssertionError("shared documents must preserve student content")


def settings(root):
    return SimpleNamespace(data_dir=root, pku_username="mock-student", pku_password="mock-password",
                           platform_token="mock-account", cloud_transcribe_url="https://relay.example/v1/transcribe")


def fake_portal(monkeypatch, count=5, partial=False, title="发展心理学"):
    from pku_sync import auth, discover, recordings, materials
    course = Course(course_id="_101545_1", name="发展心理学")
    monkeypatch.setattr(auth, "get_session", lambda **kwargs: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(discover, "discover_courses", lambda client: [course])
    monkeypatch.setattr(discover, "select_courses", lambda courses, settings: courses)
    monkeypatch.setattr(recordings, "list_recordings", lambda *args: [
        Recording(course_id=course.course_id, recorded_at=f"2026-09-{1+7*i:02} 13:00:00", title=title,
                  play_url=f"playVideo.action?token=local-private-{i}") for i in range(count)])
    def walk(*args):
        if partial:
            raise ValueError("simulated truncated directory")
        return [], []
    monkeypatch.setattr(materials, "walk_materials", walk)
    monkeypatch.setattr(materials, "fetch_announcements", lambda *args: [])
    monkeypatch.setattr(materials, "fetch_deadlines", lambda *args: {})


def test_real_refresh_contract_and_notion_self_heal_without_paid_calls(tmp_path, monkeypatch, account_service):
    fake_portal(monkeypatch)
    s = settings(tmp_path)
    recordings_api.sync_recording_index(s, lambda _: None)
    canonical = catalog.current(s, "_101545_1")
    assert canonical["revision"] == 1
    assert "local-private" not in json.dumps(canonical)
    notion = Notion()
    monkeypatch.setattr(notion_home, "NotionClient", lambda token: notion)
    seed_old_home(tmp_path, {"id": "home", "parent_id": "parent"})
    bootstrap_current_workspace(tmp_path, s, notion, monkeypatch)
    notion_home.sync_catalog(tmp_path, "mock-notion", settings=s)
    index = next(page for page in notion.pages.values() if page["title"] == "每节课")
    rows = notion.blocks[index["id"]]
    visible = lambda row: json.dumps(row, ensure_ascii=False)
    assert any("2026-09-29" in visible(row) for row in rows)
    rows[:] = [row for row in rows if "2026-09-29" not in visible(row)]
    notion_home.sync_catalog(tmp_path, "mock-notion", settings=s)
    assert any("2026-09-29" in visible(row) for row in rows)
    fake_portal(monkeypatch, count=4)
    recordings_api.sync_recording_index(s, lambda _: None)
    assert catalog.current(s, "_101545_1") == canonical
    assert len(campus_course(tmp_path, "_101545_1")["lessons"]) == 5
    with pytest.raises(catalog.CatalogUnavailable, match="已验证"):
        notion_home.sync_catalog(tmp_path, "mock-notion", settings=s)
    assert not list(tmp_path.rglob("video.mp4"))
    assert notion.pages["home"]["title"] == "旧主页保留"


def test_partial_portal_read_preserves_complete_files_and_account_snapshot(tmp_path, monkeypatch, account_service):
    fake_portal(monkeypatch)
    s = settings(tmp_path)
    recordings_api.sync_recording_index(s, lambda _: None)
    index = tmp_path / "发展心理学" / "recordings" / "index.json"
    before = index.read_bytes()
    canonical = catalog.current(s, "_101545_1")
    fake_portal(monkeypatch, count=4, partial=True)
    recordings_api.sync_recording_index(s, lambda _: None)
    assert index.read_bytes() == before
    assert catalog.current(s, "_101545_1") == canonical
    with pytest.raises(catalog.CatalogUnavailable):
        catalog.assert_portal_readable(tmp_path)


def test_new_device_reads_five_lectures_without_teaching_portal_or_media(tmp_path, monkeypatch, account_service):
    fake_portal(monkeypatch)
    original = settings(tmp_path / "windows")
    recordings_api.sync_recording_index(original, lambda _: None)
    new_device = settings(tmp_path / "other-device")
    new_device.pku_username = new_device.pku_password = ""
    result = catalog.pull(new_device)
    assert result["courses"] == 1
    detail = campus_course(Path(new_device.data_dir), "_101545_1")
    assert len(detail["lessons"]) == 5
    assert detail["lessons"][-1]["date"] == "2026-09-29"
    assert not list(Path(new_device.data_dir).rglob("video.mp4"))


def test_portal_lecture_rename_reuses_notion_source_page(tmp_path, monkeypatch, account_service):
    fake_portal(monkeypatch)
    s = settings(tmp_path)
    recordings_api.sync_recording_index(s, lambda _: None)
    notion = Notion()
    monkeypatch.setattr(notion_home, "NotionClient", lambda token: notion)
    seed_old_home(tmp_path, {"id": "home", "parent_id": "parent"})
    bootstrap_current_workspace(tmp_path, s, notion, monkeypatch)
    notion_home.sync_catalog(tmp_path, "mock-notion", settings=s)
    original = {page["id"] for page in notion.pages.values() if "2026-09-" in page["title"]}
    fake_portal(monkeypatch, title="教学网修订后的讲次名称")
    recordings_api.sync_recording_index(s, lambda _: None)
    notion_home.sync_catalog(tmp_path, "mock-notion", settings=s)
    renamed = {page["id"] for page in notion.pages.values() if "2026-09-" in page["title"]}
    assert original == renamed and len(renamed) == 5




def test_observation_precedes_calendar_and_course_metadata_reads(tmp_path, monkeypatch, account_service):
    from pku_sync import materials, discover
    fake_portal(monkeypatch)
    events = []
    begin = catalog.begin_observation
    def observe(*args):
        result = begin(*args)
        events.append("observation")
        return result
    def calendar(*args):
        assert events == ["discovery", "observation", "discovery"]
        events.append("calendar")
        return {}
    def discovery(*args):
        events.append("discovery")
        return [Course(course_id="_101545_1", name="发展心理学")]
    monkeypatch.setattr(catalog, "begin_observation", observe)
    monkeypatch.setattr(discover, "discover_courses", discovery)
    monkeypatch.setattr(materials, "fetch_deadlines", calendar)
    recordings_api.sync_recording_index(settings(tmp_path), lambda _: None)
    assert events == ["discovery", "observation", "discovery", "calendar"]


def test_official_assessment_projects_to_new_quiz_center_and_preserves_learning_pages(tmp_path, monkeypatch, account_service):
    from pku_sync import materials
    from pku_sync.models import ContentItem
    from pku_sync.panel import notion_workspace
    fake_portal(monkeypatch)
    monkeypatch.setattr(materials, 'walk_materials', lambda *a: ([ContentItem(
        course_id='_101545_1', content_id='_8_1', parent_content_id='_10_1',
        title='教学网正式测验', kind='测试', parent_path='教学内容', body_text='请在教学网完成')], []))
    s = settings(tmp_path)
    s.notion_token = 'mock-notion'
    recordings_api.sync_recording_index(s, lambda _: None)
    notion = Notion()
    parent = 'a' * 32
    notion.pages[parent] = {'id': parent, 'title': '学习空间', 'parent': None}
    notion.blocks[parent] = []
    monkeypatch.setattr(notion_workspace, 'NotionClient', lambda _: notion)
    monkeypatch.setattr(notion_home, 'available_parents', lambda _: [{'id': parent}])
    result = notion_workspace.sync_workspace(s, parent)
    center = result['courses']['_101545_1']['centers']['小测中心']['id']
    official = notion.list_child_pages(center)
    assert len(official) == 1 and official[0]['title'] == '教学网正式测验'
    encoded = json.dumps(notion.blocks[official[0]['id']], ensure_ascii=False)
    assert 'listContent.jsp' in encoded and 'uploadAssignment' not in encoded
    learning = notion.create_page(center, '学生学习小测', children=[notion_home._linked_paragraph('学生作答保留', 'https://course.pku.edu.cn/')])
    before = deepcopy(notion.blocks[learning['id']])
    notion_workspace.sync_workspace(s)
    assert len(notion.list_child_pages(center)) == 2
    assert notion.blocks[learning['id']] == before


def test_additive_workspace_preserves_legacy_tree_and_repairs_its_own_index(tmp_path, monkeypatch, account_service):
    from pku_sync.panel import notion_workspace
    fake_portal(monkeypatch)
    s = settings(tmp_path)
    s.notion_token = "mock-notion"
    recordings_api.sync_recording_index(s, lambda _: None)
    notion = Notion()
    parent = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    notion.pages[parent] = {"id": parent, "title": "学习空间", "parent": None}
    notion.blocks[parent] = []
    notion.pages["home"]["parent"] = parent
    notion.blocks["home"] = [{"id": "student", "type": "paragraph",
                                "paragraph": {"rich_text": [{"text": {"content": "学生旧笔记"}}]}}]
    seed_old_home(tmp_path, {"id": "home", "parent_id": parent})
    notion_home._save_source_state(tmp_path, {"keep": "旧同步摘要"})
    before_home = notion_home.home_path(tmp_path).read_bytes()
    before_state = notion_home._source_state_path(tmp_path).read_bytes()
    old_pages = deepcopy(notion.pages)
    old_blocks = deepcopy(notion.blocks["home"])
    monkeypatch.setattr(notion_workspace, "NotionClient", lambda _: notion)
    monkeypatch.setattr(notion_home, "available_parents", lambda _: [{"id": parent}])
    first = notion_workspace.sync_workspace(s, parent)
    assert first["id"] != "home" and first["legacy_home_id"] == "home"
    assert notion_home.saved_home(tmp_path)["id"] == first["id"]
    center = first["courses"]["_101545_1"]["centers"]["每节课"]["id"]
    assert set(first["courses"]["_101545_1"]["centers"]) == set(notion_workspace.CENTERS)
    assert len(notion.list_child_pages(center)) == 5
    from pku_sync.pipeline import collect_jobs
    job = collect_jobs(tmp_path, "_101545_1")[-1]
    target = notion_home.ensure_recording_target(tmp_path, "mock-notion", job, settings=s)
    assert notion.pages[target]["parent"] == center
    ids = set(notion.pages)
    notion.pages[first["id"]]["title"] = "学生自定义新版主页标题"
    notion.blocks[center] = [row for row in notion.blocks[center]
                             if "2026-09-29" not in json.dumps(row, ensure_ascii=False)]
    notion_workspace.sync_workspace(s)
    assert set(notion.pages) == ids
    assert any("2026-09-29" in json.dumps(row, ensure_ascii=False) for row in notion.blocks[center])
    assert notion_home.home_path(tmp_path).read_bytes() == before_home
    assert notion_home._source_state_path(tmp_path).read_bytes() == before_state
    assert notion.blocks["home"] == old_blocks
    assert all(notion.pages[key] == value for key, value in old_pages.items())
    fake_portal(monkeypatch, partial=True)
    recordings_api.sync_recording_index(s, lambda _: None)
    before = deepcopy(notion.blocks)
    with pytest.raises(catalog.CatalogUnavailable):
        notion_workspace.sync_workspace(s)
    assert notion.blocks == before


def test_workspace_readback_failure_never_marks_the_projection_complete(tmp_path, monkeypatch, account_service):
    from pku_sync.panel import notion_workspace
    fake_portal(monkeypatch)
    s = settings(tmp_path)
    s.notion_token = "mock-notion"
    recordings_api.sync_recording_index(s, lambda _: None)
    notion = Notion()
    parent = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    notion.pages[parent] = {"id": parent, "title": "空间", "parent": None}
    notion.blocks[parent] = []
    seed_old_home(tmp_path, {"id": "home", "parent_id": "parent"})
    old = notion_home.home_path(tmp_path).read_bytes()
    create = notion.create_page
    def lose_course_identity(*args, **kwargs):
        page = create(*args, **kwargs)
        if args[1] == "发展心理学":
            notion.blocks[page["id"]] = []
        return page
    monkeypatch.setattr(notion, "create_page", lose_course_identity)
    monkeypatch.setattr(notion_workspace, "NotionClient", lambda _: notion)
    monkeypatch.setattr(notion_home, "available_parents", lambda _: [{"id": parent}])
    with pytest.raises(ValueError, match="身份写入后验证失败"):
        notion_workspace.sync_workspace(s, parent)
    assert not notion_workspace.saved_workspace(tmp_path).get("active")
    assert notion_home.saved_home(tmp_path) == {}
    assert notion_home.home_path(tmp_path).read_bytes() == old
    assert notion.blocks["home"] == []
    before_ids = set(notion.pages)
    with pytest.raises(ValueError, match="缺少稳定身份"):
        notion_workspace.sync_workspace(s, parent)
    assert set(notion.pages) == before_ids


def test_workspace_identity_pulls_to_another_device_without_notion_writes(tmp_path, monkeypatch, account_service):
    from pku_sync.panel import notion_workspace
    from pku_sync import notion as notion_module
    fake_portal(monkeypatch)
    first = settings(tmp_path / "first")
    first.notion_token = "mock-notion"
    recordings_api.sync_recording_index(first, lambda _: None)
    store = Notion()
    parent = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    store.pages[parent] = {"id": parent, "title": "空间", "parent": None}
    store.blocks[parent] = []
    monkeypatch.setattr(notion_workspace, "NotionClient", lambda _: store)
    monkeypatch.setattr(notion_module, "NotionClient", lambda _: store)
    monkeypatch.setattr(notion_home, "available_parents", lambda _: [{"id": parent}])
    workspace = notion_workspace.sync_workspace(first, parent)
    lesson_id = campus_course(first.data_dir, "_101545_1")["lessons"][-1]["id"]
    lecture_id = workspace["courses"]["_101545_1"]["lectures"][lesson_id]["id"]
    note = store.create_page(lecture_id, "转写笔记 · 2026-09-29", children=[])
    notion_workspace.read_projection(first)
    refreshed = notion_workspace.sync_workspace(first)
    assert refreshed["courses"]["_101545_1"]["lectures"][lesson_id]["note_url"] == note["url"]
    before = deepcopy((store.pages, store.blocks))
    second = settings(tmp_path / "second")
    second.platform_token = "mock-second-device"
    second.notion_token = "mock-notion"
    second.pku_username = second.pku_password = ""
    platform.sync_account_profile(second)
    catalog.pull(second)
    notion_workspace.read_projection(second)
    assert notion_home.saved_home(second.data_dir)["id"] == workspace["id"]
    projected = campus_course(second.data_dir, "_101545_1")["lessons"][-1]
    assert projected["notion_note_url"] == note["url"]
    assert projected["source_id"] == workspace["courses"]["_101545_1"]["lectures"][lesson_id]["source_id"]
    assert not projected["notes_available"]
    assert deepcopy((store.pages, store.blocks)) == before
    assert account_service.get("/v1/profile", headers={"Authorization": "Bearer mock-account"}).json()["revision"] == workspace["profile_revision"]
    assert not list(second.data_dir.rglob("video.mp4"))
    first.platform_token = "another-account"
    catalog.activate_account(first)
    assert notion_workspace.saved_workspace(first.data_dir) == {}


def test_legacy_partial_catalog_sync_keeps_verified_courses_usable(
    tmp_path, monkeypatch, account_service,
):
    """HTTP workflow regression, not an installed/native desktop E2E.

    A legacy device may have extra cached courses that the account service
    legitimately returns as revision=0/snapshot=null. Those courses stay
    read-only; they must not break synchronization of verified courses.
    Only external boundaries use the existing offline fixtures.
    """
    from pku_sync.config import Settings
    from pku_sync.panel import course_summary
    from pku_sync.panel.webapi import create_app

    fake_portal(monkeypatch)
    configured = Settings(
        _env_file=None, data_dir=tmp_path,
        pku_username="mock-student", pku_password="mock-password",
        platform_token="mock-account",
        cloud_transcribe_url="https://relay.example/v1/transcribe",
        notion_token="mock-notion",
    )
    recordings_api.sync_recording_index(configured, lambda _: None)
    store = Notion()
    monkeypatch.setattr(notion_home, "NotionClient", lambda _: store)
    monkeypatch.setattr(course_summary, "NotionClient", lambda _: store)
    seed_old_home(tmp_path, {"id": "home", "parent_id": "parent"})
    bootstrap_current_workspace(tmp_path, configured, store, monkeypatch)
    legacy_ids = {"_101577_1", "_104157_1"}
    for course_id in legacy_ids:
        folder = tmp_path / course_id
        folder.mkdir()
        (folder / "course.json").write_text(
            Course(course_id=course_id, name=f"旧缓存 {course_id}").model_dump_json(),
            encoding="utf-8",
        )
    # Old installations predate this per-course read ledger. Remove only the
    # fixture's ledger, not any real student's data.
    (tmp_path / "portal-read-status.json").unlink()

    with TestClient(create_app(settings=configured)) as client:
        response = client.get("/api/campus/courses")
        assert response.status_code == 200
        courses = {row["id"]: row for row in response.json()["courses"]}
        assert not courses["_101545_1"]["read_only"]
        assert all(courses[course_id]["read_only"] for course_id in legacy_ids)
        started = client.post("/api/campus/notion/sync")
        assert started.status_code == 200, started.text
        task_id = started.json()["id"]
        deadline = time.monotonic() + 10
        while True:
            task_response = client.get("/api/recordings/task")
            assert task_response.status_code == 200
            task = task_response.json()["background_sync"]
            assert task["id"] == task_id
            if task.get("state") != "running" or time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        assert task.get("state") == "done", (
            "Verified courses must still sync when legacy extras have no canonical snapshot: "
            + json.dumps(task, ensure_ascii=False)
        )
        assert task["progress_done"] == task["progress_total"] == 1
        assert task["course_results"][0]["metadata_state"] == "confirmed"
        assert task["course_results"][0]["projection_state"] == "confirmed"
        assert task["course_results"][0]["summary_created"] == 0
        refreshed = client.get("/api/campus/courses")
        assert refreshed.status_code == 200
        current = {row["id"]: row for row in refreshed.json()["courses"]}
        assert set(current) == {"_101545_1", *legacy_ids}
        assert not current["_101545_1"]["read_only"]
        assert all(current[course_id]["read_only"] for course_id in legacy_ids)
        assert any(page["title"] == "发展心理学" for page in store.pages.values())
        assert not any(page["title"].startswith("旧缓存") for page in store.pages.values())
    assert not list(tmp_path.rglob("video.mp4"))


def test_directory_refresh_keeps_verified_courses_usable_when_another_course_is_unreadable(
    tmp_path, monkeypatch, account_service,
):
    from pku_sync import discover, recordings, materials
    from pku_sync.panel import course_summary
    fake_portal(monkeypatch)
    courses = [Course(course_id='_101545_1', name='可读课程'),
               Course(course_id='_104157_1', name='不可读课程')]
    monkeypatch.setattr(discover, 'discover_courses', lambda client: courses)
    monkeypatch.setattr(recordings, 'list_recordings', lambda client, cid: [
        Recording(course_id=cid, recorded_at='2026-09-29 13:00:00', title='第一讲')])
    s = settings(tmp_path)
    s.notion_token = 'mock-notion'
    recordings_api.sync_recording_index(s, lambda _: None)
    old_snapshot = catalog.current(s, '_104157_1')
    store = Notion()
    monkeypatch.setattr(notion_home, 'NotionClient', lambda _: store)
    seed_old_home(tmp_path, {'id': 'home', 'parent_id': 'parent'})
    bootstrap_current_workspace(tmp_path, s, store, monkeypatch)
    notion_home.sync_catalog(tmp_path, 'mock-notion', settings=s)
    bad_page = next(page['id'] for page in store.pages.values() if page['title'] == '不可读课程')
    bad_blocks = deepcopy(store.blocks[bad_page])
    def partial(client, cid):
        if cid == '_104157_1':
            raise ValueError('truncated official directory')
        return [], []
    monkeypatch.setattr(materials, 'walk_materials', partial)
    monkeypatch.setattr(course_summary, 'publish_course_summaries', lambda *args, **kwargs:
                        {'created': 0, 'existing': 0, 'urls': [], 'failures': []})
    manager = recordings_api.RecordingWorkManager(s)
    manager.start_sync()
    deadline = time.monotonic() + 10
    while manager.background_state()['state'] == 'running' and time.monotonic() < deadline:
        time.sleep(.01)
    task = manager.background_state()
    assert task['state'] == 'partial', task
    rows = {row['course_id']: row for row in task['course_results']}
    assert rows['_101545_1']['state'] == 'done'
    assert rows['_101545_1']['metadata_state'] == 'confirmed'
    assert rows['_101545_1']['projection_state'] == 'confirmed'
    assert rows['_104157_1']['state'] == 'failed'
    assert rows['_104157_1']['metadata_state'] == 'unconfirmed'
    assert rows['_104157_1']['projection_state'] == 'not_read'
    assert rows['_104157_1']['error'] and rows['_104157_1']['retryable']
    assert task['progress_done'] == 1 and task['progress_total'] == 2
    assert all(row['summary_created'] == row['summary_existing'] == 0 for row in rows.values())
    assert catalog.current(s, '_104157_1') == old_snapshot
    assert store.blocks[bad_page] == bad_blocks
    assert not campus_course(tmp_path, '_101545_1')['read_only']
    assert campus_course(tmp_path, '_104157_1')['read_only']
    with pytest.raises(catalog.CatalogUnavailable):
        catalog.assert_portal_readable(tmp_path)
    with pytest.raises(catalog.CatalogUnavailable):
        catalog.assert_portal_readable(tmp_path, '_104157_1')
    with pytest.raises(catalog.CatalogUnavailable):
        with notion_home._catalog_authority(tmp_path, s, ['_104157_1']):
            pytest.fail('unreadable course projection must never start')


def test_refresh_reports_projection_conflict_without_releasing_or_rebasing(
    tmp_path, monkeypatch, account_service,
):
    """Developer contract regression; real native evidence is recorded separately."""
    fake_portal(monkeypatch)
    configured = settings(tmp_path)
    recordings_api.sync_recording_index(configured, lambda _: None)
    original = catalog.current(configured, '_101545_1')
    headers = {'Authorization': 'Bearer mock-account'}
    route = '/v1/catalog/_101545_1'
    permit = account_service.post(route + '/projection', headers=headers,
                                 json={'revision': original['revision']}).json()
    manager = recordings_api.RecordingWorkManager(configured)
    manager.start_sync('_101545_1')
    assert manager._finished.wait(10), 'Selected course task did not reach a terminal state'
    task = manager.state()
    assert task['state'] == 'failed'
    assert '上次共享目录写入许可尚未释放' in task['error']
    assert '核对上次 Notion 写入后恢复许可' in task['error']
    assert '教学网课程目录未能完整读取' not in task['error']
    assert task['failure_id'] and not task['notion_synced']
    assert catalog.current(configured, '_101545_1') == original
    confirmed = account_service.post(route + '/projection/verify', headers=headers,
        json={'revision': original['revision'], 'permit_id': permit['permit_id']})
    assert confirmed.status_code == 200
    with pytest.raises(catalog.CatalogUnavailable):
        catalog.assert_portal_readable(tmp_path, '_101545_1')
    status = json.loads((tmp_path / 'portal-read-status.json').read_text('utf-8'))
    assert status['courses']['_101545_1']['complete'] is False
    assert status['courses']['_101545_1']['issue'] == 'projection_busy'


def test_unconfirmed_workspace_registration_stays_inactive_and_reuses_its_pending_tree(tmp_path, monkeypatch, account_service):
    from pku_sync.panel import notion_workspace
    fake_portal(monkeypatch)
    s = settings(tmp_path)
    s.notion_token = "mock-notion"
    recordings_api.sync_recording_index(s, lambda _: None)
    store = Notion()
    parent = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    store.pages[parent] = {"id": parent, "title": "空间", "parent": None}
    store.blocks[parent] = []
    seed_old_home(tmp_path, {"id": "home", "parent_id": parent})
    monkeypatch.setattr(notion_workspace, "NotionClient", lambda _: store)
    monkeypatch.setattr(notion_home, "available_parents", lambda _: [{"id": parent}])
    with monkeypatch.context() as failure:
        failure.setattr(platform, "_put_account_profile", lambda *args: {"status": "conflict"})
        with pytest.raises(catalog.CatalogUnavailable, match="未切换"):
            notion_workspace.sync_workspace(s, parent)
    pending = notion_workspace.saved_workspace(tmp_path)
    assert pending["active"] is False and notion_home.saved_home(tmp_path) == {}
    page_ids = set(store.pages)
    verified = notion_workspace.sync_workspace(s)
    assert verified["active"] and verified["id"] == pending["id"]
    assert set(store.pages) == page_ids
    destination = "cccccccccccccccccccccccccccccccc"
    store.pages[destination] = {"id": destination, "title": "新位置", "parent": None}
    store.blocks[destination] = []
    monkeypatch.setattr(notion_home, "available_parents", lambda _: [{"id": parent}, {"id": destination}])
    old_file = notion_home.home_path(tmp_path).read_bytes()
    moved = notion_workspace.move_workspace(s, destination)
    assert moved["id"] == verified["id"] and moved["active"]
    assert store.pages[moved["id"]]["parent"] == destination
    assert notion_home.home_path(tmp_path).read_bytes() == old_file
    profile = account_service.get("/v1/profile", headers={"Authorization": "Bearer mock-account"}).json()
    assert profile["profile"]["notion_workspace_v2"]["parent_id"] == destination
    assert notion_workspace.sync_workspace(s)["id"] == moved["id"]


def test_native_quiz_reads_accepted_remote_summary_on_new_device_by_official_identity(tmp_path, monkeypatch, account_service):
    from pku_sync.panel import notion_workspace, campus_quiz
    from pku_sync.panel.summary_acceptance import accept_published, status
    from pku_sync import notion as notion_module
    fake_portal(monkeypatch)
    first = settings(tmp_path / "first")
    first.notion_token = "mock-notion"
    recordings_api.sync_recording_index(first, lambda _: None)
    store = Notion()
    parent = "a" * 32
    store.pages[parent] = {"id": parent, "title": "空间", "parent": None}
    store.blocks[parent] = []
    monkeypatch.setattr(notion_workspace, "NotionClient", lambda _: store)
    monkeypatch.setattr(notion_module, "NotionClient", lambda _: store)
    monkeypatch.setattr(campus_quiz, "NotionClient", lambda _: store)
    monkeypatch.setattr(notion_home, "available_parents", lambda _: [{"id": parent}])
    workspace = notion_workspace.sync_workspace(first, parent)
    lesson = campus_course(first.data_dir, "_101545_1")["lessons"][-1]
    lecture_id = workspace["courses"]["_101545_1"]["lectures"][lesson["id"]]["id"]
    job = recordings_api.find_job(first.data_dir, lesson["id"])
    job.directory.mkdir(parents=True, exist_ok=True)
    (job.directory / "notes.md").write_text("# 第五讲\n\n已经人工核对的课堂总结", "utf-8")
    recordings_api.publish_notes(job, lecture_id, "mock-notion")
    accepted = accept_published(job, client=store, notes_sha256=status(job)["notes_sha256"], confirmed=True)
    store.pages[lecture_id]["title"] = "学生修改的讲次标题"
    second = settings(tmp_path / "second")
    second.platform_token = "mock-second-device"
    second.notion_token = "mock-notion"
    second.pku_username = second.pku_password = ""
    platform.sync_account_profile(second)
    catalog.pull(second)
    before = deepcopy((store.pages, store.blocks))
    provider = campus_quiz.CampusQuizSources(second)
    result = provider.resolve("_101545_1", [lesson["source_id"]])
    assert result["notes"][0]["source_id"] == lesson["source_id"]
    assert result["notes"][0]["remote_sha256"] == accepted["remote_sha256"]
    assert "已经人工核对" in json.dumps(result["notes"], ensure_ascii=False)
    projected = campus_course(second.data_dir, "_101545_1")["lessons"][-1]
    assert projected["summary_acceptance"]["accepted"] and not projected["notes_available"]
    assert deepcopy((store.pages, store.blocks)) == before
    with pytest.raises(campus_quiz.OrganizeBlocked):
        provider.resolve("_101545_1", ["another-official-lecture-id"])
    note_id = accepted["note_id"]
    store.blocks[note_id][0][store.blocks[note_id][0]["type"]]["rich_text"][0]["text"]["content"] = "远端已修改"
    with pytest.raises(campus_quiz.OrganizeBlocked, match="验收"):
        provider.resolve("_101545_1", [lesson["source_id"]])
    assert not list(second.data_dir.rglob("video.mp4"))


def bootstrap_current_workspace(root, configuration, store, monkeypatch):
    from pku_sync.panel import notion_workspace
    parent = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    store.pages[parent] = {"id": parent, "title": "Current parent", "parent": None}
    store.blocks[parent] = []
    configuration.notion_token = "mock-notion"
    monkeypatch.setattr(notion_workspace, "NotionClient", lambda _: store)
    monkeypatch.setattr(notion_home, "available_parents", lambda _: [{"id": parent}])
    return notion_workspace.sync_workspace(configuration, parent)
