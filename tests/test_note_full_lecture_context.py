"""Full-context transport and preservation; fixture prose is not quality proof."""
import json
from types import SimpleNamespace

import pytest

from pku_sync import note_editor


def config():
    return SimpleNamespace(notes_model="qwen3.5-flash", pku_username="isolated-fixture")


def result():
    return json.dumps({"note": "整堂课结合前后文整理的正文。", "complete": False,
                       "uncertainties": ["专业名词仍需回看原录像。"]}, ensure_ascii=False)


def test_full_lecture_arrives_in_both_editor_stages_despite_time_distance(tmp_path):
    original = {"segments": [{"start": 284, "end": 600, "text": "本段例子里的提交不是本课作业。"},
                             {"start": 6450, "end": 7000, "text": "结尾补充前述例子，尚未安排作业。"}]}
    sources = [{"status": "readable", "text": "整份课件含各主题，仅用于参考。"}]
    calls = []
    def ask(system, prompt):
        calls.append(json.loads(prompt))
        return result()
    target = tmp_path / "notes.md"
    note_editor.write_notes(original, target, config(), "课程", ask=ask, source_context=sources)
    assert len(calls) == 4
    assert all(call["transcript"] == original["segments"] and call["courseware"] == sources
               and call["editing_scope"] == "whole-lecture" and call["parts"] == 1
               for call in calls if "source_review_focus" not in call)
    report = json.loads(note_editor.report_path(target).read_text("utf-8"))
    assert report["editing_scope"] == "whole-lecture" and len(report["chapters"]) == 1
    assert report["chapters"][0]["transcript_sha256"] == note_editor.fingerprint(original["segments"])
    assert report["content_complete"] is False
    assert [call["transcript"] for call in calls if "source_review_focus" in call] == [[row] for row in original["segments"]]
    assert len(calls[-1]["source_reviews"]) == 2
    assert note_editor.review_record_issues(target, original, sources) == []
    # Losing a late clarification is an input identity failure, not a prose
    # similarity test. No implicit paid rewrite replaces the saved original.
    changed = {"segments": original["segments"][:-1]}
    saved = target.read_bytes()
    with pytest.raises(RuntimeError, match="原文件保留"):
        note_editor.write_notes(changed, target, config(), "课程", ask=ask, source_context=sources)
    assert len(calls) == 4 and target.read_bytes() == saved


def test_large_reference_batches_each_receive_complete_original_lecture(tmp_path):
    original = {"segments": [{"start": 0, "end": 12, "text": "同一个课堂主题。"},
                             {"start": 1200, "end": 1210, "text": "结尾说明前述限定。"}]}
    text = "参考原文不按词汇相关性删选。" * 3000
    sources = [{"status": "readable", "text": text}]
    calls = []
    def ask(system, prompt):
        calls.append(json.loads(prompt))
        return result()
    target = tmp_path / "notes.md"
    note_editor.write_notes(original, target, config(), "课程", ask=ask, source_context=sources)
    editorial = [row for row in calls if "source_review_focus" not in row]
    assert all(row["transcript"] == original["segments"] for row in editorial)
    count = len(note_editor.source_batches(sources))
    # Both editorial stages receive every reference page. The preceding
    # oral-source reader receives only the original spoken window.
    for stage in (editorial[offset:offset + count] for offset in range(0, len(editorial), count)):
        assert "".join(item["text"] for call in stage for item in call["courseware"]) == text
    assert note_editor.review_record_issues(target, original, sources) == []


def test_serialized_capacity_fallback_preserves_every_source_character(tmp_path):
    text = ('"\\正文' * 35000)
    original = {"segments": [{"start": 0, "end": 1200, "text": text}]}
    calls = []
    def ask(system, prompt):
        assert len(system) + len(prompt) <= note_editor.FLASH_INPUT_CHARS
        calls.append(json.loads(prompt))
        return result()
    target = tmp_path / "notes.md"
    note_editor.write_notes(original, target, config(), "课程", ask=ask)
    assert calls[0]["editing_scope"] == "transport-parts"
    for stage in range(3):
        assert "".join(row["text"] for call in calls[stage::3] for row in call["transcript"]) == text
    assert original["segments"][0]["text"] == text
    assert note_editor.review_record_issues(target, original) == []


def test_oversized_metadata_refuses_before_charge_without_cropping_source(tmp_path):
    sources = [{"title": "超长文件名" * 45000, "status": "readable", "text": "原课件。"}]
    original = {"segments": [{"start": 0, "end": 10, "text": "原转写。"}]}
    calls = []
    with pytest.raises(RuntimeError, match="任务未提交"):
        note_editor.write_notes(original, tmp_path / "notes.md", config(), "课程",
                               source_context=sources, ask=lambda *a: calls.append(a) or result())
    assert not calls and sources[0]["text"] == "原课件。"


def test_review_receipt_cannot_change_from_full_lecture_to_transport_scope(tmp_path):
    original = {"segments": [{"start": 0, "end": 10, "text": "开头。"},
                             {"start": 3600, "end": 3610, "text": "结尾。"}]}
    target = tmp_path / "notes.md"
    note_editor.write_notes(original, target, config(), "课程", ask=lambda *a: result())
    path = note_editor.report_path(target)
    value = json.loads(path.read_text("utf-8"))
    value["editing_scope"] = "transport-parts"
    path.write_text(json.dumps(value), "utf-8")
    assert note_editor.review_record_issues(target, original)
