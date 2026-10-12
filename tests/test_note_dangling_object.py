"""The actual recovered chapter contained a sentence with no verb object."""
from pku_sync import media
from pku_sync.recovery_fact_readiness import assess_recovery_facts
import json
from types import SimpleNamespace
import pytest


ACTUAL_FRAGMENT = "把每一个原始分数再转换成转换分数，就可以呈现出来非常多的。"


def test_punctuation_does_not_turn_actual_object_fragment_into_readable_sentence():
    assert not media._ledger_transcript_readable(ACTUAL_FRAGMENT)


def test_source_verification_cannot_accept_a_dangling_object_as_study_fact():
    row = {"origin": "transcript", "display_text": ACTUAL_FRAGMENT,
           "transcript_excerpt": ACTUAL_FRAGMENT, "evidence_group_id": "G01"}
    readiness = assess_recovery_facts([row], expected_group_ids=["G01"])
    assert not readiness.ready
    assert not readiness.selected
    assert readiness.review_required[0]["claim"] == row
    assert readiness.uncovered_groups == ["G01"]


def test_fragment_in_middle_of_rendered_paragraph_cannot_hide_behind_final_sentence():
    paragraph = "【课堂转写】\n" + ACTUAL_FRAGMENT + "调整的方法包括通过百分位数进行映射。"
    assert media._recovery_readability_issues(paragraph)


def test_complete_adjectival_predicate_is_not_a_missing_object():
    complete = "转换分数在实际测量中的用途是非常多的。"
    assert media._ledger_transcript_readable(complete)
    assert not media._recovery_readability_issues("【课堂转写】\n" + complete)


def test_saved_chapter_is_rechecked_even_without_dense_speech():
    window = {"start": 0, "end": 50, "text": ACTUAL_FRAGMENT}
    assert media._chapter_content_gap(window, ACTUAL_FRAGMENT, ACTUAL_FRAGMENT)


def test_success_cache_cannot_replay_fragment_and_rejected_content_is_preserved(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"schema_version": media._RECOVERY_LEDGER_SCHEMA,
        "status": "success", "content": ACTUAL_FRAGMENT, "validated_claims": []}), "utf-8")
    monkeypatch.setattr(media, "_recovery_llm", lambda *args: pytest.fail("No model call without valid facts"))
    with pytest.raises(media._NoteQualityError):
        media._recover_chapter_with_ledger(None, "model", "course", ACTUAL_FRAGMENT,
            ACTUAL_FRAGMENT, ACTUAL_FRAGMENT, settings=SimpleNamespace(),
            sources=[], cache_path=path)
    retained = json.loads(path.read_text("utf-8"))
    assert ACTUAL_FRAGMENT in retained["rejected_dangling_content"]
    assert retained["content"] == ACTUAL_FRAGMENT
