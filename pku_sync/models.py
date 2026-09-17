from __future__ import annotations

from pydantic import BaseModel, Field


class Course(BaseModel):
    course_id: str  # Blackboard internal id, e.g. "_104128_1"
    name: str
    code: str = ""  # course code shown in Blackboard, e.g. "04835010"
    term: str = ""
    source: str = ""  # which discovery strategy found it
    # Set by discover.assign_directories(); disambiguates courses that share a
    # name, such as two lecture sections of the same course.
    dir_name: str = ""

    @property
    def slug(self) -> str:
        """Filesystem-safe form of the course name, not necessarily unique."""
        import re

        cleaned = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", self.name).strip("_")
        return cleaned or self.course_id.strip("_")

    @property
    def directory(self) -> str:
        return self.dir_name or self.slug


class Announcement(BaseModel):
    course_id: str
    announcement_id: str = ""
    title: str
    posted_at: str = ""
    author: str = ""
    body_text: str = ""
    body_html: str = ""
    attachments: list["AttachmentRef"] = Field(default_factory=list)

    @property
    def date(self) -> str:
        return self.posted_at.split("T")[0] if self.posted_at else ""


class ContentItem(BaseModel):
    """A node in a course's content tree."""

    course_id: str
    content_id: str
    title: str
    kind: str = ""  # Blackboard's own icon label: 文件 / 内容文件夹 / 作业 / 项目 ...
    parent_path: str = ""  # e.g. "教学内容/第一讲"
    body_text: str = ""
    attachments: list["AttachmentRef"] = Field(default_factory=list)

    @property
    def path(self) -> str:
        return f"{self.parent_path}/{self.title}" if self.parent_path else self.title


class AttachmentRef(BaseModel):
    filename: str
    url: str
    size: int | None = None


class Assignment(BaseModel):
    course_id: str
    title: str
    content_id: str = ""
    due_at: str = ""
    source: str = ""  # "content-tree" or "calendar"
    instructions: str = ""
    attachments: list[AttachmentRef] = Field(default_factory=list)


class Recording(BaseModel):
    course_id: str
    title: str
    recorded_at: str = ""  # "2026-09-09 08:00:00" as shown by Blackboard
    teacher: str = ""
    play_url: str = ""  # playVideo.action?token=... (single-use CAS entry point)

    # Filled in by recordings.resolve_media()
    platform_course_id: str = ""  # streaming platform's numeric course id
    subject_id: str = ""  # streaming platform's numeric session id
    room: str = ""
    duration_seconds: int = 0
    mp4_url: str = ""  # direct, range-capable download
    m3u8_url: str = ""  # only when no direct mp4 is published
    unavailable_reason: str = ""  # why no media URL was obtained

    @property
    def media_url(self) -> str:
        return self.mp4_url or self.m3u8_url

    @property
    def date(self) -> str:
        return self.recorded_at.split(" ")[0] if self.recorded_at else ""

    @property
    def slug(self) -> str:
        import re

        base = f"{self.date}_{self.title}".strip("_")
        cleaned = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", base).strip("_")
        return cleaned or "recording"


Announcement.model_rebuild()
ContentItem.model_rebuild()
