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


def test_definition_restated_in_the_next_chapter_is_written_once(tmp_path, monkeypatch):
    settings = SimpleNamespace(platform_token="test", openai_api_key="",
                               notes_model="test", notes_fact_check=False,
                               notes_claim_audit=False)
    first = ["我们先把加密的定义写清楚，加密是把明文变换成密文的过程。",
             "第一部分的要点讲完了，接下来我们看模运算与乘法逆元。",
             "模运算的细节我们后面还会继续展开。"]
    second = ["再回到定义，加密是把明文变换成密文的过程。",
              "第二部分的要点讲完了，接下来我们讲频率分析与重合指数。",
              "频率分析要数出每个字母出现的次数。"]
    transcript = {"segments": [
        {"start": 0.0, "end": 20.0, "text": first[0]},
        {"start": 25.0, "end": 45.0, "text": first[1]},
        {"start": 50.0, "end": 70.0, "text": first[2]},
        {"start": 250.0, "end": 270.0, "text": second[0]},
        {"start": 275.0, "end": 295.0, "text": second[1]},
        {"start": 300.0, "end": 320.0, "text": second[2]},
    ]}
    drafts = ["### 基本概念\n" + "".join(first),
              "### 继续讨论\n" + "".join(second)]
    calls = {"count": 0}

    def draft_for_chapter(*args, **kwargs):
        draft = drafts[min(calls["count"], len(drafts) - 1)]
        calls["count"] += 1
        return draft

    monkeypatch.setattr(media, "_summarize", draft_for_chapter)
    target = tmp_path / "notes.md"
    media.write_notes(transcript, [], target, settings, "课程", window_seconds=120)
    note = target.read_text("utf-8")
    assert calls["count"] >= 2
    assert note.count("加密是把明文变换成密文的过程") == 1
    assert "第二部分的要点讲完了" in note
    assert "频率分析要数出每个字母出现的次数" in note
    assert "第一部分的要点讲完了" in note
