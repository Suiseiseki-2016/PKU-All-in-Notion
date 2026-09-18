"""Schema-driven 课程资料索引 reads (VAL-META-004/005/008).

Verified schema: 资料(title)｜课程(text — NOT a relation)｜资料类型(select)｜
处理状态(select)｜来源路径(text)｜备注(text). The schema is fetched BEFORE
rows; a missing/renamed/wrong-type property is an explicit error, never a
silent empty directory. 来源路径 and 备注 exist in the schema but are never
read into directory payloads.
"""

from __future__ import annotations

from ..notion import page_url
from .entities import (
    Association,
    MaterialEntity,
    SOURCE_MATERIAL_INDEX,
)
from .errors import SchemaError
from .titles import normalize_title

MATERIAL_DATABASE_TITLE = "课程资料索引"

# Verified select vocabularies (library/notion-workspace.md) — pinned. Values
# are displayed verbatim; an empty select serializes as "" (panel renders 未标注).
STATUS_VALUES = ("待阅读", "已索引", "重点", "待确认")
TYPE_VALUES = (
    "课程手册",
    "讲义",
    "课堂课件",
    "复习资料",
    "往年题",
    "作业",
    "参考阅读",
    "平台说明",
)

# Required properties with their verified types.
_REQUIRED_PROPERTIES = {
    "资料": "title",
    "课程": "rich_text",
    "资料类型": "select",
    "处理状态": "select",
}


def validate_material_schema(database: dict) -> None:
    """Explicit SchemaError on any deviation — never a silent empty directory."""
    properties = database.get("properties") or {}
    problems: list[str] = []
    missing: str = ""
    for name, expected in _REQUIRED_PROPERTIES.items():
        prop = properties.get(name)
        if prop is None:
            problems.append(f"缺少属性「{name}」（期望 {expected}）")
            missing = missing or name
        elif prop.get("type") != expected:
            problems.append(f"属性「{name}」类型是 {prop.get('type')}，期望 {expected}")
            missing = missing or name
    if problems:
        raise SchemaError(
            f"「{MATERIAL_DATABASE_TITLE}」数据库 schema 与预期不符：{'；'.join(problems)}。"
            "目录读取已停止（不会静默返回空目录），请先修正数据库属性。",
            database=MATERIAL_DATABASE_TITLE,
            property_name=missing,
        )


def serialize_material_row(row: dict, *, database_id: str) -> MaterialEntity:
    """One row → exactly the whitelisted metadata fields."""
    props = row.get("properties") or {}
    return MaterialEntity(
        id=row["id"],
        url=page_url(row),
        title=normalize_title(_plain(props.get("资料"))),
        course=normalize_title(_plain(props.get("课程"))),
        type=_select(props.get("资料类型")),
        status=_select(props.get("处理状态")),
        updated=str(row.get("last_edited_time", "")),
        parent=database_id,
        source=SOURCE_MATERIAL_INDEX,
        association=Association(),
    )


def _plain(prop: dict | None) -> str:
    if not prop:
        return ""
    pieces = prop.get("rich_text") or prop.get("title") or []
    return "".join(piece.get("plain_text", "") for piece in pieces)


def _select(prop: dict | None) -> str:
    if not prop:
        return ""
    return ((prop.get("select") or {}).get("name")) or ""
