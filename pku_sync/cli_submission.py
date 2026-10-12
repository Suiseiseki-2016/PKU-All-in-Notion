"""CLI preview and second confirmation backed by the panel submission engine."""
import hashlib
from pathlib import Path
import uuid

import typer
from fastapi import HTTPException

from . import catalog_snapshots as catalog
from .store import safe_name
from .submit import SubmitError, deadline_warning, resolve_course_id
from .panel.assignment_workflow import (
    ConfirmSubmission, PreparationError, _MAX_FILE, _account, _assert_account,
    _assignment, _draft_dir, _requirements, _save_json, execute_submission,
)
from .panel.campus_catalog import campus_course


def read_answer(path):
    with Path(path).open('rb') as handle:
        raw = handle.read(_MAX_FILE + 1)
    if len(raw) > _MAX_FILE:
        raise PreparationError('答案文件超过 20 MB，请先拆分。')
    return raw.decode('utf-8')


def prepare_draft(settings, course, content, mode, source):
    root = Path(settings.data_dir)
    catalog.activate_account(settings)
    course_id = resolve_course_id(root, course)
    assignment = _assignment(root, course_id, content)
    owner = _account(settings)
    if not owner:
        raise PreparationError('请先保存教学网账号。')
    if mode == 'text':
        if not isinstance(source, str) or not source.strip():
            raise PreparationError('答案为空，拒绝提交。')
        raw, filename = source.encode('utf-8'), 'answer.txt'
    elif mode == 'file':
        path = Path(source)
        with path.open('rb') as handle:
            raw = handle.read(_MAX_FILE + 1)
        filename = safe_name(path.name)
        if not filename or filename in ('.', '..'):
            raise PreparationError('答案文件名无效。')
    else:
        raise PreparationError('提交方式无效。')
    if not raw or len(raw) > _MAX_FILE:
        raise PreparationError('答案文件为空或超过 20 MB，请先核对。')
    draft_id = uuid.uuid4().hex
    folder = _draft_dir(root, draft_id)
    folder.mkdir(parents=True)
    (folder / filename).write_bytes(raw)
    metadata = {'draft_id': draft_id, 'course_id': course_id, 'content_id': content,
        'course_title': campus_course(root, course_id)['title'],
        'assignment_title': assignment['title'], 'due_at': assignment.get('due_at', ''),
        'due_at_label': assignment.get('due_at_label', ''), 'instructions': assignment.get('instructions', ''),
        'source': assignment.get('source', ''), 'submission_mode': 'text' if mode == 'text' else 'single_file',
        'archive_name': filename, 'sha256': hashlib.sha256(raw).hexdigest(),
        'files': [{'name': filename, 'bytes': len(raw)}], 'warnings': [], 'state': 'prepared',
        'portal_account_sha256': owner, 'requirements_sha256': _requirements(assignment)}
    _assert_account(metadata, settings)
    _save_json(folder / 'draft.json', metadata)
    return metadata, folder


def run(settings, course, content, mode, source, yes, console):
    try:
        draft, folder = prepare_draft(settings, course, content, mode, source)
        console.print(f"目标：{draft['course_title']} · {draft['assignment_title']}（{draft['course_id']} / {draft['content_id']}）", markup=False)
        console.print('截止：' + (draft['due_at_label'] or draft['due_at'] or '教学网未公布，请核对原要求'), markup=False)
        console.print('要求：' + draft['instructions'], markup=False)
        console.print('草稿目录：' + str(folder), markup=False, soft_wrap=True)
        for item in draft['files']:
            console.print(f"清单：{item['name']} · {item['bytes']} 字节", markup=False)
        console.print('SHA256：' + draft['sha256'], markup=False, soft_wrap=True)
        if mode == 'text':
            console.print('完整文本：\n' + source, markup=False)
        if warning := deadline_warning(draft['due_at']):
            console.print(warning, style='yellow', markup=False)
        if not yes:
            console.print('预览草稿已保存。检查文件和清单后，加 --yes 才进入二次确认。')
            return
        if not typer.confirm('已检查草稿文件、目标和清单，确认向教学网正式提交？', default=False):
            console.print('已取消提交，草稿文件保留。')
            return
        result = execute_submission(settings, draft['course_id'], content,
            ConfirmSubmission(draft_id=draft['draft_id'], sha256=draft['sha256'], confirmed=True))
        console.print(result['message'], style='green', markup=False)
        console.print('回执已保存：' + str(folder / 'draft.json'), markup=False, soft_wrap=True)
    except (PreparationError, SubmitError, OSError, ValueError, HTTPException) as exc:
        console.print(str(exc.detail if isinstance(exc, HTTPException) else exc), style='red', markup=False)
        raise typer.Exit(1) from exc
    except Exception as exc:
        console.print('提交状态未确认，草稿和原记录已保留；请到作业中心核对记录，禁止重复发送。', style='red')
        raise typer.Exit(1) from exc
