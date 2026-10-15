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

from pku_sync.panel.platform_bridge import FakePlatformBridge
from pku_sync.panel.webapi import create_app


class NoopRunner:
    def submit(self, kind): return None
    def current(self): return None
    def last(self): return None


class FakeSubmissionPermit:
    """Explicit injection for existing panel fixtures; no account HTTP calls."""
    def __init__(self, settings, metadata, assignment, save):
        self.recovered_receipt = metadata.get('receipt') if metadata.get('state') == 'receipt_verified' else None
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def arm(self): pass
    def complete(self, receipt): pass


def _client(tmp_path, monkeypatch, fence_factory=FakeSubmissionPermit):
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
    directory = None
    app = create_app(settings=settings, connection_service=build_fake_connection_service(), platform_service=FakePlatformBridge(activated=True), submission_fence_factory=fence_factory)
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
                        lambda campus, form, archive, **kw: calls.append(archive.name) or
                        {"destinationUrl": "/webapps/assignment/review", "portal_verified": True,
                         "course_id": "_103987_1", "content_id": "_1714169_1"})
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
    persisted = json.loads((tmp_path / "submission-drafts" / draft["draft_id"] / "draft.json").read_text("utf-8"))
    assert persisted["receipt"] == sent.json()["receipt"]
    assert calls == ["form", "学号姓名第1次作业.zip", "close"]
    assert client.post(base + "/submit", json=request).status_code == 400
    assert calls == ["form", "学号姓名第1次作业.zip", "close"]


def _file_draft(client):
    base = '/api/campus/courses/_103987_1/assignments/_1714169_1'
    draft = client.post(base + '/prepare', data={'submission_mode': 'single_file'},
                        files=[('attachments', ('answer.pdf', b'answer bytes', 'application/pdf'))]).json()
    return base, draft, {'draft_id': draft['draft_id'], 'sha256': draft['sha256'], 'confirmed': True}


def test_changed_preview_file_cannot_be_downloaded(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _, draft, _ = _file_draft(client)
    path = tmp_path / 'submission-drafts' / draft['draft_id'] / draft['archive_name']
    path.write_bytes(b'changed after preview')
    response = client.get(draft['download_url'])
    assert response.status_code == 409
    assert '预览' in response.json()['detail']
    assert path.read_bytes() == b'changed after preview'


def test_changed_deadline_blocks_download_until_new_preview(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _, draft, _ = _file_draft(client)
    index = tmp_path / 'course/assignments/index.json'
    rows = json.loads(index.read_text('utf-8'))
    rows[0]['due_at'] = '2026-10-12 20:00'
    index.write_text(json.dumps(rows), 'utf-8')
    response = client.get(draft['download_url'])
    assert response.status_code == 409
    assert '截止' in response.json()['detail']


def test_download_preflight_checks_exact_preview_and_target(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _, draft, _ = _file_draft(client)
    check = draft['download_url'].removesuffix('/file') + '/check'
    params = {'sha256': draft['sha256'], 'course_id': '_103987_1', 'content_id': '_1714169_1'}
    verified = client.get(check, params=params)
    assert verified.status_code == 200
    assert verified.json() == {'valid': True, 'draft_id': draft['draft_id'], 'sha256': draft['sha256']}
    for field, wrong in [('sha256', '0' * 64), ('course_id', '_999_1'), ('content_id', '_999_1')]:
        assert client.get(check, params={**params, field: wrong}).status_code == 409
    assert client.get(draft['download_url'], params={'sha256': '0' * 64}).status_code == 409


def test_download_returns_the_bytes_verified_before_later_disk_change(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _, draft, _ = _file_draft(client)
    path = tmp_path / 'submission-drafts' / draft['draft_id'] / draft['archive_name']
    original = path.read_bytes()
    verify = assignment_workflow.verified_draft_download
    def change_after_validation(*args, **kwargs):
        result = verify(*args, **kwargs)
        path.write_bytes(b'changed between verification and streaming')
        return result
    monkeypatch.setattr(assignment_workflow, 'verified_draft_download', change_after_validation)
    response = client.get(draft['download_url'])
    assert response.status_code == 200
    assert response.content == original
    assert hashlib.sha256(response.content).hexdigest() == draft['sha256']
    assert path.read_bytes() != original


def test_switching_portal_account_invalidates_prepared_submission(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    base, draft, request = _file_draft(client)
    calls = []
    monkeypatch.setattr(assignment_workflow, 'get_session', lambda **kw: calls.append('login'))
    client.app.state.recording_manager.settings.pku_username = 'another-student'
    response = client.post(base + '/submit', json=request)
    assert response.status_code == 400 and '账号' in response.json()['detail']
    assert calls == []
    assert client.get(draft['download_url']).status_code == 403


def test_changed_official_requirements_require_new_preview(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    base, _, request = _file_draft(client)
    index = tmp_path / 'course/assignments/index.json'
    rows = json.loads(index.read_text('utf-8'))
    rows[0]['instructions'] = '新增必须提交的文件'
    index.write_text(json.dumps(rows), 'utf-8')
    calls = []
    monkeypatch.setattr(assignment_workflow, 'get_session', lambda **kw: calls.append('login'))
    response = client.post(base + '/submit', json=request)
    assert response.status_code == 400 and '要求' in response.json()['detail']
    assert calls == []


def test_simultaneous_requests_for_one_draft_can_post_only_once(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    client = _client(tmp_path, monkeypatch)
    base, _, request = _file_draft(client)
    entered, release = Event(), Event()
    forms, posts = [], []
    class Campus:
        def close(self): pass
    monkeypatch.setattr(assignment_workflow, 'get_session', lambda **kw: Campus())
    def form(*args, **kwargs):
        forms.append('form')
        if len(forms) == 1:
            entered.set()
            assert release.wait(5)
        return object()
    monkeypatch.setattr(assignment_workflow, 'fetch_form', form)
    monkeypatch.setattr(assignment_workflow, 'submit_file', lambda *a, **kw: posts.append('POST') or {
        'portal_verified': True, 'course_id': '_103987_1', 'content_id': '_1714169_1'})
    with ThreadPoolExecutor(2) as pool:
        pending = pool.submit(client.post, base + '/submit', json=request)
        try:
            assert entered.wait(3)
            repeated = client.post(base + '/submit', json=request)
            assert repeated.status_code == 409
            assert posts == []
        finally:
            release.set()
        assert pending.result(timeout=5).status_code == 200
    assert forms == ['form'] and posts == ['POST']


def test_new_draft_cannot_resend_an_unknown_submission(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    base, _, request = _file_draft(client)
    class Campus:
        def close(self): pass
    posts = []
    monkeypatch.setattr(assignment_workflow, 'get_session', lambda **kw: Campus())
    monkeypatch.setattr(assignment_workflow, 'fetch_form', lambda *a, **kw: object())
    monkeypatch.setattr(assignment_workflow, 'submit_file', lambda *a, **kw: posts.append('POST') or {})
    assert client.post(base + '/submit', json=request).status_code == 400
    _, _, another = _file_draft(client)
    response = client.post(base + '/submit', json=another)
    assert response.status_code == 400 and '尚未确认' in response.json()['detail']
    assert posts == ['POST']


def test_verified_receipt_survives_page_reload_and_is_account_scoped(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    base, draft, request = _file_draft(client)
    class Campus:
        def close(self): pass
    html = '<html>Submission History: answer.pdf</html>'
    receipt = {'portal_verified': True, 'course_id': '_103987_1', 'content_id': '_1714169_1',
               'verified_at': '2026-10-04T08:00:00Z', 'review_history_html': html,
               'review_history_sha256': hashlib.sha256(html.encode()).hexdigest()}
    monkeypatch.setattr(assignment_workflow, 'get_session', lambda **kw: Campus())
    monkeypatch.setattr(assignment_workflow, 'fetch_form', lambda *a, **kw: object())
    monkeypatch.setattr(assignment_workflow, 'submit_file', lambda *a, **kw: receipt)
    submitted = client.post(base + '/submit', json=request).json()
    history = client.get('/api/campus/courses/_103987_1/submission-receipts').json()['items']
    assert len(history) == 1 and history[0]['draft_id'] == draft['draft_id']
    downloaded = client.get(submitted['receipt_download_url'])
    assert downloaded.status_code == 200 and downloaded.json()['receipt'] == receipt
    assert downloaded.headers['cache-control'] == 'no-store'
    assert 'attachment' in downloaded.headers['content-disposition']
    client.app.state.recording_manager.settings.pku_username = 'another-student'
    assert client.get(submitted['receipt_download_url']).status_code == 403
    assert client.get('/api/campus/courses/_103987_1/submission-receipts').json()['items'] == []
    client.app.state.recording_manager.settings.pku_username = 'student'
    metadata_path = tmp_path / 'submission-drafts' / draft['draft_id'] / 'draft.json'
    metadata = json.loads(metadata_path.read_text('utf-8'))
    metadata['receipt']['review_history_html'] = 'changed cached HTML'
    metadata_path.write_text(json.dumps(metadata), 'utf-8')
    assert client.get(submitted['receipt_download_url']).status_code == 409
    assert client.get('/api/campus/courses/_103987_1/submission-receipts').json()['items'] == []


def test_account_change_during_form_read_prevents_portal_post(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    base, _, request = _file_draft(client)
    class Campus:
        def close(self): pass
    posts = []
    monkeypatch.setattr(assignment_workflow, 'get_session', lambda **kw: Campus())
    def form(*a, **kw):
        client.app.state.recording_manager.settings.pku_username = 'another-student'
        return object()
    monkeypatch.setattr(assignment_workflow, 'fetch_form', form)
    monkeypatch.setattr(assignment_workflow, 'submit_file', lambda *a, **kw: posts.append('POST'))
    assert client.post(base + '/submit', json=request).status_code == 400
    assert posts == []


def test_file_change_during_form_read_never_posts_or_leaves_unknown_state(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    base, draft, request = _file_draft(client)
    class Campus:
        def close(self): pass
        def post(self, *args, **kwargs):
            raise AssertionError('changed bytes must never reach teaching portal')
    monkeypatch.setattr(assignment_workflow, 'get_session', lambda **kw: Campus())
    def form(*args, **kwargs):
        (tmp_path / 'submission-drafts' / draft['draft_id'] / 'answer.pdf').write_bytes(b'changed during form read')
        return object()
    monkeypatch.setattr(assignment_workflow, 'fetch_form', form)
    response = client.post(base + '/submit', json=request)
    assert response.status_code == 400 and '未发送' in response.json()['detail']
    metadata = json.loads((tmp_path / 'submission-drafts' / draft['draft_id'] / 'draft.json').read_text('utf-8'))
    assert metadata['state'] == 'prepared'


def test_official_status_is_live_read_only_and_does_not_resolve_pending_draft(tmp_path, monkeypatch):
    from types import SimpleNamespace
    client = _client(tmp_path, monkeypatch)
    base, draft, _ = _file_draft(client)
    metadata_path = tmp_path / 'submission-drafts' / draft['draft_id'] / 'draft.json'
    metadata = json.loads(metadata_path.read_text('utf-8'))
    metadata['state'] = 'verification_pending'
    metadata_path.write_text(json.dumps(metadata), 'utf-8')
    before = metadata_path.read_bytes()
    calls = []
    class Campus:
        def get(self, path, params):
            calls.append('GET')
            return SimpleNamespace(status_code=200, text='<h1>Review Submission History</h1>',
                url='https://course.pku.edu.cn' + path + '?course_id=_103987_1&content_id=_1714169_1')
        def post(self, *a, **kw): raise AssertionError('status query must not submit')
        def close(self): calls.append('close')
    monkeypatch.setattr(assignment_workflow, 'get_session', lambda **kw: Campus())
    response = client.get(base + '/status')
    assert response.status_code == 200 and response.json()['state'] == 'review_history'
    assert calls == ['GET', 'close'] and metadata_path.read_bytes() == before
    assert client.get(base.replace('_1714169_1', '_999999_1') + '/status').status_code == 409
    assert calls == ['GET', 'close']


def test_status_read_failure_never_uses_local_receipt(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    base, _, _ = _file_draft(client)
    monkeypatch.setattr(assignment_workflow, 'get_session', lambda **kw: (_ for _ in ()).throw(OSError('offline')))
    response = client.get(base + '/status')
    assert response.status_code == 502 and '本地回执不会代替' in response.json()['detail']


def test_unconfirmed_upload_keeps_draft_pending_and_cannot_resend(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    base = "/api/campus/courses/_103987_1/assignments/_1714169_1"
    draft = client.post(base + "/prepare", data={"submission_mode": "single_file"},
                        files=[("attachments", ("答案.pdf", b"answer", "application/pdf"))]).json()
    calls = []
    class Campus:
        def close(self): pass
    monkeypatch.setattr(assignment_workflow, "get_session", lambda **kwargs: Campus())
    monkeypatch.setattr(assignment_workflow, "fetch_form", lambda *args, **kwargs: object())
    monkeypatch.setattr(assignment_workflow, "submit_file", lambda *args, **kw: calls.append("POST") or {})
    request = {"draft_id": draft["draft_id"], "sha256": draft["sha256"], "confirmed": True}
    result = client.post(base + "/submit", json=request)
    assert result.status_code == 400 and "提交历史" in result.json()["detail"]
    metadata = json.loads((tmp_path / "submission-drafts" / draft["draft_id"] / "draft.json").read_text("utf-8"))
    assert metadata["state"] == "verification_pending" and "receipt" not in metadata
    assert client.post(base + "/submit", json=request).status_code == 400
    assert calls == ["POST"]


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
                        lambda campus, form, file, **kw: calls.append((file.name, file.read_bytes())) or
                        {"destinationUrl": "/webapps/assignment/review", "portal_verified": True,
                         "course_id": "_103987_1", "content_id": "_1714169_1"})
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
