"""One pass over a course: announcements, materials, assignments, recordings.

Everything here is safe to re-run. Text files are rewritten only when their
content changes and attachments are skipped when the manifest shows the local
copy came from the same Blackboard file id, so a daily run touches only what
instructors actually changed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .manifest import Manifest
from .materials import (
    ToolUnavailable,
    fetch_announcements,
    fetch_deadlines,
    merge_deadlines,
    walk_materials,
)
from .models import Announcement, Assignment, ContentItem, Course, Recording
from .recordings import list_recordings, resolve_media
from .store import download_attachment, safe_name, safe_path, write_text


@dataclass
class SyncReport:
    course: Course
    announcements: int = 0
    items: int = 0
    downloaded: int = 0
    skipped: int = 0
    assignments: int = 0
    recordings: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # expected gaps, not failures

    @property
    def ok(self) -> bool:
        return not self.errors


def sync_all(
    client: httpx.Client,
    courses: list[Course],
    root: Path,
    resolve_recordings: bool = True,
) -> list[SyncReport]:
    try:
        deadlines = fetch_deadlines(client)
    except httpx.HTTPError as exc:
        deadlines = {}
        deadline_error = f"deadlines unavailable: {exc}"
    else:
        deadline_error = ""

    reports = []
    for course in courses:
        report = sync_course(
            client,
            course,
            root,
            deadlines.get(course.course_id, []),
            resolve_recordings=resolve_recordings,
        )
        if deadline_error:
            report.errors.append(deadline_error)
        reports.append(report)
    return reports


def sync_course(
    client: httpx.Client,
    course: Course,
    root: Path,
    deadlines: list[Assignment],
    resolve_recordings: bool = True,
) -> SyncReport:
    report = SyncReport(course=course)
    course_dir = root / safe_name(course.directory, course.course_id.strip("_"))
    course_dir.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(course_dir / "manifest.json")

    write_text(
        course_dir / "course.json",
        json.dumps(
            {
                **course.model_dump(),
                "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=1,
        )
        + "\n",
    )

    announcements = _step(report, "announcements", lambda: fetch_announcements(client, course.course_id)) or []
    _write_announcements(client, announcements, course_dir, manifest, report)

    walked = _step(report, "materials", lambda: walk_materials(client, course.course_id))
    items, tree_assignments = walked if walked else ([], [])
    _write_materials(client, items, course_dir, manifest, report)

    assignments = merge_deadlines(tree_assignments, deadlines)
    _write_assignments(assignments, deadlines, course_dir, report)

    recordings = _step(report, "recordings", lambda: list_recordings(client, course.course_id)) or []
    _write_recordings(client, recordings, course_dir, report, resolve_recordings)

    manifest.save()
    return report


def _step(report: SyncReport, label: str, action):
    """Run one stage; a failure is recorded and the rest of the sync continues."""
    try:
        return action()
    except ToolUnavailable as exc:
        report.notes.append(f"{label}: {exc}")
        return None
    except Exception as exc:
        report.errors.append(f"{label}: {exc}")
        return None


def _write_announcements(
    client: httpx.Client,
    announcements: list[Announcement],
    course_dir: Path,
    manifest: Manifest,
    report: SyncReport,
) -> None:
    report.announcements = len(announcements)
    if not announcements:
        return

    target_dir = course_dir / "announcements"
    for announcement in announcements:
        stem = safe_name(f"{announcement.date or '0000-00-00'}_{announcement.title}", "announcement")
        lines = [
            f"# {announcement.title}",
            "",
            f"- 发布时间: {announcement.posted_at or '未知'}",
        ]
        if announcement.attachments:
            lines.append(f"- 附件: {len(announcement.attachments)}")
        lines += ["", announcement.body_text or "(无正文)", ""]

        if announcement.attachments:
            asset_dir = target_dir / stem
            lines.append("## 附件")
            lines.append("")
            for attachment in announcement.attachments:
                path, fetched = download_attachment(
                    client,
                    _absolute(attachment.url),
                    asset_dir,
                    attachment.filename,
                    manifest,
                    course_dir,
                )
                if path is None:
                    report.errors.append(f"announcement attachment: {attachment.url}")
                    continue
                report.downloaded += fetched
                report.skipped += not fetched
                lines.append(f"- [{path.name}]({_md_target(f'{stem}/{path.name}')})")
            lines.append("")

        write_text(target_dir / f"{stem}.md", "\n".join(lines))


def _write_materials(
    client: httpx.Client,
    items: list[ContentItem],
    course_dir: Path,
    manifest: Manifest,
    report: SyncReport,
) -> None:
    report.items = len(items)
    if not items:
        return

    materials_dir = course_dir / "materials"
    by_folder: dict[str, list[ContentItem]] = {}
    for item in items:
        by_folder.setdefault(item.parent_path, []).append(item)

    for folder, entries in by_folder.items():
        folder_dir = materials_dir / safe_path(*folder.split("/"))
        lines = [f"# {folder}", ""]

        for item in entries:
            lines.append(f"## {item.title}" + (f"  ({item.kind})" if item.kind else ""))
            lines.append("")
            if item.body_text:
                lines += [item.body_text, ""]

            for attachment in item.attachments:
                path, fetched = download_attachment(
                    client,
                    _absolute(attachment.url),
                    folder_dir,
                    attachment.filename or item.title,
                    manifest,
                    course_dir,
                )
                if path is None:
                    report.errors.append(f"attachment: {item.path} <- {attachment.url}")
                    continue
                report.downloaded += fetched
                report.skipped += not fetched
                lines += [f"附件: [{path.name}]({_md_target(path.name)})", ""]

        write_text(folder_dir / "_index.md", "\n".join(lines))


def _write_assignments(
    assignments: list[Assignment],
    deadlines: list[Assignment],
    course_dir: Path,
    report: SyncReport,
) -> None:
    report.assignments = len(assignments)

    lines = ["# 作业与截止时间", ""]
    if assignments:
        lines += ["| 截止时间 | 作业 | 来源 |", "| --- | --- | --- |"]
        for assignment in assignments:
            lines.append(
                f"| {assignment.due_at or '未公布'} | {assignment.title} | {assignment.source} |"
            )
    else:
        lines.append("目前没有已发布的作业。")
    lines.append("")

    for assignment in assignments:
        if assignment.instructions:
            lines += [f"## {assignment.title}", "", assignment.instructions, ""]

    write_text(course_dir / "assignments.md", "\n".join(lines))
    write_text(
        course_dir / "deadlines.json",
        json.dumps([a.model_dump() for a in deadlines], ensure_ascii=False, indent=1) + "\n",
    )


def _write_recordings(
    client: httpx.Client,
    recordings: list[Recording],
    course_dir: Path,
    report: SyncReport,
    resolve: bool,
) -> None:
    report.recordings = len(recordings)
    if not recordings:
        return

    if resolve:
        for recording in recordings:
            try:
                resolve_media(client, recording)
            except Exception as exc:
                recording.unavailable_reason = str(exc)

    write_text(
        course_dir / "recordings" / "index.json",
        json.dumps([r.model_dump() for r in recordings], ensure_ascii=False, indent=1) + "\n",
    )


def _md_target(relative: str) -> str:
    """Percent-encode spaces so Markdown viewers resolve the link."""
    return relative.replace(" ", "%20")


def _absolute(url: str) -> str:
    """Blackboard emits site-relative attachment links."""
    if url.startswith("http"):
        return url
    return f"https://course.pku.edu.cn{url}"
