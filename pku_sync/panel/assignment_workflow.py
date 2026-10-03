"""Local assignment preparation and explicitly confirmed Blackboard upload."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from ..auth import get_session
from ..notion import NotionClient, NotionError
from ..store import safe_name
from ..submit import AlreadySubmitted, SubmitError, fetch_form, submit_file
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


def add_assignment_routes(app: FastAPI, manager) -> None:
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
            }
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
    def download_draft(draft_id: str):
        try:
            folder = _draft_dir(Path(manager.settings.data_dir), draft_id)
            metadata = json.loads((folder / "draft.json").read_text("utf-8"))
            archive = folder / metadata["archive_name"]
            if not archive.is_file():
                raise PreparationError("草稿文件不存在。")
            return FileResponse(archive, filename=metadata["archive_name"])
        except (PreparationError, OSError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=404, detail="找不到这份本地提交草稿。") from exc

    @app.post("/api/campus/courses/{course_id}/assignments/{content_id}/submit")
    def submit(course_id: str, content_id: str, body: ConfirmSubmission) -> dict:
        if not body.confirmed:
            raise HTTPException(status_code=400, detail="请先检查文件、目标和截止时间。")
        root = Path(manager.settings.data_dir)
        try:
            assignment = _assignment(root, course_id, content_id)
            folder = _draft_dir(root, body.draft_id)
            metadata = json.loads((folder / "draft.json").read_text("utf-8"))
            if (metadata["course_id"] != course_id or metadata["content_id"] != content_id
                    or metadata.get("state") != "prepared"):
                raise PreparationError("草稿与这项作业不匹配，或已提交。")
            archive = folder / metadata["archive_name"]
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            if digest != body.sha256 or digest != metadata["sha256"]:
                raise PreparationError("待提交文件与预览时不同，请重新准备。")
            if not manager.settings.pku_username or not manager.settings.pku_password:
                raise PreparationError("请先保存教学网账号。")
            client = get_session(username=manager.settings.pku_username,
                                 password=manager.settings.pku_password)
            try:
                form = fetch_form(client, course_id, content_id, action="newAttempt")
                receipt = submit_file(client, form, archive)
            finally:
                client.close()
            metadata["state"] = "submitted"
            metadata["receipt"] = receipt
            metadata["assignment_title"] = assignment["title"]
            _save_json(folder / "draft.json", metadata)
            return {"state": "submitted", "receipt": receipt,
                    "message": "教学网已接受提交请求；若回执为空，请到教学网核对提交历史。"}
        except AlreadySubmitted as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (PreparationError, SubmitError, OSError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail="提交状态暂时无法确认，请先到教学网核对历史，避免重复提交。") from exc
