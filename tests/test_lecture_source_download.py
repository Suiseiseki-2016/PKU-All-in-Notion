"""A lecture only fetches verified, bounded files selected for that lecture."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx

from pku_sync.lecture_source_download import (
    MAX_FILE_BYTES, _candidate_attachments, _fetch_bounded, _safe_webdav_url,
    prepare_lesson_sources,
)
from pku_sync.models import AttachmentRef, ContentItem, Recording
from pku_sync.pipeline import RecordingJob


def _job(tmp_path: Path, *, two_same_day: bool = False) -> RecordingJob:
    course = tmp_path / "课程"
    (course / "recordings").mkdir(parents=True)
    recording = Recording(course_id="course-1", title="第1讲", recorded_at="2026-09-24 09:00:00")
    rows = [recording.model_dump()]
    if two_same_day:
        rows.append(Recording(course_id="course-1", title="第2讲",
                              recorded_at="2026-09-24 14:00:00").model_dump())
    (course / "recordings" / "index.json").write_text(json.dumps(rows), "utf-8")
    return RecordingJob("课程", course, recording)


def _index(job: RecordingJob, rows: list[dict], matches: dict | None = None):
    folder = job.course_dir / "materials"
    folder.mkdir()
    (folder / "index.json").write_text(json.dumps(rows, ensure_ascii=False), "utf-8")
    if matches is not None:
        (folder / "matches.json").write_text(json.dumps(matches, ensure_ascii=False), "utf-8")


def _row(title: str, filename: str, *, path: str = "教学内容/第一周", content_id: str = "item-1") -> dict:
    return {"title": title, "path": path + "/" + title, "kind": "文件",
            "content_id": content_id,
            "attachments": [{"filename": filename, "path": "/bbcswebdav/xid-123/file"}]}


def test_manual_match_works_without_date_and_ambiguous_date_does_not(tmp_path):
    job = _job(tmp_path, two_same_day=True)
    manual = _row("Week 1 slides", "week1.pdf")
    dated = _row("2026-09-24 课件", "lecture.pdf", content_id="item-2")
    _index(job, [manual, dated], {manual["path"]: "chosen-recording"})
    chosen = _candidate_attachments(job, "chosen-recording")
    assert [(row["title"], asset["filename"]) for row, asset in chosen] == [("Week 1 slides", "week1.pdf")]
    assert _candidate_attachments(job, "different-recording") == []


def test_unique_day_accepts_full_date_but_not_week_number_or_assignments(tmp_path):
    job = _job(tmp_path)
    dated = _row("20260924 课堂课件", "slides.pptx")
    _index(job, [dated, _row("Week 1", "week1.pdf", content_id="two"),
                 _row("2026 Week09 Chapter24", "not-a-date.pdf", content_id="four"),
                 _row("第一次作业 20260924", "answer.docx", content_id="three")])
    assert [asset["filename"] for _, asset in _candidate_attachments(job, "key")] == ["slides.pptx"]


def test_webdav_url_rejects_external_or_traversal():
    assert _safe_webdav_url("/bbcswebdav/xid-123/file") == "https://course.pku.edu.cn/bbcswebdav/xid-123/file"
    for url in ("https://evil.example/bbcswebdav/x", "https://course.pku.edu.cn:444/bbcswebdav/x",
                "https://user@course.pku.edu.cn/bbcswebdav/x", "/bbcswebdav/%2e%2e/private",
                "http://course.pku.edu.cn/bbcswebdav/x"):
        assert _safe_webdav_url(url) == ""


def test_bounded_stream_keeps_no_partial_file(tmp_path):
    url = "https://course.pku.edu.cn/bbcswebdav/xid-123/file"
    destination = tmp_path / "slides.pdf"
    def oversized(request):
        return httpx.Response(200, content=b"%PDF-" + b"x" * 100,
                              headers={"Content-Length": str(MAX_FILE_BYTES + 1)}, request=request)
    with httpx.Client(transport=httpx.MockTransport(oversized)) as client:
        assert not _fetch_bounded(client, url, destination, MAX_FILE_BYTES)
    assert not destination.exists() and not list(tmp_path.glob("*.tmp"))

    def valid(request):
        return httpx.Response(200, content=b"%PDF-1.4\n%%EOF", request=request)
    with httpx.Client(transport=httpx.MockTransport(valid)) as client:
        assert _fetch_bounded(client, url, destination, MAX_FILE_BYTES)
    assert destination.read_bytes().startswith(b"%PDF-")


def test_preparation_downloads_only_manual_matched_file(tmp_path, monkeypatch):
    job = _job(tmp_path, two_same_day=True)
    row = _row("Week 1 slides", "week1.pdf")
    # Even an untrusted key used in a saved manual match cannot form a path.
    key = "../../escape"
    _index(job, [row], {row["path"]: key})
    live = ContentItem(course_id="course-1", content_id="item-1", title="Week 1 slides",
                       kind="文件", parent_path="教学内容/第一周",
                       attachments=[AttachmentRef(filename="week1.pdf", url="/bbcswebdav/xid-123/file")])
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=b"%PDF-1.4\n%%EOF", request=request)
    from pku_sync import auth, materials
    monkeypatch.setattr(auth, "get_session", lambda **kwargs: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(materials, "walk_materials", lambda client, course_id: ([live], []))
    paths, failures = prepare_lesson_sources(job, key, SimpleNamespace(pku_username="u", pku_password="p"))
    assert failures == [] and len(paths) == 1 and paths[0].is_file()
    assert paths[0].resolve().is_relative_to((job.course_dir / "materials").resolve())
    assert calls == ["https://course.pku.edu.cn/bbcswebdav/xid-123/file"]
    # The verified local copy avoids another login or download on retry.
    paths_again, _ = prepare_lesson_sources(job, key, SimpleNamespace(pku_username="", pku_password=""))
    assert paths_again == paths and len(calls) == 1
