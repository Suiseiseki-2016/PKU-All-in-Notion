from __future__ import annotations

import json

import pytest

from pku_sync.media import _write_fact_readiness_review_packet
from pku_sync.recovery_fact_readiness import assess_recovery_facts


def spoken(text: str, group: str = "G01") -> dict:
    return {"origin": "transcript", "display_text": text,
            "evidence_group_id": group}


def slide(text: str) -> dict:
    return {"origin": "slide", "display_text": text,
            "source_filename": "讲义.pdf", "source_page": "第 4 页",
            "source_quote": text}


def test_clear_filler_and_unrelated_price_can_be_excluded_with_reasons():
    rows = [spoken("大家接着看一下这里。"),
            spoken("那现在一个比特币都七万多美元了。"),
            spoken("代替和置换是古典密码设计的基本手段。")]
    result = assess_recovery_facts(rows)
    assert result.selected == [rows[2]]
    assert [(row["index"], row["reason"]) for row in result.excluded] == [
        (0, "non_study_speech_filler"), (1, "unrelated_market_chatter")]
    assert result.ready


def test_crypto_example_with_price_is_not_silently_discarded():
    row = spoken("比特币价格上涨时，交易签名仍依赖相同的密码算法。")
    result = assess_recovery_facts([row])
    assert result.excluded == []
    assert result.selected == [row]


def test_asr_fragment_is_held_for_review_and_slide_cannot_cover_spoken_group():
    rows = [spoken("这三位作者就设计了rac算法。", "G03"),
            slide("1977年三位作者提出RSA公钥算法。")]
    result = assess_recovery_facts(rows, expected_group_ids=["G03"])
    assert result.selected == [rows[1]]
    assert result.review_required[0]["reason"] == "spoken_technical_term_needs_audio_review"
    assert "candidate_source" not in result.review_required[0]
    assert result.uncovered_groups == ["G03"]
    assert not result.ready


def test_rac_error_points_to_exact_rsa_slide_without_autocorrecting_speech():
    spoken_row = spoken("这三位作者设计了rac算法。", "G03")
    slide_row = slide("1977年Rivest，Shamir & Adleman提出了RSA公钥算法。")
    result = assess_recovery_facts([spoken_row, slide_row], expected_group_ids=["G03"])
    assert result.review_required[0]["claim"] == spoken_row
    assert result.review_required[0]["candidate_source"] == {
        "term": "RSA", "source_filename": "讲义.pdf", "source_page": "第 4 页",
        "source_quote": slide_row["source_quote"],
    }
    assert result.uncovered_groups == ["G03"]
    assert not result.ready


def test_teacher_assignment_and_attendance_are_never_excluded_as_chatter():
    clear = spoken("老师要求下周提交第一次作业，课堂展示必须到场。")
    broken = spoken("老师说作业的这个……")
    result = assess_recovery_facts([clear, broken])
    assert result.selected == [clear]
    assert result.excluded == []
    assert result.protected_selected == 1
    assert result.review_required[0]["claim"] == broken
    assert not result.ready


def test_uncovered_expected_group_blocks_even_with_many_slide_facts():
    result = assess_recovery_facts([slide("置换密码改变字母顺序。")],
                                   expected_group_ids=["G01"])
    assert result.uncovered_groups == ["G01"]
    assert not result.ready


def test_slide_source_missing_needs_review():
    row = slide("置换密码改变字母顺序。")
    row["source_quote"] = None
    result = assess_recovery_facts([row])
    assert result.review_required[0]["reason"] == "slide_source_incomplete"
    assert not result.ready


def test_complete_spoken_fact_ending_with_le_is_kept():
    row = spoken("置换密码改变了明文字母的排列顺序。")
    result = assess_recovery_facts([row])
    assert result.selected == [row]
    assert result.review_required == []
    assert result.ready


def test_complete_basic_operation_fact_is_not_mistaken_for_context_fragment():
    complete = spoken("基本的操作方法虽然是在古典密码时期出现的思想，但现在密码中仍然在使用。")
    fragment = spoken("基本的操作")
    result = assess_recovery_facts([complete, fragment])
    assert result.selected == [complete]
    assert result.review_required[0]["claim"] == fragment


def test_navigation_is_excluded_but_unnamed_book_and_assignment_are_preserved():
    navigation = spoken("接下来我们看一些古典密码的例子。")
    unnamed_book = spoken("出现了一部里程碑式的经典巨著。")
    assignment = spoken("接下来我们看第一次作业的例子。")
    result = assess_recovery_facts([navigation, unnamed_book, assignment])
    assert [(item["claim"], item["reason"]) for item in result.excluded] == [
        (navigation, "non_study_speech_filler")]
    assert result.review_required[0]["claim"] == unnamed_book
    assert result.selected == [assignment]
    assert not result.ready


def test_noisy_unwrapping_word_from_real_scytale_example_needs_review():
    row = spoken("但是如果你把这个羊皮铲解解开，那么这时候呢，你也不知道是什么信息。")
    result = assess_recovery_facts([row])
    assert result.selected == []
    assert result.review_required[0]["claim"] == row
    assert result.review_required[0]["reason"] == "spoken_claim_needs_context_or_asr_review"


def test_non_mapping_fails_closed():
    with pytest.raises(TypeError):
        assess_recovery_facts([spoken("代替改变符号映射。"), "bad"])


def test_fact_review_packet_points_to_audio_and_keeps_slide_as_candidate_only(tmp_path):
    spoken_row = spoken("这三位作者设计了rac算法。", "G03")
    spoken_row.update(transcript_excerpt="这三位作者设计了rac算法。",
                      evidence_start=2425.423, evidence_end=2439.993)
    slide_row = slide("1977年Rivest，Shamir & Adleman提出了RSA公钥算法。")
    claims = [spoken_row, slide_row]
    result = assess_recovery_facts(claims, expected_group_ids=["G03"])
    output = tmp_path / "review-packet.json"
    packet = _write_fact_readiness_review_packet(output, claims=claims,
                                                  readiness=result)
    assert json.loads(output.read_text("utf-8")) == packet
    assert packet["status"] == "manual_review_only"
    assert packet["automatic_approval"] is False
    assert packet["uncovered_groups"] == ["G03"]
    target = packet["review_items"][0]
    assert target["transcript_excerpt"] == spoken_row["transcript_excerpt"]
    assert target["audio_window"] == {"start_seconds": 2417.423,
                                      "end_seconds": 2447.993}
    assert target["candidate_source_not_proof"]["term"] == "RSA"
    assert "RSA算法" not in target["display_text"]
    changed = [{**spoken_row, "transcript_excerpt": "different"}, slide_row]
    changed_packet = _write_fact_readiness_review_packet(
        output, claims=changed, readiness=assess_recovery_facts(changed,
                                                                expected_group_ids=["G03"]))
    assert changed_packet["claims_sha256"] != packet["claims_sha256"]
