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
    DISCONNECTED_DIRECTORY_ACTION,
    DISCONNECTED_DIRECTORY_COPY,
    DISCONNECTED_DIRECTORY_TITLE,
    DISCONNECT_TOAST,
    RECONNECT_TOAST,
)
from pku_sync.panel.fake_workspace import PANEL_FORBIDDEN_STRINGS
from pku_sync.panel.student_ui import (
    ACTIVATED_TOAST,
    EMPTY_CODE_NOTICE,
    PANEL_COPY,
    STUDENT_UI_PATH,
    student_page,
)
from pku_sync.panel.webapi import create_app


def make_client() -> TestClient:
    from pku_sync.panel.connection import build_fake_connection_service
    from pku_sync.panel.fake_directory import build_fake_directory

    directory = build_fake_directory()
    return TestClient(
        create_app(
            settings=SimpleNamespace(data_dir="data", platform_token=""),
            runner=_NoopRunner(),
            directory_service=directory,
            connection_service=build_fake_connection_service(directory_service=directory),
        )
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


def test_student_ui_has_no_answer_or_editor_surface():
    assets = served_assets(make_client())
    for name, text in assets.items():
        assert "textarea" not in text.lower(), name
        assert "contenteditable" not in text.lower(), name
    # the only text input on the surface is the redemption code field
    assert assets["js"].count('type="text"') <= 1


def test_student_ui_never_carries_content_paths_or_tokens():
    assets = served_assets(make_client())
    for name, text in assets.items():
        for needle in PANEL_FORBIDDEN_STRINGS:
            if name == "css" and needle == "keyframes":
                # CSS's @keyframes at-rule is not the recordings' keyframes
                # directory: assert that is the only occurrence instead
                assert text.count("keyframes") == text.count("@keyframes")
                continue
            assert needle not in text, (name, needle)
        assert "NOTION_TOKEN" not in text
        assert "PLATFORM_TOKEN" not in text


# -- pinned onboarding copy ----------------------------------------------------


def test_onboarding_copy_matches_the_approved_prototype():
    onboarding = PANEL_COPY["onboarding"]
    assert onboarding["eyebrow"] == "激活学习空间"
    assert onboarding["title"] == "连接完成，只差一步"
    assert onboarding["subtitle"] == "确认下方的学校空间，并输入兑换码完成激活。"
    assert onboarding["progress"] == {
        "aria_label": "开通进度：第3步，共3步",
        "label": "开始使用",
        "value": "3 / 3",
    }
    assert onboarding["code_label"] == "兑换码"
    assert onboarding["code_placeholder"] == "输入 8–16 位兑换码"
    assert onboarding["activate"] == "立即激活"
    assert onboarding["code_help"] == (
        "兑换码由课程助教或学校统一发放；开通凭据只保存在学校服务端，"
        "不会出现在你的浏览器或设备中。"
    )
    assert onboarding["footer"] == "激活后即可开始建立课程索引，并随时在设置中管理 Notion 连接。"
    assert onboarding["mini_status"] == "安全连接"
    assert onboarding["aside"]["eyebrow"] == "PKU · 学习工作台"
    assert onboarding["aside"]["title_lines"] == ["把每一堂课，", "变成可复习的知识。"]
    assert onboarding["aside"]["lead"] == (
        "连接你的 Notion 学习空间，自动整理讲次与资料索引。"
        "阅读、笔记、练习与问答，都在 Notion 中完成。"
    )


def test_onboarding_shows_the_two_connection_rows_with_both_notion_states():
    onboarding = PANEL_COPY["onboarding"]
    assert onboarding["connection_list_label"] == "连接状态"
    assert onboarding["campus"]["title"] == "北京大学"
    assert onboarding["campus"]["detail"] == "校园身份已确认"
    assert onboarding["notion"]["title"] == "Notion 学习空间"
    assert onboarding["notion"]["revoke"] == "撤销连接"
    assert onboarding["notion"]["reconnect"] == "重新连接"
    connection = PANEL_COPY["connection"]
    assert connection["connected"] == CONNECTED_COPY
    assert connection["disconnected"] == DISCONNECTED_COPY
    assert connection["disconnect_toast"] == DISCONNECT_TOAST
    assert connection["reconnect_toast"] == RECONNECT_TOAST


def test_onboarding_carries_exactly_the_four_approved_trust_items():
    items = PANEL_COPY["onboarding"]["trust_items"]
    assert len(items) == 4
    assert [item["title"] for item in items] == [
        "连接由你掌控",
        "只同步你选中的内容",
        "凭据不进入你的设备",
        "Notion 可随时撤销",
    ]
    assert items[0]["detail"] == "添加或移除哪些连接，完全由你决定，只连接你需要的。"
    assert items[1]["detail"] == "只有你明确选择的学习内容会被同步，不会触碰工作区里的其他页面。"
    assert items[2]["detail"] == "课堂服务的访问凭据只保存在学校服务端，浏览器和本地设备中都不会出现。"
    assert items[3]["detail"] == "连接可在设置里一键断开，已保存的笔记与练习记录不受影响。"


def test_empty_code_and_activation_copy_are_pinned():
    assert EMPTY_CODE_NOTICE == "请输入兑换码后再试一次。"
    assert ACTIVATED_TOAST == "学习空间已激活，课程索引已就绪。"
    assert PANEL_COPY["onboarding"]["empty_code_notice"] == EMPTY_CODE_NOTICE
    assert PANEL_COPY["onboarding"]["activated_toast"] == ACTIVATED_TOAST


def test_disconnected_directory_state_copy_is_shipped_to_the_browser():
    directory = PANEL_COPY["directory"]["disconnected"]
    assert directory["title"] == DISCONNECTED_DIRECTORY_TITLE
    assert directory["body"] == DISCONNECTED_DIRECTORY_COPY
    assert directory["action"] == DISCONNECTED_DIRECTORY_ACTION


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
    client = TestClient(webapi.create_app(settings, runner=_NoopRunner()))
    page = client.get("/")
    assert page.status_code == 200
    assert "PKU All in Notion 状态面板" in page.text  # engineering panel intact
    assert STUDENT_UI_PATH in page.text  # discoverable student surface
