"""Actual HTTP capacity and durable job reuse, without judging note prose."""
import json
from types import SimpleNamespace

import httpx
import pytest

from pku_sync import note_editor, platform


@pytest.mark.parametrize("operation,model", [("notes", "qwen3.5-flash"),
    ("notes", "qwen/qwen3.5-397b-a17b"), ("quiz", None)])
def test_multibyte_wire_limit_stops_before_enqueue_or_retry_state(tmp_path, monkeypatch, operation, model):
    settings = SimpleNamespace(platform_token="offline", notes_model=model, data_dir=tmp_path,
        cloud_transcribe_url="https://relay.example", llm_timeout=600)
    prompt = "原" * 90000
    assert len(prompt) < note_editor.FLASH_INPUT_CHARS
    monkeypatch.setattr(platform, "_post", lambda *a, **kw: pytest.fail("oversized request submitted"))
    with pytest.raises(platform.PlatformError, match="传输容量") as failure:
        platform.llm(operation, prompt, settings, system="原资料完整发送")
    assert failure.value.status_code == 413
    assert list(tmp_path.iterdir()) == []


def test_source_metadata_exceeds_bytes_but_not_character_budget_before_reading(tmp_path):
    source = {"title": "课" * 90000, "status": "readable", "text": "参考原文。"}
    transcript = {"segments": [{"start": 0, "end": 12, "text": "原口述。"}]}
    calls = []
    with pytest.raises(RuntimeError, match="任务未提交"):
        note_editor.write_notes(transcript, tmp_path / "notes.md",
            SimpleNamespace(notes_model="qwen3.5-flash"), "课程", source_context=[source],
            ask=lambda *a: calls.append(a))
    assert calls == [] and source["title"] == "课" * 90000
    assert not (tmp_path / ".notes-parts").exists()


def test_wire_size_fallback_preserves_every_original_character(tmp_path):
    # Character count permits a whole lecture, but its Chinese UTF-8 bytes
    # plus reference material and draft allowance do not fit the relay.
    transcript = {"segments": [{"start": i * 600, "end": i * 600 + 300,
        "text": "原" * 3000} for i in range(12)]}
    sources = [{"title": "课" * 25000, "status": "readable", "text": "参考原文。"}]
    calls = []
    def ask(system, prompt):
        assert platform.llm_request_body_size("notes", system, prompt,
            model="qwen3.5-flash") <= platform.LLM_REQUEST_MAX_BYTES
        calls.append(json.loads(prompt))
        return json.dumps({"note": "已整理的正文。", "complete": False,
            "uncertainties": ["需回看原录像。"]}, ensure_ascii=False)
    target = tmp_path / "notes.md"
    note_editor.write_notes(transcript, target, SimpleNamespace(notes_model="qwen3.5-flash"),
        "课程", source_context=sources, ask=ask)
    assert calls[0]["editing_scope"] == "transport-parts"
    for offset in range(3):
        assert [row for call in calls[offset::3] for row in call["transcript"]] == transcript["segments"]
    assert note_editor.review_record_issues(target, transcript, sources) == []


def test_policy_change_reuses_identical_paid_format_recovery_job(tmp_path, monkeypatch):
    settings = SimpleNamespace(platform_token="offline", notes_model="qwen3.5-flash", data_dir=tmp_path,
        cloud_transcribe_url="https://relay.example", llm_timeout=600)
    sent, jobs = [], {}
    def post(url, **kwargs):
        body = kwargs["json"]
        sent.append(body)
        jobs.setdefault(body["idempotency_key"], body)
        return httpx.Response(202, json={"job_id": body["idempotency_key"]})
    def get(url, **kwargs):
        key = url.rsplit("/", 1)[-1]
        payload = json.loads(jobs[key]["user"])
        raw = json.dumps({"note": "原响应恢复后的正文。", "complete": False,
            "uncertainties": ["具体事项待核对。"]}, ensure_ascii=False) if "format_repair" in payload else "原始正文不是 JSON。"
        return httpx.Response(200, json={"job_id": key, "status": "succeeded",
            "content": raw, "points_charged": .1})
    monkeypatch.setattr(platform, "_post", post)
    monkeypatch.setattr(platform, "_get", get)
    ask = lambda system, prompt: platform.llm("notes", prompt, settings, system=system)["content"]
    payload = {"transcript": [{"start": 0, "end": 12, "text": "原文。"}]}
    saved = {}
    for policy in ("fixture-policy-before", "fixture-policy-after"):
        monkeypatch.setattr(note_editor, "POLICY", policy)
        cache = tmp_path / policy
        result, keys = note_editor._request(ask, stage="write", payload=payload,
            model=settings.notes_model, identity="same-account-source", cache_dir=cache)
        assert result["complete"] is False and len(keys) == 2
        if not saved:
            saved = {p: p.read_bytes() for p in cache.iterdir()}
    assert len(sent) == 4 and len(jobs) == 2
    assert sent[0] == sent[2] and sent[1] == sent[3]
    assert all(p.read_bytes() == raw for p, raw in saved.items())
