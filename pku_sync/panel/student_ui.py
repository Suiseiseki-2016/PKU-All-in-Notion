"""The student-facing panel UI (VAL-META-019/020): copy source + asset routes.

The approved prototype (docs/UI_DESIGN.html) is a client-rendered surface, so
the panel ships the same shape: one HTML shell, one stylesheet, one script —
all same-origin, zero external assets — plus ``window.PANEL_COPY``, which is
the SINGLE source of truth for every user-facing string. Pinning the copy in
Python keeps the approved wording verifiable by tests and identical to what
the browser renders.

This module owns the onboarding screen's copy; the connection copy lives with
the connection service (``pku_sync.panel.connection``) because the API states
and the UI labels must agree.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.responses import HTMLResponse, Response

from .connection import (
    CONNECTED_COPY,
    CONNECT_BUSY_MESSAGE,
    CONNECT_FAILED_COPY,
    CONNECTING_COPY,
    DISCONNECT_TOAST,
    DISCONNECTED_COPY,
    DISCONNECTED_DIRECTORY_ACTION,
    DISCONNECTED_DIRECTORY_COPY,
    DISCONNECTED_DIRECTORY_TITLE,
    RECONNECT_LABEL,
    RECONNECT_TOAST,
    REVOKE_LABEL,
)

STUDENT_UI_PATH = "/app"
STATIC_DIR = Path(__file__).resolve().parent / "static"

EMPTY_CODE_NOTICE = "请输入兑换码后再试一次。"
ACTIVATED_TOAST = "学习空间已激活，课程索引已就绪。"

ONBOARDING_COPY = {
    "aside": {
        "eyebrow": "PKU · 学习工作台",
        "title_lines": ["把每一堂课，", "变成可复习的知识。"],
        "lead": (
            "连接你的 Notion 学习空间，自动整理讲次与资料索引。"
            "阅读、笔记、练习与问答，都在 Notion 中完成。"
        ),
        "note": "你的学习资料只为你服务，专注于当下这门课。",
    },
    "mini_status": "安全连接",
    "role": "学生端",
    "role_note": "首次使用",
    "progress": {
        "aria_label": "开通进度：第3步，共3步",
        "label": "开始使用",
        "value": "3 / 3",
    },
    "eyebrow": "激活学习空间",
    "title": "连接完成，只差一步",
    "subtitle": "确认下方的学校空间，并输入兑换码完成激活。",
    "connection_list_label": "连接状态",
    "campus": {
        "badge": "P",
        "title": "北京大学",
        "detail": "校园身份已确认",
        "check_label": "已完成",
    },
    "notion": {
        "badge": "N",
        "title": "Notion 学习空间",
        "check_label": "已连接",
        "revoke": REVOKE_LABEL,
        "reconnect": RECONNECT_LABEL,
    },
    "trust_label": "信任与边界",
    "trust_items": [
        {
            "icon": "⌑",
            "title": "连接由你掌控",
            "detail": "添加或移除哪些连接，完全由你决定，只连接你需要的。",
        },
        {
            "icon": "✓",
            "title": "只同步你选中的内容",
            "detail": "只有你明确选择的学习内容会被同步，不会触碰工作区里的其他页面。",
        },
        {
            "icon": "⊘",
            "title": "凭据不进入你的设备",
            "detail": "课堂服务的访问凭据只保存在学校服务端，浏览器和本地设备中都不会出现。",
        },
        {
            "icon": "↺",
            "title": "Notion 可随时撤销",
            "detail": "连接可在设置里一键断开，已保存的笔记与练习记录不受影响。",
        },
    ],
    "code_label": "兑换码",
    "code_placeholder": "输入 8–16 位兑换码",
    "activate": "立即激活",
    "code_help": (
        "兑换码由课程助教或学校统一发放；开通凭据只保存在学校服务端，"
        "不会出现在你的浏览器或设备中。"
    ),
    "footer": "激活后即可开始建立课程索引，并随时在设置中管理 Notion 连接。",
    "empty_code_notice": EMPTY_CODE_NOTICE,
    "activated_toast": ACTIVATED_TOAST,
    "activate_failed": "激活没有成功，请稍后重试。",
}

CONNECTION_COPY = {
    "connected": CONNECTED_COPY,
    "disconnected": DISCONNECTED_COPY,
    "connecting": CONNECTING_COPY,
    "revoke": REVOKE_LABEL,
    "reconnect": RECONNECT_LABEL,
    "disconnect_toast": DISCONNECT_TOAST,
    "reconnect_toast": RECONNECT_TOAST,
    "busy": CONNECT_BUSY_MESSAGE,
    "failed": CONNECT_FAILED_COPY,
    "manage_label": "Notion 连接",
}

DIRECTORY_COPY = {
    "title": "学习空间",
    "desc": "讲次与资料的索引都在这里；正文、笔记与练习在 Notion 中打开。",
    "section": "我的课程",
    "loading": "正在同步索引",
    "course_counts": "{lectures} 个讲次 · {materials} 份课程资料",
    "empty": {
        "title": "还没有已索引的课程",
        "body": "连接课程并完成首次同步后，讲次与资料索引会出现在这里。",
    },
    "error": {"title": "同步遇到问题", "action": "重新同步"},
    "disconnected": {
        "title": DISCONNECTED_DIRECTORY_TITLE,
        "body": DISCONNECTED_DIRECTORY_COPY,
        "action": DISCONNECTED_DIRECTORY_ACTION,
    },
}

PANEL_COPY = {
    "brand": {
        "mark": "PKU",
        "name": "All in Notion",
        "note": "学习工作台",
        "title": "PKU All in Notion",
    },
    "nav": {"label": "主导航", "kicker": "学习空间", "dashboard": "总览"},
    "onboarding": ONBOARDING_COPY,
    "connection": CONNECTION_COPY,
    "directory": DIRECTORY_COPY,
}

_PAGE_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#f6f8f5">
<title>__TITLE__</title>
<link rel="stylesheet" href="__BASE__/app.css">
</head>
<body>
<div id="app" aria-live="polite"></div>
<div id="toast" class="toast" role="status" aria-live="polite"></div>
<script>window.PANEL_COPY = __COPY__;</script>
<script src="__BASE__/app.js" defer></script>
</body>
</html>
"""


def student_page() -> str:
    """The student SPA shell with the pinned copy embedded."""
    return (
        _PAGE_TEMPLATE.replace("__TITLE__", PANEL_COPY["brand"]["title"])
        .replace("__COPY__", json.dumps(PANEL_COPY, ensure_ascii=False))
        .replace("__BASE__", STUDENT_UI_PATH)
    )


def _asset(name: str, media_type: str) -> Response:
    text = (STATIC_DIR / name).read_text(encoding="utf-8")
    return Response(content=text, media_type=f"{media_type}; charset=utf-8")


def add_student_ui_routes(app) -> None:
    """Mount the student UI shell and its two same-origin assets."""

    @app.get(STUDENT_UI_PATH, response_class=HTMLResponse)
    def student_ui() -> str:
        return student_page()

    @app.get(f"{STUDENT_UI_PATH}/app.css")
    def student_ui_css() -> Response:
        return _asset("app.css", "text/css")

    @app.get(f"{STUDENT_UI_PATH}/app.js")
    def student_ui_js() -> Response:
        return _asset("app.js", "application/javascript")


__all__ = [
    "STUDENT_UI_PATH",
    "STATIC_DIR",
    "EMPTY_CODE_NOTICE",
    "ACTIVATED_TOAST",
    "ONBOARDING_COPY",
    "CONNECTION_COPY",
    "DIRECTORY_COPY",
    "PANEL_COPY",
    "student_page",
    "add_student_ui_routes",
]
