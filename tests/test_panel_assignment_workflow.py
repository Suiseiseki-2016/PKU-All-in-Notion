"""End-to-end local assignment draft and guarded submit, with no live POST."""
from __future__ import annotations

import io
import hashlib
import json
import zipfile
from urllib.parse import quote

from fastapi.testclient import TestClient

from pku_sync.config import Settings
from pku_sync.panel import assignment_workflow
from pku_sync.panel.connection import build_fake_connection_service
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.platform_bridge import FakePlatformBridge
from pku_sync.panel.webapi import create_app


class NoopRunner:
    def submit(self, kind): return None
    def current(self): return None
    def last(self): return None


def _client(tmp_path, monkeypatch):
    folder = tmp_path / "course"
    (folder / "recordings").mkdir(parents=True)
    (folder / "materials").mkdir()
    (folder / "assignments").mkdir()
    (folder / "course.json").write_text(json.dumps({
        "course_id": "_103987_1", "name": "信息安全引论"
    }), "utf-8")
    (folder / "recordings" / "index.json").write_text("[]", "utf-8")
    (folder / "materials" / "index.json").write_text("[]", "utf-8")
    (folder / "assignments" / "index.json").write_text(json.dumps([{
        "title": "第一次书面作业", "content_id": "_1714169_1",
        "due_at": "2026-10-01 23:30", "source": "content-tree",
        "instructions": "答案和程序打成一个 ZIP", "files": ["原题.pdf"]
    }]), "utf-8")
    class Notion:
        def __init__(self, token): assert token == "notion"
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def get_page(self, page_id):
            assert page_id == "a" * 32
            return {"properties": {"Name": {"type": "title", "title": [{"plain_text": "我的答案"}]}}}
        def list_children(self, page_id):
            assert page_id == "a" * 32
            return [
                {"type": "heading_2", "heading_2": {"rich_text": [{"plain_text": "第一题"}]}},
                {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "答案内容"}]}},
                {"type": "code", "code": {"language": "python",
                    "rich_text": [{"plain_text": "print(42)"}]}},
                {"type": "pdf", "pdf": {}},
            ]
    monkeypatch.setattr(assignment_workflow, "NotionClient", Notion)
    settings = Settings(_env_file=None, data_dir=tmp_path, pku_username="student",
                        pku_password="secret", notion_token="notion", platform_token="session")
    directory = build_fake_directory()
    app = create_app(settings=settings, runner=NoopRunner(), directory_service=directory,
                     connection_service=build_fake_connection_service(),
                     platform_service=FakePlatformBridge(activated=True))
    return TestClient(app)


def test_prepare_from_notion_and_submit_only_after_exact_confirmation(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    base = "/api/campus/courses/_103987_1/assignments/_1714169_1"
    response = client.post(base + "/prepare", data={
        "archive_name": "学号姓名第1次作业.zip",
        "notion_url": "https://app.notion.com/p/" + "a" * 32,
        "code_filename": "hill_cipher.py",
    }, files=[("attachments", ("diagram.png", b"png", "image/png"))])
    assert response.status_code == 200, response.text
    draft = response.json()
    assert [item["name"] for item in draft["files"]] == ["答案.md", "hill_cipher.py", "diagram.png"]
    assert any("pdf" in warning for warning in draft["warnings"])
    assert any("Markdown 文本" in warning for warning in draft["warnings"])
    download = client.get(draft["download_url"])
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert archive.namelist() == ["答案.md", "hill_cipher.py", "diagram.png"]
        assert "答案内容" in archive.read("答案.md").decode("utf-8")
        assert archive.read("hill_cipher.py") == b"print(42)\n"

    calls = []
    class Campus:
        def close(self): calls.append("close")
    monkeypatch.setattr(assignment_workflow, "get_session", lambda **kwargs: Campus())
    monkeypatch.setattr(assignment_workflow, "fetch_form", lambda *args, **kwargs: calls.append("form") or object())
    monkeypatch.setattr(assignment_workflow, "submit_file",
                        lambda campus, form, archive: calls.append(archive.name) or
                        {"destinationUrl": "/webapps/assignment/review"})
    request = {"draft_id": draft["draft_id"], "sha256": draft["sha256"], "confirmed": False}
    assert client.post(base + "/submit", json=request).status_code == 400
    assert calls == []
    request["confirmed"] = True
    request["sha256"] = "0" * 64
    assert client.post(base + "/submit", json=request).status_code == 400
    assert calls == []
    request["sha256"] = draft["sha256"]
    sent = client.post(base + "/submit", json=request)
    assert sent.status_code == 200
    assert sent.json()["state"] == "submitted"
    assert calls == ["form", "学号姓名第1次作业.zip", "close"]
    assert client.post(base + "/submit", json=request).status_code == 400
    assert calls == ["form", "学号姓名第1次作业.zip", "close"]


def test_zip_draft_warns_when_teacher_gives_rar_example(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    index = tmp_path / "course" / "assignments" / "index.json"
    assignment = json.loads(index.read_text("utf-8"))
    assignment[0]["instructions"] = "多文件压成一个，例：学号姓名第1次作业.rar"
    index.write_text(json.dumps(assignment, ensure_ascii=False), "utf-8")
    response = client.post(
        "/api/campus/courses/_103987_1/assignments/_1714169_1/prepare",
        data={"archive_name": "学号姓名第1次作业.zip"},
        files=[("attachments", ("答案.pdf", b"pdf", "application/pdf"))],
    )
    assert response.status_code == 200, response.text
    assert any("不能据此认定老师接受 ZIP" in warning
               for warning in response.json()["warnings"])


def test_prepare_rejects_unknown_target_and_unsafe_name(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    base = "/api/campus/courses/_103987_1/assignments/"
    assert client.post(base + "_999999_1/prepare", data={
        "archive_name": "answer.zip", "notion_url": "https://app.notion.com/p/" + "a" * 32,
    }).status_code == 400
    response = client.post(base + "_1714169_1/prepare", data={
        "archive_name": "../answer.zip", "notion_url": "https://app.notion.com/p/" + "a" * 32,
    })
    assert response.status_code == 400
    assert not list(tmp_path.rglob("answer.zip"))


def test_official_source_link_only_for_indexed_assignment(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    base = "/api/campus/courses/_103987_1/assignments/"
    found = client.get(base + "_1714169_1/source", follow_redirects=False)
    assert found.status_code == 307
    assert found.headers["location"].startswith(
        "https://course.pku.edu.cn/webapps/assignment/uploadAssignment?"
    )
    assert "content_id=_1714169_1" in found.headers["location"]
    assert client.get(base + "_999999_1/source", follow_redirects=False).status_code == 404


def test_unshared_notion_page_explains_authorization_and_allows_local_export(tmp_path, monkeypatch):
    from pku_sync.notion import NotionError

    client = _client(tmp_path, monkeypatch)
    class Denied:
        def __init__(self, token): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def get_page(self, page_id):
            raise NotionError("not found", status=404, code="object_not_found")
    monkeypatch.setattr(assignment_workflow, "NotionClient", Denied)
    base = "/api/campus/courses/_103987_1/assignments/_1714169_1/prepare"
    denied = client.post(base, data={
        "archive_name": "学号姓名第1次作业.zip",
        "notion_url": "https://app.notion.com/p/" + "b" * 32,
    })
    assert denied.status_code == 409
    assert "授权" in denied.json()["detail"]
    local = client.post(base, data={"archive_name": "学号姓名第1次作业.zip"},
                        files=[("attachments", ("答案.pdf", b"answer", "application/pdf"))])
    assert local.status_code == 200
    assert [item["name"] for item in local.json()["files"]] == ["答案.pdf"]


def test_single_file_draft_preserves_original_and_requires_exact_confirmation(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    base = "/api/campus/courses/_103987_1/assignments/_1714169_1"
    original = b"%PDF-1.7\nanswer\x00\xff\n"
    response = client.post(base + "/prepare", data={
        "submission_mode": "single_file", "archive_name": "", "notion_url": "",
        "code_filename": "",
    }, files=[("attachments", ("学号姓名作业.pdf", original, "application/pdf"))])
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft["submission_mode"] == "single_file"
    assert draft["archive_name"] == "学号姓名作业.pdf"
    assert draft["files"] == [{"name": "学号姓名作业.pdf", "bytes": len(original)}]
    assert draft["sha256"] == hashlib.sha256(original).hexdigest()
    download = client.get(draft["download_url"])
    assert download.status_code == 200
    assert download.content == original
    assert quote("学号姓名作业.pdf") in download.headers["content-disposition"]
    saved = tmp_path / "submission-drafts" / draft["draft_id"] / "学号姓名作业.pdf"
    assert saved.read_bytes() == original

    calls = []
    class Campus:
        def close(self): calls.append("close")
    monkeypatch.setattr(assignment_workflow, "get_session", lambda **kwargs: Campus())
    monkeypatch.setattr(assignment_workflow, "fetch_form", lambda *args, **kwargs: calls.append("form") or object())
    monkeypatch.setattr(assignment_workflow, "submit_file",
                        lambda campus, form, file: calls.append((file.name, file.read_bytes())) or
                        {"destinationUrl": "/webapps/assignment/review"})
    request = {"draft_id": draft["draft_id"], "sha256": draft["sha256"], "confirmed": False}
    assert client.post(base + "/submit", json=request).status_code == 400
    request["confirmed"] = True
    request["sha256"] = "0" * 64
    assert client.post(base + "/submit", json=request).status_code == 400
    assert calls == []
    request["sha256"] = draft["sha256"]
    assert client.post(base + "/submit", json=request).status_code == 200
    assert calls == ["form", ("学号姓名作业.pdf", original), "close"]


def test_single_file_draft_rejects_ambiguous_or_unsafe_inputs(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    base = "/api/campus/courses/_103987_1/assignments/_1714169_1/prepare"
    one = [("attachments", ("答案.pdf", b"answer", "application/pdf"))]
    for data, files in [
        ({"submission_mode": "single_file"}, []),
        ({"submission_mode": "single_file"}, one + [("attachments", ("图.png", b"x", "image/png"))]),
        ({"submission_mode": "single_file", "archive_name": "answer.zip"}, one),
        ({"submission_mode": "single_file", "notion_url": "https://app.notion.com/p/" + "a" * 32}, one),
        ({"submission_mode": "single_file", "code_filename": "answer.py"}, one),
        ({"submission_mode": "single_file"}, [("attachments", ("答案.pdf", b"", "application/pdf"))]),
        ({"submission_mode": "single_file"}, [("attachments", ("../answer.pdf", b"x", "application/pdf"))]),
        ({"submission_mode": "single_file"}, [("attachments", ("答案.pdf", b"x" * (20 * 1024 * 1024 + 1), "application/pdf"))]),
        ({"submission_mode": "other", "archive_name": "answer.zip"}, one),
    ]:
        assert client.post(base, data=data, files=files).status_code == 400
    assert not (tmp_path / "submission-drafts").exists()
