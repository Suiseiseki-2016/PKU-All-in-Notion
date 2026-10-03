"""Local guardrails for natural paragraphs made from validated lecture facts."""

import json

import pytest

from pku_sync.structured_note_composer import (
    CompositionError,
    build_request,
    render_reader_markdown,
    validate_and_render,
)


FACTS = [
    {"id": "F01", "origin": "slide", "display_text": "1949年Shannon提出保密系统的通信理论。",
     "source_filename": "ch02-古典密码.pdf", "source_page": "第 29 页"},
    {"id": "F02", "origin": "slide", "display_text": "1971-73年IBM Watson发表密码学报告。",
     "source_filename": "ch02-古典密码.pdf", "source_page": "29"},
    {"id": "F03", "origin": "transcript", "display_text": "老师提醒作业要按指定格式提交。"},
]


def _response(*sentences):
    return {"paragraphs": [{"sentences": list(sentences)}]}


def _sentence(text, fact_ids, origin="slide"):
    return {"text": text, "fact_ids": fact_ids, "origin": origin}


def test_request_exposes_exact_schema_and_all_facts():
    system, user = build_request(FACTS, title="现代密码学")
    payload = json.loads(user)
    assert "fact_ids" in system
    assert payload["title"] == "现代密码学"
    assert [row["id"] for row in payload["facts"]] == ["F01", "F02", "F03"]
    assert payload["facts"][0]["source_page"] == "第29页"


def test_natural_paragraphs_bind_citation_to_each_slide_sentence():
    result = validate_and_render(FACTS, {"paragraphs": [
        {"sentences": [
            _sentence("1949年Shannon提出保密系统的通信理论。", ["F01"]),
            _sentence("1971—1973年IBM Watson发表密码学报告。", ["F02"]),
        ]},
        {"sentences": [
            _sentence("老师提醒作业要按指定格式提交。", ["F03"], "transcript")
        ]},
    ]})
    assert "Shannon提出保密系统的通信理论。（参考课件：ch02-古典密码.pdf 第29页）" in result.markdown
    assert "1971—1973年IBM Watson" in result.markdown
    assert "\n\n老师提醒" in result.markdown
    assert result.markdown.count("参考课件") == 2
    assert result.used_fact_ids == ("F01", "F02", "F03")
    assert result.sentences[-1].source_labels == ()
    assert result.sentences[-1].origin == "transcript"


def test_reader_view_shows_compact_sentence_markers_and_one_source_legend():
    result = validate_and_render(FACTS, {"paragraphs": [
        {"sentences": [
            _sentence("1949年Shannon提出保密系统的通信理论。", ["F01"]),
            _sentence("1971—1973年IBM Watson发表密码学报告。", ["F02"]),
        ]},
        {"sentences": [_sentence("老师提醒作业要按指定格式提交。", ["F03"], "transcript")]},
    ]})
    reading = render_reader_markdown(result)
    assert reading.count("〔1〕") == 3
    assert reading.count("ch02-古典密码.pdf 第29页") == 1
    assert "老师提醒作业要按指定格式提交。" in reading
    assert "参考课件" not in reading
    assert result.markdown.count("参考课件") == 2


def test_multiple_same_page_facts_can_form_one_sentence():
    facts = [
        {"id": "F1", "origin": "slide", "display_text": "代替密码改变符号。",
         "source_filename": "slides.pdf", "source_page": 36},
        {"id": "F2", "origin": "slide", "display_text": "置换密码改变顺序。",
         "source_filename": "slides.pdf", "source_page": 36},
    ]
    result = validate_and_render(facts, _response(
        _sentence("代替密码改变符号，置换密码改变顺序。", ["F1", "F2"])))
    assert result.markdown.endswith("（参考课件：slides.pdf 第36页）")


def test_different_slide_pages_cannot_be_merged_in_one_sentence():
    facts = [FACTS[0], {**FACTS[1], "source_page": "第30页"}]
    with pytest.raises(CompositionError, match="different slide pages"):
        validate_and_render(facts, _response(_sentence(
            "1949年Shannon提出保密系统的通信理论；1971—1973年IBM Watson发表密码学报告。",
            ["F01", "F02"])))


def test_single_sentence_cannot_combine_three_facts():
    facts = [{"id": f"F{i}", "origin": "slide", "display_text": f"第{i}种密码改变符号。",
              "source_filename": "slides.pdf", "source_page": 29} for i in range(1, 4)]
    with pytest.raises(CompositionError, match="invalid sentence content"):
        validate_and_render(facts, _response(_sentence(
            "第1种密码改变符号，第2种密码改变符号，第3种密码改变符号。",
            ["F1", "F2", "F3"])))


def test_six_facts_need_multiple_paragraphs():
    facts = [{"id": f"F{i}", "origin": "transcript", "display_text": f"讨论了主题{i}。"}
             for i in range(1, 7)]
    response = _response(*[_sentence(f"讨论了主题{i}。", [f"F{i}"], "transcript")
                           for i in range(1, 7)])
    with pytest.raises(CompositionError, match="multiple paragraphs"):
        validate_and_render(facts, response)


def test_sentence_has_180_character_limit():
    with pytest.raises(CompositionError, match="invalid sentence content"):
        validate_and_render(FACTS[:1], _response(_sentence(
            "1949年Shannon提出保密系统的通信理论" + "，并说明理论" * 30 + "。", ["F01"])))


def test_one_row_cannot_hide_a_second_sentence_after_a_full_stop():
    with pytest.raises(CompositionError, match="multiple sentences"):
        validate_and_render(FACTS[:1], _response(_sentence(
            "1949年Shannon提出保密系统的通信理论。所有算法安全。", ["F01"])))


def test_new_relationship_between_facts_fails_closed():
    with pytest.raises(CompositionError, match="relationship"):
        validate_and_render(FACTS[:2], _response(_sentence(
            "1949年Shannon提出保密系统的通信理论，这一原则在1971—1973年IBM Watson发表密码学报告的过程中得到体现。",
            ["F01", "F02"])))


@pytest.mark.parametrize("payload,fragment", [
    (_response(_sentence("1949年Shannon提出保密系统的通信理论。", ["F01"])), "omitted"),
    (_response(_sentence("1949年Shannon提出保密系统的通信理论。", ["F01", "F01"])), "repeated"),
    (_response(_sentence("1949年Shannon提出保密系统的通信理论。", ["F99"])), "unknown"),
    ({"paragraphs": [{"sentences": [{"text": "一句。", "fact_ids": ["F01"],
                                     "origin": "slide", "note": "untrusted"}]}]}, "schema"),
])
def test_invalid_fact_coverage_or_schema_fails_closed(payload, fragment):
    with pytest.raises(CompositionError, match=fragment):
        validate_and_render(FACTS, payload)


def test_wrong_provenance_and_teacher_attribution_fail_closed():
    with pytest.raises(CompositionError, match="origin"):
        validate_and_render(FACTS[:1], _response(_sentence(
            "1949年Shannon提出保密系统的通信理论。", ["F01"], "transcript")))
    with pytest.raises(CompositionError, match="attributed"):
        validate_and_render(FACTS[:1], _response(_sentence(
            "老师说1949年Shannon提出保密系统的通信理论。", ["F01"])))
    with pytest.raises(CompositionError, match="origin"):
        validate_and_render(FACTS[:2], _response(_sentence(
            "1949年Shannon提出保密系统的通信理论，老师提醒作业。",
            ["F01", "F02"], "both")))


def test_invented_year_acronym_and_latin_name_fail_closed():
    with pytest.raises(CompositionError, match="number"):
        validate_and_render(FACTS[1:2], _response(_sentence(
            "1971—1974年IBM Watson发表密码学报告。", ["F02"])))
    with pytest.raises(CompositionError, match="acronym"):
        validate_and_render(FACTS[1:2], _response(_sentence(
            "1971—1973年IBM Watson发表AES密码学报告。", ["F02"])))
    with pytest.raises(CompositionError, match="Latin"):
        validate_and_render(FACTS[:1], _response(_sentence(
            "1949年Shannon和Turing提出保密系统的通信理论。", ["F01"])))


def test_english_gloss_of_a_grounded_chinese_term_is_allowed():
    """Bilingual terminology is how these courses are taught and written down."""
    facts = [{"id": "G01", "origin": "slide",
              "display_text": "明文经过加密算法变成密文。",
              "source_filename": "ch02-古典密码.pdf", "source_page": "第 8 页"}]
    result = validate_and_render(facts, _response(_sentence(
        "明文（plaintext）经过加密算法变成密文（ciphertext）。", ["G01"])))
    assert "明文（plaintext）" in result.markdown

    # A name asserted in running prose is still an invention, not a gloss.
    with pytest.raises(CompositionError, match="Latin"):
        validate_and_render(facts, _response(_sentence(
            "明文经过 Feistel 加密算法变成密文。", ["G01"])))


def test_supported_lead_does_not_hide_unsupported_second_clause():
    with pytest.raises(CompositionError, match="relationship|ungrounded clause"):
        validate_and_render(FACTS[:1], _response(_sentence(
            "1949年Shannon提出保密系统的通信理论，因此所有算法都已经绝对安全。",
            ["F01"])))


def test_model_cannot_supply_page_number_or_source_label():
    with pytest.raises(CompositionError, match="source label"):
        validate_and_render(FACTS[:1], _response(_sentence(
            "1949年Shannon提出保密系统的通信理论（第29页）。", ["F01"])))


def test_slide_facts_require_safe_source_page_and_transcript_cannot_claim_slide():
    with pytest.raises(CompositionError, match="source_page"):
        build_request([{**FACTS[0], "source_page": "第 29-30 页"}])
    with pytest.raises(CompositionError, match="slide provenance"):
        build_request([{**FACTS[2], "source_page": "29"}])


def test_empty_or_non_json_model_output_fails_closed():
    with pytest.raises(CompositionError, match="strict JSON"):
        validate_and_render(FACTS, "```json\n{broken}\n```")
    valid = json.dumps(_response(_sentence(
        "1949年Shannon提出保密系统的通信理论。", ["F01"])), ensure_ascii=False)
    assert validate_and_render(FACTS[:1], f"```json\n{valid}\n```").used_fact_ids == ("F01",)
    with pytest.raises(CompositionError, match="paragraphs"):
        validate_and_render(FACTS, {"paragraphs": []})
