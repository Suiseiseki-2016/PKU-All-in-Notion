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
    RECONNECT_LABEL,
    RECONNECT_TOAST,
    REVOKE_LABEL,
)

STUDENT_UI_PATH = "/app"
STATIC_DIR = Path(__file__).resolve().parent / "static"

EMPTY_CODE_NOTICE = "请输入兑换码后再试一次。"
REDEEMED_TOAST = "额度已到账。"
LOGGED_IN_TOAST = "已登录。"
REGISTERED_TOAST = "注册成功。请查收验证邮件后再兑换额度或使用云端功能。"
FORGOT_TOAST = "如果该邮箱已注册，重置链接会发到你的邮箱。"
RESEND_TOAST = "如果该邮箱尚未验证，验证链接会重新发送。"

AUTH_COPY = {
    "aside": {
        "eyebrow": "PKU · 学习工作台",
        "title_lines": ["把每一堂课，", "变成可复习的知识。"],
        "lead": (
            "同步教学网课程、课件与课堂录像，生成板书与课件图文笔记，并一键接入 AI 工具。"
        ),
        "note": "所有课程资料与课堂笔记均保存在本机，随时可通过 MCP 接入 AI 工具。",
    },
    "mini_status": "安全连接",
    "role": "学生端",
    "modes": {
        "login": "登录",
        "register": "注册",
        "forgot": "重置密码",
    },
    "login": {
        "eyebrow": "我的账号",
        "title": "登录学习空间",
        "subtitle": "使用你注册的邮箱和密码登录。",
        "submit": "登录",
        "submitting": "正在登录…",
        "switch_register": "没有账号？注册",
        "switch_forgot": "忘记密码",
    },
    "register": {
        "eyebrow": "我的账号",
        "title": "注册学习空间",
        "subtitle": "注册后会收到验证邮件；验证完成前不能兑换额度或使用云端功能。",
        "submit": "注册",
        "submitting": "正在注册…",
        "switch_login": "已有账号？登录",
    },
    "forgot": {
        "eyebrow": "我的账号",
        "title": "重置密码",
        "subtitle": "我们会向邮箱发送一次性重置链接（在浏览器中打开完成重置）。",
        "submit": "发送重置邮件",
        "submitting": "正在发送…",
        "switch_login": "返回登录",
    },
    "email_label": "邮箱",
    "email_placeholder": "name@example.com",
    "password_label": "密码",
    "password_placeholder": "至少 8 位",
    "confirm_password_label": "确认密码",
    "confirm_password_placeholder": "再次输入密码",
    "password_mismatch": "两次输入的密码不一致。",
    "empty_email": "请输入邮箱后再试一次。",
    "empty_password": "请输入密码后再试一次。",
    "failed": "操作没有成功，请稍后重试。",
    "network_failed": "连接中断，请检查网络后重试。已填写的内容仍保留。",
    "loading_failed": "已登录，部分信息暂时无法读取，请稍后刷新。",
    "logged_in_toast": LOGGED_IN_TOAST,
    "registered_toast": REGISTERED_TOAST,
    "forgot_toast": FORGOT_TOAST,
}

ACCOUNT_COPY = {
    "title": "我的账号",
    "signed_out": "未登录",
    "verified": "邮箱已验证",
    "unverified": "邮箱未验证",
    "unverified_help": "请先打开验证邮件中的链接；验证前不能兑换额度、转写或使用 AI。",
    "resend": "重发验证邮件",
    "refresh_verification": "我已验证，刷新状态",
    "verified_toast": "邮箱验证已完成。",
    "quota_stale": "额度更新失败，显示上次读取值。",
    "logout": "退出登录",
    "redeem_label": "兑换码",
    "redeem_placeholder": "输入 8–16 位兑换码",
    "redeem": "兑换额度",
    "redeem_help": "兑换码只增加额度，不会创建新账号。",
    "redeem_gated": "完成邮箱验证后即可兑换额度。",
    "empty_code_notice": EMPTY_CODE_NOTICE,
    "redeemed_toast": REDEEMED_TOAST,
    "redeem_failed": "兑换没有成功，请稍后重试。",
    "resend_toast": RESEND_TOAST,
}

CONNECTION_COPY = {
    "workspace_wait_hint": "首次创建会按课程建立 Notion 页面结构；页面保持打开即可，完成后会自动刷新。",
    "connected": CONNECTED_COPY,
    "disconnected": DISCONNECTED_COPY,
    "connecting": CONNECTING_COPY,
    "revoke": REVOKE_LABEL,
    "reconnect": RECONNECT_LABEL,
    "disconnect_toast": DISCONNECT_TOAST,
    "reconnect_toast": RECONNECT_TOAST,
    "busy": CONNECT_BUSY_MESSAGE,
    "failed": CONNECT_FAILED_COPY,
    "timeout": "仍在等待 Notion 授权；完成浏览器中的授权后，请重新打开面板查看状态。",
    "manage_label": "Notion 连接",
}

# Time readouts are assembled in the browser from these pieces so the panel
# never ships a frozen timestamp: relative ones feed the sync pill / 上次同步
# card / course cards, absolute ones the activity feed and 上课日期 row.
TIME_COPY = {
    "just_now": "刚刚",
    "minutes": "分钟前",
    "hours": "小时前",
    "days": "天前",
    "never": "尚未同步",
    "today": "今天",
    "yesterday": "昨天",
    "date": "{month}月{day}日",
    "full_date": "{year} 年 {month} 月 {day} 日",
}

ONBOARDING_COPY = {"notion": {"badge": "N", "title": "Notion 学习空间", "check_label": "已连接",
    "revoke": REVOKE_LABEL, "reconnect": RECONNECT_LABEL}}
COURSE_COPY = {
    "recordings": {
        'save': '验证并保存账号',
        'saving': '正在验证…',
        'verified': '教学网登录验证成功，账号已保存。',
        'credentials_hint': '先验证教学网登录，成功后才保存到本机。',
        'username': '教学网账号',
        'password': '教学网密码',
    },
}
LAUNCH_COPY = {"failed": "打开失败，请稍后重试。", "opened_external": "已在浏览器打开 Notion；可返回本窗口继续使用。"}
PANEL_COPY = {
    "onboarding": ONBOARDING_COPY,
    "course": COURSE_COPY,
    "launch": LAUNCH_COPY,
    "welcome": {
        "eyebrow": "开始你的学习空间",
        "title": "课程有条理，复习更轻松。",
        "description": "在这里查看教学网课程、作业和资料，把课堂录像整理成本地图文笔记并接入 AI 工具。",
        "steps": [
            {"title": "登录学习空间", "body": "用邮箱注册或登录，并完成邮件验证。"},
            {"title": "同步教学网课程", "body": "保存教学网账号，然后更新课程目录。"},
            {"title": "阅读笔记与接入 AI", "body": "整理课堂板书/课件图文笔记与课程总结，或通过 MCP 接入 AI 工具。"},
        ],
        "note": "整理录像会使用转写与 AI 额度。可随时从「使用引导」重看步骤。",
        "start": "开始设置",
        "skip": "我已了解，直接进入",
    },
    "brand": {
        "mark": "PKU", "name": "All in One", "note": "学习工作台",
        "title": "PKU All in One",
    },
    "nav": {"label": "主导航", "kicker": "学习空间", "dashboard": "我的课程"},
    "update": {
        "version": "Version {version}",
        "desktop_managed": "桌面版启动后会检查更新；发现新版本时会提示安装。",
        "up_to_date": "up to date",
        "available": "发现新版本 {version}",
        "action": "下次启动时更新",
        "retry": "Retry on next start",
        "requested": "Update requested for next start",
        "deferred": "Update deferred. The current version is still running; restart after retrying.",
        "request_failed": "Update request failed. Try again.",
    },
    "auth": AUTH_COPY,
    "account": ACCOUNT_COPY,
    "connection": CONNECTION_COPY,
    "notion": {"workspace_wait_hint": CONNECTION_COPY["workspace_wait_hint"]},
    "time": TIME_COPY,
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
    "ONBOARDING_COPY",
    "CONNECTION_COPY",
    "TIME_COPY",
    "COURSE_COPY",
    "LAUNCH_COPY",
    "PANEL_COPY",
    "student_page",
    "add_student_ui_routes",
]
