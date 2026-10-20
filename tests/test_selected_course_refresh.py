"""Windows development checks for explicitly scoped course refreshes.

These checks deliberately use isolated fixtures; they are not native E2E evidence.
"""
from contextlib import contextmanager
import json
from types import SimpleNamespace
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from pku_sync import auth, catalog_snapshots as catalog, discover, materials, recordings
from pku_sync.models import Course
from pku_sync.panel import notion_home, notion_workspace, recordings_api


def configuration(root, token="fixture-account"):
    return SimpleNamespace(data_dir=root, platform_token=token, pku_username="fixture",
                           pku_password="fixture", notion_token="fixture-notion")


@pytest.mark.parametrize('outcome', ['done', 'failed'])
def test_next_task_waits_for_terminal_workers_reservation_cleanup(tmp_path, monkeypatch, outcome):
    manager = recordings_api.RecordingWorkManager(configuration(tmp_path))
    terminal = threading.Event()
    release = threading.Event()
    threads = []
    calls = []
    original = manager._run_action

    def pause_after_terminal(action):
        threads.append(threading.current_thread())
        original(action)
        terminal.set()
        assert release.wait(5)

    def first_action():
        calls.append('first')
        if outcome == 'failed':
            raise ValueError('Fixture operation failed; its paid response remains saved.')
        return 'https://example.invalid/first'

    monkeypatch.setattr(manager, '_run_action', pause_after_terminal)
    manager._start('process', 'first-recording', first_action, course_id='selected')
    try:
        assert terminal.wait(5)
        first_id = manager.state()['id']
        assert manager.state()['state'] == outcome
        assert not manager._finished.is_set()
        with pytest.raises(ValueError, match='收尾|正在进行'):
            manager._start('process', 'second-recording', lambda: calls.append('second') or '', course_id='other')
        assert manager.state()['id'] == first_id
        assert calls == ['first']
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
    assert manager._finished.is_set()
    assert manager._writes.owners == {}
    monkeypatch.setattr(manager, '_run_action', original)
    manager._start('process', 'second-recording', lambda: calls.append('second') or '', course_id='other')
    assert manager._finished.wait(5)
    assert calls == ['first', 'second']
    assert manager.state()['recording_id'] == 'second-recording'
    assert manager._writes.owners == {}


def test_delayed_start_response_keeps_its_task_identity_after_next_task_starts(tmp_path, monkeypatch):
    manager = recordings_api.RecordingWorkManager(configuration(tmp_path))
    observed = {}
    calls = []

    def first_action():
        observed['first_id'] = manager.state()['id']
        calls.append('first')
        return ''

    class ImmediateThread:
        def __init__(self, target, args, daemon):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)
            if calls == ['first']:
                observed['second'] = manager._start('process', 'second-recording',
                    lambda: calls.append('second') or '', course_id='other')

    monkeypatch.setattr(recordings_api.threading, 'Thread', ImmediateThread)
    accepted = manager._start('process', 'first-recording', first_action, course_id='selected')
    assert calls == ['first', 'second']
    assert accepted['id'] == observed['first_id']
    assert accepted['recording_id'] == 'first-recording'
    assert manager.state()['id'] == observed['second']['id'] != accepted['id']
    assert manager._finished.is_set() and manager._writes.owners == {}


def confirmed_ledger(root, settings):
    catalog.activate_account(settings)
    catalog.read_status(root, False, "reading")
    catalog.course_read_status(root, "selected", True, "complete")
    catalog.course_read_status(root, "other", True, "complete")
    catalog.read_status(root, True, "complete")


def test_single_refresh_preserves_other_confirmed_course_without_global_success(tmp_path):
    settings = configuration(tmp_path)
    confirmed_ledger(tmp_path, settings)
    other = json.loads((tmp_path / "portal-read-status.json").read_text("utf-8"))["courses"]["other"]
    catalog.read_status(tmp_path, False, "reading", course_id="selected")
    catalog.course_read_status(tmp_path, "selected", False, "reading")
    catalog.assert_portal_readable(tmp_path, "other")
    with pytest.raises(catalog.CatalogUnavailable):
        catalog.assert_portal_readable(tmp_path, "selected")
    catalog.course_read_status(tmp_path, "selected", True, "complete")
    catalog.read_status(tmp_path, True, "complete", course_id="selected")
    status = json.loads((tmp_path / "portal-read-status.json").read_text("utf-8"))
    assert status["courses"]["other"] == other
    assert status["complete"] is False and status["scope"] == "course"
    assert status["course_id"] == "selected" and status["reason"] == "complete"
    catalog.assert_portal_readable(tmp_path, "selected")
    with pytest.raises(catalog.CatalogUnavailable):
        catalog.assert_portal_readable(tmp_path)


def test_single_refresh_cannot_preserve_another_accounts_confirmation(tmp_path):
    confirmed_ledger(tmp_path, configuration(tmp_path, "old-account"))
    catalog.activate_account(configuration(tmp_path, "new-account"))
    catalog.read_status(tmp_path, False, "reading", course_id="selected")
    catalog.course_read_status(tmp_path, "selected", True, "complete")
    with pytest.raises(catalog.CatalogUnavailable):
        catalog.assert_portal_readable(tmp_path, "other")
    assert "other" not in json.loads((tmp_path / "portal-read-status.json").read_text("utf-8"))["courses"]


def test_original_full_refresh_still_invalidates_all_previous_course_confirmations(tmp_path):
    confirmed_ledger(tmp_path, configuration(tmp_path))
    catalog.read_status(tmp_path, False, "reading")
    assert json.loads((tmp_path / "portal-read-status.json").read_text("utf-8"))["courses"] == {}


def portal_fixture(tmp_path, monkeypatch):
    settings = configuration(tmp_path)
    confirmed_ledger(tmp_path, settings)
    selected = Course(course_id="selected", name="Selected fixture")
    other = Course(course_id="other", name="Other fixture")
    other_dir = tmp_path / other.directory
    other_dir.mkdir()
    (other_dir / "course.json").write_text(json.dumps(other.model_dump()), "utf-8")
    (other_dir / "student-answer.md").write_text("student fixture answer", "utf-8")
    original = {str(path.relative_to(other_dir)): path.read_bytes() for path in other_dir.iterdir()}
    calls = []
    client = SimpleNamespace(close=lambda: calls.append(("close", "")))
    monkeypatch.setattr(auth, "get_session", lambda **kwargs: client)
    monkeypatch.setattr(discover, "discover_courses", lambda client: [selected, other])
    monkeypatch.setattr(discover, "select_courses", lambda rows, settings: rows)
    monkeypatch.setattr(catalog, "begin_observation",
                        lambda settings, identifier: calls.append(("observe", identifier)) or
                        {"revision": 1, "observation_id": "fixture"})
    monkeypatch.setattr(materials, "fetch_deadlines", lambda client: {})
    monkeypatch.setattr(recordings, "list_recordings",
                        lambda client, identifier: calls.append(("recordings", identifier)) or [])
    monkeypatch.setattr(materials, "walk_materials",
                        lambda client, identifier: calls.append(("materials", identifier)) or ([], []))
    monkeypatch.setattr(materials, "fetch_announcements",
                        lambda client, identifier: calls.append(("notices", identifier)) or [])
    def publish(settings, identifier, observation, snapshot):
        calls.append(("publish", identifier))
        return {"revision": 2, "snapshot": snapshot}
    monkeypatch.setattr(catalog, "publish", publish)
    monkeypatch.setattr(catalog, "verify_current", lambda settings, envelope: envelope)
    return settings, calls, other_dir, original


def test_real_index_function_scopes_observation_reads_writes_and_retains_other_ledger(tmp_path, monkeypatch):
    settings, calls, other_dir, original = portal_fixture(tmp_path, monkeypatch)
    recordings_api.sync_recording_index(settings, lambda stage: None, course_id="selected")
    assert calls == [(kind, "selected") for kind in
                     ("observe", "recordings", "materials", "notices", "publish")] + [("close", "")]
    assert {str(path.relative_to(other_dir)): path.read_bytes() for path in other_dir.iterdir()} == original
    catalog.assert_portal_readable(tmp_path, "other")
    catalog.assert_portal_readable(tmp_path, "selected")
    assert json.loads((tmp_path / "portal-read-status.json").read_text("utf-8"))["complete"] is False


def test_unknown_selected_course_fails_before_observation_or_ledger_mutation(tmp_path, monkeypatch):
    settings, calls, _, _ = portal_fixture(tmp_path, monkeypatch)
    before = (tmp_path / "portal-read-status.json").read_bytes()
    with pytest.raises(ValueError, match="没有这门课"):
        recordings_api.sync_recording_index(settings, lambda stage: None, course_id="missing")
    assert calls == [("close", "")]
    assert (tmp_path / "portal-read-status.json").read_bytes() == before


def synchronous_manager(settings, monkeypatch):
    manager = recordings_api.RecordingWorkManager(settings)
    monkeypatch.setattr(recordings_api.threading, "Thread", lambda target, args, daemon:
                        SimpleNamespace(start=lambda: target(*args)))
    return manager


def test_manager_single_course_pipeline_never_projects_or_summarizes_other_course(tmp_path, monkeypatch):
    settings = configuration(tmp_path)
    confirmed_ledger(tmp_path, settings)
    manager = synchronous_manager(settings, monkeypatch)
    calls = []
    def confirmed_index(settings, stage, progress, course_id="", on_course_confirmed=None):
        course = Course(course_id=course_id, name="Selected fixture")
        folder = tmp_path / course.directory
        folder.mkdir()
        (folder / "course.json").write_text(json.dumps(course.model_dump()), "utf-8")
        envelope = {"revision": 1, "snapshot": catalog.build_snapshot(course, [], [], [], [])}
        catalog.save_cache(tmp_path, envelope)
        calls.append(("portal", course_id))
        if on_course_confirmed:
            on_course_confirmed(course_id, envelope)
    monkeypatch.setattr(recordings_api, "sync_recording_index", confirmed_index)
    monkeypatch.setattr(notion_home, "saved_home", lambda root: {"id": "fixture-home"})
    monkeypatch.setattr(notion_home, "sync_catalog",
                        lambda *args, **kwargs: calls.append(("notion", kwargs.get("course_id"))))
    monkeypatch.setattr(manager, "_course_ids", lambda: pytest.fail("Single refresh must not select every course"))
    monkeypatch.setattr(manager, "_summarize_courses",
                        lambda identifiers, **kwargs: calls.append(("summary", identifiers)))
    result = manager.start_sync(course_id="selected")
    assert calls == [("portal", "selected"), ("notion", "selected")]
    assert result["state"] == "done" and result["catalog_updated"] == 1
    assert result["catalog_incomplete"] == 0 and result["course_id"] == "selected"
    assert "这门课" in result["stage"]


@pytest.mark.parametrize("has_selected_status", [False, True])
def test_selected_unconfirmed_cannot_succeed_using_other_confirmed_course(tmp_path, monkeypatch, has_selected_status):
    settings = configuration(tmp_path)
    confirmed_ledger(tmp_path, settings)
    if has_selected_status:
        catalog.course_read_status(tmp_path, "selected", False, "account_unconfirmed")
    else:
        payload = json.loads((tmp_path / "portal-read-status.json").read_text("utf-8"))
        del payload["courses"]["selected"]
        (tmp_path / "portal-read-status.json").write_text(json.dumps(payload), "utf-8")
    manager = synchronous_manager(settings, monkeypatch)
    monkeypatch.setattr(recordings_api, "sync_recording_index", lambda *args, **kwargs: None)
    monkeypatch.setattr(manager, "_sync_notion_catalog", lambda *args: pytest.fail("Unconfirmed target must not project"))
    result = manager.start_sync(course_id="selected")
    assert result["state"] == "failed" and result["notion_synced"] is False
    assert result["catalog_updated"] == 0


def test_sync_route_accepts_selected_json_and_preserves_no_body_full_refresh(monkeypatch, tmp_path):
    manager = recordings_api.RecordingWorkManager(configuration(tmp_path))
    calls = []
    monkeypatch.setattr(manager, "start_sync", lambda course_id="": calls.append(course_id) or {"state": "running"})
    monkeypatch.setattr(manager, "start_background_sync", lambda resume=False: calls.append("") or {"state": "running", "scope": "background"})
    app = FastAPI()
    recordings_api.add_recording_routes(app, manager)
    client = TestClient(app)
    assert client.post("/api/recordings/sync", json={"course_id": "selected"}).status_code == 200
    assert client.post("/api/recordings/sync").status_code == 200
    assert client.post("/api/recordings/sync", json={"course_id": ""}).status_code == 200
    assert calls == ["selected", "", ""]
    assert client.post("/api/recordings/sync", json={"course_id": ["selected"]}).status_code == 422


def test_new_workspace_receives_exact_course_scope(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(notion_workspace, "saved_workspace", lambda root: {"id": "fixture"})
    monkeypatch.setattr(notion_workspace, "sync_workspace", lambda settings, **kwargs:
                        calls.append(kwargs) or {"courses": {"selected": {}}})
    notion_home.sync_catalog(tmp_path, "fixture", settings=configuration(tmp_path), course_id="selected")
    assert calls == [{"course_id": "selected"}]






def test_cancel_at_selected_notion_stage_prevents_summary_and_preserves_read_state(tmp_path, monkeypatch):
    settings = configuration(tmp_path)
    confirmed_ledger(tmp_path, settings)
    before = (tmp_path / "portal-read-status.json").read_bytes()
    manager = synchronous_manager(settings, monkeypatch)
    calls = []
    monkeypatch.setattr(recordings_api, "sync_recording_index", lambda *args, **kwargs: None)
    monkeypatch.setattr(notion_home, "saved_home", lambda root: {"id": "fixture"})
    def sync_catalog(root, token, stage, *args, **kwargs):
        manager.cancel_sync()
        stage("同步并验证新版 Notion 目录")
        calls.append("Notion continued after explicit cancellation")
    monkeypatch.setattr(notion_home, "sync_catalog", sync_catalog)
    monkeypatch.setattr(manager, "_summarize_courses", lambda *args, **kwargs: calls.append("summary"))
    result = manager.start_sync(course_id="selected")
    assert result["state"] == "cancelled" and result["notion_synced"] is False
    assert calls == []
    assert (tmp_path / "portal-read-status.json").read_bytes() == before


def test_selected_material_failure_keeps_its_previous_files_and_other_course_usable(tmp_path, monkeypatch):
    settings, calls, other_dir, original = portal_fixture(tmp_path, monkeypatch)
    selected = Course(course_id="selected", name="Selected fixture")
    selected_dir = tmp_path / selected.directory
    selected_dir.mkdir()
    old_course = {**selected.model_dump(), "synced_at": "2026-09-30T12:00:00+00:00"}
    (selected_dir / "course.json").write_text(json.dumps(old_course), "utf-8")
    index = selected_dir / "recordings"
    index.mkdir()
    (index / "index.json").write_bytes(b"[]")
    def failed_materials(*args):
        raise RuntimeError("fixture parser incomplete")
    monkeypatch.setattr(materials, "walk_materials", failed_materials)
    recordings_api.sync_recording_index(settings, lambda stage: None, course_id="selected")
    assert (index / "index.json").read_bytes() == b"[]"
    assert json.loads((selected_dir / "course.json").read_text("utf-8"))["synced_at"] == old_course["synced_at"]
    assert ("publish", "selected") not in calls
    assert {str(path.relative_to(other_dir)): path.read_bytes() for path in other_dir.iterdir()} == original
    catalog.assert_portal_readable(tmp_path, "other")
    with pytest.raises(catalog.CatalogUnavailable):
        catalog.assert_portal_readable(tmp_path, "selected")


def test_empty_sync_route_dispatches_background_without_foreground_full_refresh(tmp_path, monkeypatch):
    manager = recordings_api.RecordingWorkManager(configuration(tmp_path))
    calls = []
    monkeypatch.setattr(manager, "start_sync", lambda course_id="": calls.append("legacy-full") or {"state": "running"})
    monkeypatch.setattr(manager, "start_background_sync", lambda resume=False: calls.append("background") or {"state": "running", "scope": "background"}, raising=False)
    app = FastAPI()
    recordings_api.add_recording_routes(app, manager)
    with TestClient(app) as client:
        result = client.post("/api/recordings/sync")
    assert result.status_code == 200
    assert calls == ["background"]
    assert result.json()["scope"] == "background"


def wait_background(manager):
    import time
    until = time.monotonic() + 5
    while time.monotonic() < until:
        state = manager.background_state()
        if state["state"] != "running":
            return state
        time.sleep(.01)
    pytest.fail("isolated background fixture did not terminate")


def test_background_course_results_are_independent_and_resume_omits_done(tmp_path, monkeypatch):
    manager = recordings_api.RecordingWorkManager(configuration(tmp_path))
    monkeypatch.setattr(manager, "_background_course_ids", lambda stage: ["selected", "other"])
    calls = []
    def course(identifier):
        calls.append(identifier)
        return {"state": "failed" if identifier == "selected" and calls.count(identifier) == 1 else "done",
                "catalog_updated": int(identifier != "selected" or calls.count(identifier) > 1),
                "notion_synced": identifier != "selected" or calls.count(identifier) > 1,
                "error": "confirmed service snapshot unavailable" if identifier == "selected" and calls.count(identifier) == 1 else "",
                "failure_id": "fixture-red", "summary_created": 0, "summary_existing": 1,
                "result_urls": [], "warnings": []}
    monkeypatch.setattr(manager, "_run_background_course", course)
    manager.start_background_sync()
    result = wait_background(manager)
    assert calls == ["selected", "other"] and result["state"] == "partial"
    assert [row["state"] for row in result["course_results"]] == ["failed", "done"]
    assert manager.state()["state"] == "idle"
    manager.start_background_sync(resume=True)
    result = wait_background(manager)
    assert calls == ["selected", "other", "selected"]
    assert result["state"] == "done"
    assert [row["attempts"] for row in result["course_results"]] == [2, 1]
    assert [row["metadata_state"] for row in result["course_results"]] == ["confirmed", "confirmed"]


def test_background_cancellation_preserves_done_and_stops_unstarted_course(tmp_path, monkeypatch):
    import threading
    manager = recordings_api.RecordingWorkManager(configuration(tmp_path))
    monkeypatch.setattr(manager, "_background_course_ids", lambda stage: ["selected", "other"])
    entered, release = threading.Event(), threading.Event()
    calls = []
    def course(identifier):
        calls.append(identifier)
        entered.set()
        assert release.wait(5)
        return {"state": "done", "catalog_updated": 1, "notion_synced": True,
                "summary_created": 0, "summary_existing": 1, "warnings": [], "result_urls": []}
    monkeypatch.setattr(manager, "_run_background_course", course)
    manager.start_background_sync()
    assert entered.wait(5)
    manager.cancel_background_sync()
    release.set()
    result = wait_background(manager)
    assert result["state"] == "cancelled" and calls == ["selected"]
    assert [row["state"] for row in result["course_results"]] == ["done", "pending"]
    manager.start_background_sync(resume=True)
    assert wait_background(manager)["state"] == "done"
    assert calls == ["selected", "other"]


def test_shared_course_reservation_rejects_conflict_but_allows_another_course(tmp_path, monkeypatch):
    import threading
    manager = recordings_api.RecordingWorkManager(configuration(tmp_path))
    sibling = manager._background_worker()
    entered, release = threading.Event(), threading.Event()
    def run():
        entered.set()
        assert release.wait(5)
        return ""
    sibling._start("process", "recording", run, course_id="selected")
    assert entered.wait(5)
    with pytest.raises(ValueError, match="这门课"):
        manager._start("process", "another-recording", lambda: "", course_id="selected")
    manager._start("process", "other-recording", lambda: "", course_id="other")
    release.set()
    sibling._finished.wait(5)
    manager._finished.wait(5)
    assert manager.state()["state"] == "done"


def test_background_manual_retry_is_bounded_and_unknown_write_is_never_replayed(tmp_path, monkeypatch):
    manager = recordings_api.RecordingWorkManager(configuration(tmp_path))
    monkeypatch.setattr(manager, "_background_course_ids", lambda stage: ["selected"])
    calls = []
    monkeypatch.setattr(manager, "_run_background_course", lambda cid: calls.append(cid) or {"state": "failed", "error": "service refused"})
    manager.start_background_sync()
    assert wait_background(manager)["state"] == "partial"
    for _ in range(3):
        manager.start_background_sync(resume=True)
        wait_background(manager)
    assert calls == ["selected"] * 3
    assert manager.background_state()["course_results"][0]["retryable"] is False
    intent = tmp_path / ".notion-projection-intents" / "owner" / "unknown.json"
    intent.parent.mkdir(parents=True)
    intent.write_text(json.dumps({"binding": {"course_id": "selected"}, "pending": {"unknown": True}}))
    original = intent.read_bytes()
    manager.start_background_sync()
    result = wait_background(manager)
    assert len(calls) == 3 and result["course_results"][0]["retryable"] is False
    assert intent.read_bytes() == original


def test_background_account_change_stops_remaining_and_keeps_old_state_namespace(tmp_path, monkeypatch):
    settings = configuration(tmp_path)
    manager = recordings_api.RecordingWorkManager(settings)
    monkeypatch.setattr(manager, "_background_course_ids", lambda stage: ["selected", "other"])
    original_path = manager._background_path()
    calls = []
    def course(cid):
        calls.append(cid)
        settings.platform_token = "different-account"
        return {"state": "done", "catalog_updated": 1, "notion_synced": True}
    monkeypatch.setattr(manager, "_run_background_course", course)
    manager.start_background_sync()
    import time
    deadline = time.monotonic() + 5
    while manager._background["state"] == "running" and time.monotonic() < deadline:
        time.sleep(.01)
    assert manager._background["state"] == "failed"
    assert manager.state() == {"state": "idle"}
    assert calls == ["selected"] and original_path.exists()
    assert not manager._background_path().exists()
    with pytest.raises(ValueError):
        manager.start_background_sync(resume=True)


def test_disk_background_progress_is_read_only_and_resume_revalidates_courses(tmp_path, monkeypatch):
    settings = configuration(tmp_path)
    original = recordings_api.RecordingWorkManager(settings)
    monkeypatch.setattr(original, "_background_course_ids", lambda stage: ["selected"])
    monkeypatch.setattr(original, "_run_background_course", lambda cid: {"state": "done", "catalog_updated": 1, "notion_synced": True})
    original.start_background_sync()
    assert wait_background(original)["state"] == "done"
    restored = recordings_api.RecordingWorkManager(settings)
    cached = restored.state()["background_sync"]
    assert cached["state"] == "interrupted"
    assert cached["course_results"][0]["metadata_state"] == "not_read"
    calls = []
    monkeypatch.setattr(restored, "_run_background_course", lambda cid: calls.append(cid) or {"state": "failed", "error": "fresh check failed"})
    restored.start_background_sync(resume=True)
    assert wait_background(restored)["state"] == "partial"
    assert calls == ["selected"]
    switched = recordings_api.RecordingWorkManager(configuration(tmp_path, 'another-account'))
    assert switched.state() == {"state": "idle"}


def test_concurrent_course_folder_collision_refuses_without_student_mutation(tmp_path):
    from pku_sync.models import Course
    first = Course(course_id="first", name="Same course title")
    second = Course(course_id="second", name="Same course title")
    with recordings_api._reserve_course_directories(tmp_path, [first]) as chosen:
        with pytest.raises(ValueError):
            with recordings_api._reserve_course_directories(tmp_path, [second]):
                pytest.fail("concurrent courses were assigned the same folder")
        assert list(tmp_path.iterdir()) == []
    with recordings_api._reserve_course_directories(tmp_path, [second]) as retry:
        assert retry["second"] == chosen["first"]


def test_projection_wait_cancels_without_entering_writer(tmp_path):
    import threading
    manager = recordings_api.RecordingWorkManager(configuration(tmp_path))
    manager._writes.projection.acquire()
    manager._cancel_sync.set()
    with pytest.raises(ValueError):
        with manager._projection_stage():
            pytest.fail("cancelled waiter entered writer")
    assert manager._writes.projection.locked()
    manager._writes.projection.release()


def test_projection_cancellation_before_free_lock_never_enters_writer(tmp_path):
    manager = recordings_api.RecordingWorkManager(configuration(tmp_path))
    manager._cancel_sync.set()
    with pytest.raises(ValueError):
        with manager._projection_stage():
            pytest.fail("cancelled task entered writer")
    assert not manager._writes.projection.locked()
