"""A lecture only fetches verified, bounded files selected for that lecture."""

from __future__ import annotations

import json
import io
import zipfile

import pytest
from pathlib import Path
from types import SimpleNamespace

import httpx

from pku_sync.lecture_source_download import (
    MAX_FILE_BYTES, _candidate_attachments, _fetch_bounded, _fetch_document, _safe_webdav_url,
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


def test_exact_native_extensionless_official_metadata_is_a_lecture_candidate(tmp_path):
    # Original failing metadata from r8. No oracle facts enter note generation.
    course = tmp_path / "native-course"
    (course / "recordings").mkdir(parents=True)
    recording = Recording(course_id="_101540_1", title="2026-09-30第5-6节", recorded_at="2026-09-30 09:00:00")
    (course / "recordings/index.json").write_text(json.dumps([recording.model_dump()]), "utf-8")
    job = RecordingJob("native-course", course, recording)
    row = {"title": "20260930中枢神经系统发育", "kind": "文件", "path": "教学内容/20260930中枢神经系统发育",
           "content_id": "_1717897_1", "attachments": [{"filename": "20260930中枢神经系统发育",
            "path": "/bbcswebdav/pid-1717897-dt-content-rid-13142651_1/xid-13142651_1"}]}
    _index(job, [row])
    assert _candidate_attachments(job, "native-source") == [(row, row["attachments"][0])]


def test_extensionless_still_requires_exact_scope_and_never_relabels_other_suffix(tmp_path):
    job = _job(tmp_path, two_same_day=True)
    dated = _row("20260924 slides", "20260924 slides")
    wrong = _row("20260924 executable", "source.exe", content_id="wrong")
    _index(job, [dated, wrong])
    assert _candidate_attachments(job, "selected") == []
    (job.course_dir / "materials/matches.json").write_text(json.dumps({dated["path"]: "selected", wrong["path"]: "selected"}))
    assert _candidate_attachments(job, "selected") == [(dated, dated["attachments"][0])]
    assert _candidate_attachments(job, "other") == []


def office_bytes(suffix):
    core, namespace, root, content_type = (
        ("ppt/presentation.xml", "http://schemas.openxmlformats.org/presentationml/2006/main", "presentation",
         "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml")
        if suffix == ".pptx" else
        ("word/document.xml", "http://schemas.openxmlformats.org/wordprocessingml/2006/main", "document",
         "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"))
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as archive:
        archive.writestr(core, f'<{root} xmlns="{namespace}"/>')
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/' + core + '" ContentType="' + content_type + '"/></Types>')
    return result.getvalue()


@pytest.mark.parametrize("suffix", [".pdf", ".pptx", ".docx"])
def test_extensionless_magic_inference_ignores_untrusted_header_path(tmp_path, suffix):
    payload = b"%PDF-1.4\n%%EOF" if suffix == ".pdf" else office_bytes(suffix)
    url = "https://course.pku.edu.cn/bbcswebdav/xid-123/file"
    def response(request):
        return httpx.Response(200, content=payload, headers={"Content-Type": "text/html", "Content-Disposition": 'attachment; filename="../../injected.exe"'}, request=request)
    base = tmp_path / "original-official-name"
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        typed = _fetch_document(client, url, base, MAX_FILE_BYTES)
    assert typed == tmp_path / (base.name + suffix)
    assert typed.read_bytes() == payload
    assert not base.exists() and not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("payload", [b"<html>login</html>", b"not a document", b"PK\x03\x04unknown-zip"])
def test_extensionless_headers_cannot_authorize_invalid_body(tmp_path, payload):
    url = "https://course.pku.edu.cn/bbcswebdav/xid-123/file"
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload, headers={"Content-Type": "application/pdf"}, request=request))) as client:
        assert _fetch_document(client, url, tmp_path / "official", MAX_FILE_BYTES) is None
    assert list(tmp_path.iterdir()) == []


def test_ambiguous_zip_core_and_byte_overflow_are_rejected(tmp_path):
    stream = io.BytesIO(office_bytes(".pptx"))
    with zipfile.ZipFile(stream, "a") as archive:
        archive.writestr("word/document.xml", "<document/>")
    url = "https://course.pku.edu.cn/bbcswebdav/xid-123/file"
    for payload, budget in [(stream.getvalue(), MAX_FILE_BYTES), (b"%PDF-" + b"x" * 30, 10)]:
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload, request=request))) as client:
            assert _fetch_document(client, url, tmp_path / "official", budget) is None
    assert list(tmp_path.iterdir()) == []


def test_extensionless_preparation_creates_bound_typed_cache_and_preserves_edited_file(tmp_path, monkeypatch):
    job = _job(tmp_path)
    row = _row("20260924 slides", "20260924 slides")
    _index(job, [row])
    live = ContentItem(course_id="course-1", content_id="item-1", title=row["title"], kind="文件", parent_path="教学内容/第一周", attachments=[AttachmentRef(filename=row["attachments"][0]["filename"], url=row["attachments"][0]["path"])])
    from pku_sync import auth, materials
    calls = []
    def response(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=b"%PDF-1.4\n%%EOF", request=request)
    monkeypatch.setattr(auth, "get_session", lambda **kwargs: httpx.Client(transport=httpx.MockTransport(response)))
    monkeypatch.setattr(materials, "walk_materials", lambda client, cid: ([live], []))
    settings = SimpleNamespace(pku_username="fixture", pku_password="fixture")
    paths, failures = prepare_lesson_sources(job, "selected", settings)
    assert not failures and len(paths) == 1 and paths[0].suffix == ".pdf"
    receipts = list(paths[0].parent.glob("*.source.json"))
    assert len(receipts) == 1 and set(json.loads(receipts[0].read_text())) == {"schema", "source_sha256", "suffix", "bytes", "sha256"}
    retry, failures = prepare_lesson_sources(job, "selected", SimpleNamespace(pku_username="", pku_password=""))
    assert retry == paths and not failures and len(calls) == 1
    paths[0].write_bytes(b"%PDF-student-edit")
    edited = paths[0].read_bytes()
    retry, failures = prepare_lesson_sources(job, "selected", settings)
    assert retry == [] and len(failures) == 1
    assert paths[0].read_bytes() == edited and len(calls) == 2


def test_changed_official_attachment_identity_cannot_replace_extensionless_source(tmp_path, monkeypatch):
    job = _job(tmp_path)
    row = _row("20260924 slides", "20260924 slides")
    _index(job, [row])
    live = ContentItem(course_id="course-1", content_id="item-1", title=row["title"], kind="文件", parent_path="教学内容/第一周", attachments=[AttachmentRef(filename=row["attachments"][0]["filename"], url="/bbcswebdav/xid-other/swapped")])
    from pku_sync import auth, materials
    monkeypatch.setattr(auth, "get_session", lambda **kwargs: httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("identity mismatch must not fetch"))))
    monkeypatch.setattr(materials, "walk_materials", lambda client, cid: ([live], []))
    paths, failures = prepare_lesson_sources(job, "selected", SimpleNamespace(pku_username="fixture", pku_password="fixture"))
    assert paths == [] and len(failures) == 1
