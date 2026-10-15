"""Local assignment preparation and explicitly confirmed Blackboard upload."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse, Response
from pydantic import BaseModel

from ..auth import get_session
from ..notion import NotionClient, NotionError
from ..store import safe_name
from ..submit import AlreadySubmitted, SubmitError, SubmissionFileChanged, fetch_form, submit_file, submit_text, inspect_submission
from .campus_catalog import campus_course

_MAX_FILE = 20 * 1024 * 1024
_MAX_TOTAL = 40 * 1024 * 1024
_PAGE_ID = re.compile(r"([0-9a-fA-F]{32})(?:$|[?/#])")
_CONTENT_ID = re.compile(r"^_\d+_\d+$")

class PreparationError(ValueError):
    pass

class ConfirmSubmission(BaseModel):
    draft_id: str
    sha256: str
    confirmed: bool

class ConfirmRequirements(BaseModel):
    confirmed: bool = False

def _page_id(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not (
        parsed.hostname in {"notion.so", "www.notion.so", "app.notion.com"}
        or (parsed.hostname or "").endswith(".notion.site")
    ):
        raise PreparationError("请输入已授权的 Notion 页面 HTTPS 链接。")
    found = _PAGE_ID.search(parsed.path + ("/" if parsed.path.endswith("/") else ""))
    if not found:
        raise PreparationError("Notion 链接中没有可识别的页面 ID。")
    return found.group(1)

def _rich_text(block: dict) -> str:
    kind = block.get("type") or ""
    return "".join(part.get("plain_text") or (part.get("text") or {}).get("content", "")
                   for part in (block.get(kind) or {}).get("rich_text") or [])

def export_notion_answer(client: NotionClient, page_id: str) -> tuple[str, str, list[str], list[str]]:
    page = client.get_page(page_id)
    properties = page.get("properties") or {}
    title_parts = next((value.get("title") or [] for value in properties.values()
                        if isinstance(value, dict) and value.get("type") == "title"), [])
    if not title_parts:
        title_parts = (properties.get("title") or {}).get("title") or page.get("title") or []
    title = "".join(part.get("plain_text") or (part.get("text") or {}).get("content", "")
                    for part in title_parts) or "Notion 答案"
    lines = ["# " + title, ""]
    python_blocks: list[str] = []
    warnings: list[str] = []
    count = 0

    def walk(parent: str, depth: int) -> None:
        nonlocal count
        if depth > 3:
            raise PreparationError("Notion 页面层级过深，请导出为文件后再添加。")
        for block in client.list_children(parent):
            count += 1
            if count > 500:
                raise PreparationError("Notion 页面超过 500 个块，请先拆分答案。")
            kind = block.get("type") or ""
            body = _rich_text(block)
            if kind.startswith("heading_"):
                level = int(kind[-1])
                lines.extend(["#" * level + " " + body, ""])
            elif kind in {"paragraph", "bulleted_list_item", "numbered_list_item", "quote", "to_do"}:
                prefix = {"paragraph": "", "bulleted_list_item": "- ",
                          "numbered_list_item": "1. ", "quote": "> ",
                          "to_do": "- [ ] "}.get(kind, "")
                lines.extend([prefix + body, ""])
            elif kind == "code":
                language = str((block.get("code") or {}).get("language") or "text").lower()
                if language in {"python", "py"}:
                    python_blocks.append(body)
                lines.extend(["\x60\x60\x60" + language, body, "\x60\x60\x60", ""])
            elif kind == "divider":
                lines.extend(["---", ""])
            elif kind in {"image", "file", "pdf", "video", "audio", "bookmark", "embed", "child_page"}:
                warnings.append("Notion 中的" + kind + "没有自动打包；如属答案附件，请手动添加。")
            elif kind:
                warnings.append("Notion 的" + kind + "块未转换；请检查导出的答案。")
            if block.get("has_children") and kind != "child_page":
                walk(block["id"], depth + 1)

    walk(page_id, 0)
    body = "\n".join(lines).strip() + "\n"
    if len(body.encode("utf-8")) > 1024 * 1024:
        raise PreparationError("导出的答案超过 1 MB，请先拆分或改用手动文件。")
    return title, body, python_blocks, sorted(set(warnings))

def _assignment(root: Path, course_id: str, content_id: str) -> dict:
    if not _CONTENT_ID.fullmatch(content_id):
        raise PreparationError("作业目标 ID 无效。")
    course = campus_course(root, course_id)
    matches = [item for item in course["assignments"]
               if item.get("content_id") == content_id and item.get("source") != "calendar"]
    if len(matches) != 1:
        raise PreparationError("目录中找不到唯一且可提交的正式作业，请先更新课程目录。")
    return matches[0]

def _draft_dir(root: Path, draft_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", draft_id):
        raise PreparationError("草稿编号无效。")
    return root / "submission-drafts" / draft_id

def _save_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
    temporary.replace(path)

def _account(settings):
    username = str(getattr(settings, 'pku_username', '') or '').strip()
    return hashlib.sha256(username.encode('utf-8')).hexdigest() if username else ''

def _assert_account(metadata, settings):
    if not metadata.get('portal_account_sha256') or metadata['portal_account_sha256'] != _account(settings):
        raise PreparationError('教学网账号已变化或旧草稿未绑定账号，请重新准备并预览。')

def _requirements(assignment):
    fields = {key: assignment.get(key, '') for key in
              ('content_id', 'source_id', 'title', 'source', 'instructions', 'due_at',
               'files', 'source_links')}
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()

def verified_draft_download(settings, draft_id, *, expected_sha256=None, course_id=None, content_id=None):
    root = Path(settings.data_dir)
    folder = _draft_dir(root, draft_id).resolve()
    if not folder.is_relative_to((root / 'submission-drafts').resolve()):
        raise PreparationError('草稿位置已变化，请重新准备并预览。')
    metadata = json.loads((folder / 'draft.json').read_text('utf-8'))
    if not isinstance(metadata, dict):
        raise PreparationError('草稿记录无效，请重新准备并预览。')
    _assert_account(metadata, settings)
    if metadata.get('draft_id') != draft_id:
        raise PreparationError('草稿身份与预览不一致，请重新准备。')
    if ((course_id is not None and metadata.get('course_id') != course_id)
            or (content_id is not None and metadata.get('content_id') != content_id)):
        raise PreparationError('草稿目标与当前作业不一致，请重新准备并预览。')
    recorded = metadata.get('sha256')
    if not isinstance(recorded, str) or not re.fullmatch('[0-9a-f]{64}', recorded):
        raise PreparationError('草稿完整性记录无效，请重新准备并预览。')
    if expected_sha256 is not None and expected_sha256 != recorded:
        raise PreparationError('草稿记录与预览时不同，请重新准备。')
    name = metadata.get('archive_name')
    if not isinstance(name, str) or Path(name).name != name:
        raise PreparationError('草稿文件名或位置已变化，请重新准备并预览。')
    archive = (folder / name).resolve()
    if not archive.is_relative_to(folder):
        raise PreparationError('草稿文件位置已变化，请重新准备并预览。')
    # Serve the exact immutable bytes we hashed, never a later FileResponse
    # path read that could race a changed file after this validation.
    with archive.open('rb') as handle:
        payload = handle.read(_MAX_TOTAL + 1024 * 1024 + 1)
    if len(payload) > _MAX_TOTAL + 1024 * 1024 or hashlib.sha256(payload).hexdigest() != recorded:
        raise PreparationError('草稿文件与预览时不同，请重新准备并核对。')
    if metadata.get('state') == 'prepared':
        assignment = _assignment(root, metadata['course_id'], metadata['content_id'])
        if metadata.get('requirements_sha256') != _requirements(assignment):
            raise PreparationError('正式作业要求或截止时间已变化，请重新准备并预览。')
    elif metadata.get('state') not in {'verification_pending', 'receipt_verified', 'submitted'}:
        raise PreparationError('草稿状态尚未核验，请重新准备并预览。')
    return metadata, archive, payload

def _verified_saved_receipt(metadata):
    receipt = metadata.get('receipt')
    if not isinstance(receipt, dict) or metadata.get('state') != 'submitted':
        return False
    html = receipt.get('review_history_html')
    return (receipt.get('portal_verified') is True
            and receipt.get('course_id') == metadata.get('course_id')
            and receipt.get('content_id') == metadata.get('content_id')
            and bool(receipt.get('verified_at')) and isinstance(html, str) and bool(html)
            and hashlib.sha256(html.encode('utf-8')).hexdigest() == receipt.get('review_history_sha256'))

@contextmanager
def _submission_lock(root, course_id, content_id, owner):
    folder = root / '.submission-locks'
    folder.mkdir(exist_ok=True)
    key = hashlib.sha256(json.dumps([owner, course_id, content_id]).encode('utf-8')).hexdigest()
    conn = sqlite3.connect(folder / (key + '.sqlite3'), timeout=0)
    try:
        try:
            conn.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=409, detail='该草稿正在提交，请等待并核对回执，勿重复发送。') from exc
        yield
    finally:
        conn.rollback()
        conn.close()

def add_assignment_routes(app: FastAPI, manager, *, fence_factory=None) -> None:
    if fence_factory is None:
        from .submission_permit import SubmissionPermit
        fence_factory = SubmissionPermit
    @app.post('/api/campus/courses/{course_id}/assignments/{content_id}/requirements')
    def identify_requirements(course_id: str, content_id: str, body: ConfirmRequirements):
        from .assignment_requirements import analyze
        from ..platform import PlatformError
        try:
            return analyze(manager.settings, course_id, content_id, confirmed=body.confirmed)
        except PreparationError as exc:
            raise HTTPException(409, detail=str(exc)) from exc
        except PlatformError as exc:
            raise HTTPException(getattr(exc, 'status_code', 503), detail=str(exc)) from exc
    @app.get('/api/campus/courses/{course_id}/assignments/{content_id}/status')
    def official_status(course_id: str, content_id: str):
        try:
            _assignment(Path(manager.settings.data_dir), course_id, content_id)
            account = _account(manager.settings)
            username, password = manager.settings.pku_username, manager.settings.pku_password
            if not account or not password:
                raise PreparationError('请先保存教学网账号，再读取正式作业提交状态。')
            client = get_session(username=username, password=password)
            try:
                result = inspect_submission(client, course_id, content_id)
            finally:
                client.close()
            if _account(manager.settings) != account:
                raise PreparationError('读取期间教学网账号已变化，请在当前账号重新核对。')
            return result
        except PreparationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail='教学网提交状态未确认，请到教学网查看原页面；本地回执不会代替当前状态。') from exc

    @app.get("/api/campus/courses/{course_id}/assignments/{content_id}/source")
    def assignment_source(course_id: str, content_id: str):
        from urllib.parse import quote
        try:
            _assignment(Path(manager.settings.data_dir), course_id, content_id)
        except (PreparationError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="找不到这项正式作业。") from exc
        target = ("https://course.pku.edu.cn/webapps/assignment/uploadAssignment?"
                  "course_id=" + quote(course_id) + "&content_id=" + quote(content_id))
        return RedirectResponse(target, status_code=307)

    @app.post("/api/campus/courses/{course_id}/assignments/{content_id}/prepare")
    async def prepare(course_id: str, content_id: str,
                      archive_name: str = Form(""), notion_url: str = Form(""),
                      code_filename: str = Form(""),
                      submission_mode: str = Form("zip"),
                      attachments: list[UploadFile] = File(default=[])) -> dict:
        root = Path(manager.settings.data_dir)
        try:
            account = _account(manager.settings)
            if not account:
                raise PreparationError('请先保存教学网账号，再准备正式作业草稿。')
            assignment = _assignment(root, course_id, content_id)
            archive_name = archive_name.strip()
            if submission_mode not in {"zip", "single_file"}:
                raise PreparationError("提交方式无效，请选择 ZIP 或单个原文件。")
            if submission_mode == "single_file":
                if archive_name or notion_url.strip() or code_filename.strip():
                    raise PreparationError("单文件模式只能上传一个本地原文件；请清空 ZIP 文件名和 Notion 答案页。")
                if len(attachments) != 1:
                    raise PreparationError("单文件模式需要恰好一个本地答案文件。")
            elif (not archive_name.lower().endswith(".zip")
                  or safe_name(archive_name) != archive_name
                  or len(archive_name) > 100):
                raise PreparationError("请按老师要求填写安全的 .zip 文件名。")
            files: dict[str, bytes] = {}
            warnings = []
            notion_title = ""
            if notion_url.strip():
                if not getattr(manager.settings, "notion_token", ""):
                    raise PreparationError("请先在本机连接 Notion，再读取答案页。")
                with NotionClient(manager.settings.notion_token) as client:
                    notion_title, body, python_blocks, export_warnings = export_notion_answer(
                        client, _page_id(notion_url))
                files["答案.md"] = body.encode("utf-8")
                warnings.extend(export_warnings)
                warnings.append(
                    "Notion 正文导出为答案.md（Markdown 文本），不是 PDF 或 Word；"
                    "请确认老师接受该格式。若要求文档，请先从 Notion 导出后以单个原文件提交。"
                )
                if code_filename.strip():
                    code_filename = code_filename.strip()
                    if safe_name(code_filename) != code_filename or not code_filename.endswith(".py"):
                        raise PreparationError("代码文件名必须是安全的 .py 名称。")
                    if len(python_blocks) != 1:
                        raise PreparationError("需要恰好一个 Python 代码块才能单独导出；也可手动添加代码文件。")
                    files[code_filename] = (python_blocks[0].rstrip() + "\n").encode("utf-8")
            elif code_filename.strip():
                raise PreparationError("先填写包含代码块的 Notion 答案页。")
            total = sum(len(value) for value in files.values())
            for upload in attachments:
                name = upload.filename or ""
                if not name or safe_name(name) != name or name in files:
                    raise PreparationError("附件文件名无效或重复：" + name)
                data = await upload.read(_MAX_FILE + 1)
                if len(data) > _MAX_FILE:
                    raise PreparationError("单个附件超过 20 MB：" + name)
                if submission_mode == "single_file" and not data:
                    raise PreparationError("提交文件为空，请选择已导出的完整答案文件。")
                total += len(data)
                if total > _MAX_TOTAL:
                    raise PreparationError("草稿包超过 40 MB，请缩小附件。")
                files[name] = data
            if not files:
                raise PreparationError("请填写 Notion 答案页，或至少添加一个本地答案文件。")
            if (submission_mode == "zip" and re.search(
                    r"\.rar\b", str(assignment.get("instructions") or ""), re.I)):
                warnings.append(
                    "老师要求中出现 .rar；本应用生成的是 ZIP，不能据此认定老师接受 ZIP。"
                    "请核对原要求，必要时在 Notion 外制作指定压缩格式。"
                )
            if submission_mode == "single_file":
                archive_name = next(iter(files))
            draft_id = uuid.uuid4().hex
            folder = _draft_dir(root, draft_id)
            folder.mkdir(parents=True)
            archive = folder / archive_name
            if submission_mode == "single_file":
                archive.write_bytes(files[archive_name])
            else:
                with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                    for name, data in files.items():
                        bundle.writestr(name, data)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            metadata = {
                "draft_id": draft_id, "course_id": course_id, "content_id": content_id,
                "assignment_title": assignment["title"], "due_at": assignment.get("due_at", ""),
                "due_at_label": assignment.get("due_at_label", ""),
                "instructions": assignment.get("instructions", ""),
                "source": assignment.get("source", ""), "archive_name": archive_name,
                "submission_mode": submission_mode,
                "sha256": digest, "files": [{"name": name, "bytes": len(data)}
                                            for name, data in files.items()],
                "notion_title": notion_title, "warnings": warnings,
                "state": "prepared",
                'portal_account_sha256': account,
                'requirements_sha256': _requirements(assignment),
            }
            _assert_account(metadata, manager.settings)
            _save_json(folder / "draft.json", metadata)
            return {**metadata, "download_url": f"/api/campus/submission-drafts/{draft_id}/file"}
        except PreparationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except NotionError as exc:
            if exc.status in {403, 404}:
                raise HTTPException(status_code=409, detail=(
                    "此 Notion 答案页尚未授权给桌面应用。请在该页的连接设置中授权本应用，"
                    "或先从 Notion 导出答案文件再作为附件添加。")) from exc
            raise HTTPException(status_code=502, detail="Notion 答案页暂时无法读取，请稍后重试。") from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Notion 答案页暂时无法读取；请检查授权，或添加已导出的本地文件。") from exc
        finally:
            for upload in attachments:
                await upload.close()

    @app.get("/api/campus/submission-drafts/{draft_id}/file")
    def download_draft(draft_id: str, sha256: str | None = None):
        try:
            metadata, archive, payload = verified_draft_download(manager.settings, draft_id, expected_sha256=sha256)
            response = FileResponse(archive, filename=metadata['archive_name'])
            headers = dict(response.headers)
            headers.pop('accept-ranges', None)
            return Response(payload, media_type=response.media_type, headers=headers)
        except PreparationError as exc:
            status = 403 if '账号' in str(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=404, detail="找不到这份本地提交草稿。") from exc

    @app.get('/api/campus/submission-drafts/{draft_id}/check')
    def check_draft(draft_id: str, sha256: str, course_id: str, content_id: str):
        try:
            metadata, _, _ = verified_draft_download(manager.settings, draft_id,
                expected_sha256=sha256, course_id=course_id, content_id=content_id)
            return {'valid': True, 'draft_id': draft_id, 'sha256': metadata['sha256']}
        except PreparationError as exc:
            status = 403 if '账号' in str(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=404, detail='草稿无法读取，请重新准备并预览。') from exc

    @app.get('/api/campus/submission-drafts/{draft_id}/receipt')
    def saved_receipt(draft_id: str):
        try:
            folder = _draft_dir(Path(manager.settings.data_dir), draft_id)
            metadata = json.loads((folder / 'draft.json').read_text('utf-8'))
            _assert_account(metadata, manager.settings)
            if not _verified_saved_receipt(metadata):
                raise HTTPException(status_code=409, detail='尚无完整教学网回执，请到教学网核对提交历史。')
            return JSONResponse({'draft_id': draft_id, 'archive_name': metadata['archive_name'],
                                 'sha256': metadata['sha256'], 'receipt': metadata['receipt']},
                headers={'Content-Disposition': f'attachment; filename="receipt-{draft_id}.json"',
                         'Cache-Control': 'no-store'})
        except PreparationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (OSError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=404, detail='回执记录不存在。') from exc

    @app.get('/api/campus/courses/{course_id}/submission-receipts')
    def receipt_history(course_id: str):
        root = Path(manager.settings.data_dir)
        owner = _account(manager.settings)
        items, pending = [], []
        if not owner:
            return {'items': items, 'pending': pending}
        for path in (root / 'submission-drafts').glob('*/draft.json'):
            if not re.fullmatch(r'[0-9a-f]{32}', path.parent.name):
                continue
            try:
                metadata = json.loads(path.read_text('utf-8'))
                if metadata.get('portal_account_sha256') != owner or metadata.get('course_id') != course_id:
                    continue
                draft_id = path.parent.name
                summary = {'draft_id': draft_id, 'content_id': metadata['content_id'],
                    'assignment_title': metadata['assignment_title'], 'archive_name': metadata['archive_name'],
                    'state': metadata.get('state'),
                    'recovery_url': f'/api/campus/submission-drafts/{draft_id}/recover'}
                verified = _verified_saved_receipt({**metadata, 'state': 'submitted'})
                identity = metadata.get('submission_fence')
                if verified and metadata.get('state') in ('submitted', 'receipt_verified'):
                    items.append({**summary, 'verified_at': metadata['receipt']['verified_at'],
                        'download_url': f'/api/campus/submission-drafts/{draft_id}/receipt' if metadata['state'] == 'submitted' else ''})
                elif (metadata.get('state') == 'verification_pending' or
                      (metadata.get('state') == 'prepared' and isinstance(identity, dict) and identity
                       and identity.get('state') != 'aborted')):
                    pending.append(summary)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return {'items': sorted(items, key=lambda row: row['verified_at'], reverse=True), 'pending': pending}

    @app.post('/api/campus/submission-drafts/{draft_id}/recover')
    def recover_submission(draft_id: str):
        root = Path(manager.settings.data_dir)
        try:
            folder = _draft_dir(root, draft_id)
            metadata = json.loads((folder / 'draft.json').read_text('utf-8'))
            _assert_account(metadata, manager.settings)
            with _submission_lock(root, metadata['course_id'], metadata['content_id'], _account(manager.settings)):
                metadata = json.loads((folder / 'draft.json').read_text('utf-8'))
                _assert_account(metadata, manager.settings)
                result = fence_factory(manager.settings, metadata, {},
                    lambda: _save_json(folder / 'draft.json', metadata)).reconcile()
                _assert_account(metadata, manager.settings)
                return result
        except PreparationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=404, detail='原提交记录未能验证，已保留现有文件。') from exc

    @app.post("/api/campus/courses/{course_id}/assignments/{content_id}/submit")
    def submit(course_id: str, content_id: str, body: ConfirmSubmission) -> dict:
        if not body.confirmed:
            raise HTTPException(status_code=400, detail="请先检查文件、目标和截止时间。")
        root = Path(manager.settings.data_dir)
        try:
            folder = _draft_dir(root, body.draft_id)
            if not folder.is_dir():
                raise PreparationError('草稿不存在，请重新准备。')
        except PreparationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _submit_locked(course_id, content_id, body)

    def _submit_locked(course_id: str, content_id: str, body: ConfirmSubmission) -> dict:
        root = Path(manager.settings.data_dir)
        try:
            return execute_submission(manager.settings, course_id, content_id, body, fence_factory=fence_factory)
        except AlreadySubmitted as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except HTTPException:
            raise
        except (PreparationError, SubmitError, OSError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail="提交状态暂时无法确认，请先到教学网核对历史，避免重复提交。") from exc

def execute_submission(settings, course_id, content_id, body, *, fence_factory=None):
    if not body.confirmed:
        raise PreparationError('请先检查文件、目标和截止时间。')
    if fence_factory is None:
        from .submission_permit import SubmissionPermit
        fence_factory = SubmissionPermit
    root = Path(settings.data_dir)
    with _submission_lock(root, course_id, content_id, _account(settings)):
        return _execute_submission_locked(settings, course_id, content_id, body, fence_factory)

def _execute_submission_locked(settings, course_id, content_id, body, fence_factory):
    root = Path(settings.data_dir)
    assignment = _assignment(root, course_id, content_id)
    folder = _draft_dir(root, body.draft_id)
    metadata = json.loads((folder / "draft.json").read_text("utf-8"))
    _assert_account(metadata, settings)
    if metadata.get('state') == 'receipt_verified':
        if (metadata['course_id'] != course_id or metadata['content_id'] != content_id
                or metadata['sha256'] != body.sha256):
            raise PreparationError('回执恢复与这份草稿不匹配。')
        with fence_factory(settings, metadata, assignment,
                           lambda: _save_json(folder / 'draft.json', metadata)) as fence:
            if fence.recovered_receipt is None:
                raise PreparationError('服务端尚未确认这次回执，禁止重复提交。')
        metadata['state'] = 'submitted'
        _save_json(folder / 'draft.json', metadata)
        return _submission_result(metadata, body.draft_id)
    for history in (root / 'submission-drafts').glob('*/draft.json'):
        previous = json.loads(history.read_text('utf-8'))
        if (previous.get('portal_account_sha256') == metadata['portal_account_sha256']
                and previous.get('course_id') == course_id and previous.get('content_id') == content_id
                and previous.get('state') == 'verification_pending'):
            raise PreparationError('已有提交请求尚未确认，请先到教学网核对历史，禁止新建草稿重复发送。')
    if metadata.get('requirements_sha256') != _requirements(assignment):
        raise PreparationError('正式作业要求或截止时间已变化，请重新准备并预览。')
    if (metadata["course_id"] != course_id or metadata["content_id"] != content_id
            or metadata.get("state") != "prepared"):
        raise PreparationError("草稿与这项作业不匹配，或已提交。")
    archive = folder / metadata["archive_name"]
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != body.sha256 or digest != metadata["sha256"]:
        raise PreparationError("待提交文件与预览时不同，请重新准备。")
    if not settings.pku_username or not settings.pku_password:
        raise PreparationError("请先保存教学网账号。")
    from ..catalog_snapshots import activate_account, assert_portal_readable
    activate_account(settings)
    assert_portal_readable(root, course_id)
    with fence_factory(settings, metadata, assignment,
                       lambda: _save_json(folder / 'draft.json', metadata)) as fence:
        client = get_session(username=settings.pku_username,
                             password=settings.pku_password)
        try:
            form = fetch_form(client, course_id, content_id,
                              action=None if metadata.get('submission_mode') == 'text' else 'newAttempt')
            _assert_account(metadata, settings)
            if metadata['requirements_sha256'] != _requirements(_assignment(root, course_id, content_id)):
                raise PreparationError('读取教学网期间作业要求发生变化，请重新预览。')
            metadata["state"] = "verification_pending"
            _save_json(folder / "draft.json", metadata)
            def before_post():
                fence.arm()
                _assert_account(metadata, settings)
                if metadata['requirements_sha256'] != _requirements(_assignment(root, course_id, content_id)):
                    raise PreparationError('确认共享许可期间作业要求发生变化，未发送，请重新预览。')
            try:
                if metadata.get('submission_mode') == 'text':
                    answer = archive.read_bytes().decode('utf-8')
                    receipt = submit_text(client, form, answer, expected_sha256=metadata['sha256'], before_post=before_post)
                else:
                    receipt = submit_file(client, form, archive, expected_sha256=metadata['sha256'], before_post=before_post)
            except SubmissionFileChanged:
                metadata['state'] = 'prepared'
                _save_json(folder / 'draft.json', metadata)
                raise
            if (not receipt.get("portal_verified") or receipt.get("course_id") != course_id
                    or receipt.get("content_id") != content_id):
                raise SubmitError("教学网提交历史未确认这项目标，请先核对历史。")
            metadata.update(state='receipt_verified', receipt=receipt, assignment_title=assignment['title'])
            _save_json(folder / 'draft.json', metadata)
            fence.complete(receipt)
        finally:
            client.close()
    metadata["state"] = "submitted"
    metadata["receipt"] = receipt
    metadata["assignment_title"] = assignment["title"]
    _save_json(folder / "draft.json", metadata)
    return _submission_result(metadata, body.draft_id)

def _submission_result(metadata, draft_id):
    return {'state': 'submitted', 'receipt': metadata['receipt'],
            'receipt_download_url': f'/api/campus/submission-drafts/{draft_id}/receipt',
            'message': '教学网提交历史已验证，正式回执已保存在本地。'}
