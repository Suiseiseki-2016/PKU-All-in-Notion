"""User-safe adapter errors for the Notion metadata directory.

All messages are safe for panel display: they never contain tokens, local
paths, signed URLs, or raw provider bodies. Titles of the user's own pages
are allowed — hub ambiguity must list the candidates (VAL-META-001).
"""

from __future__ import annotations


class NotionMetaError(RuntimeError):
    """Base class for metadata-adapter failures (user-safe copy)."""


class HubNotFoundError(NotionMetaError):
    """No current-semester 'Class Notes …' hub matched the search surface."""

    def __init__(self, semester: str):
        super().__init__(
            f"未找到当前学期（{semester}）的 Class Notes hub："
            "请在 Notion 确认 hub 标题以 Class Notes 开头并包含学期标识，"
            "或显式指定学期后重试。"
        )
        self.semester = semester


class HubAmbiguityError(NotionMetaError):
    """2+ plausible same-semester hubs: stop and list, never a silent pick."""

    def __init__(self, semester: str, candidates: list[str]):
        listed = "；".join(candidates)
        super().__init__(
            f"找到 {len(candidates)} 个当前学期（{semester}）的候选 hub：{listed}。"
            "请重命名多余页面或显式指定学期，不会自动任选其一。"
        )
        self.semester = semester
        self.candidates = list(candidates)


class SchemaError(NotionMetaError):
    """The 课程资料索引 schema deviates: explicit stop, never a silent empty directory."""

    def __init__(self, message: str, *, database: str = "", property_name: str = ""):
        super().__init__(message)
        self.database = database
        self.property_name = property_name


class MaterialDatabaseNotFoundError(SchemaError):
    """The hub has no 课程资料索引 child database."""

    def __init__(self, found: list[str]):
        listed = "、".join(found) if found else "无"
        super().__init__(
            f"hub 子页面中未找到「课程资料索引」数据库（发现的数据库：{listed}）。"
            "请先在 Class Notes hub 下建立该数据库，目录读取已停止（不会静默返回空目录）。"
        )
        self.found = list(found)
