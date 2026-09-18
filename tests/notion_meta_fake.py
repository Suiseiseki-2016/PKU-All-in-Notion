"""Fake Notion client and workspace fixtures for the notion_meta adapter tests.

Everything mirrors the VERIFIED real workspace shapes (mission
library/notion-workspace.md) with SYNTHETIC ids and honeypot values: no real
page ids, tokens, or user content belong in committed fixtures.

The fake's search() returns the whole configured result set for any query —
Notion's real search is fuzzy, so a "Class Notes" query can legitimately
surface near-miss titles (e.g. the verified "Clash Notes" typo hub); the
adapter must filter by title family + semester itself.
"""

from __future__ import annotations


def synth(n: int) -> str:
    """Deterministic synthetic dashed UUID (same 32-hex shape as real ids)."""
    return f"1f000001-0000-4000-8000-{n:012d}"


HUB = synth(1)  # Class Notes 2026 下半学期 (current-semester hub)
OLD_HUB = synth(2)  # Clash Notes 2026上半学期 (older semester, typo included)
AMBIG_HUB = synth(3)  # ambiguity-test duplicate of the current-semester hub
COURSE_NET = synth(10)  # 🌐 计算机网络
COURSE_PSY = synth(11)  # 心理咨询与治疗引论
COURSE_COG = synth(12)  # 认知心理学 (no notes subpage — a normal condition)
LEARNING_CENTER = synth(20)  # 📚 2026秋季学期学习中心
NOTES_HUB = synth(21)  # 课堂录像笔记
NOISE_PAGE = synth(22)  # Blackboard 作业提交自动化经验（2026-09-12）
DB_INDEX = synth(31)  # 课程资料索引 database
DB_TASKS = synth(32)  # 2026秋季学习任务 database
LECTURE1 = synth(101)  # 第一讲 · 计算机网络 (variant: no 《》, no date)
LECTURE2 = synth(102)  # 第二讲 · 计算机网络（2026-09-09 第1-2节）(no 《》)
LECTURE3 = synth(103)  # 《第三讲 · 计算机网络（2026-09-14 第5-6节）》(full form)
LECTURE4 = synth(104)  # 🌐 第二讲 · ... (icon-tolerated variant)
COURSE_SUMMARY = synth(105)  # 《课程总结》 — non-lecture course child
LECTURE_PSY3 = synth(110)  # 第三讲 · 心理咨询与治疗引论（2026-09-18 第1-2节）
NOTES_SUB_NET = synth(201)  # 计算机网络 subpage of 课堂录像笔记
NOTES_SUB_PSY = synth(202)  # 心理咨询与治疗引论 subpage
ROWS = [synth(301 + i) for i in range(8)]

SEMESTER = "2026 下半学期"
HUB_TITLE = "Class Notes 2026 下半学期"
OLD_HUB_TITLE = "Clash Notes 2026上半学期"

# Honeypots: synthetic values mirroring the verified forbidden-string shapes.
SIGNED_S3_URL = (
    "https://prod-files-secure.s3.amazonaws.com/fake-ws/fake-img.png"
    "?X-Amz-Algorithm=FAKE&X-Amz-Signature=fakesignature00"
)
LOCAL_PATH = "E:\\pku-course-data\\计算机网络\\recordings\\demo-slug\\notes.md"
KEYFRAMES_PATH = "E:\\pku-course-data\\计算机网络\\recordings\\demo-slug\\keyframes"
TRANSCRIPT_PREVIEW = "第一周课堂录像转写预览：老师首先回顾了存储转发的三种方式，然后讲到分组交换。"
SOURCE_PATH_VALUE = "E:\\pku-course-data\\计算机网络\\materials\\第一讲\\第一讲（1）课程介绍.pdf"
REMARKS_VALUE = "每周含 AGENTS.md 与 tutorial.md；来源：E:\\pku-course-data\\计算机网络"
FAKE_TOKEN = "secret_test_notion_internal_token"

FORBIDDEN_STRINGS = (
    SIGNED_S3_URL,
    "prod-files-secure.s3.amazonaws.com",
    "X-Amz-Signature",
    "E:\\pku-course-data",
    "notes.md",
    "transcript.json",
    "keyframes",
    "/Users/",
    SOURCE_PATH_VALUE,
    REMARKS_VALUE,
    TRANSCRIPT_PREVIEW,
    FAKE_TOKEN,
)


def text_piece(content: str) -> dict:
    return {"type": "text", "plain_text": content, "text": {"content": content}}


def page_mention_piece(title: str, page_id: str) -> dict:
    return {
        "type": "mention",
        "plain_text": title,
        "mention": {"type": "page", "page": {"id": page_id}},
    }


def paragraph(pieces) -> dict:
    if isinstance(pieces, str):
        pieces = [text_piece(pieces)]
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": list(pieces)}}


def child_page_block(page_id: str, title: str) -> dict:
    return {"object": "block", "id": page_id, "type": "child_page", "child_page": {"title": title}}


def child_database_block(database_id: str, title: str) -> dict:
    return {
        "object": "block",
        "id": database_id,
        "type": "child_database",
        "child_database": {"title": title},
    }


def notion_url(page_id: str) -> str:
    return f"https://www.notion.so/{page_id.replace('-', '')}"


def search_page(page_id: str, title: str) -> dict:
    return {
        "object": "page",
        "id": page_id,
        "url": notion_url(page_id),
        "properties": {"title": {"type": "title", "title": [{"plain_text": title}]}},
        "parent": {"type": "workspace", "workspace": True},
    }


def material_schema() -> dict:
    """The verified 课程资料索引 schema: exact property names + options."""
    return {
        "object": "database",
        "id": DB_INDEX,
        "title": [{"plain_text": "课程资料索引"}],
        "properties": {
            "资料": {"type": "title", "title": {}},
            "课程": {"type": "rich_text", "rich_text": {}},
            "资料类型": {
                "type": "select",
                "select": {
                    "options": [
                        {"name": n}
                        for n in (
                            "课程手册",
                            "讲义",
                            "课堂课件",
                            "复习资料",
                            "往年题",
                            "作业",
                            "参考阅读",
                            "平台说明",
                        )
                    ]
                },
            },
            "处理状态": {
                "type": "select",
                "select": {
                    "options": [
                        {"name": n} for n in ("待阅读", "已索引", "重点", "待确认")
                    ]
                },
            },
            "来源路径": {"type": "rich_text", "rich_text": {}},
            "备注": {"type": "rich_text", "rich_text": {}},
        },
    }


def material_row(
    row_id: str,
    *,
    title: str,
    course: str,
    type_: str,
    status: str,
    source_path: str = "",
    remarks: str = "",
) -> dict:
    props: dict = {
        "资料": {"type": "title", "title": [{"plain_text": title}]},
        "课程": {"type": "rich_text", "rich_text": [{"plain_text": course}]},
        "资料类型": {"type": "select", "select": {"name": type_} if type_ else None},
        "处理状态": {"type": "select", "select": {"name": status} if status else None},
    }
    if source_path:
        props["来源路径"] = {"type": "rich_text", "rich_text": [{"plain_text": source_path}]}
    if remarks:
        props["备注"] = {"type": "rich_text", "rich_text": [{"plain_text": remarks}]}
    return {
        "object": "page",
        "id": row_id,
        "url": notion_url(row_id),
        "last_edited_time": "2026-09-18T08:30:00.000Z",
        "parent": {"type": "database_id", "database_id": DB_INDEX},
        "properties": props,
    }


class FakeNotionClient:
    """Duck-typed stand-in for pku_sync.notion.NotionClient, with a call log."""

    def __init__(self, workspace: "FakeWorkspace"):
        self._ws = workspace
        self.calls: list[tuple] = []
        self.token = FAKE_TOKEN  # must never surface in any adapter payload

    def search(self, query: str, *, object_type: str | None = None) -> list[dict]:
        self.calls.append(("search", query, object_type))
        return [dict(page) for page in self._ws.search_results]

    def list_children(self, block_id: str) -> list[dict]:
        self.calls.append(("list_children", block_id))
        return [dict(block) for block in self._ws.children.get(block_id, [])]

    def get_page(self, page_id: str) -> dict:
        self.calls.append(("get_page", page_id))
        return dict(self._ws.pages.get(page_id, {}))

    def get_database(self, database_id: str) -> dict:
        self.calls.append(("get_database", database_id))
        return dict(self._ws.databases.get(database_id, {}))

    def query_database(
        self, database_id: str, *, filter: dict | None = None, page_size: int = 100
    ) -> list[dict]:
        self.calls.append(("query_database", database_id))
        return [dict(row) for row in self._ws.rows.get(database_id, [])]


class FakeWorkspace:
    """In-memory workspace shaped like the verified real one."""

    def __init__(self):
        self.search_results: list[dict] = []
        self.children: dict[str, list[dict]] = {}
        self.pages: dict[str, dict] = {}
        self.databases: dict[str, dict] = {}
        self.rows: dict[str, list[dict]] = {}

    def client(self) -> FakeNotionClient:
        return FakeNotionClient(self)


def build_verified_workspace() -> FakeWorkspace:
    """The verified real hierarchy: hub → courses/学习中心/课堂录像笔记/dbs."""
    ws = FakeWorkspace()
    # Fuzzy search surface: current hub, older-semester typo hub, a non-hub page.
    ws.search_results = [
        search_page(HUB, HUB_TITLE),
        search_page(OLD_HUB, OLD_HUB_TITLE),
        search_page(COURSE_NET, "🌐 计算机网络"),
    ]
    # Verified: course pages, 学习中心, 课堂录像笔记, noise pages AND the 5
    # databases are ALL direct children of the hub.
    ws.children[HUB] = [
        child_page_block(COURSE_NET, "🌐 计算机网络"),
        child_page_block(COURSE_PSY, "心理咨询与治疗引论"),
        child_page_block(COURSE_COG, "认知心理学"),
        child_page_block(LEARNING_CENTER, "📚 2026秋季学期学习中心"),
        child_page_block(NOTES_HUB, "课堂录像笔记"),
        child_page_block(NOISE_PAGE, "Blackboard 作业提交自动化经验（2026-09-12）"),
        child_database_block(DB_INDEX, "课程资料索引"),
        child_database_block(DB_TASKS, "2026秋季学习任务"),
    ]
    # Course children: the three verified lecture-title variants, an
    # icon-tolerated variant, a non-lecture child, plus body honeypots.
    ws.children[COURSE_NET] = [
        child_page_block(LECTURE1, "第一讲 · 计算机网络"),
        child_page_block(LECTURE2, "第二讲 · 计算机网络（2026-09-09 第1-2节）"),
        child_page_block(LECTURE3, "《第三讲 · 计算机网络（2026-09-14 第5-6节）》"),
        child_page_block(LECTURE4, "🌐 第二讲 · 计算机网络（2026-09-16 第1-2节）"),
        child_page_block(COURSE_SUMMARY, "《课程总结》"),
        paragraph(f"课程简介正文 honeypot {SIGNED_S3_URL} {LOCAL_PATH}"),
    ]
    ws.children[COURSE_PSY] = [
        child_page_block(LECTURE_PSY3, "第三讲 · 心理咨询与治疗引论（2026-09-18 第1-2节）"),
    ]
    ws.children[COURSE_COG] = []  # a course with no lecture pages yet (normal)
    # 课堂录像笔记 hub: per-course subpages (verified: 5 of 11 courses only).
    ws.children[NOTES_HUB] = [
        child_page_block(NOTES_SUB_NET, "计算机网络"),
        child_page_block(NOTES_SUB_PSY, "心理咨询与治疗引论"),
    ]
    ws.children[NOTES_SUB_NET] = [
        # verified line form 1: plain URL after the arrow (batch runbook format)
        paragraph(f"《第一讲 · 计算机网络（2026-09-07 第5-6节）》→ {notion_url(LECTURE1)}"),
        # verified line form 2: native page mention after the arrow
        paragraph(
            [
                text_piece("《第二讲 · 计算机网络（2026-09-09 第1-2节）》→ "),
                page_mention_piece("第二讲 · 计算机网络（2026-09-09 第1-2节）", LECTURE2),
            ]
        ),
        # week-1 transcript preview embedded in the body — never captured
        paragraph(f"{TRANSCRIPT_PREVIEW} {SIGNED_S3_URL} {LOCAL_PATH}"),
        # lecture-titled line WITHOUT a resolvable link target — skipped
        paragraph("《第四讲 · 计算机网络（2026-09-21 第3-4节）》→ 待补链接"),
    ]
    ws.children[NOTES_SUB_PSY] = [
        paragraph(
            f"《第三讲 · 心理咨询与治疗引论（2026-09-18 第1-2节）》→ {notion_url(LECTURE_PSY3)}"
        ),
    ]
    # 学习中心 body: honeypot that must never be read into directory payloads.
    ws.children[LEARNING_CENTER] = [
        paragraph(f"每日检查记录：讲义整理完成，来源路径：{KEYFRAMES_PATH}"),
    ]
    # 课程资料索引 database: verified schema + rows with honeypots and the
    # matching matrix (exact / whitespace / icon / unmatched / empty).
    ws.databases[DB_INDEX] = material_schema()
    ws.databases[DB_TASKS] = {
        "object": "database",
        "id": DB_TASKS,
        "title": [{"plain_text": "2026秋季学习任务"}],
        "properties": {"任务": {"type": "title", "title": {}}},
    }
    ws.rows[DB_INDEX] = [
        material_row(
            ROWS[0],
            title="第一讲（1）课程介绍.pdf",
            course="计算机网络",
            type_="讲义",
            status="已索引",
            source_path=SOURCE_PATH_VALUE,
            remarks=REMARKS_VALUE,
        ),
        material_row(
            ROWS[1],
            title="计算机网络教学大纲.docx",
            course=" 计算机网络\u3000",
            type_="课程手册",
            status="待阅读",
            source_path=SOURCE_PATH_VALUE,
            remarks=REMARKS_VALUE,
        ),
        material_row(
            ROWS[2],
            title="心理治疗案例集.pdf",
            course="🌐 心理咨询与治疗引论",
            type_="参考阅读",
            status="重点",
            source_path=SOURCE_PATH_VALUE,
            remarks="",
        ),
        material_row(
            ROWS[3],
            title="量子力学讲义（外校）.pdf",
            course="量子力学导论",
            type_="讲义",
            status="待确认",
            source_path=SOURCE_PATH_VALUE,
            remarks=REMARKS_VALUE,
        ),
        material_row(
            ROWS[4],
            title="认知心理学实验手册.pdf",
            course="认知心理学",
            type_="课程手册",
            status="",
            source_path="",
            remarks="",
        ),
        material_row(
            ROWS[5],
            title="平台使用说明.pdf",
            course="",
            type_="",
            status="待确认",
            source_path=SOURCE_PATH_VALUE,
            remarks=REMARKS_VALUE,
        ),
        material_row(
            ROWS[6],
            title="计算机网络导论补充材料.pdf",
            course="计算机网络（本科）",
            type_="参考阅读",
            status="待阅读",
            source_path=SOURCE_PATH_VALUE,
            remarks=REMARKS_VALUE,
        ),
    ]
    return ws
