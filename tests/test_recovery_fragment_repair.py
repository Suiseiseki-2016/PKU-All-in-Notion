from __future__ import annotations

import json
import pytest

from pku_sync.recovery_fragment_repair import (
    FragmentRepairError, build_request, validate_fragment_repairs,
)


def review(text: str, *, index: int = 0, excerpt: str | None = None,
           slide: str | None = None) -> dict:
    claim = {"origin": "transcript", "display_text": text,
             "transcript_excerpt": excerpt or text,
             "evidence_start": 60.0, "evidence_end": 74.0}
    if slide:
        claim.update(source_filename="讲义.pdf", source_page="第 30 页",
                     source_quote=slide)
    return {"index": index, "claim": claim}


def decision(identifier: str = "R0", *, status: str = "needs_audio",
             text: str | None = None, origin: str | None = None,
             quote: str | None = None, duplicate: str | None = None) -> dict:
    return {"id": identifier, "status": status, "text": text,
            "reason": "人工复核原因", "evidence_origin": origin,
            "evidence_quote": quote, "duplicate_of": duplicate}


def validate(rows: list[dict], *decisions: dict, selected: list[dict] | None = None):
    return validate_fragment_repairs(rows, {"repairs": list(decisions)},
                                     selected_facts=selected or [])


def test_keep_natural_verbatim_span_with_exact_transcript_evidence():
    row = review("然后代替密码改变符号的映射关系。",
                 excerpt="然后代替密码改变符号的映射关系。接下来讲置换密码。")
    result = validate([row], decision(status="keep", text="代替密码改变符号的映射关系。",
                                      origin="transcript",
                                      quote="然后代替密码改变符号的映射关系。"))
    assert result.ready
    assert result.kept[0]["text"] == "代替密码改变符号的映射关系。"
    assert result.kept[0]["claim"] == row["claim"]


def test_verbatim_but_dangling_reference_still_needs_context():
    row = review("所以这个变换的思想呢，就称作置换。")
    with pytest.raises(FragmentRepairError, match="dangling referent"):
        validate([row], decision(status="keep", text="所以这个变换的思想呢，就称作置换。",
                                 origin="transcript", quote="所以这个变换的思想呢，就称作置换。"))


def test_rac_to_rsa_remains_audio_review_even_with_matching_slide():
    row = review("这三位作者设计了RAC算法。",
                 slide="1977年Rivest、Shamir和Adleman提出RSA公钥算法。")
    assert not validate([row], decision()).ready
    with pytest.raises(FragmentRepairError, match="slide cannot replace"):
        validate([row], decision(status="keep", text="1977年Rivest、Shamir和Adleman提出RSA公钥算法。",
                                 origin="slide", quote=row["claim"]["source_quote"]))
    row["claim"]["transcript_excerpt"] = "这三位作者设计了RAC算法。后来提到RSA公钥算法。"
    with pytest.raises(FragmentRepairError, match="acronym"):
        validate([row], decision(status="keep", text="RSA公钥算法",
                                 origin="transcript", quote=row["claim"]["transcript_excerpt"]))


@pytest.mark.parametrize("original,rewritten,reason", [
    ("这个算法有128位密钥。", "这个算法有256位密钥。", "number"),
    ("使用DES算法。", "使用AES算法。", "acronym"),
    ("Shannon提出了这一理论。", "Feistel提出了这一理论。", "name"),
    ("这是置换密码。", "这是代替密码。", "named term"),
    ("不需要提交作业。", "需要提交作业。", "negation"),
])
def test_keep_rejects_changed_anchors_even_when_quote_contains_both(
    original: str, rewritten: str, reason: str,
):
    evidence = original + rewritten
    row = review(original, excerpt=evidence)
    with pytest.raises(FragmentRepairError, match=reason):
        validate([row], decision(status="keep", text=rewritten,
                                 origin="transcript", quote=evidence))


def test_keep_rejects_invented_term_and_noncontiguous_paraphrase():
    row = review("代替密码改变字母映射。")
    with pytest.raises(FragmentRepairError, match="contiguous verbatim"):
        validate([row], decision(status="keep", text="代替密码增强安全性。",
                                 origin="transcript", quote="代替密码改变字母映射。"))
    with pytest.raises(FragmentRepairError, match="contiguous exact evidence"):
        validate([row], decision(status="keep", text="代替密码改变字母映射。",
                                 origin="transcript", quote="置换密码改变字母顺序。"))


def test_filler_or_exact_selected_duplicate_can_be_omitted():
    filler = review("大家接着看一下这里。")
    duplicate = review("代替密码改变字母映射。", index=1)
    selected = [{"id": "F1", "display_text": "代替密码改变字母映射。"}]
    result = validate([filler, duplicate],
                      decision(status="omit"),
                      decision("R1", status="omit", duplicate="F1"),
                      selected=selected)
    assert result.ready
    assert [row["reason"] for row in result.omitted] == [
        "navigation_filler", "exact_selected_duplicate"]


@pytest.mark.parametrize("text", [
    "下周提交第一次作业。", "课堂展示必须到场。", "这三位作者设计了RAC算法。",
    "代替密码改变字母映射。",
])
def test_substantive_or_protected_claim_cannot_be_omitted(text: str):
    with pytest.raises(FragmentRepairError):
        validate([review(text)], decision(status="omit"))


def test_duplicate_requires_exact_selected_text():
    selected = [{"id": "F1", "display_text": "代替密码改变字母映射。"}]
    with pytest.raises(FragmentRepairError, match="exact selected"):
        validate([review("代替密码改变映射。")],
                 decision(status="omit", duplicate="F1"), selected=selected)


def test_selected_ledger_fact_without_id_gets_positional_duplicate_id():
    selected = [{"origin": "slide", "display_text": "代替密码改变字母映射。"}]
    system, user = build_request([review("代替密码改变字母映射。")],
                                 selected_facts=selected)
    assert '"id":"S0"' in user
    assert "needs_audio" in system
    result = validate([review("代替密码改变字母映射。")],
                      decision(status="omit", duplicate="S0"), selected=selected)
    assert result.omitted[0]["duplicate_of"] == "S0"


def test_transcript_excerpt_must_match_supplied_time_window():
    row = review("代替密码改变字母映射。")
    row["transcript_text"] = "这段时间只有无关文字。"
    with pytest.raises(FragmentRepairError, match="absent from supplied time window"):
        validate([row], decision(status="keep", text="代替密码改变字母映射。",
                                 origin="transcript", quote="代替密码改变字母映射。"))
    row.pop("transcript_text")
    row["claim"]["evidence_end"] = 59.0
    with pytest.raises(FragmentRepairError, match="exact excerpt and time"):
        validate([row], decision(status="keep", text="代替密码改变字母映射。",
                                 origin="transcript", quote="代替密码改变字母映射。"))


def test_missing_repeated_unknown_ids_fail_closed():
    rows = [review("甲乙丙丁戊己庚辛。"), review("壬癸子丑寅卯辰巳。", index=1)]
    for choices in ([decision()], [decision(), decision()],
                    [decision(), decision("R9")]):
        with pytest.raises(FragmentRepairError, match="missing, repeated, or unknown"):
            validate(rows, *choices)


def test_invalid_schema_and_non_kept_content_fail_closed():
    row = review("作业需要下周提交。")
    with pytest.raises(FragmentRepairError, match="invalid repair response schema"):
        validate_fragment_repairs([row], {"items": []})
    with pytest.raises(FragmentRepairError, match="non-kept decision includes new content"):
        validate([row], decision(text="伪造内容"))


def test_single_json_fence_and_reason_quotes_are_narrowly_repaired():
    row = review("代替密码改变字母映射。")
    item = decision(status="keep", text="代替密码改变字母映射。",
                    origin="transcript", quote="代替密码改变字母映射。")
    raw = json.dumps({"repairs": [item]}, ensure_ascii=False, indent=2)
    raw = raw.replace("人工复核原因", '原文称"代替密码"')
    result = validate_fragment_repairs([row], f"```json\n{raw}\n```")
    assert result.kept[0]["text"] == "代替密码改变字母映射。"
    with pytest.raises(FragmentRepairError, match="invalid JSON"):
        validate_fragment_repairs([row], "前言\n```json\n" + raw + "\n```")
