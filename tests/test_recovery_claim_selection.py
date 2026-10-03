from __future__ import annotations

import pytest

from pku_sync.recovery_claim_selection import select_recovery_claims


def spoken(text: str, **extra: object) -> dict:
    return {"origin": "transcript", "display_text": text,
            "evidence_group_id": "G01", **extra}


def slide(text: str, *, quote: str | None = None) -> dict:
    return {"origin": "slide", "display_text": text,
            "source_filename": "讲义.pdf", "source_page": "第 4 页",
            "source_quote": quote or text}


def test_obvious_navigation_filler_has_a_recorded_omission():
    claims = [spoken("大家接着看一下这里。"), spoken("密钥应当妥善保管。")]
    selected, omitted = select_recovery_claims(claims)
    assert selected == [claims[1]]
    assert omitted == [{"index": 0, "reason": "non_study_speech_filler",
                        "claim": claims[0]}]
    assert selected[0] is not claims[1]


def test_numbers_coursework_and_attendance_survive_even_in_asides():
    texts = [
        "刚才讲过，下周提交第一次作业。",
        "刚才讲过，第 3 题计算模 26 的逆元。",
        "大家看一下，课堂展示必须到场。",
        "大家看一下，签到算考勤。",
        "刚才讲过，密钥长 128 位。",
    ]
    claims = [spoken(text) for text in texts]
    selected, omitted = select_recovery_claims(claims + [slide(texts[0])])
    assert selected == claims + [slide(texts[0])]
    assert omitted == []


def test_repeated_aside_needs_same_complete_assertion_on_independent_slide():
    claim = spoken("刚才我们讲过，凯撒密码属于单表替代。")
    support = slide("凯撒密码属于单表替代。")
    selected, omitted = select_recovery_claims([claim, support])
    assert selected == [support]
    assert [(item["index"], item["reason"]) for item in omitted] == [
        (0, "repeated_aside_covered_by_verified_slide")]


def test_related_topic_word_or_mixed_origin_is_not_enough_to_delete_aside():
    claim = spoken("刚才讲过，凯撒密码属于单表替代。")
    related = slide("凯撒密码使用固定的字母偏移。")
    mixed = {**slide("凯撒密码属于单表替代。"), "origin": "both"}
    for support in (related, mixed):
        selected, omitted = select_recovery_claims([claim, support])
        assert selected == [claim, support]
        assert omitted == []


def test_claim_without_actual_slide_quote_is_preserved():
    claim = spoken("刚才讲过，凯撒密码属于单表替代。")
    unsupported = slide("凯撒密码属于单表替代。", quote="课件仅有密码学概论。")
    selected, omitted = select_recovery_claims([claim, unsupported])
    assert selected == [claim, unsupported]
    assert omitted == []


def test_incomplete_fragment_only_drops_when_exactly_covered_by_slide():
    claim = spoken("替代使用新的符号替换原有符号，就是。")
    support = slide("替代使用新的符号替换原有符号，就是。")
    selected, omitted = select_recovery_claims([claim, support])
    assert selected == [support]
    assert omitted[0]["reason"] == "incomplete_fragment_covered_by_verified_slide"
    assert select_recovery_claims([claim])[0] == [claim]


def test_unverified_transcript_and_non_filler_technical_fact_survive():
    claim = spoken("代替就是用一个新的符号代替原来的符号。")
    selected, omitted = select_recovery_claims([claim])
    assert selected == [claim]
    assert omitted == []


def test_punctuation_only_duplicate_from_same_evidence_block_is_removed():
    first = spoken("神经系统是两大块内容，一个是中枢，一个是周围",
                   evidence_block_id="B0003")
    duplicate = spoken("神经系统是两大块内容，一个是中枢，一个是周围。",
                       evidence_block_id="B0003")
    selected, omitted = select_recovery_claims([first, duplicate])
    assert selected == [first]
    assert [(item["index"], item["reason"]) for item in omitted] == [
        (1, "duplicate_validated_claim")
    ]


def test_same_claim_from_different_evidence_blocks_is_preserved():
    first = spoken("神经系统是两大块内容，一个是中枢，一个是周围。",
                   evidence_block_id="B0003")
    repeat = spoken("神经系统是两大块内容，一个是中枢，一个是周围。",
                    evidence_block_id="B0008")
    selected, omitted = select_recovery_claims([first, repeat])
    assert selected == [first, repeat]
    assert omitted == []


def test_non_mapping_input_fails_without_partial_selection():
    with pytest.raises(TypeError):
        select_recovery_claims([spoken("大家看一下。"), "bad"])
