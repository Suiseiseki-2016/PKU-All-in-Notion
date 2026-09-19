"""Responsive and accessibility contracts for the student panel."""

from __future__ import annotations

from fastapi.testclient import TestClient

from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.student_ui import STUDENT_UI_PATH
from pku_sync.panel.webapi import create_app


def assets() -> tuple[str, str, str]:
    client = TestClient(create_app(directory_service=build_fake_directory()))
    page = client.get(STUDENT_UI_PATH)
    css = client.get(f"{STUDENT_UI_PATH}/app.css")
    script = client.get(f"{STUDENT_UI_PATH}/app.js")
    assert page.status_code == css.status_code == script.status_code == 200
    return page.text, css.text, script.text


def test_mobile_topbar_keeps_overview_reachable_when_sidebar_is_hidden():
    _, css, script = assets()
    assert ".sidebar { display: none; }" in css
    assert 'class="mobile-overview"' in script
    assert 'data-action="open-dashboard"' in script
    assert 'aria-label="' in script
    assert 'aria-current="page"' in script


def test_narrow_layout_wraps_cards_and_preserves_tap_targets():
    _, css, _ = assets()
    assert "html { min-width: 320px; }" in css
    assert "overflow-wrap: anywhere" in css
    assert ".course-card" in css and "flex-direction: column" in css
    assert ".material-row" in css and "flex-wrap: wrap" in css
    assert ".exercise-toolbar, .exercise-row" in css
    assert "min-height: 44px" in css


def test_navigation_views_and_grading_states_expose_accessibility_cues():
    page, css, script = assets()
    assert '<div id="app" aria-live="polite"></div>' in page
    assert "button:focus-visible" in css
    assert "select:focus-visible" in css
    assert 'aria-pressed="' in script
    assert 'aria-busy="true" aria-live="polite" role="status"' in script
    assert 'class="exercise-status ' in script
    assert "row.status_label" in script
