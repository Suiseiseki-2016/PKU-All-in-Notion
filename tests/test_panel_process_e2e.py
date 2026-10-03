"""End-to-end reproduction of the per-lecture 《整理这节录像》 action.

The desktop course screen posts

    POST /api/campus/courses/{course_id}/recordings/{key}/process

(``pku_sync/panel/static/app.js`` -> ``processCampusRecording``) and then polls
``GET /api/recordings/task``.  The existing tests in
``tests/test_panel_recordings.py`` monkeypatch ``recordings_api.process_job`` /
``recordings_api.publish_notes``, so the real publication path is never
exercised.  This module drives the real path instead:

  route -> RecordingWorkManager.start_campus_process -> notion_home
  .ensure_recording_target -> RecordingWorkManager._process -> publish_notes ->
  note_publish_issues / formal_assignment_blocks / _note_format_issues ->
  NotionClient -> on-disk notion-publication.json

Only the network boundaries are replaced:

* ``pku_sync.auth.get_session`` / ``recordings_api.download_job`` -- campus
  session and media download.  They are recording stubs: if the flow ever
  reaches them the test reports the branch instead of silently faking a video.
* ``pku_sync.lecture_source_download.prepare_lesson_sources`` and
  ``pku_sync.note_sources.collect_note_sources`` -- campus courseware reading.
* ``pku_sync.media.write_notes`` -- the AI note writer.  The stub writes a note
  in the *production layout* (``## 本讲来源`` / ``## 课堂内容`` / ``###``
  chapters / ``## 作业与考试口头线索`` / ``*已整理至录像 MM:SS。*``), so every
  downstream gate (``note_coverage_gap_seconds``, ``audit_note``,
  ``note_source_conflicts``, ``_note_format_issues``) still runs for real.
* ``pku_sync.notion.NotionClient`` and ``pku_sync.panel.notion_home.NotionClient``
  -- in-memory Notion workspace that logs every call.

No network, no real Notion, no real AI, no campus session.
"""
from __future__ import annotations

import json
import logging
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pku_sync import lecture_source_download, note_sources
from pku_sync.config import Settings
from pku_sync.models import Recording
from pku_sync.panel import notion_home, recordings_api
from pku_sync.panel.campus_catalog import campus_course
from pku_sync.panel.connection import build_fake_connection_service
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.platform_bridge import FakePlatformBridge
from pku_sync.panel.webapi import create_app

COURSE_ID = "course-1"
COURSE_TITLE = "测试课程"
RECORDING_TITLE = "第一讲"
RECORDED_AT = "2026-09-24 09:00:00"
HOME_ID = "a1" * 16
NOTION_TOKEN = "notion-token"
PLATFORM_TOKEN = "platform-session"

TRANSCRIPT = {
    "segments": [
        {"start": 0, "end": 600, "text": "今天我们讲对称密码的基本概念。"},
        {"start": 600, "end": 1500, "text": "分组密码有 Feistel 网络和 SP 网络两种结构。"},
        {"start": 1500, "end": 2400, "text": "ECB 模式对相同明文块产生相同密文块。"},
        {"start": 2400, "end": 3300, "text": "密钥分发是对称密码的核心困难。"},
    ]
}

# Mirrors the layout pku_sync.media.write_notes produces: source section,
# classroom prose under ### chapters, oral-clue section and the single end
# marker.  Kept plain on purpose: it must pass the real publication gates, not
# dodge them.
NOTE_TEXT = """# 第一讲 课堂笔记

## 本讲来源

- 课堂录像自动转写：口头事项索引保留原文片段及录像时间。
- 未确认对应的可核对课件；本笔记未用其他周资料填补。

## 课堂内容

### 对称密码与分组结构

本次课介绍对称密码的基本概念。老师先回顾了古典密码的替换与置换思想，随后讲解了分组密码的两种结构：Feistel 网络与 SP 网络。Feistel 网络把分组分成左右两半，每一轮只对其中一半做变换，另一半保持不动，因此解密结构与加密结构相同，只需要把子密钥顺序颠倒。SP 网络则每一轮都同时作用于整个分组，通过混淆和扩散来抵抗差分分析。

老师强调，分组密码的安全性来自密钥的保密性和算法的公开审查。密钥长度决定穷举攻击的代价，而算法的公开性让全世界的学者都能检验它的设计。

### 工作模式与密钥分发

课堂上还讨论了工作模式：ECB 模式对相同明文块产生相同密文块，因此不适合加密有结构的数据；CBC 模式通过链接上一个密文块来隐藏重复模式，但需要随机且不可预测的初始向量。关于密钥分发，老师指出对称密码的核心困难在通信双方如何安全地共享同一把密钥，随后引入了公钥体制的基本想法。

## 作业与考试口头线索（自动转写原文，待核对）

- 老师提到下次课会讲公钥体制。

## 课件读取说明

- 本次没有可读取的课件。

*已整理至录像 55:00。*
"""


# --------------------------------------------------------------------------
# fixture helpers
# --------------------------------------------------------------------------

def _settings(root: Path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=root,
        pku_username="student",
        pku_password="secret",
        notion_token=NOTION_TOKEN,
        platform_token=PLATFORM_TOKEN,
        openai_api_key="",
    )


def _write_campus(root: Path, *, synced_at: datetime | None = None) -> str:
    """Lay out the on-disk campus course tree and return the recording key."""
    folder = root / COURSE_TITLE
    (folder / "recordings").mkdir(parents=True)
    (folder / "materials").mkdir()
    (folder / "assignments").mkdir()
    (folder / "announcements").mkdir()
    (folder / "course.json").write_text(json.dumps({
        "course_id": COURSE_ID,
        "name": COURSE_TITLE,
        "synced_at": (synced_at or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "assignments_status": "available",
        "announcements_status": "available",
    }, ensure_ascii=False), "utf-8")
    recording = Recording(course_id=COURSE_ID, title=RECORDING_TITLE, recorded_at=RECORDED_AT)
    (folder / "recordings" / "index.json").write_text(
        json.dumps([recording.model_dump()], ensure_ascii=False), "utf-8")
    for name in ("materials", "assignments", "announcements"):
        (folder / name / "index.json").write_text("[]", "utf-8")
    job = recordings_api.collect_jobs(root)[0]
    return recordings_api.recording_id(job)


def _write_recording_artifacts(root: Path, key: str, *, with_notes: bool) -> Path:
    """Transcript + already-extracted keyframe index; no video on purpose."""
    job = recordings_api.find_job(root, key)
    assert job is not None
    directory = job.directory
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "transcript.json").write_text(
        json.dumps(TRANSCRIPT, ensure_ascii=False), "utf-8")
    frames = directory / "keyframes"
    frames.mkdir(exist_ok=True)
    (frames / "index.json").write_text(json.dumps({"keyframes": [
        {"file": "frame_0000_00m00s.jpg", "timestamp": 0, "time": "00:00"},
    ]}, ensure_ascii=False), "utf-8")
    if with_notes:
        (directory / "notes.md").write_text(NOTE_TEXT, "utf-8")
    return directory


def _save_home(root: Path) -> None:
    notion_home._save_home(root, {"id": HOME_ID, "parent_id": "parent-page",
                                  "url": "https://www.notion.so/home"})


def _plain(block: dict) -> str:
    kind = block.get("type") or ""
    body = block.get(kind) or {}
    return "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                   for part in body.get("rich_text") or [])


def _url(page_id: str) -> str:
    return "https://www.notion.so/" + page_id.replace("-", "")


# --------------------------------------------------------------------------
# in-memory Notion
# --------------------------------------------------------------------------

class FakeNotionStore:
    """One shared workspace; every NotionClient instance is a new connection."""

    def __init__(self) -> None:
        self.pages: dict[str, dict] = {}
        self.blocks: dict[str, list[dict]] = {}
        self.parent_of: dict[str, str] = {}
        self.calls: list[str] = []
        self.archived_pages: list[str] = []
        self._counter = 0

    def new_id(self) -> str:
        self._counter += 1
        return f"{self._counter:032x}"

    def materialize(self, block: dict) -> dict:
        copied = json.loads(json.dumps(block))
        copied.setdefault("id", self.new_id())
        body = copied.get(copied.get("type") or "")
        if isinstance(body, dict):
            for part in body.get("rich_text") or []:
                if isinstance(part, dict) and "plain_text" not in part:
                    part["plain_text"] = (part.get("text") or {}).get("content") or ""
        return copied

    def add_page(self, page_id: str, title: str, *, parent: str | None = None,
                 children: list[dict] | None = None) -> dict:
        self.pages[page_id] = {"id": page_id, "title": title}
        self.blocks.setdefault(page_id, [])
        if parent is not None:
            self.blocks.setdefault(parent, []).append({
                "id": page_id, "type": "child_page", "child_page": {"title": title}})
            self.parent_of[page_id] = parent
        for block in children or []:
            self.blocks[page_id].append(self.materialize(block))
        return {"id": page_id, "url": _url(page_id)}

    def child_pages(self, page_id: str) -> list[dict]:
        return [{"id": block["id"],
                 "title": (block.get("child_page") or {}).get("title", "")}
                for block in self.blocks.get(page_id, [])
                if block.get("type") == "child_page"]

    def rename(self, page_id: str, title: str) -> None:
        self.pages[page_id]["title"] = title
        parent = self.parent_of.get(page_id)
        for block in self.blocks.get(parent, []) if parent else []:
            if block.get("id") == page_id and block.get("type") == "child_page":
                block["child_page"]["title"] = title

    def find_block(self, block_id: str) -> dict | None:
        for blocks in self.blocks.values():
            for block in blocks:
                if block.get("id") == block_id:
                    return block
        return None

    def remove_block(self, block_id: str) -> None:
        for blocks in self.blocks.values():
            blocks[:] = [block for block in blocks if block.get("id") != block_id]

    def count_calls(self, prefix: str) -> int:
        return sum(1 for call in self.calls if call.startswith(prefix))


class FakeNotionClient:
    """Duck-typed NotionClient covering exactly what the flow calls."""

    def __init__(self, store: FakeNotionStore, token: str) -> None:
        self.store = store
        self.token = token
        store.calls.append(f"open(token={token!r})")

    def __enter__(self) -> "FakeNotionClient":
        return self

    def __exit__(self, *exc) -> bool:
        self.store.calls.append("close()")
        return False

    def get_page(self, page_id: str) -> dict:
        self.store.calls.append(f"get_page({page_id})")
        if page_id not in self.store.pages:
            raise AssertionError(f"fake Notion: unknown page {page_id}")
        return {"id": page_id, "url": _url(page_id)}

    def list_children(self, page_id: str) -> list[dict]:
        return list(self.store.blocks.get(page_id, []))

    def list_child_pages(self, page_id: str) -> list[dict]:
        return self.store.child_pages(page_id)

    def create_page(self, parent_page_id: str, title: str, *,
                    children: list[dict] | None = None, retry: bool = True) -> dict:
        self.store.calls.append(
            f"create_page(parent={parent_page_id}, title={title!r}, "
            f"blocks={len(children or [])}, retry={retry})")
        page_id = self.store.new_id()
        return self.store.add_page(page_id, title, parent=parent_page_id, children=children)

    def append_blocks(self, page_id: str, blocks: list[dict], *,
                      after: str | None = None, retry: bool = True) -> list[dict]:
        self.store.calls.append(
            f"append_blocks({page_id}, {len(blocks)}, after={after}, retry={retry})")
        target = self.store.blocks.setdefault(page_id, [])
        index = len(target)
        if after is not None:
            index = next((position + 1 for position, block in enumerate(target)
                          if block.get("id") == after), len(target))
        created = []
        for block in blocks:
            materialized = self.store.materialize(block)
            target.insert(index, materialized)
            index += 1
            created.append(materialized)
        return created

    def update_page_properties(self, page_id: str, properties: dict) -> dict:
        title = "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                        for part in (properties.get("title") or {}).get("title") or [])
        self.store.calls.append(f"update_page_properties({page_id}, {title!r})")
        self.store.rename(page_id, title)
        return {"id": page_id}

    def update_paragraph(self, block_id: str, text: str) -> dict:
        self.store.calls.append(f"update_paragraph({block_id})")
        block = self.store.find_block(block_id)
        if block is not None:
            block.setdefault("paragraph", {})["rich_text"] = [
                {"type": "text", "text": {"content": text}, "plain_text": text}]
        return {}

    def archive_block(self, block_id: str, *, retry: bool = True) -> dict:
        self.store.calls.append(f"archive_block({block_id}, retry={retry})")
        self.store.remove_block(block_id)
        return {}

    def archive_page(self, page_id: str, *, retry: bool = True) -> dict:
        self.store.calls.append(f"archive_page({page_id}, retry={retry})")
        self.store.archived_pages.append(page_id)
        self.store.remove_block(page_id)
        self.store.parent_of.pop(page_id, None)
        return {}

    def replace_page_content(self, page_id: str, blocks: list[dict]) -> None:
        self.store.calls.append(f"replace_page_content({page_id}, {len(blocks)})")
        self.store.blocks[page_id] = [self.store.materialize(block) for block in blocks]

    def upload_file(self, path) -> str:
        self.store.calls.append(f"upload_file({Path(path).name})")
        return "https://fake.notion/upload/" + Path(path).name

    def search(self, query: str, *, object_type: str | None = None) -> list[dict]:
        return list(self.store.pages.values())

    def move_page(self, page_id: str, parent_page_id: str) -> dict:
        raise AssertionError("move_page is not part of the per-lecture flow")


def _seed_notion(store: FakeNotionStore, root: Path, key: str) -> dict:
    """Reproduce what a completed catalog sync leaves in Notion."""
    course = campus_course(root, COURSE_ID)
    lecture_base = notion_home._lecture_titles(course)[key]
    store.add_page(HOME_ID, notion_home.HOME_TITLE)
    course_page = store.add_page(store.new_id(), notion_home._course_titles(root)[COURSE_ID],
                                 parent=HOME_ID)
    lecture_page = store.add_page(
        store.new_id(), notion_home._lecture_status_title(lecture_base, False),
        parent=course_page["id"],
        children=[{"object": "block", "type": "paragraph",
                   "paragraph": {"rich_text": [{"type": "text",
                                                "text": {"content": notion_home.LECTURE_PLACEHOLDER}}]}}])
    return {"course_page": course_page["id"], "lecture_page": lecture_page["id"],
            "lecture_base": lecture_base}


class _LogCapture(logging.Handler):
    """Capture product log records (with tracebacks) for failure reports."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record)
        if record.exc_info:
            message += "\n" + "".join(traceback.format_exception(*record.exc_info))
        self.messages.append(message)


class _Harness:
    def __init__(self, client: TestClient, store: FakeNotionStore, root: Path,
                 key: str, notion: dict, logs: _LogCapture, calls: list[str],
                 note: dict) -> None:
        self.client = client
        self.store = store
        self.root = root
        self.key = key
        self.notion = notion
        self.logs = logs
        self.calls = calls
        self._note = note

    @property
    def note_text(self) -> str:
        """Text the stubbed AI writer writes.

        Individual tests override it to reproduce the note shapes the product
        can leave on disk.
        """
        return self._note["text"]

    @note_text.setter
    def note_text(self, value: str) -> None:
        self._note["text"] = value

    @property
    def job_dir(self) -> Path:
        job = recordings_api.find_job(self.root, self.key)
        assert job is not None
        return job.directory

    def process(self) -> tuple[int, dict]:
        response = self.client.post(
            f"/api/campus/courses/{COURSE_ID}/recordings/{self.key}/process",
            json={"direct_oss": False, "regenerate": False})
        return response.status_code, (response.json() if response.content else {})

    def update_course_metadata(self, **values) -> None:
        metadata = self.root / COURSE_TITLE / "course.json"
        payload = json.loads(metadata.read_text("utf-8"))
        payload.update(values)
        metadata.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")

    def wait(self, iterations: int = 200) -> dict:
        for _ in range(iterations):
            state = self.client.get("/api/recordings/task").json()
            if state.get("state") != "running":
                return state
            time.sleep(0.05)
        raise AssertionError("recording task never left the running state")

    def diagnosis(self, state: dict) -> str:
        marker = self.job_dir / "notion-publication.json"
        return (
            f"task state={state!r}\n"
            f"on-disk marker exists={marker.exists()}\n"
            f"notion calls:\n  " + "\n  ".join(self.store.calls) +
            "\nproduct logs:\n  " + "\n  ".join(self.logs.messages))


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Build the app with fake campus/Notion/platform backends and no network."""
    key = _write_campus(tmp_path)
    _write_recording_artifacts(tmp_path, key, with_notes=False)
    _save_home(tmp_path)

    store = FakeNotionStore()
    notion = _seed_notion(store, tmp_path, key)

    logs = _LogCapture()
    logs.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logging.getLogger("pku_sync").addHandler(logs)
    monkeypatch.setattr(logging.getLogger("pku_sync"), "level", logging.DEBUG, raising=False)

    calls: list[str] = []
    note = {"text": NOTE_TEXT}

    class _Session:
        def close(self) -> None:
            calls.append("session.close")

    def fake_get_session(**credentials):
        calls.append(f"get_session({sorted(credentials)})")
        return _Session()

    def fake_download_job(client, job, progress=None):
        # Reaching here means the flow wanted the real recording; report it
        # instead of inventing a video file.
        calls.append(f"download_job({job.recording.title})")
        return SimpleNamespace(path=None, error="test stub: no campus download")

    def fake_prepare_lesson_sources(job, key_, settings, **kwargs):
        calls.append("prepare_lesson_sources")
        return [], []

    def fake_collect_note_sources(*args, **kwargs):
        calls.append("collect_note_sources")
        return []

    def fake_write_notes(transcript, keyframes, target, settings, title, **kwargs):
        calls.append(f"write_notes(title={title!r})")
        Path(target).write_text(note["text"], "utf-8")
        return Path(target)

    from pku_sync import media, notion as notion_module
    monkeypatch.setattr("pku_sync.auth.get_session", fake_get_session)
    monkeypatch.setattr(recordings_api, "download_job", fake_download_job)
    monkeypatch.setattr(lecture_source_download, "prepare_lesson_sources",
                        fake_prepare_lesson_sources)
    monkeypatch.setattr(note_sources, "collect_note_sources", fake_collect_note_sources)
    monkeypatch.setattr(media, "write_notes", fake_write_notes)
    monkeypatch.setattr(notion_module, "NotionClient",
                        lambda token, *args, **kwargs: FakeNotionClient(store, token))
    monkeypatch.setattr(notion_home, "NotionClient",
                        lambda token, *args, **kwargs: FakeNotionClient(store, token))

    app = create_app(
        settings=_settings(tmp_path),
        directory_service=build_fake_directory(),
        connection_service=build_fake_connection_service(),
        platform_service=FakePlatformBridge(activated=True),
    )
    harness = _Harness(TestClient(app), store, tmp_path, key, notion, logs, calls, note)
    try:
        yield harness
    finally:
        logging.getLogger("pku_sync").removeHandler(logs)


# --------------------------------------------------------------------------
# the reproduction
# --------------------------------------------------------------------------

def test_process_action_publishes_note_end_to_end(harness):
    """《整理这节录像》 must finish, publish, and write the on-disk marker."""
    status, payload = harness.process()
    assert status == 200, payload
    assert payload["kind"] == "process"
    assert payload["state"] in {"running", "done"}

    state = harness.wait()
    assert state["state"] == "done", harness.diagnosis(state)
    assert state["error"] == ""
    assert state["stage"] == "已发布到 Notion"

    # 1. the note URL is reported and points at the created Notion page
    note_url = state["result_url"]
    assert note_url.startswith("https://www.notion.so/"), state
    lecture_id = harness.notion["lecture_page"]
    assert state["lecture_url"] == f"https://www.notion.so/{lecture_id}"

    # 2. the on-disk marker records the same publication
    marker = json.loads((harness.job_dir / "notion-publication.json").read_text("utf-8"))
    assert marker["recording_id"] == harness.key
    assert marker["home_id"] == HOME_ID
    assert marker["lecture_id"] == lecture_id
    assert marker["note_url"] == note_url
    assert marker["lecture_url"] == state["lecture_url"]
    assert marker["published_at"]

    # 3. the note page exists under the lecture page and carries the note body
    note_pages = harness.store.child_pages(lecture_id)
    assert len(note_pages) == 1, note_pages
    assert note_pages[0]["title"] == f"转写笔记 · {RECORDED_AT.split(' ')[0]} {RECORDING_TITLE}"
    note_blocks = harness.store.blocks[note_pages[0]["id"]]
    published = "\n".join(_plain(block) for block in note_blocks)
    assert "本课程近期正式作业" in published
    assert "对称密码与分组结构" in published
    assert "已整理至录像 55:00" in published

    # 4. the catalog placeholder paragraph was archived from the lecture page
    lecture_paragraphs = [_plain(block) for block in harness.store.blocks[lecture_id]
                          if block.get("type") == "paragraph"]
    assert notion_home.LECTURE_PLACEHOLDER not in lecture_paragraphs
    assert notion_home.LECTURE_PLACEHOLDER not in "\n".join(lecture_paragraphs)
    assert harness.store.count_calls("archive_block") == 1

    # 5. the real code path (not a stub) produced the note and the marker
    assert "write_notes(title=" in " ".join(harness.calls)
    assert "download_job" not in " ".join(harness.calls), harness.calls


def test_second_process_run_reuses_the_same_note_page(harness):
    """Retrying the action must not create a duplicate note page."""
    status, _ = harness.process()
    assert status == 200
    first = harness.wait()
    assert first["state"] == "done", harness.diagnosis(first)
    created_first = harness.store.count_calls("create_page")
    pages_first = harness.store.child_pages(harness.notion["lecture_page"])

    harness.store.calls.clear()
    status, _ = harness.process()
    assert status == 200
    second = harness.wait()
    assert second["state"] == "done", harness.diagnosis(second)
    assert second["result_url"] == first["result_url"]

    pages_second = harness.store.child_pages(harness.notion["lecture_page"])
    assert [page["id"] for page in pages_second] == [page["id"] for page in pages_first]
    assert harness.store.count_calls("create_page") == 0, harness.store.calls
    assert harness.store.archived_pages == []
    assert created_first == 1


def test_stale_catalog_is_refreshed_then_the_note_publishes(harness, monkeypatch):
    """A >24h old campus snapshot is renewed, not turned into a dead end.

    The note states the course's current formal assignments, so the snapshot it
    reads must be fresh. ``_refresh_stale_catalog`` re-reads just this course and
    only a failed campus login stops the run.
    """
    harness.update_course_metadata(
        synced_at=(datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds"))
    refreshed = []

    def fake_refresh(settings, stage, progress, course_id=""):
        refreshed.append(course_id)
        stage("读取教学网课程")
        harness.update_course_metadata(
            synced_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    monkeypatch.setattr(recordings_api, "sync_recording_index", fake_refresh)

    status, body = harness.process()
    assert status == 200, body
    done = harness.wait()
    assert done["state"] == "done", harness.diagnosis(done)
    assert refreshed == [COURSE_ID]
    assert (harness.job_dir / "notion-publication.json").exists()
    assert "write_notes(title=" in " ".join(harness.calls)


def test_stale_catalog_that_cannot_be_refreshed_reports_the_reason(harness, monkeypatch):
    """A failed campus login must stop the run with a readable reason."""
    harness.update_course_metadata(
        synced_at=(datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds"))

    def failing_refresh(*args, **kwargs):
        raise RuntimeError("IAAA login failed after 2 attempts: timed out")

    monkeypatch.setattr(recordings_api, "sync_recording_index", failing_refresh)

    status, body = harness.process()
    assert status == 200, body
    done = harness.wait()
    assert done["state"] == "failed", harness.diagnosis(done)
    assert "教学网目录更新失败" in done["error"]
    assert "IAAA" not in done["error"]
    assert not (harness.job_dir / "notion-publication.json").exists()
    assert "write_notes" not in " ".join(harness.calls)


def test_partial_assignment_catalog_is_refreshed_then_the_note_publishes(harness, monkeypatch):
    """``assignments_status: partial`` is recoverable, not a permanent block."""
    harness.update_course_metadata(assignments_status="partial")
    refreshed = []

    def fake_refresh(settings, stage, progress, course_id=""):
        refreshed.append(course_id)
        harness.update_course_metadata(assignments_status="available")

    monkeypatch.setattr(recordings_api, "sync_recording_index", fake_refresh)

    status, body = harness.process()
    assert status == 200, body
    done = harness.wait()
    assert done["state"] == "done", harness.diagnosis(done)
    assert refreshed == [COURSE_ID]
    assert "write_notes(title=" in " ".join(harness.calls)


LEGACY_WINDOW_NOTE = """# 第一讲 课堂笔记

## 本讲来源

- 课堂录像自动转写：口头事项索引保留原文片段及录像时间。

## 课堂内容

## 00:00–55:00

本次课介绍对称密码的基本概念。老师先回顾了古典密码的替换与置换思想，随后讲解了分组密码的两种结构：Feistel 网络与 SP 网络。老师强调，分组密码的安全性来自密钥的保密性和算法的公开审查。

## 作业与考试口头线索

- 老师提到下次课会讲公钥体制。

*已整理至录像 55:00。*
"""


@pytest.mark.xfail(strict=True, reason=(
    "KNOWN TRADEOFF (kept by product decision, 2026-10-02): a note with "
    "`## MM:SS–MM:SS` window headings is rejected by note_publish_issues "
    "('自然排版'), although note_coverage_gap_seconds and "
    "note_images._WINDOW both require/support exactly that format. The chosen "
    "remedy is 「重新整理笔记」 to rewrite such notes in the current layout, so "
    "this test documents the accepted limitation. If the gate is ever relaxed "
    "to accept window headings, this test starts passing and fails loudly on "
    "the strict marker, which is the signal to delete the marker."))
def test_legacy_window_heading_note_can_be_published(harness):
    """Reproduction: the reported 《整理这节录像》 failure for an existing note.

    ``pku_sync/panel/note_images.py`` places slide images after ``heading_2``
    blocks whose text matches ``MM:SS–MM:SS`` and ``pku_sync/media.py
    .note_coverage_gap_seconds`` documents both the window-heading and the
    end-marker note formats as valid.  ``pku_sync/panel/recordings_api.py
    .note_publish_issues`` nevertheless turns ``note_quality.audit_note``'s
    "自然排版" review status into a hard publication failure:

        ValueError: 笔记暂不能作为完整课程笔记发布：笔记仍像时间段提纲或原始转写，
        请整理成自然段落后再发布。；本机笔记已保留。

    The task ends in ``state == "failed"`` and nothing is written to Notion.
    """
    harness.note_text = LEGACY_WINDOW_NOTE

    status, _ = harness.process()
    assert status == 200
    state = harness.wait()
    assert state["state"] == "done", harness.diagnosis(state)
    assert state["error"] == ""
    assert (harness.job_dir / "notion-publication.json").is_file()


@pytest.mark.xfail(strict=True, reason=(
    "REAL DEFECT (unfixed): the note writer's own raw-transcript fallback "
    "marker ('以下为原始转录', pku_sync/media.py) makes note_publish_issues "
    "reject the note, so a locally generated note can never be published. It "
    "is unreachable on the student cloud path, which raises instead of writing "
    "the fallback, so it only bites notes produced by a local-LLM CLI/TUI run "
    "and later published from the panel."))
def test_writer_raw_transcript_fallback_note_can_be_published(harness):
    """Reproduction: the note writer's own fallback marker blocks publication.

    When a chapter cannot be summarized, ``pku_sync/media.py`` writes
    ``> 生成失败（…），以下为原始转录：`` plus the raw window text into the
    note body.  ``note_quality.audit_note`` then reports 自然排版 = "review"
    (``raw_asr = "以下为原始转录" in body``) and ``note_publish_issues`` blocks
    the note.  The note therefore exists locally but can never be published
    through 《整理这节录像》 — the failure message tells the student to do the
    one thing the action is already doing.
    """
    harness.note_text = NOTE_TEXT.replace(
        "### 对称密码与分组结构\n",
        "### 对称密码与分组结构\n\n"
        "> 生成失败（AI 服务暂时不可用），以下为原始转录：\n\n"
        "- 我们今天讲对称密码的基本概念。\n")

    status, _ = harness.process()
    assert status == 200
    state = harness.wait()
    assert state["state"] == "done", harness.diagnosis(state)
    assert (harness.job_dir / "notion-publication.json").is_file()


def test_desktop_button_posts_this_route(harness):
    """The desktop 《整理这节录像》 button targets the route this test drives."""
    script = harness.client.get("/app/app.js").content.decode("utf-8")
    assert "整理这节录像" in script
    assert '"/api/campus/courses/" + encodeURIComponent(course.id) + "/recordings/"' in script
    assert '"/process"' in script
    assert 'requestJson("/api/recordings/task")' in script
