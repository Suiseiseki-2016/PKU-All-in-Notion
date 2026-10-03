from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pku_sync.claim_audit import (
    _parse_model_json, _parse_or_repair_model_json, _validate_batch,
    audit_chapter, chapter_assertions,
)
from pku_sync import media


BLOCKS = [{"block_id": "B0001", "text": "老师说明明文是加密前的信息。"}]
PAGES = [{"title": "课件.pdf", "locator": "第 3 页", "text": "明文是加密前的信息。"}]


def _answer(system, user):
    batch = json.loads(user)["claims"]
    return json.dumps({"schema_version": 1, "verdicts": [
        {"id": row["id"], "status": "supported", "reason": "课堂原话",
         "evidence": [{"id": "B0001", "quote": "明文是加密前的信息"}]}
        for row in batch
    ]}, ensure_ascii=False)


def test_chapter_assertions_keep_all_bullets_and_sentence_context():
    rows = chapter_assertions("### 基础概念\n- **明文**：明文是加密前的信息。密文是结果。\n")
    assert [row["id"] for row in rows] == ["C001", "C002"]
    assert all(row["context"] == "基础概念" for row in rows)
    assert rows[0]["text"].startswith("明文：")


def test_unmarked_short_line_is_audited_as_a_claim():
    rows = chapter_assertions("### 古典密码\nPlayfair 分组密码\n两字母组成一个输入块。")
    assert len(rows) == 2
    assert rows[0]["text"] == "Playfair 分组密码"
    assert rows[1]["context"] == "古典密码"


def test_incomplete_short_lecture_lines_cannot_hide_as_headings():
    rows = chapter_assertions(
        "### 古典密码\n所以在这个例子里呢，其实就就蕴含了密码学的一个基本思想，就代替\n"
        "同时呢也出现了这个，就是现代对称分组密码设计的一些技术报告")
    assert len(rows) == 2
    assert all(row["context"] == "古典密码" for row in rows)


def test_teacher_requirement_in_blockquote_is_not_omitted():
    rows = chapter_assertions("### 作业要求\n> 老师要求下周提交书面作业。")
    assert len(rows) == 1
    assert rows[0]["text"] == "老师要求下周提交书面作业。"


def test_standalone_slide_locator_is_not_a_factual_claim():
    rows = chapter_assertions(
        "加密密钥控制加密算法。\n（参考课件：ch02-古典密码.pdf，第 11 页）\n"
        "解密算法还原明文（课件：ch02-古典密码.pdf，第 11 页）。\n"
        "密钥仍需保密。（参考课件：ch02-古典密码.pdf，第 17 页）")
    assert len(rows) == 3
    assert rows[0]["text"] == "加密密钥控制加密算法。"
    assert "第 11 页" in rows[1]["text"]


def test_trailing_slide_citation_stays_bound_to_the_preceding_fact():
    rows = chapter_assertions(
        "RSA 算法由三位作者提出。（课件：ch02-古典密码.pdf，第 30 页）\n"
        "置换会改变顺序。（课件：ch02-古典密码.pdf，第 36 页）")
    assert len(rows) == 2
    assert all("课件：ch02-古典密码.pdf" in row["text"] for row in rows)
    assert "第 30 页" in rows[0]["text"]
    assert "第 36 页" in rows[1]["text"]


def test_json_fence_is_accepted_without_accepting_explanatory_prose():
    assert _parse_model_json('```json\n{"schema_version":1}\n```') == {"schema_version": 1}
    with pytest.raises(ValueError):
        _parse_model_json('解释：\n```json\n{"schema_version":1}\n```')


def test_unescaped_book_title_in_one_line_evidence_quote_is_repaired():
    raw = ('{\n"schema_version":1,\n"verdicts":[\n'
           '{"id":"C001","status":"supported","evidence":[\n'
           '{"id":"S2","quote":"1949年Shannon的"The Communication Theory of Secret Systems""}\n'
           '],"reason":"课件明确列出标题"}\n]}')
    parsed = _parse_or_repair_model_json(raw)
    assert parsed is not None
    assert parsed["verdicts"][0]["evidence"][0]["quote"] == (
        '1949年Shannon的"The Communication Theory of Secret Systems"')
    assert _parse_or_repair_model_json('解释：\n' + raw) is None


def test_ascii_title_quotes_correct_only_when_curved_version_is_verbatim_source():
    batch = [{"id": "C001", "text": "1949年Shannon的著作。"}]
    evidence = {"S2": '1949年Shannon的“The Communication Theory of Secret Systems”'}
    row = {"id": "C001", "status": "supported", "reason": "课件", "evidence": [
        {"id": "S2", "quote": '1949年Shannon的"The Communication Theory of Secret Systems"'}]}
    issue, problems, _ = _validate_batch([row], batch, evidence, [])
    assert (issue, problems) == ("", [])
    assert row["evidence"][0]["quote"] in evidence["S2"]
    wrong = {**row, "evidence": [{"id": "S2", "quote": '1949年Shannon的"Wrong Systems"'}]}
    _, problems, _ = _validate_batch([wrong], batch, evidence, [])
    assert {problem["reason"] for problem in problems} == {"invented_or_missing_quote"}


def test_audit_requires_locally_present_evidence_quote(tmp_path):
    report = audit_chapter("### 概念\n明文是加密前的信息。", BLOCKS, PAGES,
                           _answer, cache_path=tmp_path / "audit.json", model="test")
    assert report["status"] == "pass"
    assert report["verdicts"][0]["evidence"][0]["id"] == "B0001"

    def fake_quote(system, user):
        parsed = json.loads(_answer(system, user))
        parsed["verdicts"][0]["evidence"][0]["quote"] = "课堂上不存在的原文"
        return json.dumps(parsed, ensure_ascii=False)

    rejected = audit_chapter("### 概念\n明文是加密前的信息。", BLOCKS, PAGES,
                             fake_quote, cache_path=tmp_path / "audit.json", model="changed")
    assert rejected["status"] == "unverified"
    assert rejected["validation_issues"] == [
        {"id": "C001", "reason": "invented_or_missing_quote"}]


def test_audit_fails_on_unsupported_and_malformed_rows():
    def unsupported(system, user):
        rows = json.loads(_answer(system, user))
        rows["verdicts"][0].update(status="unsupported", evidence=[], reason="关系不成立")
        return json.dumps(rows, ensure_ascii=False)

    result = audit_chapter("明文是加密前的信息。", BLOCKS, PAGES, unsupported)
    assert result["status"] == "fail"

    missing = audit_chapter("明文是加密前的信息。", BLOCKS, PAGES,
                            lambda *_: '{"schema_version":1,"verdicts":[]}')
    assert missing["status"] == "unverified"


def test_missing_verdict_gets_one_bounded_schema_retry():
    calls = []

    def ask(system, user):
        payload = json.loads(user)
        calls.append(payload)
        answer = json.loads(_answer(system, user))
        if "schema_retry" not in payload:
            answer["verdicts"] = answer["verdicts"][:1]
        return json.dumps(answer, ensure_ascii=False)

    report = audit_chapter("明文是加密前的信息。\n明文是加密前的信息。",
                           BLOCKS, [], ask)
    assert report["status"] == "pass"
    assert len(calls) == 2
    assert calls[1]["schema_retry"]["expected_ids_in_order"] == ["C001", "C002"]


def test_schema_retry_still_fails_closed_on_missing_verdict():
    calls = []

    def ask(system, user):
        calls.append(1)
        answer = json.loads(_answer(system, user))
        answer["verdicts"] = answer["verdicts"][:1]
        return json.dumps(answer, ensure_ascii=False)

    report = audit_chapter("明文是加密前的信息。\n明文是加密前的信息。",
                           BLOCKS, [], ask)
    assert report["status"] == "unverified"
    assert report["reason"] == "missing_or_misordered_verdict"
    assert len(calls) == 2


def test_invalid_verdict_fields_get_one_strict_retry():
    calls = []

    def ask(system, user):
        payload = json.loads(user)
        calls.append(payload)
        answer = json.loads(_answer(system, user))
        if "schema_retry" not in payload:
            answer["verdicts"][0]["extra"] = "not allowed"
        return json.dumps(answer, ensure_ascii=False)

    report = audit_chapter("明文是加密前的信息。", BLOCKS, [], ask)
    assert report["status"] == "pass"
    assert len(calls) == 2
    assert calls[1]["schema_retry"]["previous_validation_issue"] == "invalid_verdict_schema"


def test_audit_cache_includes_evidence_and_model(tmp_path):
    cache = tmp_path / "audit.json"
    calls = []
    def ask(system, user):
        calls.append(1)
        return _answer(system, user)

    audit_chapter("明文是加密前的信息。", BLOCKS, PAGES, ask,
                  cache_path=cache, model="one")
    audit_chapter("明文是加密前的信息。", BLOCKS, PAGES, ask,
                  cache_path=cache, model="one")
    assert len(calls) == 1
    audit_chapter("明文是加密前的信息。", BLOCKS, PAGES, ask,
                  cache_path=cache, model="two")
    audit_chapter("明文是加密前的信息。", BLOCKS,
                  [{**PAGES[0], "text": "明文是加密前的信息。新增内容。"}], ask,
                  cache_path=cache, model="two")
    assert len(calls) == 3


def test_complete_batches_survive_later_malformed_json(tmp_path):
    cache = tmp_path / "audit.json"
    chapter = "\n".join("明文是加密前的信息。" for _ in range(9))
    calls = []

    def broken_fifth(system, user):
        ids = [row["id"] for row in json.loads(user)["claims"]]
        calls.append(ids)
        if ids == ["C009"]:
            return '{"schema_version":1,"verdicts":['
        return _answer(system, user)

    first = audit_chapter(chapter, BLOCKS, PAGES, broken_fifth,
                          cache_path=cache, model="test")
    assert first["status"] == "unverified"
    assert [row["id"] for row in first["verdicts"]] == [
        f"C{i:03d}" for i in range(1, 9)]
    assert len(first["accepted_batches"]) == 2

    second = audit_chapter(chapter, BLOCKS, PAGES, broken_fifth,
                           cache_path=cache, model="test")
    assert second["status"] == "unverified"
    assert calls == [[f"C{i:03d}" for i in range(1, 5)],
                     [f"C{i:03d}" for i in range(5, 9)],
                     ["C009"], ["C009"], ["C009"], ["C009"]]

    completed = audit_chapter(chapter, BLOCKS, PAGES, _answer,
                              cache_path=cache, model="test")
    assert completed["status"] == "pass"
    assert len(completed["verdicts"]) == 9


def test_malformed_json_gets_one_fresh_structured_retry():
    calls = []

    def ask(system, user):
        request = json.loads(user)
        calls.append(request)
        if "format_retry" not in request:
            return '{"schema_version":1,"verdicts":['
        return _answer(system, user)

    report = audit_chapter("明文是加密前的信息。", BLOCKS, PAGES, ask)
    assert report["status"] == "pass"
    assert len(calls) == 2
    assert calls[1]["format_retry"]["attempt"] == 1


def test_legacy_partial_manifest_reuses_only_valid_complete_prefix(tmp_path):
    cache = tmp_path / "audit.json"
    chapter = "\n".join("明文是加密前的信息。" for _ in range(5))
    calls = []

    def fail_last(system, user):
        ids = [row["id"] for row in json.loads(user)["claims"]]
        calls.append(ids)
        return "bad-json" if ids == ["C005"] else _answer(system, user)

    first = audit_chapter(chapter, BLOCKS, PAGES, fail_last,
                          cache_path=cache, model="test")
    first.pop("accepted_batches")
    cache.write_text(json.dumps(first, ensure_ascii=False), "utf-8")
    audit_chapter(chapter, BLOCKS, PAGES, fail_last,
                  cache_path=cache, model="test")
    assert calls == [["C001", "C002", "C003", "C004"],
                     ["C005"], ["C005"], ["C005"], ["C005"]]


def test_batch_cache_bound_to_exact_assertions_and_evidence(tmp_path):
    cache = tmp_path / "audit.json"
    chapter = "\n".join("明文是加密前的信息。" for _ in range(5))
    calls = []

    def ask(system, user):
        calls.append(1)
        return _answer(system, user)

    assert audit_chapter(chapter, BLOCKS, PAGES, ask,
                         cache_path=cache, model="one")["status"] == "pass"
    assert len(calls) == 2
    assert audit_chapter(chapter, BLOCKS, PAGES, ask,
                         cache_path=cache, model="two")["status"] == "pass"
    assert len(calls) == 4
    assert audit_chapter(chapter, BLOCKS,
                         [{**PAGES[0], "text": "不同证据文本"}], ask,
                         cache_path=cache, model="two")["status"] == "pass"
    assert len(calls) == 6


def test_only_reason_quote_format_is_repaired_and_evidence_is_still_checked():
    def malformed_reason(system, user):
        answer = _answer(system, user)
        return answer.replace('"reason": "课堂原话"',
                              '"reason": "老师标注“前移 3"。"')

    report = audit_chapter("明文是加密前的信息。", BLOCKS, PAGES,
                           malformed_reason)
    assert report["status"] == "pass"

    def invented_evidence(system, user):
        answer = malformed_reason(system, user)
        return answer.replace("明文是加密前的信息", "从未出现的虚构证据")

    rejected = audit_chapter("明文是加密前的信息。", BLOCKS, PAGES,
                             invented_evidence)
    assert rejected["status"] == "unverified"
    assert rejected["validation_issues"] == [
        {"id": "C001", "reason": "invented_or_missing_quote"}]


def test_invalid_slide_quote_gets_one_distinct_targeted_reask(tmp_path):
    cache = tmp_path / "audit.json"
    chapter = "明文是加密前的信息（课件：课件.pdf，第 3 页）。"
    calls = []

    def ask(system, user):
        request = json.loads(user)
        calls.append(request)
        answer = json.loads(_answer(system, user))
        answer["verdicts"][0]["evidence"] = [
            {"id": "S1", "quote": "课件中不存在的完整引句"}]
        if "quote_repair" in request:
            repair = request["quote_repair"]
            assert repair["validation_issues"] == [
                {"id": "C001", "reason": "invented_or_missing_quote"}]
            assert repair["original_verdicts"][0]["evidence"][0]["id"] == "S1"
            answer["verdicts"][0]["evidence"] = [
                {"id": "S1", "quote": "明文是加密前的信息"}]
        return json.dumps(answer, ensure_ascii=False)

    report = audit_chapter(chapter, BLOCKS, PAGES, ask,
                           cache_path=cache, model="test")
    assert report["status"] == "pass"
    assert len(calls) == 2
    assert "quote_repair" not in calls[0]
    assert "quote_repair" in calls[1]
    assert audit_chapter(chapter, BLOCKS, PAGES, ask,
                         cache_path=cache, model="test")["status"] == "pass"
    assert len(calls) == 2


def test_quote_repair_malformed_json_gets_one_format_retry():
    calls = []

    def ask(system, user):
        request = json.loads(user)
        calls.append(request)
        answer = json.loads(_answer(system, user))
        answer["verdicts"][0]["evidence"] = [{
            "id": "S1", "quote": ("明文是加密前的信息" if "quote_repair" in request
                                  else "不存在的证据原文") }]
        raw = json.dumps(answer, ensure_ascii=False)
        return raw if "format_retry" in request or "quote_repair" not in request else raw + "}"

    report = audit_chapter("明文是加密前的信息（课件：课件.pdf，第 3 页）。",
                           BLOCKS, PAGES, ask)
    assert report["status"] == "pass"
    assert len(calls) == 3
    assert calls[2]["format_retry"]["attempt"] == 1


def test_short_source_quote_is_explained_in_targeted_repair():
    chapter = "明文是加密前的信息（课件：课件.pdf，第 3 页）。"
    calls = []

    def ask(system, user):
        request = json.loads(user)
        calls.append(request)
        quote = "明文是" if "quote_repair" not in request else "明文是加密前的信息"
        return json.dumps({"schema_version": 1, "verdicts": [{
            "id": "C001", "status": "supported", "reason": "课件明文定义",
            "evidence": [{"id": "S1", "quote": quote}],
        }]}, ensure_ascii=False)

    report = audit_chapter(chapter, BLOCKS, PAGES, ask)
    assert report["status"] == "pass"
    assert len(calls) == 2
    repair = calls[1]["quote_repair"]
    assert repair["invalid_quote_details"][0]["reason"] == "too_short_after_removing_whitespace"
    assert "明文是加密前的信息" in repair["invalid_quote_details"][0]["nearby_verbatim_source"]
    assert "至少六个字符" in repair["instruction"]


def test_exact_five_character_numeric_slide_label_is_valid_evidence():
    pages = [{"title": "课件.pdf", "locator": "第 8 页",
              "text": "明文\n加密\n后移3\n密文\n加密密钥\n3\n解密\n前移3\n明文\n解密密钥\n3"}]
    chapter = "凯撒密码的加密密钥为 3（课件：课件.pdf，第 8 页）。"

    def ask(system, user):
        return json.dumps({"schema_version": 1, "verdicts": [{
            "id": "C001", "status": "supported", "reason": "课件标签给出数值",
            "evidence": [{"id": "S1", "quote": "加密密钥\n3"}],
        }]}, ensure_ascii=False)

    assert audit_chapter(chapter, [], pages, ask)["status"] == "pass"


def test_invalid_quote_retry_fails_closed_and_can_resume_from_failed_batch(tmp_path):
    cache = tmp_path / "audit.json"
    chapter = "\n".join("明文是加密前的信息。" for _ in range(5))
    calls = []

    def ask(system, user):
        request = json.loads(user)
        ids = [row["id"] for row in request["claims"]]
        calls.append((ids, "quote_repair" in request))
        answer = json.loads(_answer(system, user))
        if ids == ["C005"]:
            answer["verdicts"][0]["evidence"][0]["quote"] = "不存在的虚构引句"
        return json.dumps(answer, ensure_ascii=False)

    first = audit_chapter(chapter, BLOCKS, PAGES, ask,
                          cache_path=cache, model="test")
    assert first["status"] == "unverified"
    assert first["validation_issues"] == [
        {"id": "C005", "reason": "invented_or_missing_quote"}]
    assert calls == [(["C001", "C002", "C003", "C004"], False),
                     (["C005"], False), (["C005"], True),
                     (["C005"], False), (["C005"], False)]
    audit_chapter(chapter, BLOCKS, PAGES, ask,
                  cache_path=cache, model="test")
    # The valid prefix stays cached; the failed claim receives a fresh verdict.
    assert calls[-3:] == [(["C005"], True), (["C005"], False),
                          (["C005"], False)]
    assert len(calls) == 9


def test_failed_batch_quote_repair_rechecks_one_claim_independently():
    calls = []

    def ask(system, user):
        request = json.loads(user)
        calls.append(request)
        quote = ("明文是加密前的信息" if "focused_evidence_retry" in request
                 else "课件中不存在的完整引句")
        return json.dumps({"schema_version": 1, "verdicts": [{
            "id": "C001", "status": "supported", "reason": "原文直接定义",
            "evidence": [{"id": "B0001", "quote": quote}],
        }]}, ensure_ascii=False)

    result = audit_chapter("明文是加密前的信息。", BLOCKS, PAGES, ask)
    assert result["status"] == "pass"
    assert len(calls) == 3
    assert "quote_repair" in calls[1]
    assert "focused_evidence_retry" in calls[2]


def test_snippet_id_fallback_resolves_exact_source_quote_after_failed_reasks():
    calls = []

    def ask(system, user):
        request = json.loads(user)
        calls.append(request)
        if "snippets" in request:
            choice = next(row["id"] for row in request["snippets"]
                          if "明文是加密前的信息" in row["quote"])
            return json.dumps({"schema_version": 1, "verdicts": [{
                "id": "C001", "status": "supported", "reason": "转写逐字定义明文",
                "evidence_ids": [choice],
            }]}, ensure_ascii=False)
        return json.dumps({"schema_version": 1, "verdicts": [{
            "id": "C001", "status": "supported", "reason": "错误引文",
            "evidence": [{"id": "B0001", "quote": "不存在的虚构引句"}],
        }]}, ensure_ascii=False)

    report = audit_chapter("明文是加密前的信息。", BLOCKS, PAGES, ask)
    assert report["status"] == "pass"
    assert len(calls) == 4
    assert report["verdicts"][0]["evidence"] == [
        {"id": "B0001", "quote": "老师说明明文是加密前的信息。"}]


def test_snippet_id_review_names_required_cited_page():
    from pku_sync.claim_audit import _review_with_snippet_ids

    claim = {"id": "C001", "text": "明文是加密前的信息（课件：课件.pdf，第 3 页）。"}

    def ask(system, user):
        request = json.loads(user)
        assert request["required_page_ids"] == ["S1"]
        assert "所引页" in system
        chosen = next(row["id"] for row in request["snippets"]
                      if row["source_id"] == "S1")
        return json.dumps({"schema_version": 1, "verdicts": [{
            "id": "C001", "status": "supported", "reason": "所引页直接定义",
            "evidence_ids": [chosen],
        }]}, ensure_ascii=False)

    row = _review_with_snippet_ids(claim, BLOCKS, PAGES, ask)
    assert row is not None
    assert row["evidence"] == [{"id": "S1", "quote": "明文是加密前的信息。"}]


def test_snippet_id_review_can_select_a_caesar_support_bundle():
    from pku_sync.claim_audit import _review_with_snippet_ids

    page = {
        "title": "ch02-古典密码.pdf",
        "locator": "第 8 页",
        "status": "readable",
        "text": "加密密钥3\n解密密钥3\nA 明文\nD 密文",
    }
    claim = {
        "id": "C001",
        "text": '明文"A"后移3位变为密文"D"（课件：ch02-古典密码.pdf，第 8 页）。',
    }

    def ask(_system, user):
        request = json.loads(user)
        bundle = next(
            row for row in request["support_bundles"]
            if row["source_id"] == "S1" and "加密密钥3" in row["quote"]
        )
        return json.dumps({"schema_version": 1, "verdicts": [{
            "id": "C001", "status": "supported", "reason": "同页证据包支持",
            "evidence_ids": [bundle["id"]],
        }]}, ensure_ascii=False)

    row = _review_with_snippet_ids(claim, [], [page], ask)
    assert row is not None
    assert row["evidence"][0]["id"] == "S1"
    assert "加密密钥3" in row["evidence"][0]["quote"]


def test_snippet_id_review_repairs_quotes_only_inside_reason():
    from pku_sync.claim_audit import _review_with_snippet_ids

    def ask(system, user):
        request = json.loads(user)
        choice = request["snippets"][0]["id"]
        return ('{"schema_version":1,"verdicts":[{"id":"C001",'
                '"status":"supported","evidence_ids":["' + choice + '"],'
                '"reason":"原话写了"明文"的定义"}]}')

    row = _review_with_snippet_ids({"id": "C001", "text": "明文是加密前的信息。"},
                                   BLOCKS, [], ask)
    assert row is not None
    assert row["evidence"] == [{"id": "B0001", "quote": "老师说明明文是加密前的信息。"}]


def test_exact_caesar_slide_example_is_verified_by_source_and_math():
    pages = [{"title": "ch02-古典密码.pdf", "locator": "第 7 页",
              "text": ("凯撒密表\n"
                       "omnia gallia est divisa in partes tres\n"
                       "RPQLD JDOOLD HVW GLYLVD LQ SDUWHV WUHV\n")}]
    sentence = ('例如，拉丁文句子"omnia gallia est divisa in partes tres"'
                '经此变换后成为"RPQLD JDOOLD HVW GLYLVD LQ SDUWHV WUHV"'
                '（课件：ch02-古典密码.pdf，第 7 页）。')

    def unsupported(_system, user):
        claim_id = json.loads(user)["claims"][0]["id"]
        return json.dumps({"schema_version": 1, "verdicts": [{
            "id": claim_id, "status": "unsupported", "evidence": [],
            "reason": "未在口头转写中找到例句",
        }]}, ensure_ascii=False)

    report = audit_chapter(sentence, [], pages, unsupported)
    assert report["status"] == "pass"
    assert report["verdicts"][0]["evidence"] == [
        {"id": "S1", "quote": "omnia gallia est divisa in partes tres"},
        {"id": "S1", "quote": "RPQLD JDOOLD HVW GLYLVD LQ SDUWHV WUHV"},
    ]
    wrong = sentence.replace("RPQLD", "RPQLX")
    assert audit_chapter(wrong, [], pages, unsupported)["status"] == "fail"
    missing_slide = [{**pages[0], "text": pages[0]["text"].replace("凯撒密表", "") }]
    assert audit_chapter(sentence, [], missing_slide, unsupported)["status"] == "fail"


def test_extra_bad_quote_gets_independent_valid_subset_review_before_slide_review():
    pages = [{"title": "课件.pdf", "locator": "第 3 页",
              "text": "明文是加密前的信息。密文是加密后的信息。"}]
    chapter = ("明文是加密前的信息。\n"
               "课件写着“密文是加密后的信息”（课件：课件.pdf，第 3 页）。")
    calls = []

    def ask(_system, user):
        request = json.loads(user)
        calls.append(request)
        if "valid_quote_subset_review" in request:
            assert [row["id"] for row in request["claims"]] == ["C001"]
            assert request["valid_quote_subset_review"]["valid_quotes"] == [
                {"id": "B0001", "quote": "明文是加密前的信息"}]
            assert request["valid_quote_subset_review"]["original_evidence"] == [
                {"id": "B0001", "text": "老师说明明文是加密前的信息。"}]
            verdicts = [{"id": "C001", "status": "supported", "reason": "原话支持",
                         "evidence": [{"id": "B0001", "quote": "明文是加密前的信息"}]}]
        elif "slide_fact_review" in request:
            assert [row["id"] for row in request["slide_fact_review"]["candidates"]] == [
                "C002"]
            verdicts = [
                {"id": "C001", "status": "supported", "reason": "原话支持",
                 "evidence": [{"id": "B0001", "quote": "明文是加密前的信息"}]},
                {"id": "C002", "status": "supported", "reason": "课件支持",
                 "evidence": [{"id": "S1", "quote": "密文是加密后的信息"}]},
            ]
        else:
            verdicts = [
                {"id": "C001", "status": "supported", "reason": "有一条多余引文",
                 "evidence": [{"id": "B0001", "quote": "明文是加密前的信息"},
                              {"id": "B0001", "quote": "虚构的完整引文"}]},
                {"id": "C002", "status": "unsupported", "reason": "教师没念出",
                 "evidence": []},
            ]
        return json.dumps({"schema_version": 1, "verdicts": verdicts}, ensure_ascii=False)

    report = audit_chapter(chapter, BLOCKS, pages, ask)
    assert report["status"] == "pass"
    assert len(calls) == 3
    assert "quote_repair" not in calls[1]


@pytest.mark.parametrize("status,quote,expected", [
    ("unsupported", "", "fail"),
    ("supported", "不存在的虚构引文", "unverified"),
])
def test_valid_subset_review_must_independently_pass(status, quote, expected):
    calls = []

    def ask(_system, user):
        request = json.loads(user)
        calls.append(request)
        if "valid_quote_subset_review" in request:
            verdicts = [{"id": "C001", "status": status, "reason": "重新判断",
                         "evidence": ([{"id": "B0001", "quote": quote}] if quote else [])}]
        else:
            verdicts = [{"id": "C001", "status": "supported", "reason": "初判",
                         "evidence": [{"id": "B0001", "quote": "明文是加密前的信息"},
                                      {"id": "B0001", "quote": "虚构的完整引文"}]}]
        return json.dumps({"schema_version": 1, "verdicts": verdicts}, ensure_ascii=False)

    report = audit_chapter("明文是加密前的信息。", BLOCKS, PAGES, ask)
    assert report["status"] == expected
    assert len(calls) >= 2
    assert sum("valid_quote_subset_review" in call for call in calls) == 1


def test_valid_subset_never_bypasses_required_cited_page():
    calls = []

    def ask(_system, user):
        request = json.loads(user)
        calls.append(request)
        verdicts = [{"id": "C001", "status": "supported", "reason": "初判",
                     "evidence": [{"id": "B0001", "quote": "明文是加密前的信息"},
                                  {"id": "S1", "quote": "虚构的完整引文"}]}]
        return json.dumps({"schema_version": 1, "verdicts": verdicts}, ensure_ascii=False)

    report = audit_chapter("明文是加密前的信息（课件：课件.pdf，第 3 页）。",
                           BLOCKS, PAGES, ask)
    assert report["status"] == "unverified"
    assert all("valid_quote_subset_review" not in call for call in calls)


def test_explicit_slide_example_can_be_rechecked_without_teacher_speech():
    pages = [{"title": "ch02.pdf", "locator": "第 7 页",
              "text": ("凯撒密码\n明文字母 abcdefghijklmnopqrstuvwxyz\n"
                       "密文字母 DEFGHIJKLMNOPQRSTUVWXYZABC\n"
                       "omnia gallia est divisa in partes tres\n"
                       "RPQLD JDOOLD HVW GLYLVD LQ SDUWHV WUHV")}]
    chapter = ('"omnia gallia est divisa in partes tres"经变换得到'
               '"RPQLD JDOOLD HVW GLYLVD LQ SDUWHV WUHV"'
               '（课件：ch02.pdf，第 7 页）。')
    calls = []

    def ask(system, user):
        request = json.loads(user)
        calls.append(request)
        if "slide_fact_review" not in request:
            return json.dumps({"schema_version": 1, "verdicts": [
                {"id": "C001", "status": "unsupported", "evidence": [],
                 "reason": "老师没有念出这个例子"}]}, ensure_ascii=False)
        review = request["slide_fact_review"]
        assert review["candidates"][0]["id"] == "C001"
        assert review["candidates"][0]["literal_examples_on_page"] == [
            "omnia gallia est divisa in partes tres",
            "RPQLD JDOOLD HVW GLYLVD LQ SDUWHV WUHV"]
        return json.dumps({"schema_version": 1, "verdicts": [
            {"id": "C001", "status": "supported",
             "evidence": [{"id": "S1", "quote": "omnia gallia est divisa in partes tres"},
                          {"id": "S1", "quote": "RPQLD JDOOLD HVW GLYLVD LQ SDUWHV WUHV"}],
             "reason": "课件同页给出该明文和密文示例"}]}, ensure_ascii=False)

    assert audit_chapter(chapter, [], pages, ask)["status"] == "pass"
    assert len(calls) == 2


def test_slide_recheck_never_uses_page_to_verify_teacher_attribution():
    pages = [{"title": "ch02.pdf", "locator": "第 7 页",
              "text": "omnia gallia est divisa in partes tres"}]
    calls = []

    def unsupported(system, user):
        calls.append(1)
        return json.dumps({"schema_version": 1, "verdicts": [
            {"id": "C001", "status": "unsupported", "evidence": [],
             "reason": "没有老师口述证据"}]}, ensure_ascii=False)

    chapter = '老师说"omnia gallia est divisa in partes tres"（课件：ch02.pdf，第 7 页）。'
    assert audit_chapter(chapter, [], pages, unsupported)["status"] == "fail"
    assert len(calls) == 1


def test_slide_review_malformed_json_gets_one_fresh_retry():
    pages = [{"title": "ch02.pdf", "locator": "第 7 页",
              "text": "明文字母 abcdefghijklmnopqrstuvwxyz\n密文字母 DEFGHIJKLMNOPQRSTUVWXYZABC"}]
    calls = []

    def ask(system, user):
        request = json.loads(user)
        calls.append(request)
        if "slide_fact_review" not in request:
            return json.dumps({"schema_version": 1, "verdicts": [
                {"id": "C001", "status": "unsupported", "evidence": [],
                 "reason": "需核对课件"}]}, ensure_ascii=False)
        answer = json.dumps({"schema_version": 1, "verdicts": [
            {"id": "C001", "status": "supported",
             "evidence": [{"id": "S1", "quote": "明文字母 abcdefghijklmnopqrstuvwxyz"}],
             "reason": "课件原文"}]}, ensure_ascii=False)
        return answer if "format_retry" in request else answer + "}"

    report = audit_chapter("课件给出明文字母\"abcdefghijklmnopqrstuvwxyz\"（课件：ch02.pdf，第 7 页）。",
                           [], pages, ask)
    assert report["status"] == "pass"
    assert len(calls) == 3
    assert calls[2]["format_retry"]["attempt"] == 1


def test_slide_recheck_needs_literal_examples_on_cited_page():
    pages = [{"title": "ch02.pdf", "locator": "第 7 页",
              "text": "课件仅有另一个例子"}]
    calls = []

    def unsupported(system, user):
        calls.append(1)
        return json.dumps({"schema_version": 1, "verdicts": [
            {"id": "C001", "status": "unsupported", "evidence": [],
             "reason": "课件没有这个例子"}]}, ensure_ascii=False)

    chapter = '"omnia gallia est divisa in partes tres"（课件：ch02.pdf，第 7 页）。'
    assert audit_chapter(chapter, [], pages, unsupported)["status"] == "fail"
    assert len(calls) == 1


def test_bullet_separated_slide_lines_need_verbatim_quote_repair():
    pages = [{"title": "ch02.pdf", "locator": "第 7 页",
              "text": ("凯撒密码单表代换\n"
                       "▪ 明文字母 abcdefghijklmnopqrstuvwxyz\n"
                       "▪ 密文字母 DEFGHIJKLMNOPQRSTUVWXYZABC")}]
    requests = []

    def ask(system, user):
        request = json.loads(user)
        requests.append(request)
        quote = ("明文字母 abcdefghijklmnopqrstuvwxyz\n"
                 "密文字母 DEFGHIJKLMNOPQRSTUVWXYZABC")
        if "quote_repair" in request:
            quote = "明文字母 abcdefghijklmnopqrstuvwxyz"
        return json.dumps({"schema_version": 1, "verdicts": [
            {"id": "C001", "status": "supported",
             "evidence": [{"id": "S1", "quote": quote}],
             "reason": "同页给出代换表"}]}, ensure_ascii=False)

    report = audit_chapter("凯撒密码是单表代换示例。", [], pages, ask)
    assert report["status"] == "pass"
    assert len(requests) == 2
    assert requests[1]["quote_repair"]["validation_issues"] == [
        {"id": "C001", "reason": "invented_or_missing_quote"}]


def test_cited_page_must_itself_supply_a_verbatim_quote():
    chapter = "明文是加密前的信息（课件：课件.pdf，第 3 页）。"
    transcript_only = audit_chapter(chapter, BLOCKS, PAGES, _answer)
    assert transcript_only["status"] == "unverified"
    assert {row["reason"] for row in transcript_only["validation_issues"]} == {
        "cited_page_lacks_support_quote"}

    def cite_page(system, user):
        rows = json.loads(_answer(system, user))
        rows["verdicts"][0]["evidence"] = [
            {"id": "S1", "quote": "明文是加密前的信息"}]
        return json.dumps(rows, ensure_ascii=False)

    assert audit_chapter(chapter, BLOCKS, PAGES, cite_page)["status"] == "pass"
    wrong_page = audit_chapter(chapter, BLOCKS,
                               [{**PAGES[0], "locator": "第 4 页"}], cite_page)
    assert wrong_page["status"] == "unverified"
    assert any(row["reason"].startswith("cited_page_not_supplied")
               for row in wrong_page["validation_issues"])


def test_cited_page_must_name_the_claims_technical_term():
    page = {"title": "课件.pdf", "locator": "第 36 页",
            "text": "代替密码用新的字符替换原来的字符。"}
    chapter = "RSA 算法由三位作者提出（课件：课件.pdf，第 36 页）。"

    def irrelevant_quote(_system, user):
        claim = json.loads(user)["claims"][0]
        return json.dumps({"schema_version": 1, "verdicts": [{
            "id": claim["id"], "status": "supported",
            "evidence": [{"id": "S1", "quote": "代替密码用新的字符替换原来的字符"}],
            "reason": "page quote"}]}, ensure_ascii=False)

    report = audit_chapter(chapter, [], [page], irrelevant_quote)
    assert report["status"] == "unverified"
    assert any(issue["reason"] == "cited_page_missing_named_term:RSA"
               for issue in report["validation_issues"])


def test_verbatim_quote_can_relabel_one_adjacent_transcript_block():
    blocks = [BLOCKS[0], {"block_id": "B0002", "text": "老师说明明文是加密前的信息。"}]
    # The duplicated quote is ambiguous and must not be silently relabeled.
    ambiguous = audit_chapter("明文是加密前的信息。", blocks, [],
                              lambda system, user: _answer(system, user).replace(
                                  '"B0001"', '"B9999"'))
    assert ambiguous["status"] == "unverified"

    unique = audit_chapter("明文是加密前的信息。", [BLOCKS[0]], [],
                           lambda system, user: _answer(system, user).replace(
                               '"B0001"', '"B9999"'))
    assert unique["status"] == "pass"
    assert unique["relabeled_evidence"] == [
        {"claim_id": "C001", "from": "B9999", "to": "B0001"}]


def test_unsupported_plain_claim_stays_without_quarantine(tmp_path, monkeypatch):
    settings = SimpleNamespace(platform_token="test", openai_api_key="",
                               notes_model="test", notes_fact_check=False,
                               notes_claim_audit=True)
    transcript = {"segments": [{"start": 0, "end": 12,
                                 "text": "老师说明明文是加密前的信息。"}]}
    target = tmp_path / "notes.md"
    monkeypatch.setattr(media, "_summarize", lambda *a, **k: "明文是加密前的信息。")

    def unsupported(_llm, _model, prompt, system, _settings):
        parsed = json.loads(_answer(system, prompt))
        parsed["verdicts"][0].update(status="unsupported", evidence=[], reason="缺少依据")
        return json.dumps(parsed, ensure_ascii=False)

    monkeypatch.setattr(media, "_recovery_llm", unsupported)
    # The note reads as complete: a plain sentence the audit could not settle
    # stays as written, and nothing is quarantined.
    assert media.write_notes(transcript, [], target, settings, "课程") == target
    note = target.read_text("utf-8")
    assert "明文是加密前的信息" in note
    assert "待核对（未能完全核实）" not in note
    assert "已移出正文" not in note
    assert list((tmp_path / ".notes-parts").glob("*.claim-audit.json"))
    report = json.loads((tmp_path / "notes.md.claim-audit.json").read_text("utf-8"))
    assert report["status"] == "pass"
    assert report["pending_verification"] == 0

    # Identical evidence replays as a stable existing-note pass.
    assert media.write_notes(transcript, [], target, settings, "课程") == target

    # A corrected reviewer keeps the claim in the chapter body instead.
    fresh = tmp_path / "fresh.md"
    monkeypatch.setattr(media, "_recovery_llm",
                        lambda _llm, _model, prompt, system, _settings: _answer(system, prompt))
    corrected = SimpleNamespace(platform_token="test", openai_api_key="",
                                notes_model="corrected-reviewer",
                                notes_fact_check=False, notes_claim_audit=True)
    assert media.write_notes(transcript, [], fresh, corrected, "课程") == fresh
    fresh_note = fresh.read_text("utf-8")
    assert "明文是加密前的信息" in fresh_note
    assert "待核对（未能完全核实）" not in fresh_note
    assert "（回看 " not in fresh_note

    # An unaudited addition still cannot sneak into a finished note.
    target.write_text(note + "\n未经审校的新增断言。", "utf-8")
    with pytest.raises(RuntimeError, match="没有匹配当前证据"):
        media.write_notes(transcript, [], target, settings, "课程")


def test_partial_slide_text_only_enters_focused_fact_repair(tmp_path, monkeypatch):
    settings = SimpleNamespace(platform_token="test", openai_api_key="",
                               notes_model="test", notes_fact_check=False)
    transcript = {"segments": [{"start": 0, "end": 20,
                                 "text": "卡契斯基分析密钥长度四个字母以上，计算重复片段的距离。"}]}
    source = {"title": "古典密码.pdf", "locator": "第 119 页",
              "status": "partial", "text": "一般来说密码分析家只考虑4个字母以上的密钥长度。\n图表另见原件。"}
    initial_sources = []
    repair_sources = []

    def draft(*args, **kwargs):
        initial_sources.extend(kwargs["sources"])
        return ("卡契斯基测试对长字符串（通常 4 字母以上）的重复距离求因数。"
                "重合指数 IC 证实周期猜测。")

    def repair(*args, **kwargs):
        repair_sources.extend(kwargs["sources"])
        return ("卡契斯基分析一般只考虑 4 个字母以上的密钥候选长度。"
                "重合指数 IC 的结果支持周期猜测。")

    monkeypatch.setattr(media, "_summarize", draft)
    monkeypatch.setattr(media, "_repair_factual_conflicts", repair)
    target = tmp_path / "notes.md"
    media.write_notes(transcript, [], target, settings, "课程", source_context=[source])
    assert initial_sources == []
    assert [(row["locator"], row["status"]) for row in repair_sources] == [
        ("第 119 页", "partial")]
    assert "4 个字母以上的密钥候选长度" in target.read_text("utf-8")
    assert "图片和未提取部分未用于笔记生成" in target.read_text("utf-8")


def test_misattributed_kasiski_limit_is_removed_only_with_exact_matched_slide():
    sentence = "卡契斯基分析对长字符串（通常 4 字母以上）的重复距离求因数。"
    slide = {"status": "partial", "text": "一般来说密码分析家只考虑4个字母以上的密钥长度"}
    assert media._remove_misattributed_kasiski_limit(sentence, [slide]) == (
        "卡契斯基分析对长字符串的重复距离求因数。")
    variant = "卡契斯基测试提取密文中重复出现的字母串（通常要求长度在四字母以上以增加可信度）。"
    assert media._remove_misattributed_kasiski_limit(variant, [slide]) == (
        "卡契斯基测试提取密文中重复出现的字母串。")
    correction = "卡契斯基测试基于重复串距离。\n* 卡契斯基的“四字母以上”是重复字符串长度限制，而非密钥长度。"
    assert media._remove_misattributed_kasiski_limit(correction, [slide]) == (
        "卡契斯基测试基于重复串距离。\n")
    short = "卡契斯基通过计算长串（通常考虑四字母以上）重复出现的距离猜测密钥长度。"
    assert media._remove_misattributed_kasiski_limit(short, [slide]) == (
        "卡契斯基通过计算长串重复出现的距离猜测密钥长度。")
    assert media._remove_misattributed_kasiski_limit(sentence, []) == sentence
