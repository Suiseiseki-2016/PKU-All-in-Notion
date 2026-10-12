"""Source delivery, feedback binding and paid recovery; no prose verdicts."""
import json
from types import SimpleNamespace

import pytest

from pku_sync import note_editor


TRANSCRIPT = {"segments": [{"start": 0, "end": 20, "text": "较早讲述。"},
                           {"start": 1200, "end": 1220, "text": "后续条件。"}]}


def response(note, complete=True, uncertainties=None):
    return json.dumps({"note": note, "complete": complete,
                       "uncertainties": uncertainties or []}, ensure_ascii=False)


def run(tmp_path, ask):
    target = tmp_path / "notes.md"
    note_editor.write_notes(TRANSCRIPT, target, SimpleNamespace(notes_model="qwen3.5-flash"),
                           "课程", ask=ask)
    return target


def test_source_feedback_reaches_editor_without_replacing_draft(tmp_path):
    calls = []
    def ask(system, prompt):
        packet = json.loads(prompt)
        calls.append(packet)
        if "source_review_focus" in packet:
            # A doubtful intermediate reading can be resolved by the final
            # editor. Code preserves it, without making its own verdict.
            assert packet["draft"] == "" and "response_contract" not in packet
            return "原段需要结合后文核对。这里的主体仍不确定。"
        if packet["source_reviews"]:
            assert len(packet["source_reviews"]) == 2
            assert all(item["note"] == "原段需要结合后文核对。这里的主体仍不确定。"
                       and "complete" not in item for item in packet["source_reviews"])
            return response("结合原文澄清后的正文。" if packet["draft"] else "最初正文。")
        return response("最初正文。")
    target = run(tmp_path, ask)
    readings = [p for p in calls if "source_review_focus" in p]
    assert [p["transcript"] for p in readings] == [[s] for s in TRANSCRIPT["segments"]]
    assert all(p["courseware"] == [] for p in readings)
    assert all(p["transcript"] == TRANSCRIPT["segments"] for p in calls if "source_review_focus" not in p)
    report = json.loads(note_editor.report_path(target).read_text("utf-8"))
    assert report["content_complete"] is True
    assert report["assessment"] == "model-review-not-independent-proof"
    assert note_editor.review_record_issues(target, TRANSCRIPT) == []
    assert "原段需要结合后文核对。" not in target.read_text("utf-8")


def test_interruption_during_source_reading_reuses_all_finished_responses(tmp_path):
    calls, fail = [], [True]
    def ask(system, prompt):
        packet = json.loads(prompt)
        calls.append(packet)
        if packet.get("source_review_window") == 2 and fail[0]:
            fail[0] = False
            raise RuntimeError("request still in progress")
        return response("审阅意见。" if "source_review_focus" in packet else "课堂正文。")
    with pytest.raises(RuntimeError, match="in progress"):
        run(tmp_path, ask)
    assert not (tmp_path / "notes.md").exists()
    saved = {p.name: p.read_bytes() for p in (tmp_path / ".notes-parts" / note_editor.POLICY).glob("*.json")}
    assert len(saved) == 1
    target = run(tmp_path, ask)
    assert len(calls) == 5
    assert all((tmp_path / ".notes-parts" / note_editor.POLICY / name).read_bytes() == raw for name, raw in saved.items())
    assert note_editor.review_record_issues(target, TRANSCRIPT) == []


def test_source_reading_quotes_survive_verbatim_without_format_rebilling(tmp_path):
    calls = []
    raw = '老师提到"140个亿”，然后说"860个亿”。\n这里仅供参考，需要核对。'
    def ask(system, prompt):
        packet = json.loads(prompt)
        calls.append(packet)
        assert "format_repair" not in packet
        if "source_review_focus" in packet:
            return raw
        if packet["source_reviews"]:
            assert [r["note"] for r in packet["source_reviews"]] == [raw] * 2
        return response("课堂正文。")
    target = run(tmp_path, ask)
    assert len(calls) == 4
    assert note_editor.review_record_issues(target, TRANSCRIPT) == []
    assert raw not in target.read_text("utf-8")
    records = [json.loads(p.read_text("utf-8")) for p in (tmp_path / ".notes-parts" / note_editor.POLICY).glob("*.json")]
    assert [r["response"] for r in records if r["stage"] == "source-review"] == [raw] * 2


def test_missing_source_window_receipt_cannot_claim_finished_review(tmp_path):
    target = run(tmp_path, lambda *args: response("课堂正文。"))
    path = note_editor.report_path(target)
    report = json.loads(path.read_text("utf-8"))
    report["chapters"][0]["requests"].pop(2)
    path.write_text(json.dumps(report, ensure_ascii=False), "utf-8")
    assert note_editor.review_record_issues(target, TRANSCRIPT)


def test_individually_valid_final_request_cannot_drop_saved_source_feedback(tmp_path):
    target = run(tmp_path, lambda *args: response("课堂正文。"))
    path = note_editor.report_path(target)
    report = json.loads(path.read_text("utf-8"))
    cache = target.parent / ".notes-parts" / note_editor.POLICY
    record = json.loads((cache / (report["chapters"][0]["requests"][-1] + ".json")).read_text("utf-8"))
    record["input"]["source_reviews"] = []
    key = note_editor.fingerprint({k: record[k] for k in ("policy", "stage", "system", "input", "model", "identity")})
    record["request_sha256"] = key
    note_editor.write_json(cache / (key + ".json"), record)
    report["chapters"][0]["requests"][-1] = key
    path.write_text(json.dumps(report, ensure_ascii=False), "utf-8")
    assert note_editor.review_record_issues(target, TRANSCRIPT)
