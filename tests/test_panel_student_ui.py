"""Student panel UI surface: onboarding copy + zero external assets (VAL-META-019/020).

The onboarding screen is rendered from ONE pinned copy source
(`pku_sync.panel.student_ui.PANEL_COPY`), which the browser assertions read
back out of the served page. These tests pin the copy against the approved
prototype (docs/UI_DESIGN.html onboarding screen) and guard the surface
rules: same-origin assets only, no content/token/path fragments anywhere,
and no answer/editor surface.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from pku_sync.panel.connection import (
    CONNECTED_COPY,
    DISCONNECTED_COPY,
    DISCONNECT_TOAST,
    RECONNECT_TOAST,
)
from grading_workspace import PANEL_FORBIDDEN_STRINGS
from pku_sync.panel.student_ui import (
    EMPTY_CODE_NOTICE,
    PANEL_COPY,
    STUDENT_UI_PATH,
    student_page,
)
from pku_sync.panel.webapi import create_app


def make_client() -> TestClient:
    from pku_sync.panel.connection import build_fake_connection_service


    directory = None
    return TestClient(
        create_app(settings=SimpleNamespace(data_dir='data', platform_token=''), connection_service=build_fake_connection_service(directory_service=directory))
    )


class _NoopRunner:
    def submit(self, kind):
        return None

    def current(self):
        return None

    def last(self):
        return None


def served_assets(client) -> dict[str, str]:
    page = client.get(STUDENT_UI_PATH)
    css = client.get(f"{STUDENT_UI_PATH}/app.css")
    script = client.get(f"{STUDENT_UI_PATH}/app.js")
    for response in (page, css, script):
        assert response.status_code == 200
    return {"page": page.text, "css": css.text, "js": script.text}


# -- routes + assets -----------------------------------------------------------


def test_student_ui_page_and_assets_are_served():
    client = make_client()
    page = client.get(STUDENT_UI_PATH)
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert '<html lang="zh-CN">' in page.text
    assert 'name="viewport"' in page.text
    css = client.get(f"{STUDENT_UI_PATH}/app.css")
    assert css.headers["content-type"].startswith("text/css")
    script = client.get(f"{STUDENT_UI_PATH}/app.js")
    assert "javascript" in script.headers["content-type"]


def test_student_ui_loads_zero_external_assets():
    assets = served_assets(make_client())
    for name, text in assets.items():
        assert "http://" not in text, name
        assert "https://" not in text, name
        assert "@import" not in text, name
        assert "//fonts." not in text, name
    # the only asset references are the panel's own same-origin files
    assert f'href="{STUDENT_UI_PATH}/app.css"' in assets["page"]
    assert f'src="{STUDENT_UI_PATH}/app.js"' in assets["page"]


def test_student_ui_has_only_bounded_past_paper_reference_input():
    assets = served_assets(make_client())
    for name, text in assets.items():
        assert "contenteditable" not in text.lower(), name
    # Native quizzes accept student reference text; answers still live in
    # Notion. Allow only this explicitly labelled, bounded reference input.
    assert "data-campus-quiz-paper" not in assets["js"]
    assert "generateCampusQuiz" not in assets["js"]
    # redeem / legacy activation keep one plain text code field; auth uses
    # email + password inputs instead of free-form answer editors
    assert 'type="email"' in assets["js"]
    assert 'type="password"' in assets["js"]
    assert 'id="auth-form"' in assets["js"]
    assert 'id="redeem-form' in assets["js"]


def test_auth_copy_is_shipped_for_product_account_flows():
    auth = PANEL_COPY["auth"]
    assert auth["login"]["title"] == "登录学习空间"
    assert auth["register"]["submit"] == "注册"
    assert auth["forgot"]["submit"] == "发送重置邮件"
    account = PANEL_COPY["account"]
    assert account["unverified"] == "邮箱未验证"
    assert account["redeem"] == "兑换额度"
    assert account["redeem_gated"] == "完成邮箱验证后即可兑换额度。"
    page = student_page()
    assert "登录学习空间" in page
    assert "兑换额度" in page


def test_student_ui_never_carries_content_paths_or_tokens():
    assets = served_assets(make_client())
    for name, text in assets.items():
        for needle in PANEL_FORBIDDEN_STRINGS:
            if name == "css" and needle == "keyframes":
                # CSS's @keyframes at-rule is not the recordings' keyframes
                # directory: assert that is the only occurrence instead
                assert text.count("keyframes") == text.count("@keyframes")
                continue
            if name == "js" and needle == "keyframes":
                # Public availability and matching controls contain this word;
                # neither exposes a recording directory or an artifact path.
                public_names_removed = text.replace('keyframes_available', '').replace(
                    'campus-match-slides-keyframes', '')
                assert needle not in public_names_removed
                continue
            assert needle not in text, (name, needle)
        assert "NOTION_TOKEN" not in text
        assert "PLATFORM_TOKEN" not in text


# -- pinned onboarding copy ----------------------------------------------------












# -- the page ships the copy the browser renders from --------------------------


def test_page_embeds_the_whole_copy_blob_for_the_client_renderer():
    page = student_page()
    assert "window.PANEL_COPY" in page
    blob = page.split("window.PANEL_COPY = ", 1)[1].split(";</script>", 1)[0]
    parsed = json.loads(blob)
    assert parsed == PANEL_COPY
    # the copy is a single source of truth: the served page carries the exact
    # strings the browser assertions look for
    assert "请输入兑换码后再试一次。" in page
    assert CONNECTED_COPY in page and DISCONNECTED_COPY in page


def test_engineering_panel_is_untouched_and_links_to_the_student_ui(tmp_path):
    from pku_sync.panel import webapi

    settings = SimpleNamespace(
        data_dir=tmp_path,
        cloud_transcribe_url="https://relay.example/v1/transcribe",
        platform_token="",
        transcription_backend="local",
    )
    client = TestClient(webapi.create_app(settings))
    page = client.get("/")
    assert page.status_code == 200
    assert "window.PANEL_COPY" in page.text
    assert str(page.url).endswith(STUDENT_UI_PATH)
    assert "跑 daily" not in page.text


def test_responsive_navigation_and_accessibility_contract():
    """One asset read covers the former three small accessibility tests."""
    assets = served_assets(make_client())
    page, css, script = assets["page"], assets["css"], assets["js"]
    for expected in (
        ".sidebar{ display: none; }", "html { min-width: 320px; }",
        "overflow-wrap: anywhere", ".course-card", "flex-direction: column",
        ".recording-row", "flex-wrap: wrap",
        "min-height: 44px", "button:focus-visible", "select:focus-visible",
    ):
        assert "".join(expected.split()) in "".join(css.split()), expected
    for expected in (
        'class="mobile-overview"', 'data-action="open-dashboard"',
        'aria-label="', 'aria-current="page"', 'data-persistent-disclosure=',
        'aria-busy=', 'role="status" aria-live="polite"',
    ):
        assert expected in script, expected
    assert '<div id="app" aria-live="polite"></div>' in page
