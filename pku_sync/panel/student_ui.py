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
from .directory import LAUNCH_FAILED_COPY
from .exercises import (
    EXERCISE_IDENTITY_MISSING_COPY,
    EXERCISE_IDENTITY_MISSING_REASON,
    EXERCISE_IDENTITY_MISSING_TITLE,
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
    "course_counts": "{lectures} 个讲次 · {materials} 份课程资料 · 最近同步 {synced}",
    "empty": {
        "title": "还没有已索引的课程",
        "body": "连接课程并完成首次同步后，讲次与资料索引会出现在这里。",
        "action": "重新同步",
    },
    "error": {"title": "同步遇到问题", "action": "重新同步"},
    "success": {"title": "同步完成", "action": "回到总览"},
    "disconnected": {
        "title": DISCONNECTED_DIRECTORY_TITLE,
        "body": DISCONNECTED_DIRECTORY_COPY,
        "action": DISCONNECTED_DIRECTORY_ACTION,
    },
    "exercises": {
        "title": "\u004eotion \u7ec3\u4e60\u76ee\u5f55",
        "note": "\u5df2\u6574\u7406\u4e0e\u5df2\u751f\u6210\u7684\u7ec3\u4e60\u9875",
        "list_label": "\u7ec3\u4e60\u5217\u8868",
        "estimate": "预计 1–5 AI 点", "regrade": "重新批改", "confirm_regrade": "确认重新批改", "cancel_regrade": "取消", "mismatch_notice": "本地批改记录与 Notion 标记不一致，请手动确认。", "settlement_unknown": "结算未知", "actions": {"organized": "\u5728 Notion \u4f5c\u7b54", "pending-answer": "\u5728 Notion \u4f5c\u7b54", "pending-grade": "\u6279\u6539\u5df2\u63d0\u4ea4\u7b54\u6848", "graded": "\u5728 Notion \u67e5\u770b\u89e3\u6790"},
        "empty": "\u8fd8\u6ca1\u6709\u5df2\u6574\u7406\u7684\u7ec3\u4e60\u3002\u5b8c\u6210\u7ec4\u7ec7\u540e\uff0c\u7ec3\u4e60\u4f1a\u51fa\u73b0\u5728\u8fd9\u91cc\u3002",
        "grade_notice": "批改入口将在练习服务就绪后开放。",
        "grading": {
            "working": "正在批改「{title}」",
            "explanation": "后端正在读取 Notion 中的答案，按需调用 AI 评分并写回解析。请稍候。",
            "estimate": "预计 1–5 AI 点 · 完成后按实际用量结算",
            "completed": "批改完成",
            "settlement": "本次消耗 {points} AI 点",
            "result": "在 Notion 查看解析",
            "back": "回到练习目录",
            "blocked": "批改没有开始，请先完成作答。",
            "failed": "批改服务暂时没有完成请求。",
            "retry": "重试"
        },
        "launch": {
            "opening": "\u6b63\u5728\u6253\u5f00 Notion",
            "failed_title": "\u6253\u5f00\u6ca1\u6709\u6210\u529f",
            "failed_body": "Notion \u9875\u9762\u6682\u65f6\u6ca1\u6709\u6253\u5f00\u3002\u53ef\u4ee5\u76f4\u63a5\u91cd\u8bd5\uff1b\u4f60\u7684\u7ec3\u4e60\u76ee\u5f55\u4e0e\u8bb0\u5f55\u90fd\u4e0d\u53d7\u5f71\u54cd\u3002",
            "retry": "\u91cd\u65b0\u6253\u5f00",
            "missing_title": EXERCISE_IDENTITY_MISSING_TITLE,
            "missing_body": EXERCISE_IDENTITY_MISSING_COPY,
            "grade_missing": EXERCISE_IDENTITY_MISSING_REASON,
            "fallback": "\u67e5\u770b\u8bfe\u7a0b\u9875",
        },
        "organize": {
            "title": "整理一套练习到 Notion",
            "body": "选择课程和讲次，整理一套混合题型练习页。题目、答案和解析只写入 Notion。",
            "action": "整理一套练习",
            "scope_label": "选择练习范围",
            "course_label": "课程",
            "lecture_label": "讲次",
            "estimate": "预计 1–5 AI 点 · 完成后按实际用量结算",
            "confirm": "确认整理",
            "cancel": "取消",
            "insufficient": "AI 点不足，至少需要保留 5 AI 点才能开始整理。",
            "working": "正在整理练习，请稍候。",
            "completed": "练习已整理到 Notion",
            "settlement": "本次消耗 {points} AI 点",
            "blocked": "练习没有开始",
            "retry": "重试",
        },
    }
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

DASHBOARD_COPY = {
    "sync_pill": "上次同步 · {relative}",
    "sync_action": "同步课程",
    "overview": {"title": "学习空间概览", "note": "索引与同步状态"},
    "stats": {
        "lectures": {
            "label": "已索引讲次",
            "unit": "个",
            "foot": "{courses} 门课程 · 讲次页均已就绪",
        },
        "materials": {
            "label": "已索引资料",
            "unit": "份",
            "foot": "课件 · 手册 · 课堂录像笔记",
        },
        "sync": {"label": "上次同步", "foot": "索引与状态保持最新"},
    },
    "activity": {
        "title": "最近动态",
        "note": "索引、同步与 Notion 学习记录",
        "item": "已同步「{title}」",
        "detail": "{course} · {type}",
    },
    "course_action": "查看课程",
}

COURSE_COPY = {
    "breadcrumb_home": "总览",
    "breadcrumb_courses": "我的课程",
    "kicker": "{scope}学习中心 · Notion 课程页",
    "desc": (
        "课程页包含讲次索引、资料概览与同步状态。选择一个讲次，查看标题、日期与关联资料清单；"
        "正文、笔记与练习都在 Notion 讲次页中打开。"
    ),
    "sync_action": "同步已选内容",
    "lectures": {
        "title": "课程讲次",
        "note": "{lectures} 个讲次 · 选择后查看讲次索引",
        "hint_mapped": "{topic} · {count} 份关联资料",
        "hint_unmapped": "资料映射待确认 · 可先查看课程资料",
        "empty": "还没有讲次页；完成整理后会自动出现在这里。",
        "missing_page": "待建讲次页",
        "missing_page_copy": "讲次页尚未建立 · 整理完成后自动出现",
        "missing_page_fallback": "查看课程页",
    },
    "materials": {
        "title": "课程资料概览",
        "note": "课程级",
        "total_unit": "份已索引资料",
        "row_unit": "{count} 份",
        "source_note": (
            "来源：课程资料索引 · 课堂录像笔记\n"
            "只显示你选中并已索引的内容；客户端不保存资料正文。"
        ),
        "empty": "这门课程还没有已索引的资料。",
        "loading": "正在载入资料索引",
        "error": "资料索引读取失败，请稍后重试。",
    },
}

LECTURE_COPY = {
    "desc": (
        "{course} · {scope}\n"
        "本页是讲次索引：标题、日期、同步状态与关联资料。完整内容在 Notion 讲次页中打开。"
    ),
    "open_lecture": "在 Notion 打开讲次页",
    "info": {
        "title": "讲次信息",
        "date": "上课日期",
        "duration": "录制时长",
        "sync": "同步状态",
        "related": "关联资料",
        "page": "Notion 页面",
        "related_value": "{count} 份",
        "related_pending_suffix": " · 映射待确认",
        # the metadata-only directory carries no recording duration or class
        # date of its own: an unknown row says so instead of inventing one
        "unknown": "暂未记录",
        "page_ready": "讲次页已就绪",
        "page_pending_suffix": " · 资料关联待确认",
        "page_missing": "讲次页尚未建立",
        "source_note": "客户端只保存讲次与资料的索引信息，不保存课堂正文、转写或资料内容。",
    },
    "launch": {
        "title": "在 Notion 继续",
        "notes": {
            "title": "写笔记",
            "detail": "在讲次页的笔记区记录想法",
            "action": "在 Notion 写笔记",
        },
        "quiz": {
            "title": "做练习",
            "detail": "打开本讲关联的练习页",
            "action": "在 Notion 开始练习",
        },
        "hint": "这些动作都会在你的 Notion 学习空间中打开。",
    },
    "materials": {
        "title": "本讲相关资料",
        "note": "仅索引信息与来源 · 正文在 Notion 中查看",
        "controls_label": "资料查看方式",
        "views": {"lecture": "按讲次", "type": "按资料类型", "all": "全部资料"},
        "linked": "已关联本讲",
        "course_level": "课程级资料",
        "pending": "资料映射待确认",
        "pending_group": "待确认关联 · {count} 份",
        "group": "{type} · {count} 份",
        "source": "来源 · {source}",
        "open": "在 Notion 查看",
        "empty": {
            "title": "当前视图下暂无资料",
            "body": "可以切换「全部资料」查看课程级索引内容。",
        },
        "unmapped": {
            "title": "本讲资料映射尚未确认",
            "body": (
                "目前无法从课程资料索引建立确定的讲次关联。你可以先查看课程级资料，"
                "不会把未确认的内容误当成本讲材料。"
            ),
            "action": "查看课程资料",
        },
        "hint": (
            "提示：同一资料可能存在版本或重复项；此处只展示已索引的来源与状态，"
            "不暴露本地路径或资料正文。"
        ),
        "loading": "正在载入资料索引",
        "error": "资料索引读取失败，请稍后重试。",
    },
}

LAUNCH_COPY = {
    "lecture": "正在打开 Notion 讲次页。",
    "notes": "正在打开 Notion 笔记区。",
    "quiz": "正在打开 Notion 练习页。",
    "material": "正在打开 Notion 中的资料页。",
    "course": "正在打开 Notion 课程页。",
    "failed": LAUNCH_FAILED_COPY,
    "retry": "重新打开",
    "opening": "正在打开 Notion",
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
    "time": TIME_COPY,
    "dashboard": DASHBOARD_COPY,
    "course": COURSE_COPY,
    "lecture": LECTURE_COPY,
    "launch": LAUNCH_COPY,
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
    "TIME_COPY",
    "DASHBOARD_COPY",
    "COURSE_COPY",
    "LECTURE_COPY",
    "LAUNCH_COPY",
    "PANEL_COPY",
    "student_page",
    "add_student_ui_routes",
]
