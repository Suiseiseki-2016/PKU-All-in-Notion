"""Cross-chapter repetition: the student reads the note in order.

A chapter is summarized for its own time window, so a definition the speaker
returns to later is written down twice and the note reads as though the lecture
repeated itself. The first statement keeps its place; a later restatement
leaves the body.
"""

from types import SimpleNamespace

from pku_sync import media


def test_repeated_sentence_leaves_the_body_once():
    chapter = "加密是把明文变换成密文的过程。本节还讨论了模运算与逆元的求法。"
    assert media.drop_repeated_chapter_sentences(
        chapter, ["加密是把明文变换成密文的过程。"]) \
        == "本节还讨论了模运算与逆元的求法。"


def test_heading_and_caveat_lines_survive_a_repeat_filter():
    chapter = ("### 古典密码的替换思想\n"
               "古典密码把每个字母单独替换成另一个字母。\n"
               "本节还给出了密钥空间的估算方法。\n"
               "> 本节一处表述未能核实，建议回听 12:03")
    kept = media.drop_repeated_chapter_sentences(
        chapter, ["古典密码把每个字母单独替换成另一个字母。"])
    assert kept == ("### 古典密码的替换思想\n"
                    "本节还给出了密钥空间的估算方法。\n"
                    "> 本节一处表述未能核实，建议回听 12:03")


def test_chapter_is_left_alone_when_every_sentence_repeats():
    chapter = "加密是把明文变换成密文的过程。\n把密文恢复成明文的过程称为解密。"
    seen = ["加密是把明文变换成密文的过程。", "把密文恢复成明文的过程称为解密。"]
    assert media.drop_repeated_chapter_sentences(chapter, seen) == chapter


def test_sentence_with_numbers_is_never_dropped_as_a_repeat():
    line = "成绩由平时作业 40% 与期末笔试 60% 组成。"
    chapter = f"{line}\n本节还讨论了模运算与乘法逆元的求法。"
    assert media.drop_repeated_chapter_sentences(chapter, [line]) == chapter


def test_coursework_sentence_is_never_dropped_as_a_repeat():
    line = "老师要求下周三之前提交第一次作业。"
    chapter = f"{line}\n本节还讨论了模运算与乘法逆元的求法。"
    assert media.drop_repeated_chapter_sentences(chapter, [line]) == chapter


def test_sentence_that_adds_new_material_is_kept():
    seen = ["古典密码把每个字母单独替换成另一个字母，密钥空间只有二十六个可能。"]
    chapter = ("古典密码把每个字母单独替换成另一个字母，但频率分析可以据此破译，"
               "所以密钥空间小并不足以保证安全。")
    assert media.drop_repeated_chapter_sentences(chapter, seen) == chapter


def test_contextual_editor_receives_previous_chapter_without_rule_deletion(tmp_path, monkeypatch):
    """The provider controls repetition; this verifies full input and output preservation."""
    import copy
    import json
    import pytest
    from pku_sync import note_editor
    settings = SimpleNamespace(platform_token="test", openai_api_key="", notes_model="test")
    first = "加密是把明文变换成密文的过程。后面继续讨论模运算。"
    second = "再回到加密的定义，现在讲频率分析与重合指数，统计每个字母出现的次数。"
    transcript = {"segments": [{"start": 0, "end": 70, "text": first},
                               {"start": 250, "end": 320, "text": second}]}
    original = copy.deepcopy(transcript)
    final = {1: "### 基本概念\n加密是把明文变换成密文的过程，并涉及模运算。",
             2: "### 频率分析\n在前述定义基础上，老师说明频率分析与重合指数，需要统计每个字母出现的次数。"}
    calls = []
    def provider(_llm, _model, prompt, system, _settings):
        payload = json.loads(prompt); calls.append(payload)
        part = payload["part"]
        assert payload["transcript"] == [transcript["segments"][part-1]]
        assert payload["previous_context"] == ([] if part == 1 else [original["segments"][0]])
        return final[part]
    monkeypatch.setattr(media, "_recovery_llm", provider)
    monkeypatch.setattr(media, "drop_repeated_chapter_sentences", lambda *_: pytest.fail("lexical deletion"))
    target = tmp_path / "notes.md"
    media.write_notes(transcript, [], target, settings, "课程", window_seconds=120)
    note = target.read_text("utf-8")
    assert len(calls) == 2 and transcript == original
    assert all(body in note for body in final.values())
    assert note_editor.review_record_issues(target, transcript) == []
