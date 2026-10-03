"""Script normalization: ASR script must not decide note text or anchors.

Cloud ASR returned Traditional Chinese for a real course, and one recording
mixed both scripts segment by segment. Every text comparison in the note
pipeline is a character-overlap comparison, so an unconverted transcript made
re-listen markers collapse onto the first second of their chapter and left
Traditional fragments in a Simplified note.
"""

import json
from types import SimpleNamespace

from pku_sync import media


def _answer(system, user):
    batch = json.loads(user)["claims"]
    return json.dumps({"schema_version": 1, "verdicts": [
        {"id": row["id"], "status": "supported", "reason": "课堂原话",
         "evidence": [{"id": "B0001", "quote": "明文是加密前的信息"}]}
        for row in batch
    ]}, ensure_ascii=False)


def _window():
    """One chapter whose second evidence block holds the sentence we want."""
    return {
        "start": 0.0,
        "end": 480.0,
        "text": "那麼今天我們先講一下密碼學的基本概念 老師要求下週三之前提交第一次作業",
        "evidence_blocks": [
            {"block_id": "B0001", "start": 0.0, "end": 12.0,
             "text": "那麼今天我們先講一下密碼學的基本概念"},
            {"block_id": "B0002", "start": 40.0, "end": 52.0,
             "text": "老師要求下週三之前提交第一次作業"},
        ],
    }


def test_to_simplified_is_idempotent_and_keeps_numbers_and_latin():
    once = media.to_simplified("老師要求下週三之前提交第一次作業，例如 AES 與 DES。")
    assert once == "老师要求下周三之前提交第一次作业，例如 AES 与 DES。"
    assert media.to_simplified(once) == once


def test_simplified_segments_preserves_timing_and_the_callers_transcript():
    already = {"start": 0, "end": 4, "text": "密码学的基本概念"}
    traditional = {"start": 40, "end": 52, "text": "老師要求下週三之前提交第一次作業",
                   "speaker": "teacher"}
    converted = media.simplified_segments([already, traditional])
    assert converted[0] is already
    assert converted[1] == {"start": 40, "end": 52,
                            "text": "老师要求下周三之前提交第一次作业",
                            "speaker": "teacher"}
    assert traditional["text"] == "老師要求下週三之前提交第一次作業"


def test_pending_anchor_finds_the_spoken_moment_behind_a_traditional_block():
    stamp = media._pending_time_label("老师要求下周三之前提交第一次作业。", _window())
    assert stamp == "00:40"


def test_pending_anchor_accepts_a_traditional_note_sentence():
    stamp = media._pending_time_label("老師要求下週三之前提交第一次作業。", _window())
    assert stamp == "00:40"


_LONG_TRADITIONAL_BLOCK = (
    "那麼我們今天呢 就先來看一看 老師給我們提出的這個問題 "
    "我們與老師討論這個問題的時候呢 其實要先弄清楚一些最基本的概念 "
    "然後才能往下走 所以我們先從最簡單的地方開始 一步一步地來"
)


def _long_window():
    """A real ASR block is long, which dilutes any accidental character overlap."""
    return {
        "start": 0.0,
        "end": 480.0,
        "text": "先講基本概念 " + _LONG_TRADITIONAL_BLOCK,
        "evidence_blocks": [
            {"block_id": "B0001", "start": 0.0, "end": 12.0,
             "text": "那麼今天我們先講一下密碼學的基本概念"},
            {"block_id": "B0002", "start": 40.0, "end": 52.0,
             "text": _LONG_TRADITIONAL_BLOCK},
        ],
    }


def test_conversion_is_what_makes_a_long_traditional_block_findable():
    """Simplified and Traditional share too many characters to rely on luck.

    A short block was still matched through the characters the two scripts
    have in common. Once the block is a real ASR block, that leftover overlap
    drops under the match threshold and the marker collapses onto the chapter
    start instead of the moment the sentence was spoken.
    """
    sentence = "我们与老师讨论这个问题。"
    raw = _long_window()
    assert media._pending_time_label(sentence, raw) == "00:00"
    converted = {**raw, "evidence_blocks": [
        {**block, "text": media.to_simplified(block["text"])}
        for block in raw["evidence_blocks"]]}
    assert media._pending_time_label(sentence, converted) == "00:40"


def test_missing_converter_degrades_to_the_original_text(monkeypatch):
    monkeypatch.setattr(media, "zhconv", None)
    assert media.to_simplified("老師") == "老師"
    assert media.simplified_segments(
        [{"start": 0, "end": 1, "text": "老師"}])[0]["text"] == "老師"


def test_traditional_transcript_still_anchors_the_marker_on_the_spoken_moment(
        tmp_path, monkeypatch):
    settings = SimpleNamespace(platform_token="test", openai_api_key="",
                               notes_model="test", notes_fact_check=False,
                               notes_claim_audit=True)
    transcript = {"segments": [
        {"start": 0, "end": 12, "text": "那麼今天我們先講一下密碼學的基本概念"},
        {"start": 40, "end": 52, "text": "老師要求下週三之前提交第一次作業"},
    ]}
    target = tmp_path / "notes.md"
    monkeypatch.setattr(media, "_summarize",
                        lambda *a, **k: "老师要求下周三之前提交第一次作业。")

    def uncertain(_llm, _model, prompt, system, _settings):
        parsed = json.loads(_answer(system, prompt))
        for verdict in parsed["verdicts"]:
            verdict.update(status="uncertain", evidence=[], reason="转写含混")
        return json.dumps(parsed, ensure_ascii=False)

    monkeypatch.setattr(media, "_recovery_llm", uncertain)
    assert media.write_notes(transcript, [], target, settings, "课程") == target
    note = target.read_text("utf-8")
    assert "提交第一次作業" not in note
    assert "（回看 00:40）" in note
    assert "（回看 00:00）" not in note
