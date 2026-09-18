"""Schema-driven 课程资料索引 reads (VAL-META-004/005): vocabulary, errors, whitelist."""

from __future__ import annotations

import json

import pytest

import notion_meta_fake as fake

from pku_sync.notion_meta import materials
from pku_sync.notion_meta.errors import SchemaError


def test_validate_schema_accepts_verified_schema():
    materials.validate_material_schema(fake.material_schema())


def test_vocabularies_match_verified_values():
    assert set(materials.STATUS_VALUES) == {"待阅读", "已索引", "重点", "待确认"}
    assert set(materials.TYPE_VALUES) == {
        "课程手册",
        "讲义",
        "课堂课件",
        "复习资料",
        "往年题",
        "作业",
        "参考阅读",
        "平台说明",
    }


def test_validate_schema_missing_property_raises_explicit_error():
    schema = fake.material_schema()
    del schema["properties"]["资料类型"]
    with pytest.raises(SchemaError) as err:
        materials.validate_material_schema(schema)
    assert "资料类型" in str(err.value)
    assert err.value.property_name == "资料类型"
    # never a silent empty directory: the copy says the read stopped
    assert "不会静默" in str(err.value)


def test_validate_schema_renamed_property_raises_explicit_error():
    schema = fake.material_schema()
    schema["properties"]["资料类别"] = schema["properties"].pop("资料类型")
    with pytest.raises(SchemaError, match="资料类型"):
        materials.validate_material_schema(schema)


def test_validate_schema_wrong_property_type_raises_explicit_error():
    # 课程 is a TEXT property in the real workspace — a relation is a deviation
    schema = fake.material_schema()
    schema["properties"]["课程"] = {"type": "relation", "relation": {}}
    with pytest.raises(SchemaError) as err:
        materials.validate_material_schema(schema)
    assert "课程" in str(err.value)
    assert err.value.property_name == "课程"
    assert "relation" in str(err.value)


def test_material_row_serializes_to_exact_whitelist():
    row = fake.material_row(
        fake.ROWS[0],
        title="第一讲（1）课程介绍.pdf",
        course="计算机网络",
        type_="讲义",
        status="已索引",
        source_path=fake.SOURCE_PATH_VALUE,
        remarks=fake.REMARKS_VALUE,
    )
    material = materials.serialize_material_row(row, database_id=fake.DB_INDEX)
    dump = material.model_dump()
    assert set(dump) == {
        "id",
        "url",
        "title",
        "course",
        "type",
        "status",
        "updated",
        "parent",
        "source",
        "association",
    }
    assert dump["id"] == fake.ROWS[0]
    assert dump["url"] == fake.notion_url(fake.ROWS[0])
    assert dump["title"] == "第一讲（1）课程介绍.pdf"
    assert dump["course"] == "计算机网络"
    assert dump["type"] == "讲义"
    assert dump["status"] == "已索引"
    assert dump["updated"] == "2026-09-18T08:30:00.000Z"
    assert dump["parent"] == fake.DB_INDEX
    assert dump["source"] == "课程资料索引"
    # association state is its own structure, distinct from 处理状态
    assert dump["association"] == {"state": "none", "lecture": None}


def test_populated_row_never_leaks_source_path_or_remarks():
    row = fake.material_row(
        fake.ROWS[0],
        title="第一讲（1）课程介绍.pdf",
        course="计算机网络",
        type_="讲义",
        status="已索引",
        source_path=fake.SOURCE_PATH_VALUE,
        remarks=fake.REMARKS_VALUE,
    )
    text = json.dumps(materials.serialize_material_row(row, database_id=fake.DB_INDEX).model_dump(), ensure_ascii=False)
    assert "来源路径" not in text
    assert "备注" not in text
    assert fake.SOURCE_PATH_VALUE not in text
    assert fake.REMARKS_VALUE not in text


def test_empty_select_values_serialize_as_empty_strings():
    row = fake.material_row(
        fake.ROWS[5],
        title="平台使用说明.pdf",
        course="",
        type_="",
        status="待确认",
    )
    dump = materials.serialize_material_row(row, database_id=fake.DB_INDEX).model_dump()
    assert dump["course"] == ""
    assert dump["type"] == ""
