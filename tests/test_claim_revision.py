from __future__ import annotations

import json

from pku_sync.claim_audit import chapter_assertions
from pku_sync.claim_revision import revise_failed_chapter


PAGES = [
    {"title": "绪论.pdf", "locator": "第 7 页",
     "text": "凯撒密码用固定偏移替换字母。课件介绍单表代换。"},
    {"title": "古典.pdf", "locator": "第 8 页",
     "text": "另一页仅介绍频率分析。"},
]
BLOCKS = [{"block_id": "B0001", "text": "老师说密钥需要妥善保管。"}]


def _audit(chapter: str, statuses: list[str]) -> dict:
    assertions = chapter_assertions(chapter)
    return {"status": "fail", "assertions": assertions,
            "verdicts": [{"id": row["id"], "status": status,
                          "evidence": [], "reason": "原句关系证据不足"}
                         for row, status in zip(assertions, statuses)]}


def _answer(revisions: list[dict], *, refusal: bool = False):
    calls = []

    def ask(system: str, user: str) -> str:
        calls.append(json.loads(user))
        return json.dumps({"schema_version": 1, "refusal": refusal,
                           "revisions": revisions}, ensure_ascii=False)

    return ask, calls


def test_source_quote_only_replaces_failed_claim_and_requires_reaudit():
    chapter = "### 凯撒密码\n明文是加密前的信息。凯撒密码属于多表代换。"
    ask, calls = _answer([{"id": "C002", "source_id": "S1",
                           "quote": "凯撒密码用固定偏移替换字母。"}])
    result = revise_failed_chapter(chapter, _audit(chapter, ["supported", "unsupported"]),
                                   BLOCKS, PAGES, ask)
    assert result["status"] == "candidate"
    assert result["requires_reaudit"] is True
    assert "明文是加密前的信息。" in result["candidate"]
    assert "凯撒密码属于多表代换" not in result["candidate"]
    assert "凯撒密码用固定偏移替换字母（课件：绪论.pdf，第7页）。" in result["candidate"]
    assert len(calls) == 1
    assert [row["id"] for row in calls[0]["failed_claims"]] == ["C002"]


def test_fabricated_or_stitched_quote_fails_closed():
    chapter = "凯撒密码属于多表代换。"
    for quote in ("凯撒密码使用多表代换。", "凯撒密码用固定偏移替换字母。课件介绍单表代换。"):
        ask, _ = _answer([{"id": "C001", "source_id": "S1", "quote": quote}])
        result = revise_failed_chapter(chapter, _audit(chapter, ["unsupported"]),
                                       BLOCKS, PAGES, ask)
        # Neither an invented quote nor two stitched assertions is allowed.
        assert result["status"] == "refused"


def test_explicit_page_citation_must_use_the_same_page():
    chapter = "凯撒密码属于多表代换（课件：绪论.pdf，第7页）。"
    ask, _ = _answer([{"id": "C001", "source_id": "S2",
                       "quote": "另一页仅介绍频率分析。"}])
    result = revise_failed_chapter(chapter, _audit(chapter, ["uncertain"]),
                                   BLOCKS, PAGES, ask)
    assert result["status"] == "refused"
    assert result["reason"] == "cited_page_not_supplied"


def test_coursework_attendance_and_calculation_are_not_silently_deleted():
    chapters = [
        "老师要求下周提交书面作业。",
        "课堂分组展示必须到场。",
        "21 模 50 的逆元是 31。",
        "50=2×21+8。",
    ]
    ask, calls = _answer([])
    for chapter in chapters:
        result = revise_failed_chapter(chapter, _audit(chapter, ["uncertain"]),
                                       BLOCKS, PAGES, ask)
        assert result["status"] == "refused"
        assert result["reason"] == "sensitive_claim_requires_manual_revision"
    assert calls == []


def test_stale_incomplete_audit_does_not_call_model():
    chapter = "凯撒密码属于多表代换。"
    ask, calls = _answer([])
    report = _audit(chapter, ["unsupported"])
    report["assertions"][0]["text"] = "旧内容。"
    assert revise_failed_chapter(chapter, report, BLOCKS, PAGES, ask)["status"] == "refused"
    assert calls == []


def test_duplicate_claim_span_cannot_replace_supported_copy():
    chapter = "凯撒密码属于多表代换。\n凯撒密码属于多表代换。"
    ask, calls = _answer([])
    result = revise_failed_chapter(chapter, _audit(chapter, ["supported", "unsupported"]),
                                   BLOCKS, PAGES, ask)
    assert result["status"] == "refused"
    assert result["reason"] == "ambiguous_claim_span"
    assert calls == []


def test_model_refusal_and_extra_factual_prose_fail_closed():
    chapter = "凯撒密码属于多表代换。"
    audit = _audit(chapter, ["unsupported"])
    ask, _ = _answer([], refusal=True)
    assert revise_failed_chapter(chapter, audit, BLOCKS, PAGES, ask)["status"] == "refused"
    ask, _ = _answer([{"id": "C001", "source_id": "S1",
                       "quote": "凯撒密码用固定偏移替换字母。",
                       "replacement": "凯撒密码绝对安全。"}])
    result = revise_failed_chapter(chapter, audit, BLOCKS, PAGES, ask)
    assert result["status"] == "refused"
    assert result["reason"] == "invalid_revision_schema"


def test_transcript_quote_and_malformed_response():
    chapter = "密钥应该公开。"
    audit = _audit(chapter, ["unsupported"])
    ask, _ = _answer([{"id": "C001", "source_id": "B0001",
                       "quote": "老师说密钥需要妥善保管。"}])
    result = revise_failed_chapter(chapter, audit, BLOCKS, PAGES, ask)
    assert result["status"] == "candidate"
    assert result["candidate"] == "老师说密钥需要妥善保管。"
    result = revise_failed_chapter(chapter, audit, BLOCKS, PAGES,
                                   lambda *_: "not json")
    assert result["status"] == "refused"


def test_reviser_cannot_use_source_text_outside_audit_window():
    chapter = "凯撒密码属于多表代换。"
    beyond = "这是不在审核窗口内的完整课件句子。"
    pages = [{"title": "绪论.pdf", "locator": "第 7 页",
              "text": "x" * 2500 + beyond}]
    ask, _ = _answer([{"id": "C001", "source_id": "S1", "quote": beyond}])
    result = revise_failed_chapter(chapter, _audit(chapter, ["uncertain"]),
                                   BLOCKS, pages, ask)
    assert result["status"] == "refused"
    assert result["reason"] == "quote_not_contiguous_complete_sentence"
