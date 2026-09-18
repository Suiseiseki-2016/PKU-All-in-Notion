"""Seeded fake Notion workspace for the student panel's demo startup mode.

Ships in the package so ``pku-sync panel --fake`` serves a realistic metadata
directory WITHOUT any Notion token. The seeded fixtures mirror the VERIFIED
real workspace shapes (mission library/notion-workspace.md) with synthetic
ids, and cover every M3 material-view state so later browser-verified panel
features never re-derive their own fake data:

- 计算机网络  (mapped course): 第一讲 has a CONFIRMED material (an explicit
  canonical lecture-page URL in 备注 — channel (ii)) plus a confirmed
  课堂笔记 entry and an inferred (待确认) subsection; 第二讲 is inferred-only.
- 发展心理学  (unmapped course): 第三讲 has only inferred (待确认) materials —
  the mapping-unconfirmed panel state.
- 认知心理学  (inferred-empty): 第一讲 has zero materials at all — the empty
  按讲次 state.
- 计算概论B   (no lecture pages yet): a normal course without lectures.

Honeypot strings — signed S3 URLs, local paths INCLUDING a full path ending
in ``transcript.json`` (the non-vacuous seeding the M2 discipline requires),
``notes.md``/``keyframes`` paths, 来源路径/备注 values, a body-content probe
phrase, and a fake token — are seeded into bodies the student panel must
never surface; the metadata-only adapter boundary keeps them out of every
payload (the panel tests scan full serialized output for them).

The fake data is a FIXTURE, not a claim about the real workspace: it adds a
forward-compatible ``讲次`` url property to the 课程资料索引 schema so the
explicit-link channel (ii) is exercised end-to-end. The verified empty/full
select vocabularies (处理状态, 资料类型) are unchanged.
"""

from __future__ import annotations


def synth(n: int) -> str:
    """Deterministic synthetic dashed UUID (same 32-hex shape as real ids)."""
    return f"2c000001-0000-4000-8000-{n:012d}"


# -- seeded identities -------------------------------------------------------

HUB = synth(1)  # Class Notes 2026 下半学期 (current-semester hub)
OLD_HUB = synth(2)  # Clash Notes 2026上半学期 (older semester, typo included)
AMBIG_HUB = synth(3)  # fault-variant duplicate of the current-semester hub

COURSE_NET = synth(10)  # 计算机网络 (mapped course)
COURSE_DEV = synth(11)  # 发展心理学 (unmapped / inferred-only)
COURSE_COG = synth(12)  # 认知心理学 (inferred-empty lecture)
COURSE_CS = synth(13)  # 计算概论B (no lecture pages yet — a normal condition)

LEARNING_CENTER = synth(20)  # 📚 2026秋季学期学习中心
NOTES_HUB = synth(21)  # 课堂录像笔记
NOISE_PAGE = synth(22)  # Blackboard 作业提交自动化经验（2026-09-12）
DB_INDEX = synth(31)  # 课程资料索引 database
DB_TASKS = synth(32)  # 2026秋季学习任务 database

NET_L1 = synth(101)  # 第一讲 · 计算机网络（2026-09-08 第1-2节）— mapped
NET_L2 = synth(102)  # 第二讲 · 计算机网络（2026-09-15 第3-4节）— inferred-only
DEV_L1 = synth(110)  # 第三讲 · 发展心理学（2026-09-18 第5-6节）— inferred-only
COG_L1 = synth(120)  # 第一讲 · 认知心理学 — zero materials
NET_MISSING = synth(150)  # 待确认 note-line target (NOT one of the lecture pages)

NOTES_SUB_NET = synth(201)  # 计算机网络 subpage of 课堂录像笔记
NOTES_SUB_DEV = synth(202)  # 发展心理学 subpage (no resolvable link lines)

NET_ROWS = [synth(301 + i) for i in range(5)]  # 计算机网络 index rows
DEV_ROWS = [synth(311 + i) for i in range(2)]  # 发展心理学 index rows
CS_ROW = synth(321)  # 计算概论B index row

SEMESTER = "2026 下半学期"
HUB_TITLE = "Class Notes 2026 下半学期"
OLD_HUB_TITLE = "Clash Notes 2026上半学期"
AMBIG_HUB_TITLE = "Class Notes 2026 下半学期（备份）"

# -- honeypots (mirror the verified forbidden-string shapes) -----------------

SIGNED_S3_URL = (
    "https://prod-files-secure.s3.amazonaws.com/fake-ws/fake-img.png"
    "?X-Amz-Algorithm=FAKE&X-Amz-Signature=fakesignature00"
)
LOCAL_PATH = "E:\\pku-course-data\\计算机网络\\recordings\\demo-slug\\notes.md"
FULL_TRANSCRIPT_PATH = "E:\\pku-course-data\\计算机网络\\2026-09-09\\transcript.json"
KEYFRAMES_PATH = "E:\\pku-course-data\\计算机网络\\recordings\\demo-slug\\keyframes"
POSIX_PATH = "/Users/pku/course-data/materials/第一章.pdf"
SOURCE_PATH_VALUE = "E:\\pku-course-data\\计算机网络\\materials\\第一讲\\第一讲（1）课程介绍.pdf"
REMARKS_VALUE = "每周含 AGENTS.md 与 tutorial.md；来源：E:\\pku-course-data\\计算机网络"
# A known lecture-body phrase: must never appear anywhere in the panel
# (the adapter never reads lecture page bodies).
BODY_PROBE = "课堂正文标记：分组交换与存储转发是网络层核心算法。"
FAKE_TOKEN = "secret_test_notion_internal_token"

PANEL_FORBIDDEN_STRINGS = (
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
    FULL_TRANSCRIPT_PATH,
    LOCAL_PATH,
    KEYFRAMES_PATH,
    POSIX_PATH,
    BODY_PROBE,
    FAKE_TOKEN,
)


# -- block/property helpers ---------------------------------------------------


def notion_url(page_id: str) -> str:
    return f"https://www.notion.so/{page_id.replace('-', '')}"


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


def search_page(page_id: str, title: str) -> dict:
    return {
        "object": "page",
        "id": page_id,
        "url": notion_url(page_id),
        "properties": {"title": {"type": "title", "title": [{"plain_text": title}]}},
        "parent": {"type": "workspace", "workspace": True},
    }


def material_schema() -> dict:
    """The verified 课程资料索引 schema PLUS the forward-compatible 讲次 url
    property so the explicit-link channel (ii) is exercised by the fake."""
    props = {
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
                "options": [{"name": n} for n in ("待阅读", "已索引", "重点", "待确认")]
            },
        },
        "来源路径": {"type": "rich_text", "rich_text": {}},
        "备注": {"type": "rich_text", "rich_text": {}},
        "讲次": {"type": "url", "url": {}},  # forward-compatible explicit link
    }
    return {
        "object": "database",
        "id": DB_INDEX,
        "title": [{"plain_text": "课程资料索引"}],
        "properties": props,
    }


def material_row(
    row_id: str,
    *,
    title: str,
    course: str,
    type_: str,
    status: str,
    source_path: str = SOURCE_PATH_VALUE,
    remarks: str = "",
    lecture_url: str = "",
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
    if lecture_url:
        props["讲次"] = {"type": "url", "url": lecture_url}
    return {
        "object": "page",
        "id": row_id,
        "url": notion_url(row_id),
        "last_edited_time": "2026-09-18T08:30:00.000Z",
        "parent": {"type": "database_id", "database_id": DB_INDEX},
        "properties": props,
    }


class FakeWorkspace:
    """In-memory workspace shaped like the verified real one (panel seed)."""

    def __init__(self):
        self.search_results: list[dict] = []
        self.children: dict[str, list[dict]] = {}
        self.pages: dict[str, dict] = {}
        self.databases: dict[str, dict] = {}
        self.rows: dict[str, list[dict]] = {}


class PanelFakeClient:
    """Duck-typed stand-in for ``pku_sync.notion.NotionClient`` with a call log.

    ``delay`` simulates slow reads so the panel's syncing/loading states are
    observable in the browser (the ``slow`` startup variant).
    """

    def __init__(self, workspace: FakeWorkspace, *, delay: float = 0.0):
        self._ws = workspace
        self.delay = delay
        self.calls: list[tuple] = []
        self.token = FAKE_TOKEN  # must never surface in any panel payload

    def _call(self, method: str, *args):
        if self.delay:
            import time

            time.sleep(self.delay)
        self.calls.append((method,) + args)

    def search(self, query: str, *, object_type: str | None = None) -> list[dict]:
        self._call("search", query, object_type)
        return [dict(page) for page in self._ws.search_results]

    def list_children(self, block_id: str) -> list[dict]:
        self._call("list_children", block_id)
        return [dict(block) for block in self._ws.children.get(block_id, [])]

    def get_page(self, page_id: str) -> dict:
        self._call("get_page", page_id)
        return dict(self._ws.pages.get(page_id, {}))

    def get_database(self, database_id: str) -> dict:
        self._call("get_database", database_id)
        return dict(self._ws.databases.get(database_id, {}))

    def query_database(
        self, database_id: str, *, filter: dict | None = None, page_size: int = 100
    ) -> list[dict]:
        self._call("query_database", database_id)
        return [dict(row) for row in self._ws.rows.get(database_id, [])]


def build_panel_workspace() -> FakeWorkspace:
    """The seeded M3 workspace: four courses covering every material-view state."""
    ws = FakeWorkspace()
    # Fuzzy search surface: current hub, older-semester typo hub, a course page.
    ws.search_results = [
        search_page(HUB, HUB_TITLE),
        search_page(OLD_HUB, OLD_HUB_TITLE),
        search_page(COURSE_NET, "计算机网络"),
    ]
    ws.children[HUB] = [
        child_page_block(COURSE_NET, "计算机网络"),
        child_page_block(COURSE_DEV, "发展心理学"),
        child_page_block(COURSE_COG, "认知心理学"),
        child_page_block(COURSE_CS, "计算概论B"),
        child_page_block(LEARNING_CENTER, "📚 2026秋季学期学习中心"),
        child_page_block(NOTES_HUB, "课堂录像笔记"),
        child_page_block(NOISE_PAGE, "Blackboard 作业提交自动化经验（2026-09-12）"),
        child_database_block(DB_INDEX, "课程资料索引"),
        child_database_block(DB_TASKS, "2026秋季学习任务"),
    ]
    # -- courses -----------------------------------------------------------
    # 计算机网络: two lectures; lecture bodies never read (honeypots below).
    ws.children[COURSE_NET] = [
        child_page_block(NET_L1, "第一讲 · 计算机网络（2026-09-08 第1-2节）"),
        child_page_block(NET_L2, "第二讲 · 计算机网络（2026-09-15 第3-4节）"),
        paragraph(f"课程简介 honeypot：{SIGNED_S3_URL} {LOCAL_PATH}"),
    ]
    ws.children[NET_L1] = [
        paragraph(f"{BODY_PROBE} 图片 {SIGNED_S3_URL} 本地 {FULL_TRANSCRIPT_PATH}"),
    ]
    ws.children[NET_L2] = [paragraph(f"{BODY_PROBE} {POSIX_PATH}")]
    ws.children[COURSE_DEV] = [
        child_page_block(DEV_L1, "第三讲 · 发展心理学（2026-09-18 第5-6节）"),
        paragraph(f"发展心理学课程页正文 {KEYFRAMES_PATH}"),
    ]
    ws.children[COURSE_COG] = [child_page_block(COG_L1, "第一讲 · 认知心理学")]
    ws.children[COURSE_CS] = []  # a course with no lecture pages yet (normal)

    # -- 学习中心 body: honeypot that must never be read --------------------
    ws.children[LEARNING_CENTER] = [
        paragraph(f"每日检查记录：来源路径：{KEYFRAMES_PATH}；{POSIX_PATH}"),
    ]

    # -- 课堂录像笔记 hub ------------------------------------------------------
    ws.children[NOTES_HUB] = [
        child_page_block(NOTES_SUB_NET, "计算机网络"),
        child_page_block(NOTES_SUB_DEV, "发展心理学"),
    ]
    ws.children[NOTES_SUB_NET] = [
        # confirmed channel (i): resolves to NET_L1, a directory lecture page
        paragraph(f"《第一讲 · 计算机网络（2026-09-08 第1-2节）》→ {notion_url(NET_L1)}"),
        # 待确认 note entry: lecture-titled line whose target is NOT one of the
        # directory lectures (stays course-level flagged, never confirmed)
        paragraph(
            [
                text_piece("《第五讲 · 计算机网络（2026-09-28 第1-2节）》→ "),
                page_mention_piece("第五讲 · 计算机网络（2026-09-28 第1-2节）", NET_MISSING),
            ]
        ),
        # week-1 transcript preview honeypot — never captured
        paragraph(f"转写预览 honeypot：{SIGNED_S3_URL} {FULL_TRANSCRIPT_PATH}"),
    ]
    # 发展心理学 HAS a subpage but no resolvable lecture-link lines: the three
    # lines below are lecture-titled with no target → all skipped (normal, not
    # an error), keeping DEV_L1 inferred-only.
    ws.children[NOTES_SUB_DEV] = [
        paragraph("《第三讲 · 发展心理学（2026-09-18 第5-6节）》→ 待补链接"),
    ]

    # -- 课程资料索引 database ------------------------------------------------
    ws.databases[DB_INDEX] = material_schema()
    ws.databases[DB_TASKS] = {
        "object": "database",
        "id": DB_TASKS,
        "title": [{"plain_text": "2026秋季学习任务"}],
        "properties": {"任务": {"type": "title", "title": {}}},
    }
    # 计算机网络 rows: explicit link (备注 URL → NET_L1, channel ii), inferred
    # subsections, and plain course-level materials.
    ws.rows[DB_INDEX] = [
        material_row(
            NET_ROWS[0],
            title="第一讲（1）课程介绍.pdf",
            course="计算机网络",
            type_="课堂课件",
            status="已索引",
            remarks=f"关联讲次页：{notion_url(NET_L1)}",
        ),
        material_row(
            NET_ROWS[1],
            title="第一讲（2）基础知识.pdf",
            course="计算机网络",
            type_="课堂课件",
            status="已索引",
            remarks=REMARKS_VALUE,
        ),
        material_row(
            NET_ROWS[2],
            title="第二讲 应用层讲义.pdf",
            course="计算机网络",
            type_="讲义",
            status="待阅读",
        ),
        material_row(
            NET_ROWS[3],
            title="计算机网络课程计划.pdf",
            course="计算机网络",
            type_="课程手册",
            status="已索引",
        ),
        material_row(
            NET_ROWS[4],
            title="期中复习指南.pdf",
            course="计算机网络",
            type_="复习资料",
            status="重点",
        ),
        material_row(
            DEV_ROWS[0],
            title="第三讲发展心理学讲义.pdf",
            course="发展心理学",
            type_="讲义",
            status="已索引",
            source_path=SOURCE_PATH_VALUE,
        ),
        material_row(
            DEV_ROWS[1],
            title="发展心理学课程大纲.pdf",
            course="发展心理学",
            type_="课程手册",
            status="待确认",
            source_path=SOURCE_PATH_VALUE,
            remarks=REMARKS_VALUE,
        ),
        material_row(
            CS_ROW,
            title="计算概论B课程手册.pdf",
            course="计算概论B",
            type_="课程手册",
            status="已索引",
        ),
    ]
    return ws
