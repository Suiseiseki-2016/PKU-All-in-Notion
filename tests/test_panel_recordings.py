"""Manual recording flow: no bulk AI call, explicit Notion target, safe retry."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pku_sync.models import Recording
from pku_sync.panel import recordings_api
from pku_sync.media import write_notes
from pku_sync.notion import markdown_to_blocks
from pku_sync import note_editor
from pku_sync.catalog_schema import fingerprint
from tests.retired_data_support import seed_old_home


def model_response(body, *, complete=True, uncertainties=None):
    return json.dumps({"note": body, "complete": complete,
                       "uncertainties": uncertainties or []}, ensure_ascii=False)


def save_reviewed_note(directory, cfg, body, transcript, *, sources=None,
                       complete=True, uncertainties=None, target=None):
    """Exercise the real review/cache protocol with an offline provider."""
    target = target or directory / "notes.md"
    assert not target.exists()
    (directory / "transcript.json").write_text(json.dumps(transcript), "utf-8")
    note_editor.write_notes(transcript, target, cfg, "第一讲",
                           source_context=sources,
                           ask=lambda *_: model_response(body, complete=complete,
                                                         uncertainties=uncertainties))
    return target


def test_markdown_converter_builds_native_note_blocks():
    sample = "## 课堂重点\n*提示*：$a=1$\n- **结论**：`AES`\n| 项目 | 说明 |\n| --- | --- |\n| A | 内容 |\n"
    rendered = markdown_to_blocks(sample)
    assert [block["type"] for block in rendered] == [
        "heading_2", "paragraph", "bulleted_list_item", "table"
    ]


@pytest.mark.parametrize("readback_change", ["none", "missing_block", "equation"])
def test_literal_markup_publishes_and_actual_readback_loss_fails(
        tmp_path, monkeypatch, readback_change):
    """Syntax examples are content; a remote write losing them is a real failure."""
    import copy
    from pku_sync import notion
    from pku_sync.panel import note_images, note_videos

    key, directory = setup_index(tmp_path)
    directory.mkdir()
    (directory / "notes.md").write_text(
        "# 语法示例\n\n正文中的代码 `$a=1$`、`**强调**` 和 `|` 应保留；公式为 $b=2$。", "utf-8")
    state = {"blocks": [], "archived": []}

    class Notion:
        def __init__(self, token): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def list_child_pages(self, page_id): return []
        def create_page(self, parent, title, *, children, retry):
            state["blocks"] = copy.deepcopy(children)
            if readback_change == "missing_block":
                state["blocks"].pop()
            elif readback_change == "equation":
                equation = next(part for part in state["blocks"][-1]["paragraph"]["rich_text"]
                                if part.get("type") == "equation")
                equation["equation"]["expression"] = "b=3"
            return {"id": "c" * 32, "url": "https://www.notion.so/new"}
        def list_children(self, page_id):
            return state["blocks"] if page_id == "c" * 32 else []
        def archive_page(self, page_id, *, retry):
            state["archived"].append(page_id)

    monkeypatch.setattr(notion, "NotionClient", Notion)
    monkeypatch.setattr(note_images, "publish_note_images", lambda *_, **kwargs: 0)
    monkeypatch.setattr(note_videos, "publish_note_videos", lambda *_, **kwargs: 0)
    job = recordings_api.find_job(tmp_path, key)
    if readback_change != "none":
        with pytest.raises(ValueError, match="回读内容与本次提交不一致"):
            recordings_api.publish_notes(job, "lecture-page", "notion")
        assert state["archived"] == ["c" * 32]
        assert not (directory / "published-note-version.json").exists()
    else:
        assert recordings_api.publish_notes(job, "lecture-page", "notion").endswith("new")
        assert state["archived"] == []
        assert (directory / "published-note-version.json").exists()
    assert "`$a=1$`" in (directory / "notes.md").read_text("utf-8")


def setup_index(root: Path) -> tuple[str, Path]:
    course = root / "测试课程"
    index = course / "recordings"
    index.mkdir(parents=True)
    (course / "course.json").write_text(json.dumps({
        "name": "测试课程", "course_id": "course-1",
        "assignments_status": "available", "synced_at": datetime.now(timezone.utc).isoformat(),
    }), "utf-8")
    recording = Recording(course_id="course-1", title="第一讲", recorded_at="2026-09-24 09:00:00",
                          play_url="https://campus.example/private?token=do-not-expose")
    (index / "index.json").write_text(json.dumps([recording.model_dump()]), "utf-8")
    job = recordings_api.collect_jobs(root)[0]
    return recordings_api.recording_id(job), job.directory


def test_term_review_requires_source_audio_and_preserves_original(tmp_path, monkeypatch):
    key, directory = setup_index(tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "transcript.json").write_text(json.dumps({"segments": [
        {"start": 2425.0, "end": 2437.0, "text": "老师讲了RAC算法和公钥的应用。"},
        {"start": 2437.0, "end": 2442.0, "text": "其他内容。"},
    ]}, ensure_ascii=False), "utf-8")
    (directory / "notes.md").write_text("旧笔记", "utf-8")
    app = FastAPI()
    manager = recordings_api.RecordingWorkManager(settings(tmp_path))
    recordings_api.add_recording_routes(app, manager)
    client = TestClient(app)
    route = f"/api/campus/courses/course-1/recordings/{key}/term-review"
    target = client.get(route).json()
    assert target["status"] == "needs_review"
    assert target["target"]["start_label"] == "40:25"
    assert target["target"]["video_url"] == f"/api/recordings/{key}/preview"
    payload = {"segment_index": 0, "transcript_sha256": target["transcript_sha256"],
               "confirmed_spoken_term": "RSA", "confirmed": True}
    assert client.post(route, json=payload).status_code == 409  # no source audio
    (directory / "video.mp4").write_bytes(b"cached-source-audio")
    preview = client.get(target["target"]["video_url"])
    assert preview.status_code == 200 and preview.content == b"cached-source-audio"
    assert preview.headers["content-disposition"].startswith("inline;")
    partial = client.get(target["target"]["video_url"], headers={"Range": "bytes=0-5"})
    assert partial.status_code == 206
    assert partial.headers["content-range"] == "bytes 0-5/19"
    assert partial.content == b"cached"
    assert client.post(route, json={**payload, "confirmed": False}).status_code == 409
    assert client.post(route, json={**payload, "transcript_sha256": "old"}).status_code == 409
    result = client.post(route, json=payload)
    assert result.status_code == 200
    assert result.json()["pending_regeneration"] is True
    assert "RSA算法" in (directory / "transcript.json").read_text("utf-8")
    assert "RAC算法" in next(directory.glob("transcript.before-review-*.json")).read_text("utf-8")
    assert (directory / "notes.md").read_text("utf-8") == "旧笔记"
    assert client.post(route, json=payload).status_code == 409
    assert client.get(route).json()["pending_regeneration"] is True
    assert client.get(route.replace("course-1", "wrong-course")).status_code == 404

    from pku_sync import lecture_source_download, note_sources, media
    monkeypatch.setattr(lecture_source_download, "prepare_lesson_sources",
                        lambda *args, **kwargs: ([], []))
    monkeypatch.setattr(note_sources, "collect_note_sources", lambda *args, **kwargs: [])
    def failed_notes(*args, **kwargs):
        raise RuntimeError("AI 服务不可用")
    monkeypatch.setattr(media, "write_notes", failed_notes)
    job = recordings_api.find_job(tmp_path, key)
    with pytest.raises(ValueError, match="新版笔记没有完成.*原笔记保留"):
        manager._process(job, "lecture-page", regenerate=False)
    assert (directory / "notes.md").read_text("utf-8") == "旧笔记"
    assert client.get(route).json()["pending_regeneration"] is True


def test_unrelated_rac_term_is_not_presented_as_rsa(tmp_path):
    key, directory = setup_index(tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "transcript.json").write_text(json.dumps({"segments": [
        {"start": 10, "end": 18, "text": "这里介绍RAC算法在别的领域的用法。"}
    ]}, ensure_ascii=False), "utf-8")
    (directory / "video.mp4").write_bytes(b"cached-source-audio")
    app = FastAPI()
    manager = recordings_api.RecordingWorkManager(settings(tmp_path))
    recordings_api.add_recording_routes(app, manager)
    client = TestClient(app)
    route = f"/api/campus/courses/course-1/recordings/{key}/term-review"
    status = client.get(route).json()
    assert status["status"] == "none"
    assert client.post(route, json={"segment_index": 0,
        "transcript_sha256": status["transcript_sha256"],
        "confirmed_spoken_term": "RSA", "confirmed": True}).status_code == 409


def settings(root: Path):
    return SimpleNamespace(data_dir=root, pku_username="student", pku_password="secret",
                           platform_token="session", notion_token="notion", openai_api_key="",
                           delete_video_after_processing=False, notes_model="test-model")


class Directory:
    def ensure_loaded(self):
        return SimpleNamespace(
            courses=[SimpleNamespace(id="course-page", title="测试课程")],
            lectures_by_course={"course-page": [SimpleNamespace(id="lecture-page", url="https://www.notion.so/lecture")]},
        )

def wait_done(manager):
    for _ in range(100):
        result = manager.state()
        if result["state"] == "idle" and result.get("background_sync", {}).get("state"):
            result = result["background_sync"]
        if result["state"] != "running":
            return result
        time.sleep(.01)
    raise AssertionError("task did not finish")


@pytest.mark.parametrize(("kind", "stage", "expected"), [
    ("sync", "登录教学网", "教学网连接中断，课程目录未完整更新"),
    ("sync", "建立 Notion 课程与讲次页", "同步到 Notion 失败"),
    ("catalog", "建立 Notion 课程与讲次页", "Notion 课程目录暂时无法更新"),
])
def test_catalog_transport_failure_names_the_failed_step(tmp_path, kind, stage, expected):
    manager = recordings_api.RecordingWorkManager(settings(tmp_path))
    manager._work = recordings_api.Work(kind=kind, stage=stage)
    def failed():
        raise RuntimeError("private transport error")
    manager._run(failed)
    result = manager.state()
    assert result["state"] == "failed"
    assert expected in result["error"]
    assert "private transport error" not in result["error"]
    assert "转写会保留" not in result["error"]
    assert len(result["failure_id"]) == 8


@pytest.mark.parametrize(("stage", "expected"), [
    ("准备 Notion 课程与讲次页", "写入 Notion 时中断"),
    ("下载录像", "读取教学网或录像时中断"),
    ("转写并生成笔记", "音频处理或转写时中断"),
    ("重新生成课堂笔记", "生成课堂笔记时中断"),
    ("提取课堂画面", "提取课堂画面时中断"),
    ("准备中", "任务在“准备中”阶段中断"),
])
def test_unexpected_process_failure_names_stage_and_keeps_details_private(
        tmp_path, stage, expected):
    manager = recordings_api.RecordingWorkManager(settings(tmp_path))
    manager._work = recordings_api.Work(kind="process", stage=stage)

    def failed():
        raise RuntimeError("private token and transport details")

    manager._run(failed)
    result = manager.state()
    assert result["state"] == "failed"
    assert expected in result["error"]
    assert "private token" not in result["error"]
    assert len(result["failure_id"]) == 8


def test_list_never_exposes_campus_media_url_or_secret(tmp_path):
    key, _ = setup_index(tmp_path)
    manager = recordings_api.RecordingWorkManager(settings(tmp_path))
    app = FastAPI()
    recordings_api.add_recording_routes(app, manager)
    result = TestClient(app).get("/api/recordings")
    assert result.status_code == 200
    assert result.json()["items"][0]["id"] == key
    assert "token=" not in result.text and "secret" not in result.text


def test_download_only_keeps_video_and_serves_a_safe_file(tmp_path, monkeypatch):
    key, directory = setup_index(tmp_path)
    cfg = settings(tmp_path)
    cfg.platform_token = ""
    cfg.notion_token = ""
    cfg.delete_video_after_processing = True
    calls = []

    class Session:
        def close(self):
            pass

    monkeypatch.setattr("pku_sync.auth.get_session", lambda **kwargs: Session())
    def fake_download(client, job, progress=None):
        calls.append("download")
        job.directory.mkdir(parents=True, exist_ok=True)
        job.video.write_bytes(b"video-bytes")
        return SimpleNamespace(path=job.video)
    monkeypatch.setattr(recordings_api, "download_job", fake_download)
    manager = recordings_api.RecordingWorkManager(cfg)
    app = FastAPI()
    recordings_api.add_recording_routes(app, manager)
    client = TestClient(app)
    assert client.get(f"/api/recordings/{key}/video").status_code == 404
    assert client.post(f"/api/recordings/{key}/download").status_code == 200
    assert wait_done(manager)["state"] == "done"
    assert calls == ["download"]
    assert (directory / ".keep-video").exists()
    response = client.get(f"/api/recordings/{key}/video")
    assert response.status_code == 200 and response.content == b"video-bytes"
    assert "attachment" in response.headers["content-disposition"]
    assert client.get("/api/recordings/unknown/video").status_code == 404
    assert client.get("/api/recordings").json()["items"][0]["video_available"] is True

    from pku_sync import pipeline
    monkeypatch.setattr(pipeline, "transcribe", lambda *args, **kwargs: {"segments": [{"start": 0, "text": "内容"}]})
    monkeypatch.setattr(pipeline, "write_notes", lambda transcript, frames, target, settings, title, **kwargs: target)
    result = pipeline.process_job(recordings_api.find_job(tmp_path, key), cfg, include_keyframes=False)
    assert result.notes and not result.removed_video
    assert (directory / "video.mp4").exists()




def test_selected_recording_generates_once_then_retry_only_publishes(tmp_path, monkeypatch):
    key, directory = setup_index(tmp_path)
    directory.mkdir()
    (directory / "video.mp4").write_bytes(b"fixture")
    calls = {"process": 0, "publish": 0}

    def fake_process(job, _settings, *, include_keyframes=True, progress=None, direct_oss=False, **kwargs):
        assert include_keyframes is True
        calls["process"] += 1
        save_reviewed_note(job.directory, _settings, "# 笔记\n- 内容", {"segments": [
            {"start": 0, "end": 10, "text": "内容"}
        ]})
        frames = job.directory / "keyframes"
        frames.mkdir()
        (frames / "frame_0000_00m00s.jpg").write_bytes(b"frame")
        (frames / "index.json").write_text(json.dumps({"keyframes": [
            {"file": "frame_0000_00m00s.jpg", "timestamp": 0, "time": "00:00"}
        ]}), "utf-8")
        return SimpleNamespace(notes=True)

    def fake_publish(job, lecture_id, token):
        calls["publish"] += 1
        assert lecture_id == "lecture-page" and token == "notion"
        return "https://www.notion.so/example"

    monkeypatch.setattr(recordings_api, "process_job", fake_process)
    monkeypatch.setattr(recordings_api, "publish_notes", fake_publish)
    manager = recordings_api.RecordingWorkManager(settings(tmp_path))
    for _ in (1, 2):
        manager._start("process", key, lambda: manager._process(recordings_api.find_job(tmp_path, key), "lecture-page"), course_id="course-1")
        assert wait_done(manager)["state"] == "done"
    assert calls == {"process": 1, "publish": 2}


def test_existing_unreviewed_note_stops_before_download_or_publication(tmp_path, monkeypatch):
    key, directory = setup_index(tmp_path)
    directory.mkdir()
    original = "# 第一讲\n\n## 00:00–02:00\n\n- 已有内容\n"
    (directory / "notes.md").write_text(original, "utf-8")
    (directory / "transcript.json").write_text(json.dumps({"segments": [
        {"start": 0, "end": 600, "text": "课堂内容"}
    ]}), "utf-8")
    monkeypatch.setattr(recordings_api, "publish_notes", lambda *args: pytest.fail("partial note published"))
    monkeypatch.setattr("pku_sync.auth.get_session", lambda **kwargs: pytest.fail("campus login started"))
    job = recordings_api.find_job(tmp_path, key)
    with pytest.raises(ValueError, match="尚无可读取的整理保存记录"):
        recordings_api.RecordingWorkManager(settings(tmp_path))._process(job, "lecture-page")
    assert (directory / "notes.md").read_text("utf-8") == original


def test_publish_gate_blocks_unsynced_assignments_before_notion_write(tmp_path, monkeypatch):
    key, directory = setup_index(tmp_path)
    directory.mkdir()
    (directory / "transcript.json").write_text(json.dumps({"segments": [
        {"start": 0, "end": 600, "text": "课堂内容"}
    ]}), "utf-8")
    (directory / "notes.md").write_text(
        "# 第一讲\n\n## 课堂内容\n\n课堂内容。\n\n*已整理至录像 10:00。*", "utf-8")
    from pku_sync.panel import campus_catalog
    from pku_sync import notion
    monkeypatch.setattr(campus_catalog, "campus_course", lambda *args: {
        "assignments_status": "partial"})
    monkeypatch.setattr(notion, "NotionClient", lambda *args: pytest.fail("Notion opened"))
    with pytest.raises(ValueError, match="正式作业目录尚未完整同步"):
        recordings_api.publish_notes(recordings_api.find_job(tmp_path, key),
                                     "lecture-page", "notion")


def test_publish_gate_preserves_legacy_note_without_review_record(tmp_path, monkeypatch):
    key, directory = setup_index(tmp_path)
    directory.mkdir()
    note = directory / "notes.md"
    original = ("# 第一讲\n\n## 课堂内容\n\n"
                "第四周是胚胎期的起始阶段。\n\n*已整理至录像 10:00。*")
    note.write_text(original, "utf-8")
    (directory / "transcript.json").write_text(json.dumps({"segments": [
        {"start": 0, "end": 600, "text": "胚胎期从第二周起"}
    ]}), "utf-8")
    from pku_sync.panel import campus_catalog
    from pku_sync import lecture_source_download, note_sources, notion
    monkeypatch.setattr(campus_catalog, "campus_course", lambda *args: {
        "assignments_status": "available", "synced_at": datetime.now(timezone.utc).isoformat()})
    monkeypatch.setattr(lecture_source_download, "prepare_lesson_sources",
                        lambda *args, **kwargs: ([], []))
    monkeypatch.setattr(note_sources, "collect_note_sources", lambda *args, **kwargs: [
        {"title": "Week 2.pdf", "locator": "第 40 页", "status": "readable",
         "text": "The EMBRYONIC STAGE 胚胎期 （2-8 weeks）"}
    ])
    monkeypatch.setattr(notion, "NotionClient", lambda *args: pytest.fail("Notion opened"))
    with pytest.raises(ValueError, match="尚无可读取的整理保存记录"):
        recordings_api.publish_notes(recordings_api.find_job(tmp_path, key),
                                     "lecture-page", "notion")
    assert note.read_text("utf-8") == original


def test_publish_gate_allows_no_matched_slides_when_assignments_are_synced(tmp_path, monkeypatch):
    key, directory = setup_index(tmp_path)
    directory.mkdir()
    note = directory / "notes.md"
    save_reviewed_note(directory, settings(tmp_path), "内容。", {"segments": [
        {"start": 0, "end": 600, "text": "内容"}
    ]})
    from pku_sync.panel import campus_catalog
    monkeypatch.setattr(campus_catalog, "campus_course", lambda *args: {
        "assignments_status": "available", "synced_at": datetime.now(timezone.utc).isoformat()})
    assert recordings_api.note_publish_issues(
        recordings_api.find_job(tmp_path, key), note) == []


def test_assignment_snapshot_must_be_recent_and_timezone_aware():
    now = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
    assert recordings_api.assignment_snapshot_issue({
        "synced_at": (now - timedelta(hours=23)).isoformat()}, now=now) is None
    assert "超过 24 小时" in recordings_api.assignment_snapshot_issue({
        "synced_at": (now - timedelta(hours=25)).isoformat()}, now=now)
    assert "缺少可核对" in recordings_api.assignment_snapshot_issue({}, now=now)
    assert "缺少时区" in recordings_api.assignment_snapshot_issue({
        "synced_at": "2026-09-29T11:00:00"}, now=now)
    assert "晚于本机" in recordings_api.assignment_snapshot_issue({
        "synced_at": (now + timedelta(minutes=6)).isoformat()}, now=now)


def test_publish_gate_rejects_stale_assignment_snapshot(tmp_path, monkeypatch):
    key, directory = setup_index(tmp_path)
    directory.mkdir()
    note = directory / "notes.md"
    note.write_text("# 第一讲\n\n## 课堂内容\n\n概念讲解。\n\n*已整理至录像 10:00。*", "utf-8")
    (directory / "transcript.json").write_text(json.dumps({"segments": [
        {"start": 0, "end": 600, "text": "概念讲解"}
    ]}), "utf-8")
    from pku_sync.panel import campus_catalog
    monkeypatch.setattr(campus_catalog, "campus_course", lambda *args: {
        "assignments_status": "available",
        "synced_at": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()})
    assert "超过 24 小时" in "；".join(recordings_api.note_publish_issues(
        recordings_api.find_job(tmp_path, key), note))


def test_publish_gate_preserves_contextual_uncertainty_without_rac_keyword_rejection(tmp_path):
    key, directory = setup_index(tmp_path)
    directory.mkdir()
    note = directory / "notes.md"
    save_reviewed_note(directory, settings(tmp_path), "课件列出 RSA 公钥算法，老师说的术语需回看。", {"segments": [
        {"start": 0, "end": 600, "text": "三位作者设计了公钥算法，rac算法。"}
    ]}, complete=False, uncertainties=["转写中的 RAC 是否指 RSA，需回看原音。"])
    original = note.read_bytes()
    assert recordings_api.note_publish_issues(recordings_api.find_job(tmp_path, key), note) == []
    assert note.read_bytes() == original
    assert json.loads(note_editor.report_path(note).read_text("utf-8"))["content_complete"] is False


def test_stale_catalog_is_refreshed_instead_of_blocking_the_note(tmp_path, monkeypatch):
    """A snapshot older than 24h must be re-read, not turned into a dead end."""
    key, directory = setup_index(tmp_path)
    metadata_path = directory.parents[1] / "course.json"
    metadata = json.loads(metadata_path.read_text("utf-8"))
    metadata["synced_at"] = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    metadata_path.write_text(json.dumps(metadata), "utf-8")
    (tmp_path / "notion-workspace-v2.json").write_text(json.dumps({
        "schema": 2, "active": True, "account_namespace": fingerprint({"session": settings(tmp_path).platform_token}), "id": "home", "parent_id": "parent", "url": "https://notion.so/home"
    }), "utf-8")
    from pku_sync.panel import notion_home
    refreshed = []

    def fake_refresh(_settings, stage, progress, course_id=""):
        assert course_id == "course-1"
        refreshed.append(course_id)
        stage("读取教学网课程")
        metadata["synced_at"] = datetime.now(timezone.utc).isoformat()
        metadata_path.write_text(json.dumps(metadata), "utf-8")

    monkeypatch.setattr(recordings_api, "sync_recording_index", fake_refresh)
    monkeypatch.setattr(notion_home, "ensure_recording_target", lambda *args, **kwargs: "lecture-1")
    manager = recordings_api.RecordingWorkManager(settings(tmp_path))
    monkeypatch.setattr(manager, "_process", lambda *args, **kwargs: "https://notion.so/note")
    started = manager.start_campus_process(key, "course-1")
    assert started["state"] == "running"
    done = wait_done(manager)
    assert refreshed == ["course-1"]
    assert done["state"] == "done" and done["result_url"] == "https://notion.so/note"
    assert "已过期" in done["stage"] or done["stage"] == "已发布到 Notion"


def test_stale_catalog_without_campus_accounts_still_reports_the_gate(tmp_path, monkeypatch):
    key, directory = setup_index(tmp_path)
    metadata_path = directory.parents[1] / "course.json"
    metadata = json.loads(metadata_path.read_text("utf-8"))
    metadata["synced_at"] = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    metadata_path.write_text(json.dumps(metadata), "utf-8")
    (tmp_path / "notion-workspace-v2.json").write_text(json.dumps({
        "schema": 2, "active": True, "account_namespace": fingerprint({"session": settings(tmp_path).platform_token}), "id": "home", "parent_id": "parent", "url": "https://notion.so/home"
    }), "utf-8")
    from pku_sync.panel import notion_home
    monkeypatch.setattr(notion_home, "ensure_recording_target",
                        lambda *args: pytest.fail("Notion opened without a fresh snapshot"))
    monkeypatch.setattr(recordings_api, "sync_recording_index",
                        lambda *args, **kwargs: pytest.fail("campus read without credentials"))
    no_credentials = SimpleNamespace(**{**vars(settings(tmp_path)),
                                        "pku_username": "", "pku_password": ""})
    manager = recordings_api.RecordingWorkManager(no_credentials)
    monkeypatch.setattr(manager, "_process", lambda *args, **kwargs: pytest.fail("AI processing started"))
    with pytest.raises(ValueError, match="超过 24 小时"):
        manager.start_campus_process(key, "course-1")
    assert manager.state() == {"state": "idle"}


def test_publish_existing_skips_campus_refresh_and_credentials(tmp_path, monkeypatch):
    key, directory = setup_index(tmp_path)
    directory.mkdir()
    (directory / "transcript.json").write_text('{"segments": []}', "utf-8")
    (directory / "notes.md").write_text("# 已有笔记", "utf-8")
    (directory / "keyframes").mkdir()
    (directory / "keyframes" / "index.json").write_text(
        '{"keyframes": [{"file": "frame.jpg", "timestamp": 0}]}', "utf-8")
    (tmp_path / "notion-workspace-v2.json").write_text(json.dumps({
        "schema": 2, "active": True, "account_namespace": fingerprint({"session": settings(tmp_path).platform_token}), "id": "home", "parent_id": "parent", "url": "https://notion.so/home"
    }), "utf-8")
    no_campus = SimpleNamespace(**{
        **vars(settings(tmp_path)), "pku_username": "", "pku_password": ""
    })
    from pku_sync.panel import notion_home
    monkeypatch.setattr(notion_home, "ensure_recording_target", lambda *args, **kwargs: "lecture-1")
    manager = recordings_api.RecordingWorkManager(no_campus)
    monkeypatch.setattr(
        manager,
        "_refresh_stale_catalog",
        lambda *_: pytest.fail("campus catalog was refreshed"),
    )
    monkeypatch.setattr(
        manager,
        "_process",
        lambda _job, _lecture, **kwargs: (
            "https://notion.so/note" if kwargs["reuse_existing"]
            else pytest.fail("reuse_existing was not forwarded")
        ),
    )

    started = manager.start_campus_process(
        key, "course-1", reuse_existing=True
    )
    assert started["state"] in {"running", "done"}
    done = wait_done(manager)
    assert done["state"] == "done"
    assert done["result_url"] == "https://notion.so/note"


def test_failed_catalog_refresh_stops_with_a_clear_reason(tmp_path, monkeypatch):
    key, directory = setup_index(tmp_path)
    metadata_path = directory.parents[1] / "course.json"
    metadata = json.loads(metadata_path.read_text("utf-8"))
    metadata["synced_at"] = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    metadata_path.write_text(json.dumps(metadata), "utf-8")
    (tmp_path / "notion-workspace-v2.json").write_text(json.dumps({
        "schema": 2, "active": True, "account_namespace": fingerprint({"session": settings(tmp_path).platform_token}), "id": "home", "parent_id": "parent", "url": "https://notion.so/home"
    }), "utf-8")
    from pku_sync.panel import notion_home
    monkeypatch.setattr(notion_home, "ensure_recording_target",
                        lambda *args: pytest.fail("Notion opened after a failed refresh"))
    monkeypatch.setattr(recordings_api, "sync_recording_index",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("IAAA login timed out")))
    manager = recordings_api.RecordingWorkManager(settings(tmp_path))
    monkeypatch.setattr(manager, "_process", lambda *args, **kwargs: pytest.fail("AI processing started"))
    manager.start_campus_process(key, "course-1")
    done = wait_done(manager)
    assert done["state"] == "failed"
    assert "教学网目录更新失败" in done["error"]
    assert "IAAA login timed out" not in done["error"]


@pytest.mark.parametrize("body", [
    "## 0:00–10:00\n\n概念列表。",
    "老师举例说‘本课程没有作业’，用它说明否定命题。",
])
def test_publish_gate_does_not_classify_reviewed_prose_by_headings_or_negative_phrases(
        tmp_path, monkeypatch, body):
    key, directory = setup_index(tmp_path)
    directory.mkdir()
    note = directory / "notes.md"
    save_reviewed_note(directory, settings(tmp_path), body, {"segments": [
        {"start": 0, "end": 600, "text": "老师举例讲解概念与否定命题。"}
    ]})
    from pku_sync.panel import campus_catalog
    monkeypatch.setattr(campus_catalog, "campus_course", lambda *args: {
        "assignments_status": "available", "synced_at": datetime.now(timezone.utc).isoformat()})
    assert recordings_api.note_publish_issues(recordings_api.find_job(tmp_path, key), note) == []


def test_existing_note_without_transcript_cannot_use_student_publish_flow(tmp_path, monkeypatch):
    key, directory = setup_index(tmp_path)
    directory.mkdir()
    (directory / "notes.md").write_text("# 旧笔记", "utf-8")
    monkeypatch.setattr(recordings_api, "publish_notes", lambda *args: pytest.fail("published"))
    with pytest.raises(ValueError, match="缺少这节课的转写"):
        recordings_api.RecordingWorkManager(settings(tmp_path))._process(
            recordings_api.find_job(tmp_path, key), "lecture-page")


@pytest.mark.parametrize("saved_strategy", [None, "transcript", "topics"])
def test_pipeline_passes_date_matched_sources_to_note_writer(tmp_path, monkeypatch, saved_strategy):
    key, directory = setup_index(tmp_path)
    directory.mkdir()
    (directory / "video.mp4").write_bytes(b"fixture")
    from pku_sync import pipeline, media, transcript_notes
    observed = {}
    transcript={"segments": [{"start": 0, "end": 10, "text": "课堂内容"}]}
    sources=[{"title": "9月24日课件.pdf", "status": "readable", "text": "概念"}]
    cfg=settings(tmp_path); cfg.platform_token="offline"; cfg.notes_model="qwen3.5-flash"
    cfg.delete_video_after_processing=False
    model_calls=[]
    def fake_model(*args):
        from pku_sync import knowledge_workflow
        model_calls.append(args)
        if args[3]==knowledge_workflow.PLAN_SYSTEM:
            return json.dumps({'units':[{'title':'本讲','first_segment':0,
                'reference_segments':[],'courseware_indices':[0]}]})
        if args[3]==knowledge_workflow.MERGE_SYSTEM:
            return json.dumps({'sections':[{'title':'本讲','first_unit':0,
                'last_unit':0,'body':'课堂整理正文。'}]})
        return '课堂整理正文。'
    monkeypatch.setattr(media,'_recovery_llm',fake_model)
    monkeypatch.setattr(pipeline, "transcribe", lambda *args, **kwargs: transcript)
    if saved_strategy:
        media.write_notes(transcript,[],directory/"notes.md",cfg,"第一讲",
                          strategy=saved_strategy,source_context=sources)
    saved={path:path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    first_calls=len(model_calls)
    monkeypatch.setattr(pipeline, "collect_note_sources", lambda course_dir, day, title, **kwargs: (
        observed.update({"course_dir": course_dir, "day": day, "title": title})
        or sources
    ))
    result = pipeline.process_job(recordings_api.find_job(tmp_path, key), cfg, include_keyframes=False)
    assert result.notes and result.errors==[]
    if saved_strategy:
        assert observed=={} and len(model_calls)==first_calls
        assert all(path.read_bytes()==raw for path,raw in saved.items())
    else:
        assert observed["day"] == "2026-09-24" and len(model_calls)==1
    assert note_editor.review_record_issues(directory/"notes.md",transcript,sources)==[]
    assert json.loads(transcript_notes.report_path(directory/"notes.md").read_text("utf-8"))["sources_sha256"]==note_editor.fingerprint(sources)


@pytest.mark.parametrize("transcribed,errors,expected", [
    (False, ["transcribe: cloud transcription rejected the upload (HTTP 402): 转写额度不足"], "转写没有完成"),
    (True, ["notes: 第 2 段笔记生成失败：云端服务暂时不可用"], "转写已完成，但笔记未完成"),
])
def test_recording_error_identifies_failed_stage(tmp_path, monkeypatch, transcribed, errors, expected):
    key, directory = setup_index(tmp_path)
    directory.mkdir()
    (directory / "video.mp4").write_bytes(b"fixture")
    monkeypatch.setattr(recordings_api, "process_job",
                        lambda job, cfg, **kwargs: SimpleNamespace(notes=False, transcribed=transcribed, errors=errors))
    manager = recordings_api.RecordingWorkManager(settings(tmp_path))
    manager._start("process", key, lambda: manager._process(recordings_api.find_job(tmp_path, key), "lecture-page"), course_id="course-1")
    state = wait_done(manager)
    assert state["state"] == "failed"
    assert expected in state["error"]
    assert "转写" in state["error"]


def test_publish_existing_results_never_redownloads_video_for_optional_clips(
        tmp_path, monkeypatch):
    key, directory = setup_index(tmp_path)
    directory.mkdir()
    save_reviewed_note(directory, settings(tmp_path), "课堂内容。", {
        "segments": [{"start": 0, "end": 10, "text": "课堂内容"}]
    })
    (directory / "keyframes").mkdir()
    (directory / "keyframes" / "index.json").write_text(
        json.dumps({"keyframes": [{"timestamp": 1, "file": "frame.jpg"}]}), "utf-8")
    (tmp_path / "notion-workspace-v2.json").write_text(json.dumps({
        "schema": 2, "active": True, "account_namespace": fingerprint({"session": settings(tmp_path).platform_token}), "id": "home", "parent_id": "parent", "url": "https://notion.so/home"
    }), "utf-8")

    monkeypatch.setattr("pku_sync.panel.note_videos.needs_video_clips", lambda *_: True)
    monkeypatch.setattr(recordings_api, "download_job",
                        lambda *_args, **_kwargs: pytest.fail("video was redownloaded"))
    monkeypatch.setattr(recordings_api, "publish_notes", lambda *_args, **_kwargs: "https://notion.so/note")
    manager = recordings_api.RecordingWorkManager(settings(tmp_path))
    job = recordings_api.find_job(tmp_path, key)

    result = manager._process(job, "lecture-page", reuse_existing=True)
    assert result == "https://notion.so/note"
    assert not job.video.exists()


def test_cloud_notes_use_relay_without_local_ai_key(tmp_path, monkeypatch):
    from pku_sync import platform
    settings_obj = settings(tmp_path)
    seen = []

    def fake_llm(operation, prompt, _, *, system):
        seen.append((operation, prompt, system))
        return {"content": "- 考试重点：数据结构", "points_charged": .1}

    monkeypatch.setattr(platform, "llm", fake_llm)
    note = tmp_path / "notes.md"
    transcript = {"segments": [{"start": 0, "end": 10, "text": "数据结构会考"}]}
    assert write_notes(transcript, [], note, settings_obj, "第一讲") == note
    assert "考试重点" in note.read_text("utf-8")
    assert seen[0][0] == "notes" and "数据结构会考" in seen[0][1]
    assert len(seen) == 1 and "文字整理" in seen[0][2]

    note.unlink()
    monkeypatch.setattr(platform, "llm", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("vendor failed")))
    assert write_notes(transcript, [], note, settings_obj, "第一讲") == note
    assert "考试重点" in note.read_text("utf-8")
    note.unlink()
    transcript["segments"][0]["text"] = "新的课堂内容"
    from pku_sync.note_editor import report_path
    receipt = report_path(note).read_bytes()
    with pytest.raises(RuntimeError, match="原文件保留"):
        write_notes(transcript, [], note, settings_obj, "第一讲")
    assert not note.exists() and report_path(note).read_bytes() == receipt
    # A deliberate regeneration stages a new output. Deleting the failed
    # commit must not silently reinterpret its old paid receipt as new work.
    fresh_note = tmp_path / "regenerated.md"
    with pytest.raises(RuntimeError, match="vendor failed"):
        write_notes(transcript, [], fresh_note, settings_obj, "第一讲")
    assert not note.exists()
    assert not fresh_note.exists() and report_path(note).read_bytes() == receipt


def test_cloud_note_retry_reuses_completed_windows(tmp_path, monkeypatch):
    from pku_sync import platform
    settings_obj = settings(tmp_path)
    transcript = {"segments": [
        {"start": 0, "end": 10, "text": "第一段"},
        {"start": 481, "end": 490, "text": "第二段"},
    ]}
    calls = []
    fail_second = {True}

    def fake_llm(operation, prompt, cfg, *, system):
        payload = json.loads(prompt)
        calls.append(payload)
        if payload["part"] == 2 and fail_second:
            raise platform.PlatformError("云端服务暂时不可用")
        return {"content": "- 整理：" + ("第二段" if payload["part"] == 2 else "第一段"),
                "points_charged": 0.1}

    monkeypatch.setattr(platform, "llm", fake_llm)
    note = tmp_path / "notes.md"
    with pytest.raises(platform.PlatformError, match="云端服务暂时不可用"):
        write_notes(transcript, [], note, settings_obj, "课程")
    assert not note.exists()
    fail_second.clear()
    calls.clear()
    write_notes(transcript, [], note, settings_obj, "课程")
    assert len(calls) == 1 and all(call["part"] == 2 for call in calls)
    assert "第一段" in note.read_text("utf-8")
    assert "第二段" in note.read_text("utf-8")


def test_notion_child_page_is_reused_instead_of_duplicated(tmp_path, monkeypatch):
    key, directory = setup_index(tmp_path)
    directory.mkdir()
    (directory / "notes.md").write_text("# 第一讲\n- 重点", "utf-8")
    job = recordings_api.find_job(tmp_path, key)
    state = {"children": [], "created": 0, "blocks": []}

    class Notion:
        def __init__(self, token): assert token == "notion"
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def list_child_pages(self, page_id):
            assert page_id == "lecture-page"
            return state["children"]
        def list_children(self, page_id):
            if page_id == "lecture-page":
                return []
            assert page_id == "a" * 32
            return state["blocks"]
        def create_page(self, page_id, title, *, children, retry):
            assert children and retry is False
            state["created"] += 1
            state["blocks"] = children
            state["children"].append({"id": "a" * 32, "title": title})
            return {"id": "a" * 32, "url": "https://www.notion.so/" + "a" * 32}

    from pku_sync import notion
    from pku_sync.panel import note_images
    image_targets = []
    monkeypatch.setattr(note_images, "publish_note_images",
                        lambda client, note_id, root, **kwargs: image_targets.append(note_id) or 0)
    monkeypatch.setattr(notion, "NotionClient", Notion)
    first = recordings_api.publish_notes(job, "lecture-page", "notion")
    second = recordings_api.publish_notes(job, "lecture-page", "notion")
    assert first == second
    assert state["created"] == 1
    assert image_targets == ["a" * 32, "a" * 32]
    from pku_sync.panel.summary_acceptance import document_fingerprint, status
    version = json.loads((directory / "published-note-version.json").read_text("utf-8"))
    assert version["note_id"] == "a" * 32
    assert version["lecture_id"] == "lecture-page"
    assert version["notes_sha256"] == status(job)["notes_sha256"]
    assert version["remote_sha256"] == document_fingerprint(Notion("notion"), "a" * 32)


@pytest.mark.parametrize("missing_body", [False, True])
def test_inserted_classroom_media_does_not_break_body_readback_or_page_reuse(
        tmp_path, monkeypatch, missing_body):
    import copy
    from pku_sync import notion
    from pku_sync.panel import note_images, note_videos
    key, directory = setup_index(tmp_path)
    directory.mkdir()
    (directory / "notes.md").write_text("# 第一讲\n\n课堂正文。", "utf-8")
    state = {"blocks": [], "pages": [], "created": 0, "archived": []}
    class Notion:
        def __init__(self, token): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def list_child_pages(self, page_id): return state["pages"]
        def list_children(self, page_id): return state["blocks"] if page_id == "a" * 32 else []
        def create_page(self, parent, title, *, children, retry):
            state["blocks"] = copy.deepcopy(children)
            state["pages"] = [{"id": "a" * 32, "title": title}]
            state["created"] += 1
            return {"id": "a" * 32, "url": "https://www.notion.so/" + "a" * 32}
        def archive_page(self, page_id, *, retry): state["archived"].append(page_id)
    def insert_media(*_, **kwargs):
        if not any(block["type"] == "image" for block in state["blocks"]):
            state["blocks"].insert(1, {"type": "image", "image": {"caption": [
                {"type": "text", "text": {"content": "课堂画面 00:20 · 来自课程录像"}}]}})
            if missing_body: state["blocks"].pop()
    monkeypatch.setattr(notion, "NotionClient", Notion)
    monkeypatch.setattr(note_images, "publish_note_images", insert_media)
    monkeypatch.setattr(note_videos, "publish_note_videos", lambda *_, **kwargs: 0)
    job = recordings_api.find_job(tmp_path, key)
    if missing_body:
        with pytest.raises(ValueError, match="回读内容与本次提交不一致"):
            recordings_api.publish_notes(job, "lecture-page", "notion")
        assert state["archived"] == ["a" * 32]
    else:
        first = recordings_api.publish_notes(job, "lecture-page", "notion")
        assert recordings_api.publish_notes(job, "lecture-page", "notion") == first
        assert state["created"] == 1 and state["archived"] == []


@pytest.mark.parametrize("change", ["none", "due", "link", "body"])
def test_existing_note_assignment_snapshot_revision_preserves_annotations(
        tmp_path, monkeypatch, change):
    key, directory = setup_index(tmp_path)
    directory.mkdir()
    (directory / "notes.md").write_text(
        "# 课堂笔记\n\n- 课件可对照本讲原件\n- 内容", "utf-8")
    job = recordings_api.find_job(tmp_path, key)
    from pku_sync import notion
    from pku_sync.panel import note_images

    source = [
        recordings_api._plain_note_block("本课程近期正式作业", kind="heading_2"),
        recordings_api._plain_note_block("截止：10 月 1 日"),
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
            {"type": "text", "text": {"content": "提交入口", "link": {"url": "https://example.edu/new"}}}
        ]}},
    ]
    monkeypatch.setattr(recordings_api, "formal_assignment_blocks", lambda _: source)
    old = [
        {"type": "heading_2", "heading_2": {"rich_text": [
            {"type": "text", "plain_text": "本课程近期", "text": {"content": "本课程近期"}},
            {"type": "text", "plain_text": "正式作业", "text": {"content": "正式作业"}},
        ]}},
        {"type": "paragraph", "paragraph": {"rich_text": [
            {"type": "text", "plain_text": "截止：10 月 1 日"}
        ]}},
        {"type": "paragraph", "paragraph": {"rich_text": [
            {"type": "text", "plain_text": "提交入口",
             "text": {"link": {"url": "https://example.edu/new"}}}
        ]}},
    ]
    old.append(recordings_api._plain_note_block(
        "回看提示：请结合本讲原录像核对待核事项；课件不能替代老师的实际讲述。"))
    old.extend(notion.markdown_to_blocks((directory / "notes.md").read_text("utf-8")))
    old.append(recordings_api._plain_note_block("我的批注：保留这段"))
    if change == "due":
        old[1]["paragraph"]["rich_text"][0]["plain_text"] = "截止：9 月 30 日"
    elif change == "link":
        old[2]["paragraph"]["rich_text"][0]["text"]["link"]["url"] = "https://example.edu/old"
    elif change == "body":
        old[len(source) + 1]["heading_1"]["rich_text"][0]["text"]["content"] = "旧版课堂笔记"

    created = []
    pages = [{"id": "a" * 32,
              "title": f"转写笔记 · {job.recording.date} {job.recording.title}"[:100]}]
    class Notion:
        def __init__(self, token): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def list_child_pages(self, page_id):
            return pages
        def list_children(self, page_id):
            if page_id == "lecture-page":
                return []
            return old if page_id == "a" * 32 else created[0][1]
        def create_page(self, parent, title, *, children, retry):
            created.append((title, children))
            pages.append({"id": "b" * 32, "title": title})
            return {"id": "b" * 32, "url": "https://www.notion.so/new"}
        def archive_page(self, *args, **kwargs):
            pytest.fail("existing annotated note must not be archived")

    monkeypatch.setattr(notion, "NotionClient", Notion)
    monkeypatch.setattr(note_images, "publish_note_images", lambda *args, **kwargs: 0)
    url = recordings_api.publish_notes(job, "lecture-page", "notion")
    if change == "none":
        assert not created
        assert url.endswith("a" * 32)
    else:
        assert len(created) == 1
        assert created[0][0].startswith("更新笔记 · ")
        assert created[0][1][:len(source)] == source
        assert url.endswith("new")
        again = recordings_api.publish_notes(job, "lecture-page", "notion")
        assert again.endswith("b" * 32)
        assert len(created) == 1
    assert old[-1]["paragraph"]["rich_text"][0]["text"]["content"] == "我的批注：保留这段"


def test_regeneration_keeps_old_note_on_generation_or_publish_failure(tmp_path, monkeypatch):
    key, directory = setup_index(tmp_path)
    directory.mkdir()
    old = "# 原有笔记\n\n*已整理至录像 0:10。*"
    (directory / "notes.md").write_text(old, "utf-8")
    (directory / "transcript.json").write_text(json.dumps({"segments": [
        {"start": 0, "end": 10, "text": "讲义内容"}
    ]}), "utf-8")
    from pku_sync import lecture_source_download, note_sources, media
    monkeypatch.setattr(lecture_source_download, "prepare_lesson_sources",
                        lambda *args, **kwargs: ([], []))
    monkeypatch.setattr(note_sources, "collect_note_sources", lambda *args, **kwargs: [])
    def failed_notes(*args, **kwargs):
        raise RuntimeError("AI 服务不可用")
    monkeypatch.setattr(media, "write_notes", failed_notes)
    job = recordings_api.find_job(tmp_path, key)
    with pytest.raises(ValueError, match="新版笔记没有完成.*原笔记保留"):
        recordings_api.RecordingWorkManager(settings(tmp_path))._process(
            job, "lecture-page", regenerate=True)
    assert (directory / "notes.md").read_text("utf-8") == old
    assert not list(directory.glob("notes.rebuild-*.md"))

    def fresh_notes(transcript, frames, target, config, title, **kwargs):
        return save_reviewed_note(target.parent, config, "# 新版笔记", transcript, target=target)
    monkeypatch.setattr(media, "write_notes", fresh_notes)
    monkeypatch.setattr(recordings_api, "publish_notes",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Notion 不可用")))
    with pytest.raises(ValueError, match="原笔记及旧页面均已保留"):
        recordings_api.RecordingWorkManager(settings(tmp_path))._process(
            job, "lecture-page", regenerate=True)
    assert (directory / "notes.md").read_text("utf-8") == old
    assert not list(directory.glob("notes.rebuild-*.md"))

    monkeypatch.setattr(recordings_api, "publish_notes", lambda *args, **kwargs: (
        _ for _ in ()).throw(ValueError(
            "笔记暂不能作为完整课程笔记发布：教学网正式作业目录尚未完整同步；请刷新课程目录后再发布。")))
    with pytest.raises(ValueError, match="正式作业目录尚未完整同步.*原笔记及旧页面均已保留"):
        recordings_api.RecordingWorkManager(settings(tmp_path))._process(
            job, "lecture-page", regenerate=True)
    assert (directory / "notes.md").read_text("utf-8") == old
    assert not list(directory.glob("notes.rebuild-*.md"))


@pytest.mark.parametrize('notes_strategy', [None, 'topics'])
def test_regeneration_publishes_new_note_then_keeps_local_backup(tmp_path, monkeypatch, notes_strategy):
    key, directory = setup_index(tmp_path)
    directory.mkdir()
    old = "# 原有笔记\n\n*已整理至录像 0:10。*"
    (directory / "notes.md").write_text(old, "utf-8")
    (directory / "transcript.json").write_text(json.dumps({"segments": [
        {"start": 0, "end": 10, "text": "课堂内容"}
    ]}), "utf-8")
    from pku_sync import lecture_source_download, note_sources, media
    monkeypatch.setattr(lecture_source_download, "prepare_lesson_sources",
                        lambda *args, **kwargs: ([], []))
    monkeypatch.setattr(note_sources, "collect_note_sources", lambda *args, **kwargs: [])
    def fresh_notes(transcript, frames, target, config, title, **kwargs):
        assert getattr(config,'notes_strategy','transcript') == (notes_strategy or 'transcript')
        return save_reviewed_note(target.parent, config, "# 更好的笔记", transcript, target=target)
    monkeypatch.setattr(media, "write_notes", fresh_notes)
    seen = []
    def publish(job, lecture_id, token, **kwargs):
        seen.append((kwargs["revision"], kwargs["note_path"].read_text("utf-8")))
        assert (directory / "notes.md").read_text("utf-8") == old
        return "https://www.notion.so/new-note"
    monkeypatch.setattr(recordings_api, "publish_notes", publish)
    from pku_sync.panel import notion_home
    monkeypatch.setattr(notion_home, "saved_home", lambda root: {"id": "home"})
    shared_settings = settings(tmp_path)
    url = recordings_api.RecordingWorkManager(shared_settings)._process(
        recordings_api.find_job(tmp_path, key), "a" * 32, regenerate=True, notes_strategy=notes_strategy)
    assert getattr(shared_settings,'notes_strategy','transcript') == 'transcript'
    assert url.endswith("new-note")
    assert "更好的笔记" in (directory / "notes.md").read_text("utf-8")
    assert [path.read_text("utf-8") for path in directory.glob("notes.previous-*.md")] == [old]
    assert seen == [(True, (directory / "notes.md").read_text("utf-8"))]
    assert note_editor.review_record_issues(directory / "notes.md", json.loads(
        (directory / "transcript.json").read_text("utf-8"))) == []


@pytest.mark.parametrize("failure_stage", ["note", "publication-marker"])
def test_failed_local_rebuild_commit_never_deletes_original_recording(
        tmp_path, monkeypatch, failure_stage):
    from pku_sync import lecture_source_download, note_sources, media
    from pku_sync.panel import note_videos
    key, directory = setup_index(tmp_path)
    directory.mkdir()
    old = "原笔记必须保留。"
    (directory / "notes.md").write_text(old, "utf-8")
    (directory / "transcript.json").write_text(json.dumps({"segments": [
        {"start": 0, "end": 10, "text": "课堂内容。"}]}), "utf-8")
    (directory / "video.mp4").write_bytes(b"original-recording")
    cfg = settings(tmp_path)
    cfg.delete_video_after_processing = True
    monkeypatch.setattr(lecture_source_download, "prepare_lesson_sources", lambda *a, **k: ([], []))
    monkeypatch.setattr(note_sources, "collect_note_sources", lambda *a, **k: [])
    monkeypatch.setattr(media, "write_notes", lambda transcript, frames, target, config, title, **kw:
                        save_reviewed_note(directory, config, "新版笔记。", transcript, target=target))
    monkeypatch.setattr(recordings_api, "publish_notes", lambda *a, **kw: "https://www.notion.so/new")
    cleanup_checks = []
    monkeypatch.setattr(note_videos, "video_publication_complete",
                        lambda *a, **kw: cleanup_checks.append(kw) or True)
    original_replace = Path.replace
    def replace(path, target):
        if ((failure_stage == "note" and path.name.startswith("notes.rebuild-") and path.suffix == ".md")
                or (failure_stage == "publication-marker" and path.name == "notion-publication.json.tmp")):
            raise OSError("isolated local commit failed")
        return original_replace(path, target)
    monkeypatch.setattr(Path, "replace", replace)
    with pytest.raises(OSError, match="local commit failed"):
        recordings_api.RecordingWorkManager(cfg)._process(
            recordings_api.find_job(tmp_path, key), "lecture-page", regenerate=True)
    assert (directory / "video.mp4").read_bytes() == b"original-recording"
    assert cleanup_checks == []
    if failure_stage == "note":
        assert (directory / "notes.md").read_text("utf-8") == old
    else:
        assert [p.read_text("utf-8") for p in directory.glob("notes.previous-*.md")] == [old]


def test_revision_creates_distinct_notion_page_without_archiving_old(tmp_path, monkeypatch):
    key, directory = setup_index(tmp_path)
    directory.mkdir()
    staged = directory / "revised.md"
    staged.write_text("# 新版笔记\n\n- 新内容", "utf-8")
    job = recordings_api.find_job(tmp_path, key)
    from pku_sync import notion
    from pku_sync.panel import note_images
    created = []
    class Notion:
        def __init__(self, token): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def list_child_pages(self, page_id):
            return [{"id": "b" * 32, "title": "转写笔记 · 旧版"}]
        def create_page(self, parent, title, *, children, retry):
            created.append((title, children))
            return {"id": "c" * 32, "url": "https://www.notion.so/new"}
        def list_children(self, page_id):
            return created[0][1] if page_id == "c" * 32 else []
        def archive_page(self, page_id, *, retry=True):
            pytest.fail("old note must not be archived")
    monkeypatch.setattr(notion, "NotionClient", Notion)
    monkeypatch.setattr(note_images, "publish_note_images", lambda *args, **kwargs: 0)
    assert recordings_api.publish_notes(job, "lecture-page", "notion",
                                        note_path=staged, revision=True).endswith("new")
    assert len(created) == 1 and created[0][0].startswith("更新笔记 · ")


def test_published_note_starts_with_nearby_formal_homework_and_native_safe_links(tmp_path, monkeypatch):
    key, directory = setup_index(tmp_path)
    directory.mkdir()
    (directory / "notes.md").write_text("# 课堂笔记\n\n- 概念讲解", "utf-8")
    from pku_sync.panel import campus_catalog, note_images
    from pku_sync import notion
    monkeypatch.setattr(campus_catalog, "campus_course", lambda root, course_id: {
        "assignments_status": "available", "synced_at_label": "2026-09-27 12:00 北京时间",
        "assignments": [
            {"title": "第一次书面作业", "due_at": "2026-10-01T15:00:00Z",
             "due_at_label": "2026-10-01 23:00 北京时间", "source": "content-tree",
             "content_id": "_123_1", "instructions": "计算矩阵 $A^{-1}$，图片题见原 PDF。",
             "files": ["作业.pdf"], "attachments": [
                 {"filename": "作业.pdf", "path": "/bbcswebdav/pid-12/作业.pdf"},
                 {"filename": "恶意链接", "path": "https://evil.example/steal"}]},
            {"title": "太晚的作业", "due_at": "2026-12-01T15:00:00Z",
             "source": "content-tree", "content_id": "_456_1", "instructions": "不应出现"},
            {"title": "随堂测试一", "due_at": "", "source": "content-tree",
             "content_id": "_789_1", "instructions": "随堂考勤，计入平时成绩"},
            {"title": "待公布截止的书面作业", "due_at": "", "source": "content-tree",
             "content_id": "_790_1", "instructions": "阅读原题并完成书面回答。",
             "files": ["原题.pdf"], "attachments": [
                 {"filename": "原题.pdf", "path": "/bbcswebdav/pid-13/original.pdf"}]},
            {"title": "日期格式异常的作业", "due_at": "2026-10-XX", "source": "content-tree",
             "content_id": "_791_1", "instructions": "以教学网原页为准。"},
        ]
    })
    captured = []
    class Notion:
        def __init__(self, token): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def list_child_pages(self, page_id): return []
        def create_page(self, parent, title, *, children, retry):
            captured.extend(children)
            return {"id": "c" * 32, "url": "https://www.notion.so/new"}
        def list_children(self, page_id):
            return captured if page_id == "c" * 32 else []
    monkeypatch.setattr(notion, "NotionClient", Notion)
    monkeypatch.setattr(note_images, "publish_note_images", lambda *args, **kwargs: 0)
    url = recordings_api.publish_notes(recordings_api.find_job(tmp_path, key),
                                       "lecture-page", "notion")
    assert url.endswith("new")
    plain = json.dumps(captured, ensure_ascii=False)
    assert "本课程近期正式作业" in plain
    assert "不代表老师在这节课布置" in plain
    assert "2026-10-01 23:00 北京时间" in plain
    assert "计算矩阵 $A^{-1}$" in plain
    assert "图片、矩阵或表格" in plain
    assert "随堂测试一" in plain and "必须到场" in plain
    assert "教学网目录未提供具体举行时间" in plain
    assert "待公布截止的书面作业" in plain
    assert "正式作业（截止时间待核对）" in plain
    assert "截止：教学网未公布；请核对老师最新通知和作业原页。" in plain
    assert "阅读原题并完成书面回答。" in plain
    assert "日期格式异常的作业" in plain
    assert "截止：教学网截止时间无法解析；请打开作业原页核对。" in plain
    assert "太晚的作业" not in plain and "evil.example" not in plain
    links = [part["text"]["link"]["url"]
             for block in captured
             for part in block[block["type"]].get("rich_text", [])
             if part.get("text", {}).get("link")]
    assert "https://course.pku.edu.cn/bbcswebdav/pid-12/%E4%BD%9C%E4%B8%9A.pdf" in links
    assert any("uploadAssignment?course_id=course-1&content_id=_123_1" in link for link in links)
    assert any("uploadAssignment?course_id=course-1&content_id=_789_1" in link for link in links)
    assert any("uploadAssignment?course_id=course-1&content_id=_790_1" in link for link in links)
    assert "https://course.pku.edu.cn/bbcswebdav/pid-13/original.pdf" in links
    assert all(link.startswith("https://course.pku.edu.cn/") for link in links)



def test_formal_assignment_blocks_link_synced_notion_card_and_keep_campus_fallback(tmp_path, monkeypatch):
    from pku_sync.panel import campus_catalog, notion_home

    key, _ = setup_index(tmp_path)
    job = recordings_api.find_job(tmp_path, key)
    linked = {"title": "有卡片的作业", "due_at": "2026-10-01T15:00:00Z",
              "source": "content-tree", "content_id": "_123_1", "instructions": "完成第一题"}
    unlinked = {"title": "尚无卡片的作业", "due_at": "2026-10-02T15:00:00Z",
                "source": "content-tree", "content_id": "_124_1", "instructions": "完成第二题"}
    monkeypatch.setattr(campus_catalog, "campus_course", lambda *_: {
        "assignments": [linked, unlinked], "assignments_status": "available"})
    card_id = "12345678-1234-1234-1234-123456789abc"
    (tmp_path / "notion-source-state.json").write_text(json.dumps({
        notion_home._source_key("course-1", "作业", linked): {"id": card_id},
        notion_home._source_key("course-1", "作业", unlinked): {"id": "invalid-page-id"},
    }), "utf-8")

    blocks = recordings_api.formal_assignment_blocks(job)
    links = [part["text"]["link"]["url"]
             for block in blocks
             for part in block[block["type"]].get("rich_text", [])
             if part.get("text", {}).get("link")]
    assert links.count("https://www.notion.so/12345678123412341234123456789abc") == 1
    assert any("content_id=_123_1" in link for link in links)
    assert any("content_id=_124_1" in link for link in links)
    assert sum(link.startswith("https://www.notion.so/") for link in links) == 1


def test_manual_sync_indexes_recordings_and_slide_metadata_without_download(tmp_path, monkeypatch):
    from pku_sync import auth, discover, recordings, materials
    from pku_sync.models import Course, Recording, ContentItem, Announcement, Assignment, AttachmentRef
    events = []

    class Client:
        def close(self): events.append("closed")

    monkeypatch.setattr(auth, "get_session", lambda **credentials: (events.append(credentials), Client())[1])
    monkeypatch.setattr(discover, "discover_courses", lambda client: [
        Course(course_id="course-1", name="测试课程", dir_name="测试课程")])
    monkeypatch.setattr(discover, "select_courses", lambda courses, settings: courses)
    monkeypatch.setattr(recordings, "list_recordings", lambda client, course_id: [
        Recording(course_id=course_id, title="第一讲", recorded_at="2026-09-24 09:00:00")])
    monkeypatch.setattr(materials, "walk_materials", lambda client, course_id: ([
        ContentItem(course_id=course_id, content_id="slide-1", title="第1讲课件", kind="文件", parent_path="教学内容")
    ], []))
    monkeypatch.setattr(materials, "fetch_announcements", lambda client, course_id: [
        Announcement(course_id=course_id, announcement_id="notice-1", title="本周通知",
                     body_text="请查看教学网", attachments=[
                         AttachmentRef(filename="说明.pdf", url="https://signed.example/private")])])
    monkeypatch.setattr(materials, "fetch_deadlines", lambda client: {"course-1": [
        Assignment(course_id="course-1", title="第一次作业", content_id="_123_1",
                   due_at="2026-10-01", source="calendar", instructions="提交压缩包")
    ]})
    recordings_api.sync_recording_index(settings(tmp_path), events.append)
    assert len(recordings_api.list_recordings(tmp_path)) == 1
    materials_index = json.loads((tmp_path / "测试课程" / "materials" / "index.json").read_text("utf-8"))
    assert materials_index[0]["title"] == "第1讲课件"
    assert "url" not in json.dumps(materials_index)
    notice_index = json.loads((tmp_path / "测试课程" / "announcements" / "index.json").read_text("utf-8"))
    assignment_index = json.loads((tmp_path / "测试课程" / "assignments" / "index.json").read_text("utf-8"))
    assert notice_index[0]["title"] == "本周通知"
    assert notice_index[0]["files"] == ["说明.pdf"]
    assert assignment_index[0]["due_at"] == "2026-10-01"
    assert "signed.example" not in json.dumps(notice_index + assignment_index)
    metadata = json.loads((tmp_path / "测试课程" / "course.json").read_text("utf-8"))
    assert metadata["announcements_status"] == "available"
    assert metadata["assignments_status"] == "available"
    assert events[0] == "登录教学网"
    assert events[1] == {"username": "student", "password": "secret"}
    assert events[-1] == "closed"
    assert not list(tmp_path.rglob("video.mp4"))
    def unavailable(*args):
        raise RuntimeError("temporary campus source failure")
    monkeypatch.setattr(materials, "walk_materials", unavailable)
    monkeypatch.setattr(materials, "fetch_deadlines", unavailable)
    monkeypatch.setattr(materials, "fetch_announcements", unavailable)
    recordings_api.sync_recording_index(settings(tmp_path), lambda *_: None)
    assert json.loads((tmp_path / "测试课程" / "announcements" / "index.json").read_text("utf-8")) == notice_index
    assert json.loads((tmp_path / "测试课程" / "assignments" / "index.json").read_text("utf-8")) == assignment_index
    metadata = json.loads((tmp_path / "测试课程" / "course.json").read_text("utf-8"))
    assert metadata["announcements_status"] == "error"
    assert metadata["assignments_status"] == "error"


def test_desktop_navigation_and_manual_page_are_available(tmp_path):
    from pku_sync.config import Settings
    from pku_sync.panel.webapi import create_app
    client = TestClient(create_app(settings=Settings(_env_file=None, data_dir=tmp_path)))
    script = client.get("/app/app.js").text
    assert 'data-action="open-recordings"' not in script
    assert '"open-campus-course"' in script
    assert '"campus-process"' in script
    page = client.get("/recordings", follow_redirects=False)
    assert page.status_code == 404








@pytest.mark.parametrize('notes_strategy', [None, 'topics'])
def test_campus_confirmation_creates_target_before_processing(tmp_path, monkeypatch, notes_strategy):
    key, _ = setup_index(tmp_path)
    (tmp_path / "notion-workspace-v2.json").write_text(json.dumps({
        "schema": 2, "active": True, "account_namespace": fingerprint({"session": settings(tmp_path).platform_token}), "id": "home", "parent_id": "oauth-template", "url": "https://notion.so/home"
    }), "utf-8")
    manager = recordings_api.RecordingWorkManager(settings(tmp_path))
    from pku_sync.panel import notion_home
    calls = []
    monkeypatch.setattr(notion_home, "ensure_recording_target", lambda *args, **kwargs: (calls.append("target") or "lecture"))
    monkeypatch.setattr(manager, "_process", lambda job, lecture_id, **kwargs: (calls.append(("process", lecture_id)) or "https://notion.so/note"))
    assert calls == []
    with pytest.raises(ValueError, match="不属于当前"):
        manager.start_campus_process(key, "different-course")
    assert calls == []
    app=FastAPI(); recordings_api.add_recording_routes(app,manager)
    client=TestClient(app)
    endpoint=f'/api/campus/courses/course-1/recordings/{key}/process'
    assert client.post(endpoint,json={'notes_strategy':'unknown'}).status_code==422
    assert calls==[]
    choices=[]
    original=manager._process
    def process(*args,**kwargs):
        choices.append(kwargs.get('notes_strategy')); return original(*args,**kwargs)
    monkeypatch.setattr(manager,'_process',process)
    assert client.post(endpoint,json={'notes_strategy':notes_strategy}).status_code==200
    assert wait_done(manager)["state"] == "done"
    assert calls == ["target", ("process", "lecture")]
    assert choices==[notes_strategy]


def test_campus_sync_builds_notion_pages_after_home_was_selected(tmp_path, monkeypatch):
    from pku_sync.panel import notion_home
    from pku_sync.panel import notion_workspace
    home_id, parent_id = "a" * 32, "b" * 32
    (tmp_path / "notion-workspace-v2.json").write_text(json.dumps({
        "schema": 2, "active": True, "account_namespace": fingerprint({"session": settings(tmp_path).platform_token}), "id": home_id, "parent_id": parent_id, "url": "https://app.notion.com/home"
    }), "utf-8")
    class Notion:
        def __init__(self, token): assert token == "notion"
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def get_page(self, page_id):
            if page_id == parent_id:
                return {"id": parent_id, "parent": {"type": "workspace", "workspace": True}}
            assert page_id == home_id
            return {"id": home_id, "parent": {"page_id": parent_id}}
        def list_child_pages(self, parent): return []
    monkeypatch.setattr(notion_workspace, "NotionClient", Notion)
    calls = []
    def fake_index(_settings, stage, progress, course_id="", on_course_confirmed=None):
        setup_index(tmp_path)
        from pku_sync import catalog_snapshots as catalog
        from pku_sync.models import Course
        catalog.activate_account(_settings)
        course = Course(course_id="course-1", name="测试课程")
        envelope = {"revision": 1, "snapshot": catalog.build_snapshot(
            course, [job.recording for job in recordings_api.collect_jobs(tmp_path)], [], [], [])}
        catalog.save_cache(tmp_path, envelope)
        catalog.read_status(tmp_path, False, "reading")
        catalog.course_read_status(tmp_path, "course-1", True, "complete")
        catalog.read_status(tmp_path, True, "complete")
        stage("读取教学网课程")
        progress(1, 1, "courses", 0)
        calls.append("campus")
        if on_course_confirmed:
            on_course_confirmed("course-1", envelope)
    def fake_catalog(root, token, stage, progress, item_progress=None, *, settings=None, course_id="", **kwargs):
        assert root == tmp_path and token == "notion"
        assert calls == ["campus"]
        stage("建立 Notion 课程页")
        progress(1, 1, "courses", 0)
        if item_progress:
            item_progress(1, 4, "计算机网络 · 建目录页：通知")
        calls.append("notion")
    monkeypatch.setattr(recordings_api, "sync_recording_index", fake_index)
    monkeypatch.setattr(notion_home, "sync_catalog", fake_catalog)
    from pku_sync.panel import course_summary
    def fake_summaries(data_dir, token, course_ids, *, day=None, on_progress=None,
                       on_course=None):
        assert calls == ["campus", "notion"] and token == "notion"
        assert course_ids == ["course-1"]
        if on_course:
            on_course("测试课程")
        if on_progress:
            on_progress(0, 1, "测试课程")
            on_progress(1, 1, "测试课程")
        calls.append("summary")
        return {"requested": 1, "created": 1, "existing": 0,
                "urls": ["https://app.notion.com/summary"], "failures": []}
    monkeypatch.setattr(course_summary, "publish_course_summaries", fake_summaries)
    from pku_sync import catalog_snapshots as catalog
    catalog.activate_account(settings(tmp_path))
    manager = recordings_api.RecordingWorkManager(settings(tmp_path))
    monkeypatch.setattr(manager, "_background_course_ids", lambda stage: ["course-1"])
    manager.start_sync()
    done = wait_done(manager)
    assert calls == ["campus", "notion"]
    assert done["state"] == "done" and done["scope"] == "background"
    row = done["course_results"][0]
    assert row["projection_state"] == "confirmed"
    assert row["summary_created"] == 0 and row["summary_existing"] == 0
    assert done["progress_percent"] == 100 and done["progress_remaining"] == 0
    # The previous summary outcome is retained in the explicitly requested action.
    manager.start_course_summaries("course-1")
    explicit = wait_done(manager)
    assert calls == ["campus", "notion", "summary"]
    assert explicit["summary_created"] == 1
    assert explicit["result_urls"] == ["https://app.notion.com/summary"]
    assert not list(tmp_path.rglob("video.mp4"))


def test_local_notes_publish_then_course_button_has_lecture_link(tmp_path, monkeypatch):
    from pku_sync.panel import notion_home
    from pku_sync.panel.campus_catalog import campus_course
    key, directory = setup_index(tmp_path)
    directory.mkdir()
    save_reviewed_note(directory, settings(tmp_path), "# 已转写的笔记", {"segments": [
        {"start": 0, "end": 10, "text": "已转写的内容"}
    ]})
    frames = directory / "keyframes"
    frames.mkdir()
    (frames / "frame_0000_00m00s.jpg").write_bytes(b"frame")
    (frames / "index.json").write_text(json.dumps({"keyframes": [
        {"file": "frame_0000_00m00s.jpg", "timestamp": 0, "time": "00:00"}
    ]}), "utf-8")
    (tmp_path / "notion-workspace-v2.json").write_text(json.dumps({
        "schema": 2, "active": True, "account_namespace": fingerprint({"session": settings(tmp_path).platform_token}), "id": "home", "parent_id": "parent", "url": "https://app.notion.com/home"
    }), "utf-8")
    monkeypatch.setattr(notion_home, "ensure_recording_target", lambda *args, **kwargs: "a" * 32)
    calls = []
    def failing_publish(*_):
        calls.append("failed")
        raise RuntimeError("Notion unavailable")
    monkeypatch.setattr(recordings_api, "publish_notes", failing_publish)
    manager = recordings_api.RecordingWorkManager(settings(tmp_path))
    manager.start_campus_process(key, "course-1")
    failed = wait_done(manager)
    assert failed["state"] == "failed"
    assert "笔记已保存在本机" in failed["error"]
    assert (directory / "notes.md").exists()
    assert not (directory / "notion-publication.json").exists()
    assert "notion_lecture_url" not in campus_course(tmp_path, "course-1")["lessons"][0]

    note_url = "https://app.notion.com/" + "b" * 32
    monkeypatch.setattr(recordings_api, "publish_notes",
                        lambda *_: (calls.append("published") or note_url))
    manager.start_campus_process(key, "course-1")
    done = wait_done(manager)
    assert done["state"] == "done" and done["result_url"] == note_url
    assert done["lecture_url"] == "https://www.notion.so/" + "a" * 32
    lesson = campus_course(tmp_path, "course-1")["lessons"][0]
    assert lesson["notion_lecture_url"] == done["lecture_url"]
    assert lesson["notion_note_url"] == note_url
    assert calls == ["failed", "published"]

    # Switching to the additive home must retain the exact old publication
    # marker so the original remote note can still be found for rollback.
    from pku_sync import catalog_snapshots
    from pku_sync.panel import notion_workspace
    previous = (directory / "notion-publication.json").read_bytes()
    seed_old_home(tmp_path, {"id": "old-home", "parent_id": "parent"})
    old_home = (tmp_path / "notion-learning-home.json").read_bytes()
    catalog_snapshots.activate_account(manager.settings)
    notion_workspace._save(notion_workspace.manifest_path(tmp_path), {
        "schema": 2, "active": True, "id": "new-home", "parent_id": "parent",
        "url": "https://www.notion.so/new-home",
        "account_namespace": fingerprint({"session": manager.settings.platform_token}),
    })
    monkeypatch.setattr(notion_home, "ensure_recording_target", lambda *args, **kwargs: "c" * 32)
    new_note = "https://www.notion.so/" + "d" * 32
    monkeypatch.setattr(recordings_api, "publish_notes", lambda *_: new_note)
    manager.start_campus_process(key, "course-1", reuse_existing=True)
    switched = wait_done(manager)
    assert switched["state"] == "done" and switched["result_url"] == new_note
    backups = list(directory.glob("notion-publication.legacy-*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == previous
    assert (tmp_path / "notion-learning-home.json").read_bytes() == old_home
    assert json.loads((directory / "notion-publication.json").read_text("utf-8"))["home_id"] == "new-home"


def test_campus_material_download_resolves_fresh_url_and_rejects_other_host(tmp_path, monkeypatch):
    course = tmp_path / "测试课程"
    (course / "materials").mkdir(parents=True)
    (course / "course.json").write_text(json.dumps({"name": "测试课程", "course_id": "course-1"}), "utf-8")
    (course / "materials" / "index.json").write_text(json.dumps([
        {"title": "第一讲课件", "kind": "文件", "path": "教学内容/第一讲课件",
         "files": ["slides.pdf"]},
    ]), "utf-8")
    requested = []
    redirected = [""]
    class Session:
        def get(self, url):
            requested.append(url)
            return SimpleNamespace(content=b"%PDF-test", headers={"content-type": "application/pdf"},
                                   url=redirected[0] or url, raise_for_status=lambda: None)
        def close(self):
            pass
    monkeypatch.setattr("pku_sync.auth.get_session", lambda **kwargs: Session())
    asset = SimpleNamespace(filename="slides.pdf", url="/bbcswebdav/private?token=fresh")
    monkeypatch.setattr("pku_sync.materials.walk_materials",
                        lambda client, course_id: ([SimpleNamespace(
                            path="教学内容/第一讲课件", title="第一讲课件", attachments=[asset])], []))
    app = FastAPI()
    recordings_api.add_recording_routes(app, recordings_api.RecordingWorkManager(settings(tmp_path)))
    client = TestClient(app)
    endpoint = "/api/campus/courses/course-1/materials/0/files/0"
    response = client.get(endpoint)
    assert response.status_code == 200
    assert response.content == b"%PDF-test"
    assert "fresh" not in response.headers["content-disposition"]
    assert requested == [asset.url]
    assert client.get("/api/campus/courses/course-1/materials/1/files/0").status_code == 404
    asset.url = "https://evil.example/bbcswebdav/private"
    assert client.get(endpoint).status_code == 502
    assert requested == ["/bbcswebdav/private?token=fresh"]
    asset.url = "/bbcswebdav/private?token=fresh"
    redirected[0] = "https://evil.example/bbcswebdav/private"
    assert client.get(endpoint).status_code == 502
    assert requested == [asset.url, asset.url]


def test_student_can_read_downloaded_handbook_in_course_view(tmp_path, monkeypatch):
    from pku_sync.panel import handbook_requirements
    from pku_sync.panel.campus_catalog import campus_course

    course = tmp_path / "测试课程"
    (course / "materials").mkdir(parents=True)
    (course / "course.json").write_text(json.dumps({"name": "测试课程", "course_id": "course-1"}), "utf-8")
    (course / "materials" / "index.json").write_text(json.dumps([{
        "title": "课程手册及时间安排", "kind": "文件", "path": "教学大纲/课程手册及时间安排",
        "files": ["课程学生手册.pdf"],
    }]), "utf-8")
    assert not campus_course(tmp_path, "course-1")["handbook_reviews"][0]["time_preview"]
    monkeypatch.setattr(handbook_requirements, "_pdf_pages", lambda path: [
        (6, "课程考核方式和文字报告提交要求。" * 20 +
         "\n所有组最晚于 10 月 31 日 24 点前，将所选文献发至助教邮箱。"),
    ])

    class Session:
        def get(self, url):
            return SimpleNamespace(content=b"%PDF-test", headers={"content-type": "application/pdf"},
                                   raise_for_status=lambda: None)
        def close(self):
            pass

    monkeypatch.setattr("pku_sync.auth.get_session", lambda **kwargs: Session())
    asset = SimpleNamespace(filename="课程学生手册.pdf", url="/bbcswebdav/manual?token=fresh")
    monkeypatch.setattr("pku_sync.materials.walk_materials",
                        lambda client, course_id: ([SimpleNamespace(
                            path="教学大纲/课程手册及时间安排", title="课程手册及时间安排",
                            attachments=[asset])], []))
    app = FastAPI()
    recordings_api.add_recording_routes(app, recordings_api.RecordingWorkManager(settings(tmp_path)))
    response = TestClient(app).get("/api/campus/courses/course-1/materials/0/files/0")
    assert response.status_code == 200 and response.headers["x-handbook-cached"] == "true"
    assert (course / "materials" / "教学大纲" / "课程学生手册.pdf").read_bytes() == b"%PDF-test"
    card = campus_course(tmp_path, "course-1")["handbook_reviews"][0]
    assert any("10 月 31 日 24 点" in row["text"] for row in card["time_preview"]), (
        card["pages"], card["status"], card["time_preview"])
    assert "due_at" not in card


def test_campus_material_can_be_assigned_ignored_and_restored(tmp_path):
    key, _ = setup_index(tmp_path)
    course = tmp_path / "测试课程"
    (course / "materials").mkdir()
    (course / "materials" / "index.json").write_text(json.dumps([
        {"title": "补充阅读", "kind": "文件", "path": "资料/补充阅读", "files": ["reading.pdf"]}
    ]), "utf-8")
    app = FastAPI()
    recordings_api.add_recording_routes(app, recordings_api.RecordingWorkManager(settings(tmp_path)))
    client = TestClient(app)
    path = "/api/campus/courses/course-1/materials/0/match"
    assert client.post(path, json={"lecture_id": "wrong"}).status_code == 422
    assigned = client.post(path, json={"lecture_id": key}).json()
    assert assigned["lessons"][0]["materials"][0]["title"] == "补充阅读"
    assert assigned["unassigned_materials"] == []
    ignored = client.post(path, json={"ignore": True}).json()
    assert ignored["unassigned_materials"][0]["match_state"] == "ignored"
    restored = client.post(path, json={}).json()
    assert restored["unassigned_materials"][0]["match_state"] == "needs_review"


def test_demo_notion_connection_is_labeled_without_claiming_real_pages(tmp_path):
    manager = recordings_api.RecordingWorkManager(settings(tmp_path))
    manager.settings.notion_token = ""
    app = FastAPI()
    recordings_api.add_recording_routes(app, manager, demo_connection=True)
    client = TestClient(app)
    assert client.get("/api/campus/courses").json()["demo_connection"] is True
    response = client.get("/api/campus/notion/parents")
    assert response.status_code == 409
    assert "演示模式" in response.json()["detail"]




def test_cancel_sync_stops_before_next_course_and_preserves_completed_index(tmp_path, monkeypatch):
    import threading
    entered = threading.Event()
    release = threading.Event()
    def fake_sync(settings, stage, progress, course_id="", on_course_confirmed=None):
        stage("读取第一门课程")
        entered.set()
        assert release.wait(5)
        stage("读取第二门课程")
    monkeypatch.setattr(recordings_api, "sync_recording_index", fake_sync)
    manager = recordings_api.RecordingWorkManager(settings(tmp_path))
    monkeypatch.setattr(manager, "_background_course_ids", lambda stage: ["course-1", "course-2"])
    app = FastAPI()
    recordings_api.add_recording_routes(app, manager)
    client = TestClient(app)
    assert client.post("/api/recordings/sync").status_code == 200
    assert entered.wait(5)
    assert client.post("/api/recordings/cancel-sync").status_code == 200
    release.set()
    result = wait_done(manager)
    assert result["state"] == "cancelled"
    assert client.post("/api/recordings/cancel-sync").status_code == 409
