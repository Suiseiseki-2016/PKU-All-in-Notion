"""Coverage-first triage: knowledge points stay visible, uncertainty is
flagged for re-listening, and only system-level audit failures block."""

import json
from types import SimpleNamespace

import pytest

from pku_sync import media
from pku_sync.claim_audit import chapter_assertions
from pku_sync.note_quality import note_factual_conflicts, strip_conflicting_paragraphs


def _answer(system, user):
    batch = json.loads(user)["claims"]
    return json.dumps({"schema_version": 1, "verdicts": [
        {"id": row["id"], "status": "supported", "reason": "课堂原话",
         "evidence": [{"id": "B0001", "quote": "明文是加密前的信息"}]}
        for row in batch
    ]}, ensure_ascii=False)


def test_audit_uncertain_claim_keeps_sentence_with_pending_flag(tmp_path, monkeypatch):
    settings = SimpleNamespace(platform_token="test", openai_api_key="",
                               notes_model="test", notes_fact_check=False,
                               notes_claim_audit=True)
    transcript = {"segments": [{"start": 0, "end": 12,
                                 "text": "老师说明明文是加密前的信息。"}]}
    target = tmp_path / "notes.md"
    monkeypatch.setattr(media, "_summarize", lambda *a, **k: "明文是加密前的信息。")

    def uncertain(_llm, _model, prompt, system, _settings):
        parsed = json.loads(_answer(system, prompt))
        parsed["verdicts"][0].update(status="uncertain", evidence=[], reason="转写含混")
        return json.dumps(parsed, ensure_ascii=False)

    monkeypatch.setattr(media, "_recovery_llm", uncertain)
    assert media.write_notes(transcript, [], target, settings, "课程") == target
    note = target.read_text("utf-8")
    # A plain sentence the audit could not settle stays as written: the note
    # does not mark its own ordinary prose, and nothing is quarantined.
    assert "明文是加密前的信息" in note
    assert "待核对（未能完全核实）" not in note
    assert "（回看 " not in note


def test_audit_uncertain_coursework_stays_with_relisten_marker(
        tmp_path, monkeypatch):
    settings = SimpleNamespace(platform_token="test", openai_api_key="",
                               notes_model="test", notes_fact_check=False,
                               notes_claim_audit=True)
    transcript = {"segments": [{"start": 0, "end": 16,
                                 "text": "老师要求下周三之前提交第一次作业。"}]}
    target = tmp_path / "notes.md"
    monkeypatch.setattr(media, "_summarize",
                        lambda *a, **k: "下周三之前提交第一次作业。")

    def uncertain(_llm, _model, prompt, system, _settings):
        parsed = json.loads(_answer(system, prompt))
        parsed["verdicts"][0].update(status="uncertain", evidence=[], reason="转写含混")
        return json.dumps(parsed, ensure_ascii=False)

    monkeypatch.setattr(media, "_recovery_llm", uncertain)
    assert media.write_notes(transcript, [], target, settings, "课程") == target
    note = target.read_text("utf-8")
    # Coursework the audit could not settle stays visible; the marker points
    # at the recording instead of a verification section.
    assert "提交第一次作业" in note
    assert "（回看 " in note
    assert "已移出正文" not in note
    assert "待核对（未能完全核实）" not in note


def test_audit_batch_system_failure_still_blocks_chapter(tmp_path, monkeypatch):
    settings = SimpleNamespace(platform_token="test", openai_api_key="",
                               notes_model="test", notes_fact_check=False,
                               notes_claim_audit=True)
    transcript = {"segments": [{"start": 0, "end": 12,
                                 "text": "老师说明明文是加密前的信息。"}]}
    target = tmp_path / "notes.md"
    monkeypatch.setattr(media, "_summarize", lambda *a, **k: "明文是加密前的信息。")
    monkeypatch.setattr(media, "_recovery_llm",
                        lambda *_args, **_kwargs: "这不是 JSON")
    with pytest.raises(RuntimeError, match="逐条事实审计未完成"):
        media.write_notes(transcript, [], target, settings, "课程")
    assert not target.exists()


def test_invented_quote_review_flags_claim_without_failing_chapter(
        tmp_path, monkeypatch):
    settings = SimpleNamespace(platform_token="test", openai_api_key="",
                               notes_model="test", notes_fact_check=False,
                               notes_claim_audit=True)
    transcript = {"segments": [{"start": 0, "end": 12,
                                 "text": "老师说明明文是加密前的信息。"}]}
    target = tmp_path / "notes.md"
    monkeypatch.setattr(media, "_summarize", lambda *a, **k: "明文是加密前的信息。")

    def invented(_llm, _model, prompt, system, _settings):
        payload = json.loads(prompt)
        if "snippets" in payload or "support_bundles" in payload:
            return json.dumps({"schema_version": 1, "verdicts": [
                {"id": "C001", "status": "supported", "reason": "杜撰",
                 "evidence_ids": ["G000000000000000"]}]}, ensure_ascii=False)
        return json.dumps({"schema_version": 1, "verdicts": [
            {"id": "C001", "status": "supported", "reason": "杜撰",
             "evidence": [{"id": "B0001", "quote": "杜撰的引文"}]}
        ]}, ensure_ascii=False)

    monkeypatch.setattr(media, "_recovery_llm", invented)
    assert media.write_notes(transcript, [], target, settings, "课程") == target
    note = target.read_text("utf-8")
    # The auditor's failure to quote must not cost the chapter its content.
    assert "明文是加密前的信息" in note
    assert "待核对（未能完全核实）" not in note


def test_rac_ambiguity_keeps_chapter_with_relisten_notice(tmp_path, monkeypatch):
    settings = SimpleNamespace(platform_token="test", openai_api_key="",
                               notes_model="test", notes_fact_check=False,
                               notes_claim_audit=False)
    transcript = {"segments": [{"start": 0, "end": 20,
                                 "text": "这里介绍RAC算法和公钥。"}]}
    target = tmp_path / "notes.md"
    monkeypatch.setattr(media, "_summarize", lambda *a, **k: "这里介绍RAC算法。")
    assert media.write_notes(transcript, [], target, settings, "课程") == target
    note = target.read_text("utf-8")
    # The spoken term stays as transcribed; the acronym becomes one compact
    # re-listen note at the end of the chapter instead of blocking it.
    assert "RAC算法" in note
    assert "转写把公钥算法术语记为 RAC" in note
    assert "请回听这段原音" in note
    assert "待核对（未能完全核实）" not in note


def test_annotate_audited_sentences_keeps_headings_bold_and_citations():
    chapter = ("### 凯撒密码\n"
               "- **凯撒密码**是移位密码（课件：ch02.pdf，第 39 页）。\n"
               "普通例子保留。\n"
               "明文后移 3 位得到密文。（课件：ch02.pdf，第 8 页）\n")
    claims = {row["text"] for row in chapter_assertions(chapter)}
    assert "明文后移 3 位得到密文（课件：ch02.pdf，第 8 页）。" in claims
    marked = media.annotate_audited_sentences(chapter, {
        "明文后移 3 位得到密文（课件：ch02.pdf，第 8 页）。": "（回看 12:00）"})
    # The sentence stays; only the marker is added, after its citation.
    assert ("明文后移 3 位得到密文。（课件：ch02.pdf，第 8 页）（回看 12:00）"
            in marked)
    assert "### 凯撒密码" in marked
    assert "- **凯撒密码**是移位密码（课件：ch02.pdf，第 39 页）。" in marked
    assert "普通例子保留。" in marked


def test_annotate_audited_sentences_handles_citation_after_sentence():
    chapter = "句子A。（课件：x.pdf，第 1 页）句子B。\n"
    claims = {row["text"] for row in chapter_assertions(chapter)}
    assert claims == {"句子A（课件：x.pdf，第 1 页）。", "句子B。"}
    marked = media.annotate_audited_sentences(
        chapter, {"句子B。": "（回看 03:00）"})
    assert marked == "句子A。（课件：x.pdf，第 1 页）句子B。（回看 03:00）"
    bulleted = "- 句子A。（课件：x.pdf，第 1 页）句子B。\n"
    assert media.annotate_audited_sentences(bulleted, {"句子A（课件：x.pdf，第 1 页）。": "（回看 04:00）"}) == (
        "- 句子A。（课件：x.pdf，第 1 页）（回看 04:00）句子B。")


def test_collect_unverified_drops_skips_meta_and_rewrites():
    before = ("普通句子一保留。\n"
              "根据上下文推测，这个术语可能指代某个现象。\n"
              "阿普加评分是 2 分，临床上很重要。\n"
              "旧表述是卡契斯基分析。\n")
    after = "普通句子一保留。\n新表述是卡契斯基分析。\n"
    assert media.collect_unverified_drops(before, after) == [
        "阿普加评分是 2 分，临床上很重要。"]


def test_without_review_markers_drops_only_marked_sentences():
    text = ("# 课程\n\n"
            "已核验的句子保留。（课件：x.pdf，第 1 页）\n"
            "需要回听的句子。（回看 12:00）\n"
            "同一行还有已核验的句子。需要回听的另一句。（回看 13:00）\n"
            "## 作业与考试口头线索（自动转写原文，待核对）\n- [00:00] 原话\n")
    stripped = media.without_review_markers(text)
    assert "需要回听的句子" not in stripped
    assert "需要回听的另一句" not in stripped
    assert "已核验的句子保留。" in stripped
    assert "同一行还有已核验的句子。" in stripped
    assert "## 作业与考试口头线索（自动转写原文，待核对）" in stripped
    assert "- [00:00] 原话" in stripped


def test_strip_conflicting_paragraphs_removes_only_the_conflict():
    sources = [{"title": "ch02-古典密码.pdf", "locator": "第 5 页",
                "status": "readable",
                "text": "密码学分为密码编码学和密码分析学两个分支。"}]
    note = ("古典密码是本课起点。\n\n"
            "对称加密体制包含编码和分析两个研究方向。\n\n"
            "明文是加密前的信息。")
    cleaned, removed = strip_conflicting_paragraphs(note, sources)
    assert "对称加密体制" not in cleaned
    assert "古典密码是本课起点。" in cleaned
    assert "明文是加密前的信息。" in cleaned
    assert removed == [("对称加密体制包含编码和分析两个研究方向。",
                        "把密码学的编码与分析两分支误归到对称密码")]
    assert note_factual_conflicts(cleaned, sources) == []


def test_unrepairable_hard_conflict_moves_out_of_body(tmp_path, monkeypatch):
    settings = SimpleNamespace(platform_token="test", openai_api_key="",
                               notes_model="test", notes_fact_check=False,
                               notes_claim_audit=False)
    source = {"title": "ch02-古典密码.pdf", "locator": "第 5 页",
              "status": "readable",
              "text": "密码学分为密码编码学和密码分析学两个分支。"}
    transcript = {"segments": [{"start": 0, "end": 40,
                                "text": "今天开始讲古典密码的基本概念。"}]}
    target = tmp_path / "notes.md"
    monkeypatch.setattr(media, "_summarize",
                        lambda *a, **k: ("古典密码是本课的起点。\n\n"
                                         "对称加密体制包含编码和分析两个研究方向。\n\n"
                                         "明文是加密前的信息。"))
    # The repair model fails to fix the classification error, which used to
    # cost the whole chapter.
    monkeypatch.setattr(media, "_repair_factual_conflicts",
                        lambda *a, **k: a[3])
    assert media.write_notes(transcript, [], target, settings, "课程",
                             source_context=[source]) == target
    note = target.read_text("utf-8")
    # The conflicting paragraph leaves the body; one compact line tells the
    # reader where the recording covers it.
    assert "正文已按课件更正，原话可回看" in note
    assert "把密码学的编码与分析两分支误归到对称密码" in note
    assert "对称加密体制" not in note
    assert "明文是加密前的信息" in note
    assert "待核对（未能完全核实）" not in note


def test_rewrite_guard_falls_back_to_verified_original(tmp_path, monkeypatch):
    claim = {
        "origin": "transcript",
        "display_text": "凯撒密码沿字母表移动字母。",
        "transcript_excerpt": "凯撒密码沿字母表移动字母。",
        "evidence_block_id": "B0001",
    }
    record: dict = {}
    cache = tmp_path / "ledger.json"
    fallbacks: list[dict] = []

    def fake_llm(_llm, _model, prompt, system, _settings):
        payload = json.loads(prompt)
        if "claims" in payload:
            # The rewrite strengthens an ordinary statement into a
            # requirement, which the local guard must reject.
            return json.dumps({"schema_version": 1, "sentences": [
                {"id": payload["claims"][0]["id"],
                 "sentence": "凯撒密码必须沿字母表移动字母。"}
            ]}, ensure_ascii=False)
        checks = payload["checks"]
        return json.dumps({"schema_version": 1, "verdicts": [
            {"id": check["id"], "entailed": True,
             "qualifiers_preserved": True, "unsupported_terms": []}
            for check in checks
        ]}, ensure_ascii=False)

    monkeypatch.setattr(media, "_recovery_llm", fake_llm)
    result = media._verified_ledger_sentences(
        None, "test", [claim], settings=SimpleNamespace(),
        record=record, cache_path=cache, fallbacks=fallbacks,
    )
    # Coverage first: the verified original stays instead of failing.
    assert result[0]["display_text"] == "凯撒密码沿字母表移动字母。"
    assert fallbacks == [{"guard": "rewrite_changed_scope_or_requirement",
                          "text": "凯撒密码沿字母表移动字母。"}]
    with pytest.raises(media._NoteQualityError, match="自然笔记改写未通过本地核验"):
        media._verified_ledger_sentences(
            None, "test", [claim], settings=SimpleNamespace(),
            record={}, cache_path=tmp_path / "ledger2.json",
        )


_SPOKEN_FACTS = [
    "课堂先从凯撒密码的历史背景讲起。",
    "移位密码按字母表顺序移动每个字母。",
    "课件用字母表示明文与密文的对应。",
    "老师用一张表列出全部字母的替换关系。",
    "课堂随后比较单表代换与多表代换的特点。",
]


def _ledger_claim(sentence, group="G01"):
    return {"origin": "transcript", "display_text": sentence,
            "evidence_group_id": group, "transcript_excerpt": sentence,
            "source_filename": None, "source_page": None, "source_quote": None}


def _seed_ledger(cache, claims, groups):
    cache.write_text(json.dumps({
        "schema_version": media._RECOVERY_LEDGER_SCHEMA,
        "status": "validated", "validated_claims": claims,
        "transcript_groups": groups,
    }, ensure_ascii=False), "utf-8")


def _render_from_prompt(prompt):
    rows = json.loads(prompt)
    return "【课堂转写】\n" + "\n".join(
        row["display_text"] for row in rows.get("validated_claims", []))


def test_uncovered_topic_group_defers_to_visible_pointer(tmp_path, monkeypatch):
    claims = [_ledger_claim(sentence) for sentence in _SPOKEN_FACTS]
    cache = tmp_path / "group-gap.json"
    _seed_ledger(cache, claims, {"G01": {}, "G02": {}})
    monkeypatch.setattr(media, "_verified_ledger_sentences",
                        lambda _llm, _model, claims, **_kwargs: claims)
    monkeypatch.setattr(media, "_recovery_llm",
                        lambda _llm, _model, prompt, _system, _settings:
                        _render_from_prompt(prompt))
    gaps = []
    content = media._recover_chapter_with_ledger(
        None, "test", "课程", " ".join(_SPOKEN_FACTS), "旧草稿", "安全摘要",
        settings=SimpleNamespace(platform_token="test"), sources=[],
        cache_path=cache, natural_rewrite=True, recovery_gaps=gaps,
    )
    # Coverage first: the uncovered topic becomes a re-listen pointer and the
    # verified content of the chapter still renders.
    assert gaps == [{"start": None, "text": "",
                     "label": "主题 G02 未能整理出可核验的笔记句子"}]
    assert "【课堂转写】" in content
    record = json.loads(cache.read_text("utf-8"))
    assert record["group_coverage_gaps"] == ["G02"]
    assert record["status"] == "rendered"


def test_uncovered_topic_group_still_blocks_without_sink(tmp_path, monkeypatch):
    claims = [_ledger_claim(sentence) for sentence in _SPOKEN_FACTS]
    cache = tmp_path / "group-gap-blocking.json"
    _seed_ledger(cache, claims, {"G01": {}, "G02": {}})
    monkeypatch.setattr(media, "_verified_ledger_sentences",
                        lambda _llm, _model, claims, **_kwargs: claims)
    monkeypatch.setattr(media, "_recovery_llm",
                        lambda _llm, _model, prompt, _system, _settings:
                        _render_from_prompt(prompt))
    with pytest.raises(media._NoteQualityError,
                       match="部分课堂主题缺少完整且可核验的笔记句子"):
        media._recover_chapter_with_ledger(
            None, "test", "课程", " ".join(_SPOKEN_FACTS), "旧草稿", "安全摘要",
            settings=SimpleNamespace(platform_token="test"), sources=[],
            cache_path=cache, natural_rewrite=True,
        )
    assert json.loads(cache.read_text("utf-8"))["reason"] == (
        "transcript_group_coverage_gap")


def test_spoken_term_review_defers_to_visible_pointer(tmp_path, monkeypatch):
    claims = [_ledger_claim(sentence) for sentence in _SPOKEN_FACTS]
    claims.append({
        "origin": "transcript",
        "display_text": "这三位作者设计了 RAC 算法。",
        "evidence_group_id": "G01",
        "transcript_excerpt": "这三位作者设计了 rac算法。",
    })
    cache = tmp_path / "term-review.json"
    _seed_ledger(cache, claims, {"G01": {}})
    monkeypatch.setattr(media, "_verified_ledger_sentences",
                        lambda _llm, _model, claims, **_kwargs: claims)
    monkeypatch.setattr(media, "_recovery_llm",
                        lambda _llm, _model, prompt, _system, _settings:
                        _render_from_prompt(prompt))
    gaps = []
    content = media._recover_chapter_with_ledger(
        None, "test", "课程", " ".join(_SPOKEN_FACTS), "旧草稿", "安全摘要",
        settings=SimpleNamespace(platform_token="test"), sources=[],
        cache_path=cache, natural_rewrite=True, recovery_gaps=gaps,
    )
    # The disputed acronym leaves the body but stays visible for re-listening;
    # a slide still cannot prove what was spoken.
    assert gaps == [{"start": 0.0, "text": "这三位作者设计了 RAC 算法。",
                     "label": "术语转写待回听原音"}]
    assert "RAC" not in content
    assert json.loads(cache.read_text("utf-8"))["audio_review_deferred"] == 1


def test_recovery_gap_becomes_one_compact_chapter_note(tmp_path, monkeypatch):
    settings = SimpleNamespace(platform_token="test", openai_api_key="",
                               notes_model="test", notes_fact_check=False,
                               notes_claim_audit=False)
    speech = "".join(_SPOKEN_FACTS) * 10
    transcript = {"segments": [{"start": 0.0, "end": 400.0, "text": speech}]}
    target = tmp_path / "notes.md"
    monkeypatch.setattr(media, "_summarize",
                        lambda *a, **k: "凯撒密码是移位密码。")

    def fake_recover(*_args, **kwargs):
        kwargs["recovery_gaps"].append({
            "start": 240.0, "text": "主题 G02 的课堂原话。",
            "label": "主题 G02 未能整理出可核验的笔记句子"})
        kwargs["cache_path"].write_text(
            json.dumps({"schema_version": media._RECOVERY_LEDGER_SCHEMA}),
            "utf-8")
        return "凯撒密码把字母按固定位数平移，课堂用字母表核对加密与解密结果。" * 10

    monkeypatch.setattr(media, "_recover_chapter_with_ledger", fake_recover)
    assert media.write_notes(transcript, [], target, settings, "课程") == target
    note = target.read_text("utf-8")
    # One line at the end of the chapter, not a verification section.
    assert "待核对（未能完全核实）" not in note
    assert "> 04:00 起的一段讲述未完整还原，建议回听该段" in note
    assert "凯撒密码把字母按固定位数平移" in note


def test_recovery_group_pointers_without_blocks_name_the_topic():
    assert media._recovery_group_pointers(["G03"], None) == [
        {"start": None, "text": "",
         "label": "主题 G03 未能整理出可核验的笔记句子"}]


_SLIDE_CLAIM = {
    "origin": "slide",
    "display_text": "1977年三位作者提出RSA公钥算法。",
    "source_filename": "本讲.pdf", "source_page": "第 30 页",
    "source_quote": "1977年Rivest，Shamir & Adleman提出了RSA公钥算法",
}


def _bad_layout() -> str:
    """A model layout that keeps every fact but rewrites one citation."""
    return "\n".join([
        "【课堂转写】",
        *_SPOKEN_FACTS,
        "【课件补充】1977年三位作者提出RSA公钥算法。（课件：本讲.pdf，第 31 页）",
    ])


def test_local_render_of_verified_claims_passes_the_render_guard():
    claims = [_ledger_claim(sentence) for sentence in _SPOKEN_FACTS]
    claims.append(dict(_SLIDE_CLAIM))
    content = media._render_verified_claims_locally(claims)
    assert content.startswith("【课堂转写】\n")
    assert "【课件补充】1977年三位作者提出RSA公钥算法。" \
           "（课件：本讲.pdf，第 30 页）" in content
    assert media._ledger_render_guard(content, claims,
                                     require_transcript_label=True) is None


_RISKY_SLIDE_CLAIM = {
    "origin": "slide",
    "display_text": "必须立即给药。",
    "source_filename": "本讲.pdf", "source_page": "第 12 页",
    "source_quote": "the drug is administered immediately",
}


def test_local_ledger_layout_puts_spoken_facts_in_one_paragraph():
    claims = [_ledger_claim(sentence) for sentence in _SPOKEN_FACTS]
    content = media._render_verified_claims_locally(claims)
    lines = content.splitlines()
    # A column of one-line fragments is what made this fallback unreadable.
    assert lines[0] == "【课堂转写】"
    assert len(lines) == 2
    assert lines[1].startswith(_SPOKEN_FACTS[0])
    assert media._ledger_render_guard(content, claims,
                                      require_transcript_label=True,
                                      grouped_claims=True) is None


def test_local_ledger_layout_gives_a_slide_page_one_pointer():
    claims = [dict(_SLIDE_CLAIM),
              {**_SLIDE_CLAIM, "display_text": "RSA的安全性依赖大数分解。"}]
    content = media._render_verified_claims_locally(claims)
    assert len(content.splitlines()) == 1
    assert content.count("（课件：本讲.pdf，第 30 页）") == 1
    assert media._ledger_render_guard(content, claims,
                                      grouped_claims=True) is None
    # A model's layout is still held to one fact and one pointer per line.
    assert media._ledger_render_guard(content, claims) is not None


def test_local_ledger_layout_keeps_a_high_risk_fact_on_its_own_line():
    claims = [*[_ledger_claim(sentence) for sentence in _SPOKEN_FACTS],
              dict(_RISKY_SLIDE_CLAIM), dict(_SLIDE_CLAIM)]
    content = media._render_verified_claims_locally(claims)
    risky = [line for line in content.splitlines()
             if _RISKY_SLIDE_CLAIM["display_text"] in line]
    assert len(risky) == 1
    assert "原文：" in risky[0]
    assert media._ledger_render_guard(content, claims,
                                      require_transcript_label=True,
                                      grouped_claims=True) is None


def test_rejected_layout_falls_back_to_local_render(tmp_path, monkeypatch):
    claims = [_ledger_claim(sentence) for sentence in _SPOKEN_FACTS]
    claims.append(dict(_SLIDE_CLAIM))
    cache = tmp_path / "render-fallback.json"
    _seed_ledger(cache, claims, {"G01": {}})
    monkeypatch.setattr(media, "_verified_ledger_sentences",
                        lambda _llm, _model, claims, **_kwargs: claims)
    monkeypatch.setattr(media, "_recovery_llm",
                        lambda *_args, **_kwargs: _bad_layout())
    gaps = []
    content = media._recover_chapter_with_ledger(
        None, "test", "课程", " ".join(_SPOKEN_FACTS), "旧草稿", "安全摘要",
        settings=SimpleNamespace(platform_token="test"), sources=[],
        cache_path=cache, natural_rewrite=True, recovery_gaps=gaps,
    )
    # Coverage first: verified facts survive a rejected layout, and the
    # original citation is restored from the claim itself.
    assert "（课件：本讲.pdf，第 30 页）" in content
    assert "第 31 页" not in content
    assert gaps == [{"note": "- 本章排版步骤未通过本地核验"
                             "（renderer_changed_source_citation），"
                             "正文已改用已核验事实的直接排版；事实内容未变。"}]
    record = json.loads(cache.read_text("utf-8"))
    assert record["renderer_fallback"] == "renderer_changed_source_citation"
    assert record["status"] == "rendered"


def test_rejected_layout_still_blocks_without_sink(tmp_path, monkeypatch):
    claims = [_ledger_claim(sentence) for sentence in _SPOKEN_FACTS]
    cache = tmp_path / "render-blocking.json"
    _seed_ledger(cache, claims, {"G01": {}})
    monkeypatch.setattr(media, "_verified_ledger_sentences",
                        lambda _llm, _model, claims, **_kwargs: claims)
    monkeypatch.setattr(media, "_recovery_llm",
                        lambda *_args, **_kwargs: _bad_layout())
    with pytest.raises(media._NoteQualityError,
                       match="证据账本渲染未通过本地核验"):
        media._recover_chapter_with_ledger(
            None, "test", "课程", " ".join(_SPOKEN_FACTS), "旧草稿", "安全摘要",
            settings=SimpleNamespace(platform_token="test"), sources=[],
            cache_path=cache, natural_rewrite=True,
        )


def test_structural_assertion_rule_keeps_real_short_facts():
    assert media._is_structural_assertion("【课堂转写】")
    assert media._is_structural_assertion("【课件补充】")
    assert media._is_structural_assertion("古典密码体制与密钥分类原则")
    assert not media._is_structural_assertion("凯撒密码把字母后移三位。")
    assert not media._is_structural_assertion("共 26 个字母")


def test_structural_lines_stay_in_body_and_unmarked():
    chapter = ("【课堂转写】\n"
               "古典密码体制与密钥分类原则\n"
               "凯撒密码把字母后移三位。\n")
    audit = {
        "status": "fail", "reason": "",
        "assertions": [
            {"id": "C001", "context": "", "text": "【课堂转写】"},
            {"id": "C002", "context": "", "text": "古典密码体制与密钥分类原则"},
            {"id": "C003", "context": "", "text": "凯撒密码把字母后移三位。"},
        ],
        "verdicts": [
            {"id": "C001", "status": "unsupported", "reason": "非事实", "evidence": []},
            {"id": "C002", "status": "unsupported", "reason": "非事实", "evidence": []},
            {"id": "C003", "status": "unsupported", "reason": "缺少依据", "evidence": []},
        ],
        "validation_issues": [],
    }
    window = {"start": 0.0, "end": 60.0, "evidence_blocks": [
        {"block_id": "B0001", "start": 12.0, "end": 20.0,
         "text": "凯撒密码把字母后移三位。"}]}
    markers: dict[str, str] = {}
    reviewed = media.triage_claim_audit(audit, chapter, window, markers)
    # The ledger renderer needs its origin label, and a bare section title is
    # structure: neither is a classifiable fact, so neither is marked.
    assert "【课堂转写】" in reviewed
    assert "古典密码体制与密钥分类原则" in reviewed
    # The knowledge sentence stays and points at the recording.
    assert "凯撒密码把字母后移三位。（回看 00:12）" in reviewed
    assert list(markers) == ["凯撒密码把字母后移三位。"]


def test_spoken_numbers_survive_without_a_slide_or_inline_timestamp():
    transcript = ("平时作业占 40%，期末笔试占 60%。"
                  "香农在 1949 年发表了保密系统的通信理论。")
    grading = "成绩由平时作业（40%）和期末笔试（60%）组成。"
    shannon = "1949 年，香农发表《保密系统的通信理论》，奠定了数学理论基础。"
    invented = "该标准在 1992 年被三十七个国家采用。"
    checked = media._filter_unsupported_claims(
        grading + shannon + invented, [], transcript)
    # Coursework and history the teacher said out loud are course content even
    # when no slide repeats them; the claim audit adds a marker if needed.
    assert grading in checked
    assert shannon in checked
    # A number in neither the slides nor the transcript is still unfounded.
    assert "1992" not in checked


def test_sweeping_classroom_claims_keep_their_place_with_a_pointer():
    # Teachers overstate things in class. The note records the class, so the
    # claim stays where it was said and points at the recording.
    boast = "老师强调，只要部署了国密算法，系统就绝对安全，几乎没有被攻破的风险。"
    aside = "他还说熬夜会直接导致免疫力下降，必然造成感染风险上升。"
    transcript = "老师讲了国密算法的部署，又聊到作息。"
    assert media._filter_unsupported_claims(boast + aside, [], transcript) == ""
    kept = media._filter_unsupported_claims(
        boast + aside, [], transcript, lambda text: text.rstrip() + "（回看 08:10）")
    assert kept == boast + "（回看 08:10）" + aside + "（回看 08:10）"


def test_unsettled_numbers_are_marked_in_place_instead_of_deleted():
    draft = ("1949 年，香农发表《保密系统的通信理论》。"
             "该标准服役二十余年后于 2001 年被 AES 取代。\n"
             "老师建议课后阅读这两篇文献。\n")
    # The teacher said "一九四九年"; only the slides spell digits, so neither
    # check can settle the value on its own.
    dropped = media._filter_unsupported_claims(draft, [], "香农那篇论文很重要。")
    assert "1949" not in dropped
    kept = media._filter_unsupported_claims(
        draft, [], "香农那篇论文很重要。", lambda text: text.rstrip() + "（回看 12:30）")
    assert "1949 年，香农发表《保密系统的通信理论》。（回看 12:30）" in kept
    assert "2001 年被 AES 取代。（回看 12:30）" in kept
    # Prose without a contested value is left exactly as written.
    assert "老师建议课后阅读这两篇文献。" in kept
    assert kept.count("（回看 12:30）") == 2


def test_repeated_relisten_markers_collapse_into_one():
    summary = ("1949 年之前密码学还是一门艺术。（回看 31:30）（回看 31:30）"
               "1976 年之后公钥密码出现。（回看 23:51） （回看 24:02）\n")
    normalized = media._normalize_window_summary(summary)
    assert normalized.count("（回看") == 2
    assert "艺术。（回看 31:30）1976" in normalized
    assert normalized.rstrip().endswith("（回看 23:51）")


def test_triage_does_not_add_a_second_marker_to_a_marked_sentence():
    marked = "成绩由平时作业（40%）和期末笔试（60%）组成。（回看 12:30）"
    chapter = f"考核方式\n\n{marked}\n"
    audit = {
        "status": "fail", "reason": "",
        "assertions": [{"id": "C001", "context": "", "text": marked}],
        "verdicts": [{"id": "C001", "status": "unsupported", "reason": "缺少依据",
                      "evidence": []}],
        "validation_issues": [],
    }
    window = {"start": 600.0, "end": 720.0, "evidence_blocks": []}
    markers: dict[str, str] = {}
    reviewed = media.triage_claim_audit(audit, chapter, window, markers)
    assert markers == {}
    assert reviewed.count("（回看") == 1
    assert marked in reviewed


def test_cited_slide_page_is_pointer_enough_without_a_marker():
    cited = "凯撒密码把字母后移三位。（课件：ch02-古典密码.pdf，第 17 页）"
    formula = "$$ IC = \\frac{\\sum N_i(N_i-1)}{n(n-1)} $$"
    chapter = f"{cited}\n{formula}\n卡奇斯基在三百年后提出重码分析方法。\n"
    audit = {
        "status": "fail", "reason": "",
        "assertions": [
            {"id": "C001", "context": "", "text": cited},
            {"id": "C002", "context": "", "text": formula},
            {"id": "C003", "context": "", "text": "卡奇斯基在三百年后提出重码分析方法。"},
        ],
        "verdicts": [
            {"id": "C001", "status": "unsupported", "reason": "转写未逐字对应",
             "evidence": []},
            {"id": "C002", "status": "unsupported", "reason": "转写未逐字对应",
             "evidence": []},
            {"id": "C003", "status": "unsupported", "reason": "缺少依据",
             "evidence": []},
        ],
        "validation_issues": [],
    }
    window = {"start": 0.0, "end": 120.0, "evidence_blocks": [
        {"block_id": "B0001", "start": 30.0, "end": 40.0,
         "text": "卡奇斯基在三百年后提出重码分析方法。"}]}
    markers: dict[str, str] = {}
    reviewed = media.triage_claim_audit(audit, chapter, window, markers)
    # A slide page the reader can open beats a timestamp, and a bare formula
    # is not a claim of its own.
    assert list(markers) == ["卡奇斯基在三百年后提出重码分析方法。"]
    assert formula + "\n" in reviewed
    assert "卡奇斯基在三百年后提出重码分析方法。（回看 00:30）" in reviewed


def test_marker_flood_collapses_into_one_chapter_note():
    sentences = [f"第{n}条结论给出了 {n} 个不同的参数取值。" for n in range(1, 9)]
    chapter = "维吉尼亚密码的破译\n\n" + "".join(sentences) + "\n"
    audit = {
        "status": "fail", "reason": "",
        "assertions": [{"id": f"C{n:03d}", "context": "", "text": text}
                       for n, text in enumerate(sentences, start=1)],
        "verdicts": [{"id": f"C{n:03d}", "status": "unsupported",
                      "reason": "缺少依据", "evidence": []}
                     for n in range(1, 9)],
        "validation_issues": [],
    }
    window = {"start": 300.0, "end": 420.0, "evidence_blocks": []}
    markers: dict[str, str] = {}
    caveats: list[str] = []
    reviewed = media.triage_claim_audit(audit, chapter, window, markers, caveats)
    # Eight markers in one chapter is noise; one line carries the same news.
    assert markers == {}
    assert "（回看" not in reviewed
    assert caveats == ["本节有 8 处细节未能逐句核实，建议回听 05:00 起的讲解"]
    for text in sentences:
        assert text in reviewed


def test_origin_label_is_not_promoted_to_chapter_heading():
    summary = "【课堂转写】\n凯撒密码按字母表移动字母。\n"
    normalized = media._normalize_window_summary(summary)
    assert "### 【课堂转写】" not in normalized
    assert normalized.startswith("### 凯撒密码按字母表移动字母")
    # The ledger label itself must stay in the body for provenance.
    assert "【课堂转写】" in normalized
    # A label is never a usable chapter title, not even as a fallback.
    assert media._usable_chapter_title("【课堂转写】") == ""
    assert media._leading_chapter_title(summary) == ""


def test_chapter_title_line_survives_claim_triage():
    title = "古典密码与现代密码的演变及攻击模型"
    chapter = f"{title}\n\n凯撒密码把字母后移三位。\n"
    audit = {
        "status": "fail", "reason": "",
        "assertions": [
            {"id": "C001", "context": "", "text": title},
            {"id": "C002", "context": "", "text": "凯撒密码把字母后移三位。"},
        ],
        "verdicts": [
            {"id": "C001", "status": "unsupported", "reason": "标题不是事实断言",
             "evidence": []},
            {"id": "C002", "status": "unsupported", "reason": "缺少依据",
             "evidence": []},
        ],
        "validation_issues": [],
    }
    window = {"start": 0.0, "end": 60.0, "evidence_blocks": [
        {"block_id": "B0001", "start": 12.0, "end": 20.0,
         "text": "凯撒密码把字母后移三位。"}]}
    markers = {}
    reviewed = media.triage_claim_audit(audit, chapter, window, markers)
    # The chapter keeps its heading and its body; the unsettled claim points
    # at the recording instead of leaving the note.
    assert reviewed.startswith(title)
    assert "凯撒密码把字母后移三位。（回看 00:12）" in reviewed
    assert list(markers) == ["凯撒密码把字母后移三位。"]
    assert media._leading_chapter_title(chapter) == title


def test_long_chapter_title_keeps_its_heading():
    title = "对抗统计分析的多表代替密码与弗吉尼亚密码"
    chapter = f"{title}\n\n维吉尼亚密码循环使用多个代替表。\n"
    # 20 characters: past the derived-label limit, still a real title.
    assert len(title) > 18
    normalized = media._normalize_window_summary(
        chapter, media._leading_chapter_title(chapter))
    assert normalized.startswith(f"### {title}")
    assert normalized.count(title) == 1


def test_recovered_body_regains_its_chapter_heading():
    title = "古典密码安全性定义与分类"
    recovered = ("【课堂转写】\n"
                 "無論破譯者擁有多少密文，他也無法解釋對應的明文。\n")
    normalized = media._normalize_window_summary(recovered, title)
    assert normalized.startswith(f"### {title}")
    assert "【课堂转写】" in normalized
    assert "### 【课堂转写】" not in normalized


def test_h1_chapter_title_is_used_instead_of_dropped():
    title = "维吉尼亚密码的破译与重合指数法"
    chapter = f"# {title}\n\n维吉尼亚密码利用周期多表代替机制。\n"
    assert media._leading_chapter_title(chapter) == title
    normalized = media._normalize_window_summary(
        chapter, media._leading_chapter_title(chapter))
    assert normalized.startswith(f"### {title}")
    assert normalized.count(title) == 1


def test_body_title_line_beats_the_draft_fallback_title():
    # A review pass may retitle a chapter; the printed heading must match the
    # body it heads instead of leaving the body's own title as stray prose.
    body = ("扩展欧几里得算法与逆元计算\n"
            "\n"
            "逆元通过扩展欧几里得算法求得。\n")
    normalized = media._normalize_window_summary(
        body, "扩展欧几里得算法的逆元计算与课程考核说明")
    assert normalized.startswith("### 扩展欧几里得算法与逆元计算")
    assert "课程考核说明" not in normalized
    assert normalized.count("扩展欧几里得算法与逆元计算") == 1


def test_emptied_provenance_tag_and_slide_bullets_are_tidied():
    summary = ("维吉尼亚密码演示\n"
               "\n"
               "推测密钥长度 $d=6$（转写：97:00-99:00）。\n"
               "*   **单表加密**：频率分布与原文一致（课件：ch02.pdf，第 124 页）。\n"
               "* 均匀分布：IC 为 1/26。\n")
    normalized = media._normalize_window_summary(summary)
    # The generated range is removed by design; an empty tag must not remain.
    assert "（转写：" not in normalized and "转写：" not in normalized
    assert "推测密钥长度 $d=6$。" in normalized
    # One bullet marker, matching the rest of the note.
    assert normalized.count("- **单表加密**") == 1
    assert normalized.count("- 均匀分布") == 1
    assert "*   " not in normalized
    # A single (non-range) timestamp is still kept for oral requirements.
    kept = media._normalize_window_summary("老师说明作业下周交（转写：41:52）。\n")
    assert "（转写：41:52）" in kept


def test_note_keeps_chapter_heading_when_audit_calls_title_unverified(
        tmp_path, monkeypatch):
    settings = SimpleNamespace(platform_token="test", openai_api_key="",
                               notes_model="test", notes_fact_check=False,
                               notes_claim_audit=True)
    transcript = {"segments": [{"start": 0, "end": 12,
                                 "text": "老师说明明文是加密前的信息。"}]}
    target = tmp_path / "notes.md"
    title = "古典密码与现代密码的演变及攻击模型"
    monkeypatch.setattr(media, "_summarize",
                        lambda *a, **k: f"{title}\n\n明文是加密前的信息。")

    def title_unsupported(_llm, _model, prompt, system, _settings):
        parsed = json.loads(_answer(system, prompt))
        parsed["verdicts"][0].update(status="unsupported", evidence=[],
                                     reason="标题不是可在课堂印证的断言")
        return json.dumps(parsed, ensure_ascii=False)

    monkeypatch.setattr(media, "_recovery_llm", title_unsupported)
    assert media.write_notes(transcript, [], target, settings, "课程") == target
    note = target.read_text("utf-8")
    assert f"### {title}" in note
    assert "明文是加密前的信息。" in note
    # The title is structure, so it is never marked for re-listening.
    assert [line for line in note.splitlines()
            if line.startswith("> ") and title in line] == []


def test_only_verifiable_sentences_earn_a_relisten_marker():
    # Numbers, definitions, formulas, coursework and clinical statements are
    # what a student has to check against the recording.
    for sentence in ("凯撒密码只有 25 种可能密钥。",
                     "这种加密方式称为代替密码。",
                     "课件定义穷举法为对截获密文依次尝试所有可能密钥的方法。",
                     "重合指数的定义是 $IC = \\frac{\\sum N_i(N_i-1)}{n(n-1)}$。",
                     "老师要求下周三之前提交第一次作业。",
                     "本次考试范围是前三章。"):
        assert media._worth_relisten_marker(sentence), sentence
    # Narration and attribution are not facts the note has to prove; marking
    # them is what turned the markers into noise.
    for sentence in ("此类算法历史久远，如凯撒密码。",
                     "其特点是加密密钥和解密密钥不相同且难以互相推导。",
                     "这进一步印证了古典密码在面对唯密文攻击时的脆弱性。",
                     "柯克霍夫原则由科克霍夫提出。"):
        assert not media._worth_relisten_marker(sentence), sentence
    # The same attribution with a year is a date a student must check.
    assert media._worth_relisten_marker("柯克霍夫原则由科克霍夫于 1883 年提出。")


def test_slide_page_number_does_not_make_a_sentence_load_bearing():
    # A citation's page number is a pointer, not a fact the student has to
    # check. Reading it as one marked every cited sentence in a real lecture.
    assert not media._worth_relisten_marker(
        "这一原则已成为判定密码强度的标准（课件：ch02.pdf，第 17 页）。")
    assert not media._worth_relisten_marker(
        "密码学被视为艺术而非科学，出现了代替与置换算法"
        "（课件：ch02.pdf，第 21 页）。")
    # A number in the prose still qualifies, citation or not.
    assert media._worth_relisten_marker(
        "1949 年之后密码学逐渐发展成为一门科学（课件：ch02.pdf，第 21 页）。")
    # So does a definition, which carries no number at all.
    assert media._worth_relisten_marker("这种加密方式称为代替密码。")


def test_relisten_marker_is_skipped_for_slides_and_fragments():
    window = {"start": 0.0, "end": 480.0, "text": "讲解中",
              "evidence_blocks": [{"block_id": "B0001", "start": 30.0,
                                   "end": 40.0, "text": "这种加密方式称为代替密码"}]}
    # The cited page is a sharper pointer than a timestamp.
    assert media._relisten_marker_text(
        "这种加密方式称为代替密码（课件：ch02.pdf，第 38 页）。", window) == ""
    # A formula line carries no claim of its own; its introducing sentence does.
    assert media._relisten_marker_text("$IC = 0.0687$", window) == ""
    assert media._relisten_marker_text("", window) == ""
    # A definition with nothing to cite gets the pointer to its moment.
    assert media._relisten_marker_text("这种加密方式称为代替密码。", window) == "（回看 00:30）"
    # An existing marker is never doubled.
    assert media._relisten_marker_text(
        "这种加密方式称为代替密码。（回看 12:00）", window) == ""


def test_unsettled_narration_stays_unmarked_while_a_definition_is_marked(
        tmp_path, monkeypatch):
    settings = SimpleNamespace(platform_token="test", openai_api_key="",
                               notes_model="test", notes_fact_check=False,
                               notes_claim_audit=True)
    transcript = {"segments": [
        {"start": 0, "end": 12, "text": "此类算法历史久远，如凯撒密码。"},
        {"start": 40, "end": 52, "text": "这种加密方式称为代替密码。"},
    ]}
    target = tmp_path / "notes.md"
    monkeypatch.setattr(media, "_summarize",
                        lambda *a, **k: "此类算法历史久远，如凯撒密码。"
                                        "这种加密方式称为代替密码。")

    def uncertain(_llm, _model, prompt, system, _settings):
        parsed = json.loads(_answer(system, prompt))
        for verdict in parsed["verdicts"]:
            verdict.update(status="uncertain", evidence=[], reason="转写含混")
        return json.dumps(parsed, ensure_ascii=False)

    monkeypatch.setattr(media, "_recovery_llm", uncertain)
    assert media.write_notes(transcript, [], target, settings, "课程") == target
    note = target.read_text("utf-8")
    narration = [line for line in note.splitlines() if "此类算法历史久远" in line]
    definition = [line for line in note.splitlines() if "称为代替密码" in line]
    assert narration and "（回看 " not in narration[0]
    assert definition and "（回看 " in definition[0]
