"""Campus-first, metadata-only course catalog for the student panel.

The course and lecture order comes from the Teaching Network recording index.
Attachments and video addresses never leave the local data directory here.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from ..pipeline import collect_jobs, recording_stage
from ..lecture_source_download import _override_lecture_ids, list_local_lecture_materials
from .recordings_api import recording_id, publication_matches_job, _safe_attachment_path
from .campus_todos import distinct_announcement_tasks, announcement_source_url
from .handbook_requirements import handbook_review_cards

_LECTURE_NUMBER = re.compile(r"第\s*([0-9０-９]+)\s*[讲次]", re.IGNORECASE)

def _summary_marker(folder: Path) -> dict:
    marker = _read_json(folder / "summary.json", {})
    if not isinstance(marker, dict):
        marker = {}
    if (folder / "course-summary.md").is_file() and not marker.get("title"):
        marker = {**marker, "title": "课程总结", "url": ""}
    return marker

def official_content_source_url(course_id, item):
    parent_id = str(item.get('parent_content_id') or '')
    if re.fullmatch(r'_\d+_\d+', parent_id):
        return ('https://course.pku.edu.cn/webapps/blackboard/content/listContent.jsp?' +
                urlencode({'course_id': course_id, 'content_id': parent_id}))
    return 'https://course.pku.edu.cn/'
_TEACHER_DEADLINE = re.compile(
    r"(?:DDL|截止(?:时间|日期)?|最晚(?:提交)?)(?:\s|[:：]|为|是){0,10}"
    r"(?P<date>20\d{2}[/-]\d{1,2}[/-]\d{1,2}"
    r"(?:（[^）]{1,8}）|\([^)]{1,8}\))?\s*\d{1,2}[:：]\d{2})",
    re.I,
)

def display_campus_time(value: str) -> str:
    """Show timezone-aware campus timestamps in PKU's local time."""
    raw = str(value or "")
    if not raw:
        return ""
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if moment.tzinfo is None:
        return raw
    beijing = timezone(timedelta(hours=8))
    return moment.astimezone(beijing).strftime("%Y-%m-%d %H:%M 北京时间")

def teacher_deadline_quote(instructions: str) -> str:
    """Extract one explicit teacher-written deadline, without assigning a timezone.

    This is a display clue when the Teaching Network's structured due field is
    empty. Conflicting dates or invalid calendar values must stay in the
    original instructions for the student to resolve.
    """
    candidates: set[str] = set()
    for match in _TEACHER_DEADLINE.finditer(str(instructions or "")):
        raw = match.group("date").strip()
        date = re.match(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})", raw)
        clock = re.search(r"(\d{1,2})[:：](\d{2})$", raw)
        if not date or not clock:
            continue
        try:
            datetime(*(int(value) for value in (*date.groups(), *clock.groups())))
        except ValueError:
            continue
        candidates.add(raw)
    return next(iter(candidates)) if len(candidates) == 1 else ""

def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default

def _number(text: str) -> int | None:
    match = _LECTURE_NUMBER.search(text or "")
    return int(match.group(1)) if match else None

def _section_states(root: Path, folder: Path, course_id: str, metadata: dict) -> dict:
    """Display cached evidence without granting it publication authority."""
    from ..catalog_snapshots import cached
    envelope = cached(root, course_id)
    result = {}
    for section, label in (("recordings", "讲次"), ("materials", "课程资料"),
                           ("announcements", "通知"), ("assignments", "作业")):
        path = folder / section / 'index.json'
        sections = envelope.get('snapshot', {}).get('sections', {}) if envelope else {}
        source = sections.get(section, {})
        rows = source.get('items') if isinstance(source, dict) and 'items' in source else _read_json(path, None)
        count = len(rows) if isinstance(rows, list) else 0
        status = metadata.get(section + '_status', '')
        failed = (status in {'error', 'partial', 'unavailable'} or
                  section == 'recordings' and metadata.get('_display_read_reason') == 'recordings_unavailable')
        if failed:
            state = 'cached' if isinstance(rows, list) else 'error'
            message = f'{label}读取失败或不完整；保留之前的内容，请更新这门课或到教学网核对。'
        elif not isinstance(rows, list) or status == 'not_synced':
            state = 'not_synced'
            message = f'{label}尚未读取，请更新这门课；不能据此认定没有内容。'
        else:
            state = 'available' if count else 'empty'
            message = f'已读取 {count} 项{label}。'
        result[section] = {'state': state, 'count': count, 'message': message}
    return result

def _courses(root: Path):
    for path in sorted(root.glob("*/course.json")):
        metadata = _read_json(path, {})
        if not isinstance(metadata, dict):
            continue
        course_id = metadata.get("course_id")
        local_status = {key: value for key, value in metadata.items() if key.endswith('_status')}
        from ..catalog_snapshots import cached
        canonical = cached(root, course_id) if isinstance(course_id, str) else None
        if canonical:
            metadata = {**metadata, **canonical["snapshot"]["course"],
                        "catalog_revision": canonical["revision"],
                        "snapshot_complete": True,
                        "announcements_status": "available", "assignments_status": "available"}
        title = metadata.get("name")
        from ..catalog_snapshots import CatalogUnavailable, assert_portal_readable
        metadata["read_only"] = canonical is None
        try:
            assert_portal_readable(root, course_id)
        except CatalogUnavailable:
            metadata["read_only"] = True
        # A complete older snapshot is readable evidence, not proof that a
        # failed new section read succeeded.
        metadata.update({key: value for key, value in local_status.items()
                         if value in {'error', 'partial', 'unavailable', 'not_synced'}})
        ledger = _read_json(root / 'portal-read-status.json', {})
        reason = ''
        if isinstance(ledger, dict) and isinstance(course_id, str) and course_id:
            from ..catalog_snapshots import cache_path
            if ledger.get('account_namespace') == cache_path(root, course_id).parent.name:
                row = ledger.get('courses', {}).get(course_id, {}) if isinstance(ledger.get('courses'), dict) else {}
                reason = row.get('reason', '') if isinstance(row, dict) else ''
        metadata['_display_read_reason'] = reason
        metadata['read_only_message'] = (
            '账号目录版本核对尚未完成；已有内容可读，请更新这门课后重试。'
            if reason == 'account_unconfirmed' else
            '教学网录像目录读取失败；已有讲次保留，请检查教学网连接后更新这门课。'
            if reason == 'recordings_unavailable' else
            '教学网读取尚未完成；保留已有内容，请检查教学网连接后更新这门课。')
        if isinstance(course_id, str) and course_id and isinstance(title, str) and title:
            yield path.parent, course_id, title, metadata

def _official_index(root: Path, folder: Path, course_id: str, section: str, default):
    from ..catalog_snapshots import cached
    envelope = cached(root, course_id)
    if envelope:
        return envelope["snapshot"]["sections"][section]["items"]
    rows = _read_json(folder / section / "index.json", default)
    return [item for item in rows if isinstance(item, dict)] if isinstance(rows, list) else default

def _category(item: dict) -> str:
    kind = str(item.get("kind") or "")
    title = str(item.get("title") or "")
    if kind in {"作业", "考试", "测试"} or any(word in title for word in ("作业", "测验", "考试", "随堂测试")):
        return "assignment"
    if item.get("files") or kind == "文件":
        return "material"
    return "information"

def campus_courses(root: Path) -> dict:
    rows = []
    for folder, course_id, title, metadata in _courses(root):
        materials = _official_index(root, folder, course_id, "materials", [])
        notices = _official_index(root, folder, course_id, "announcements", [])
        assignments = _official_index(root, folder, course_id, "assignments", None)
        from ..catalog_schema import ASSESSMENT_KINDS
        assessment_ids = {item.get("content_id") for item in materials
                          if isinstance(item, dict) and item.get("kind") in ASSESSMENT_KINDS
                          and item.get("content_id")} if isinstance(materials, list) else set()
        if isinstance(assignments, list):
            assignments = [item for item in assignments if isinstance(item, dict)
                           and item.get("content_id") not in assessment_ids]
        handbooks = handbook_review_cards(folder / "materials", materials) if isinstance(materials, list) else []
        rows.append({
            "id": course_id,
            "title": title,
            "code": str(metadata.get("code") or ""),
            "term": str(metadata.get("term") or ""),
            "recordings": len(collect_jobs(root, course_id)),
            "materials": sum(1 for item in materials if isinstance(item, dict)
                             and _category(item) == "material") if isinstance(materials, list) else 0,
            "assignments": sum(1 for item in assignments if item.get("source") != "calendar") if isinstance(assignments, list) else
                sum(1 for item in materials if isinstance(item, dict)
                    and item.get("kind") in {"作业", "Assignment"}) if isinstance(materials, list) else 0,
            "calendar_items": sum(1 for item in assignments if item.get("source") == "calendar") if isinstance(assignments, list) else 0,
            "handbook_reviews": len(handbooks),
            "announcements": len(notices) if isinstance(notices, list) else 0,
            "announcement_tasks": len(distinct_announcement_tasks(course_id, notices, assignments if isinstance(assignments, list) else [])) if isinstance(notices, list) else 0,
            "information": sum(1 for item in materials if isinstance(item, dict)
                               and _category(item) == "information") if isinstance(materials, list) else 0,
            "synced_at": str(metadata.get("synced_at") or ""),
            "catalog_revision": metadata.get("catalog_revision"),
            "read_only": metadata["read_only"],
            "read_only_message": metadata.get('read_only_message', '') if metadata['read_only'] else '',
        })
    return {"courses": rows}

def _historical_lessons(root: Path, course_id: str, metadata: dict) -> list[dict]:
    """Show retained sources without granting current processing eligibility."""
    from .. import catalog_snapshots as catalog, recording_history
    from .notion_workspace import historical_lecture
    from .recordings_api import assignment_snapshot_issue
    envelope = catalog.cached(root, course_id)
    if envelope is None or metadata.get('catalog_revision') != envelope['revision']:
        return []
    history = recording_history.read_cache(root, course_id, envelope)
    if not history or not history.get('recordings'):
        return []
    result = []
    for record in history['recordings']:
        source = record['source']
        row = {
            'course_id': course_id, 'source_id': record['source_id'],
            'source': deepcopy(source), 'title': source['title'],
            'recorded_at': source['recorded_at'],
            'original_revision': record['original_revision'],
            'accepted_revision': record['accepted_revision'],
            'history_current_revision': history['current_revision'],
            'historical': True, 'read_only': True, 'processing_allowed': False,
            'notion_lecture_url': '',
        }
        # Navigation needs the exact saved source/parent readback and a
        # readable, timely official directory. Cached history grants no
        # projection or publication authority.
        if not metadata['read_only'] and assignment_snapshot_issue(metadata) is None:
            target = historical_lecture(root, course_id, record, history)
            if target is not None:
                from uuid import UUID
                row['notion_lecture_url'] = 'https://www.notion.so/' + UUID(target['id']).hex
        result.append(row)
    return result

def campus_course(root: Path, course_id: str) -> dict:
    from ..catalog_snapshots import recording_source_id
    found = next(((folder, title, meta) for folder, key, title, meta in _courses(root)
                  if key == course_id), None)
    if found is None:
        raise ValueError("教学网课程尚未同步，请先刷新课程目录。")
    folder, title, metadata = found
    jobs = sorted(collect_jobs(root, course_id),
                  key=lambda job: (job.recording.recorded_at, job.recording.title))
    lessons = []
    keyframe_matches_by_path = {}
    from .notion_home import saved_home
    home = saved_home(root)
    home_id = home.get("id") if isinstance(home, dict) else None
    by_number: dict[int, list[dict]] = {}
    for job in jobs:
        stage, unavailable = recording_stage(job)
        try:
            source_id = recording_source_id(job.recording)
        except ValueError:
            source_id = ""
        from .transcript_review import review_pending
        row = {
            "id": recording_id(job),
            "source_id": source_id,
            "title": job.recording.title,
            "date": job.recording.date,
            "number": _number(job.recording.title),
            "stage": stage,
            "video_available": job.video.exists(),
            "transcript_available": (job.directory / "transcript.json").is_file(),
            "notes_available": (job.directory / "notes.md").is_file(),
            "keyframes_available": (job.directory / "keyframes" / "index.json").is_file(),
            "transcript_reviewed_pending": review_pending(job),
            "duration_seconds": (
                _read_json(job.directory / "duration.json", {}).get("duration_seconds")
                or _read_json(job.directory / "recording.json", {}).get("duration_seconds")
                or job.recording.duration_seconds
            ),
            "video_size": job.video.stat().st_size if job.video.exists() else None,
            "unavailable_reason": unavailable,
            "materials": [],
            "local_materials": list_local_lecture_materials(job, recording_id(job)),
        }
        note_topic = ""
        if row["notes_available"]:
            from .course_summary import note_outline
            try:
                outline = note_outline((job.directory / "notes.md").read_text("utf-8", errors="replace"))
                note_topic = " · ".join(name for name, _ in outline.get("topics", [])[:2])
            except OSError:
                pass
        row["note_topic"] = note_topic
        row["publish_ready"] = bool(
            row["transcript_available"]
            and row["notes_available"]
            and row["keyframes_available"]
        )
        publication = _read_json(job.directory / "notion-publication.json", {})
        if (isinstance(publication, dict)
                and publication_matches_job(job, publication)
                and publication.get("home_id") == home_id
                and publication.get("note_url")
                and publication.get("lecture_url")):
            row["notion_note_url"] = publication["note_url"]
            row["notion_lecture_url"] = publication["lecture_url"]
        projected = {}
        if home.get("schema") == 2:
            entries = home.get("courses", {}).get(course_id, {}).get("lectures", {})
            matches = [entry for entry in entries.values() if isinstance(entry, dict)
                       and row["source_id"] and entry.get("source_id") == row["source_id"]]
            projected = matches[0] if len(matches) == 1 else {}
            if (not row.get("notion_note_url") and projected.get("source_id") == row["source_id"] and projected.get("url")):
                row["notion_lecture_url"] = projected["url"]
                if projected.get("note_url"):
                    row["notion_note_url"] = projected["note_url"]
        from .summary_acceptance import status as summary_status
        row["summary_acceptance"] = summary_status(job)
        remote_acceptance = projected.get("summary_acceptance", {})
        if (projected.get("source_id") == row["source_id"] and remote_acceptance
                and (not row["summary_acceptance"]["notes_sha256"]
                     or remote_acceptance.get("notes_sha256") == row["summary_acceptance"]["notes_sha256"])):
            row["summary_acceptance"] = remote_acceptance
        lessons.append(row)
        receipt = _read_json(job.directory / "keyframes" / "keyframe-matches.json", {})
        if isinstance(receipt, dict):
            for entry in receipt.get("matches", []):
                if isinstance(entry, dict) and entry.get("material_path"):
                    keyframe_matches_by_path[(entry["material_path"], row["id"])] = entry
        if row["number"] is not None:
            by_number.setdefault(row["number"], []).append(row)
    raw_materials = _official_index(root, folder, course_id, "materials", [])
    notices = _official_index(root, folder, course_id, "announcements", [])
    assignments = _official_index(root, folder, course_id, "assignments", [])
    handbook_reviews = handbook_review_cards(folder / "materials", raw_materials) if isinstance(raw_materials, list) else []
    unassigned = []
    all_materials = []
    overrides = _read_json(folder / "materials" / "matches.json", {})
    if not isinstance(overrides, dict):
        overrides = {}
    if isinstance(raw_materials, list):
        for item in raw_materials:
            if not isinstance(item, dict):
                continue
            material = {
                "source_id": str(item.get("source_id") or ""),
                "title": str(item.get("title") or "未命名课件"),
                "content_id": str(item.get("content_id") or ""),
                "kind": str(item.get("kind") or "资料"),
                "path": str(item.get("path") or ""),
                "files": [str(name) for name in item.get("files", []) if isinstance(name, str)],
                "attachments": [{"filename": str(asset.get("filename") or "附件"),
                                 "path": path}
                                for asset in item.get("attachments", [])
                                if isinstance(asset, dict)
                                if (path := _safe_attachment_path(asset.get("path")))],
                "parent_content_id": str(item.get("parent_content_id") or ""),
                "body_text": str(item.get("body_text") or ""),
                "category": _category(item),
                "index": len(all_materials),
                "assigned_lecture_ids": [],
            }
            all_materials.append(material)
            override = overrides.get(material["path"])
            if override in ("ignore", "unassigned"):
                material["match_state"] = "ignored" if override == "ignore" else "needs_review"
                unassigned.append(material)
                continue
            if lecture_ids := _override_lecture_ids(override):
                matched_lessons = [lesson for lesson in lessons if lesson["id"] in lecture_ids]
                if matched_lessons:
                    material["match_state"] = "confirmed"
                    material["assigned_lecture_ids"] = [lesson["id"] for lesson in matched_lessons]
                    for match in matched_lessons:
                        entry = keyframe_matches_by_path.get((material["path"], match["id"]))
                        match["materials"].append({**material, "match_source": "keyframe" if entry else "manual",
                                                   **({"matched_frames": entry.get("matched_frames", 0)} if entry else {})})
                    continue
            number = _number(material["path"] + " " + material["title"])
            matches = by_number.get(number, []) if number is not None else []
            if len(matches) == 1:
                material["match_state"] = "auto"
                material["match_source"] = "auto"
                material["assigned_lecture_ids"] = [matches[0]["id"]]
                matches[0]["materials"].append({**material})
            else:
                material["match_state"] = "needs_review"
                unassigned.append(material)
    # The formal assignment index intentionally stores only filenames.  Its
    # corresponding content-tree material retains stable, login-protected
    # WebDAV paths. Match by Blackboard content ID, never by a similar title.
    assignment_materials = {
        item["content_id"]: item for item in all_materials
        if item["category"] == "assignment" and item["content_id"]
    }
    formal_assignments = []
    from ..materials import ASSESSMENT_KINDS
    assessment_ids = {item['content_id'] for item in all_materials
                      if item['kind'] in ASSESSMENT_KINDS and item['content_id']}
    if isinstance(assignments, list):
        for item in assignments:
            if not isinstance(item, dict):
                continue
            if item.get('content_id') in assessment_ids:
                continue
            row = {**item, "due_at_label": display_campus_time(item.get("due_at")),
                   "teacher_deadline_quote": teacher_deadline_quote(item.get("instructions"))}
            if item.get("source") == "content-tree":
                material = assignment_materials.get(str(item.get("content_id") or ""))
                row["attachments"] = material["attachments"] if material else []
            formal_assignments.append(row)
    official_assessments = []
    for item in all_materials:
        if item.get('kind') not in ASSESSMENT_KINDS:
            continue
        content_id = str(item.get('content_id') or '')
        official_assessments.append({**item,
            'source_id': item.get('source_id') or (f'pku:{course_id}:materials:{content_id}' if content_id else ''),
            'source_url': official_content_source_url(course_id, item),
            'submission_state': 'unknown', 'due_at': '', 'due_at_label': '',
            'status_label': '请在教学网核对测验截止时间、作答与提交状态。'})
    result = {
        "id": course_id,
        "title": title,
        "code": str(metadata.get("code") or ""),
        "term": str(metadata.get("term") or ""),
        "lessons": lessons,
        "unassigned_materials": unassigned,
        "all_materials": all_materials,
        "announcements": [{**item, "source_url": announcement_source_url(course_id, str(item.get("id") or "")), "posted_at_label": display_campus_time(item.get("posted_at"))}
                          for item in notices if isinstance(item, dict)] if isinstance(notices, list) else [],
        "announcement_tasks": distinct_announcement_tasks(course_id, notices, assignments if isinstance(assignments, list) else []) if isinstance(notices, list) else [],
        "handbook_reviews": handbook_reviews,
        "announcements_status": str(metadata.get("announcements_status") or "not_synced"),
        "assignments_status": str(metadata.get("assignments_status") or "not_synced"),
        "assignments": formal_assignments,
        "official_assessments": official_assessments,
        "synced_at": str(metadata.get("synced_at") or ""),
        "catalog_revision": metadata.get("catalog_revision"),
        "read_only": metadata["read_only"],
        "read_only_message": metadata.get('read_only_message', '') if metadata['read_only'] else '',
        "section_states": _section_states(root, folder, course_id, metadata),
        "synced_at_label": display_campus_time(metadata.get("synced_at")),
        "existing_notion_url": str(metadata.get("existing_notion_url") or ""),
        "summary": _summary_marker(folder),
        "source_priority": ["recordings", "materials"],
    }
    historical = _historical_lessons(root, course_id, metadata)
    if historical:
        result['historical_lessons'] = historical
    return result
