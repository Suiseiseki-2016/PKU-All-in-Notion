"""Processing-status class coverage for material rows.

Association uncertainty and processing status are separate dimensions. This
 test keeps the approved 处理状态 vocabulary from collapsing into the blue
pending style used only by 待确认.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.student_ui import STUDENT_UI_PATH
from pku_sync.panel.webapi import create_app


def _client() -> TestClient:
    return TestClient(create_app(directory_service=build_fake_directory()))


def _material_row_function(js: str) -> str:
    start = js.index("function materialRow(")
    return js[start : js.index("\n  }", start) + len("\n  }")]


def test_processing_statuses_have_explicit_non_association_classes():
    js = _client().get(f"{STUDENT_UI_PATH}/app.js").text
    row = _material_row_function(js)

    assert '"待阅读": "reading"' in row
    assert '"重点": "focus"' in row
    assert '"待确认": "pending"' in row
    assert '"已索引": "indexed"' in row
    assert "item.status === \"已索引\" ? \"indexed\" : \"pending\"" not in row


def test_approved_status_classes_are_styled_separately_from_pending():
    css = _client().get(f"{STUDENT_UI_PATH}/app.css").text

    assert ".status-badge.reading" in css
    assert ".status-badge.focus" in css
    assert ".status-badge.pending" in css
    assert ".status-badge.indexed" in css
    assert ".assoc.pending" in css
