"""Incomplete recovery cannot become a complete note through cache replay."""
import json
import hashlib
from types import SimpleNamespace

import pytest

from pku_sync import media, note_quality
from pku_sync.panel import recordings_api


ACTUAL_GAP = "37:13 起的一段讲述未完整还原，建议回听该段"
ACTUAL_EMPTY_TIME = "标准分数以均值和标准差为基础（录像时间：）。"


def _note(body):
    return "# 课程\n\n## 课堂内容\n\n" + body + "\n\n*已整理至录像 02:00。*\n"


@pytest.mark.parametrize("body", [ACTUAL_GAP, ACTUAL_EMPTY_TIME])
def test_actual_failure_cannot_pass_complete_content_audit(body):
    report = note_quality.audit_note(_note(body), {"segments": [
        {"start": 0, "end": 120, "text": "课堂原文"}]})
    assert any(row["name"] == "内容恢复完整性" and row["status"] == "fail"
               for row in report["checks"])
    assert report["verdict"] == "fail"


def test_existing_unreviewed_note_is_preserved_and_not_implicitly_rewritten(tmp_path):
    target = tmp_path / "notes.md"
    original = _note(ACTUAL_GAP)
    target.write_text(original, "utf-8")
    with pytest.raises(RuntimeError, match="原文件保留"):
        media.write_notes({"segments": [{"start": 0, "end": 120, "text": "课堂原文"}]}, [], target,
                          SimpleNamespace(platform_token="", openai_api_key=""), "课程")
    assert target.read_text("utf-8") == original


def test_publish_missing_review_record_stops_without_source_preparation(tmp_path, monkeypatch):
    target = tmp_path / "notes.md"
    target.write_text(_note(ACTUAL_EMPTY_TIME), "utf-8")
    (tmp_path / "transcript.json").write_text(json.dumps({"segments": [
        {"start": 0, "end": 120, "text": "课堂原文"}]}), "utf-8")
    monkeypatch.setattr(recordings_api, "assignment_catalog_publish_issues", lambda *_: [])
    monkeypatch.setattr("pku_sync.lecture_source_download.prepare_lesson_sources",
                        lambda *a, **k: pytest.fail("unreviewed note caused source download"))
    issues = recordings_api.note_publish_issues(SimpleNamespace(directory=tmp_path), target)
    assert any("整理保存记录" in issue for issue in issues)


def test_success_cache_with_uncovered_group_cannot_return_partial_content(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"schema_version": media._RECOVERY_LEDGER_SCHEMA,
        "status": "success", "content": "已核验但缺少主要定义的句子。",
        "validated_claims": [], "group_coverage_gaps": ["G01"]}), "utf-8")
    original = path.read_bytes()
    monkeypatch.setattr(media, "_recovery_llm", lambda *args: pytest.fail("no model without bound evidence"))
    with pytest.raises(media._NoteQualityError, match="证据"):
        media._recover_chapter_with_ledger(None, "model", "course", "原转写", "草稿", "",
            settings=SimpleNamespace(), sources=[], cache_path=path)
    assert path.read_bytes() == original


def test_complete_content_is_not_rejected_for_a_specific_uncertain_term():
    note = _note("本章解释概念、条件及例子。某个专业术语需回看原录像核对。")
    report = note_quality.audit_note(note, {"segments": [{"start": 0, "end": 120, "text": "课堂原文"}]})
    assert any(row["name"] == "内容恢复完整性" and row["status"] == "pass"
               for row in report["checks"])


def _bound_record():
    fact = "常模用于比较测验者与参照群体的表现。"
    blocks = [{"block_id": f"B{i:04d}", "start": i * 100.0,
               "end": (i + 1) * 100.0, "text": fact * 30} for i in range(6)]
    groups = media._recovery_evidence_groups(blocks)
    assert len(groups) == 3
    stored = {}
    for group in groups:
        block = group["blocks"][0]
        claim = {"text": fact, "display_text": fact, "translation_status": "not_needed",
                 "origin": "transcript", "evidence_block_id": block["block_id"],
                 "transcript_excerpt": fact, "evidence_start": block["start"],
                 "evidence_end": block["end"], "source_filename": None,
                 "source_page": None, "source_quote": None}
        stored[group["group_id"]] = {"signature": group["signature"],
            "block_ids": [row["block_id"] for row in group["blocks"]],
            "validated_claims": [claim]}
    return blocks, {"schema_version": media._RECOVERY_LEDGER_SCHEMA, "status": "success",
                    "content": "此前的不完整付费结果", "transcript_groups": stored,
                    "group_coverage_gaps": ["G01"], "verified_sentences": {"old": "旧核验结果"}}


def test_retry_only_missing_group_and_preserve_exact_history_and_other_chapters(tmp_path):
    blocks, record = _bound_record()
    path = tmp_path / "ledger.json"
    original = json.dumps(record, ensure_ascii=False).encode("utf-8")
    path.write_bytes(original)
    other = tmp_path / "other.review.md"
    other.write_text("其他已完成章节", "utf-8")
    other_original = other.read_bytes()
    resumed = media._resume_incomplete_recovery(record, path, blocks)
    assert resumed["retried_groups"] == ["G01"]
    assert resumed["transcript_groups"] == {key: record["transcript_groups"][key]
                                           for key in ("G02", "G03")}
    assert "validated_claims" not in resumed and "verified_sentences" not in resumed
    digest = hashlib.sha256(original).hexdigest()
    history = path.with_name(path.name + f".incomplete-{digest[:20]}.json")
    assert history.read_bytes() == original
    assert resumed["incomplete_history_sha256"] == digest
    assert other.read_bytes() == other_original


def test_changed_source_stops_before_retry_or_any_cache_write(tmp_path):
    blocks, record = _bound_record()
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(record), "utf-8")
    original = path.read_bytes()
    blocks[0]["text"] += "来源发生改变"
    with pytest.raises(media._NoteQualityError, match="证据块与缓存不一致"):
        media._resume_incomplete_recovery(record, path, blocks)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_contextually_incomplete_window_does_not_reuse_its_claims(tmp_path):
    blocks, record = _bound_record()
    record["fact_readiness"] = {"review_required": [{"index": 0, "reason": "needs_context"}]}
    # This old claim is rejected rather than being reused during repair.
    record["transcript_groups"]["G02"]["validated_claims"][0]["text"] = "错误缓存事实"
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(record), "utf-8")
    resumed = media._resume_incomplete_recovery(record, path, blocks)
    assert resumed["retried_groups"] == ["G01", "G02", "G03"]
    assert resumed["transcript_groups"] == {}
    assert "content" not in resumed


def test_retry_does_not_accept_tampered_claim_in_a_group_it_would_reuse(tmp_path):
    blocks, record = _bound_record()
    record["transcript_groups"]["G02"]["validated_claims"][0]["text"] = "错误缓存事实"
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(record), "utf-8")
    original = path.read_bytes()
    with pytest.raises(media._NoteQualityError, match="逐字转写证据"):
        media._resume_incomplete_recovery(record, path, blocks)
    assert path.read_bytes() == original


def test_same_incomplete_retry_policy_stops_repeated_paid_requests(tmp_path, monkeypatch):
    blocks, record = _bound_record()
    record["complete_recovery_retry_policy"] = "complete-window-v1"
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(record), "utf-8")
    original = path.read_bytes()
    monkeypatch.setattr(media, "_recovery_llm", lambda *args: pytest.fail("retry must be bounded"))
    with pytest.raises(media._NoteQualityError, match="停止重复请求"):
        media._recover_chapter_with_ledger(None, "model", "course", "原转写", "草稿", "",
            settings=SimpleNamespace(), sources=[], cache_path=path, evidence_blocks=blocks)
    assert path.read_bytes() == original


def test_production_never_normalizes_or_conceals_model_review_input(tmp_path, monkeypatch):
    original = "### 概念\n\n" + ACTUAL_EMPTY_TIME
    revised = "标准分数以均值和标准差为基础，参见本段录像。"
    run, calls, _ = _organizer_run(tmp_path, monkeypatch, revised)
    monkeypatch.setattr(media, "_normalize_window_summary", lambda *a: pytest.fail("text normalization called"))
    target = run()
    assert len(calls) == 1 and revised in target.read_text("utf-8")


def test_sparse_model_review_is_publishable_with_uncertainty_and_not_complete_certification(tmp_path, monkeypatch):
    from pku_sync import note_editor, transcript_notes
    transcript = {"segments": [{"start": 0, "end": 20, "text": "开始"},
        {"start": 100, "end": 120, "text": "讲解中"}, {"start": 195, "end": 205, "text": "听不清"}]}
    run, calls, _ = _organizer_run(tmp_path, monkeypatch, "保留可辨认的讲述。",
        transcript=transcript, replay_notices=["主要讲述尚需回看原录像。"])
    target = run()
    original = target.read_bytes()
    report = json.loads(note_editor.report_path(target).read_text("utf-8"))
    assert report["status"] == "organized" and report["content_complete"] is None
    assert report["assessment"] == "organization-not-content-certification"
    (tmp_path / "transcript.json").write_text(json.dumps(transcript), "utf-8")
    monkeypatch.setattr(recordings_api, "assignment_catalog_publish_issues", lambda *_: [])
    assert recordings_api.note_publish_issues(SimpleNamespace(directory=tmp_path), target) == []
    assert run() == target and len(calls) == 1 and target.read_bytes() == original


def test_recovery_does_not_reuse_unbound_legacy_slide_claims(tmp_path):
    blocks, record = _bound_record()
    record.update(slide_extraction={"claims": []}, slide_claims=[{"text": "未绑定的课件事实"}],
                  slide_batches={"S001": {"signature": "must-be-rechecked-by-normal-source-stage"}})
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(record), "utf-8")
    resumed = media._resume_incomplete_recovery(record, path, blocks)
    assert "slide_extraction" not in resumed and "slide_claims" not in resumed
    assert resumed["slide_batches"] == record["slide_batches"]


def test_actual_source_time_range_survives_normalization_instead_of_becoming_empty():
    original = "评估测评工具的核心指标为准确性与可靠性（录像时间：67:03-69:01）。"
    normalized = media._normalize_window_summary("### 稳定性与准确性\n\n" + original)
    assert original in normalized
    assert not note_quality.note_completeness_issues(normalized)


def test_generated_chapter_range_is_removed_without_removing_source_locator():
    original = "课堂介绍概念（录像时间：01:10-01:20）。"
    normalized = media._normalize_window_summary("### 00:00–02:00\n\n" + original)
    assert "00:00–02:00" not in normalized
    assert original in normalized


def _citation_window():
    return {"start": 60.0, "end": 120.0, "evidence_blocks": [
        {"block_id": "B0001", "start": 70.0, "end": 80.0, "text": "课堂介绍常模用于比较参照群体的表现。"},
        {"block_id": "B0002", "start": 100.0, "end": 110.0, "text": "课堂介绍常模用于比较参照群体的表现。"}]}


def _citation_audit(evidence_id="B0001", status="pass"):
    return {"status": status, "signature": "offline-test",
            "assertions": [{"id": "C001", "text": "常模用于比较参照群体的表现（录像时间：01:10-01:20）。"}],
            "verdicts": [{"id": "C001", "status": "supported",
                          "evidence": [{"id": evidence_id, "quote": "常模用于比较参照群体的表现"}]}]}


def test_preserved_source_time_requires_independent_evidence_in_its_actual_range():
    audit = _citation_audit()
    note = audit["assertions"][0]["text"]
    media._check_source_time_citations(note, _citation_window(), audit)
    with pytest.raises(media._NoteQualityError, match="对应时段"):
        media._check_source_time_citations(note, _citation_window(), _citation_audit("B0002"))
    with pytest.raises(media._NoteQualityError, match="独立逐句审计"):
        media._check_source_time_citations(note, _citation_window(), _citation_audit(status="fail"))


@pytest.mark.parametrize("locator", ["01:10-02:01", "00:01-00:10", "01:90-02:00", "01:20-01:10", ""])
def test_invalid_source_time_never_becomes_a_valid_candidate(locator):
    with pytest.raises(media._NoteQualityError):
        media._check_source_time_citations(f"课堂介绍概念（录像时间：{locator}）。", _citation_window())


@pytest.mark.parametrize('locator', ['[01:10]', '[01:10]-[01:20]', '[00:01:10]', '[00:01:10]—[00:01:20]'])
def test_complete_bracketed_source_time_still_requires_exact_independent_evidence(locator):
    note = f"常模用于比较参照群体的表现（录像时间：{locator}）。"
    audit = _citation_audit()
    audit['assertions'][0]['text'] = note
    media._check_source_time_citations(note, _citation_window(), audit)
    wrong = _citation_audit('B0002')
    wrong['assertions'][0]['text'] = note
    with pytest.raises(media._NoteQualityError, match='对应时段'):
        media._check_source_time_citations(note, _citation_window(), wrong)
    audit['status'] = 'fail'
    with pytest.raises(media._NoteQualityError, match='独立逐句审计'):
        media._check_source_time_citations(note, _citation_window(), audit)


@pytest.mark.parametrize('locator', [
    '[01:10', '01:10]', '[[01:10]]', '[01:10]]', '[01:10]-01:20',
    '01:10-[01:20]', '[01:10-01:20]', '[01:10]-[00:01:20]',
    '[01:99]', '[00:99:10]', '[00:01:99]', '[01:10]-[02:01]',
    '[00:01]-[00:10]', '[01:20]-[01:10]', '[00:01:20]-[00:01:10]',
    '[00:01:10]-[00:02:01]', '[]', '[01:10] junk', '[01:10]、[01:20]',
])
def test_bad_bracketed_source_time_cannot_be_repaired_or_certified(locator):
    with pytest.raises(media._NoteQualityError):
        media._check_source_time_citations(f"常模用于比较参照群体的表现（录像时间：{locator}）。", _citation_window())


def test_generic_positive_audit_cannot_certify_a_locator_without_matching_assertions():
    note = "常模用于比较参照群体的表现（录像时间：01:10-01:20）。"
    with pytest.raises(media._NoteQualityError, match="缺少独立审计句子"):
        media._check_source_time_citations(note, _citation_window(),
            {"status": "pass", "assertions": [], "verdicts": []})


def test_actual_r13_wrong_locator_still_fails_even_when_reviewer_calls_it_supported():
    # Exact r13 B0040 boundaries. Its initial reviewer called 13:30 supported
    # while citing this 14:22 block; local time authority must still reject it.
    quote = "有一些功能，它的形成是一个关键期"
    window = {"start": 283.98, "end": 880.5, "evidence_blocks": [
        {"block_id": "B0040", "start": 862.17, "end": 880.5,
         "text": "就会尤其是后面会讲到关键期的作用，对吧？ " + quote +
                 "的那如果在关键期，你的各种各样的因素干扰了这个过程呢，造成非常非常严重的后果等等。"}]}
    bad = "关键期受干扰可能导致严重后果（录像时间：[13:30]）。"
    def observed_positive(text):
        return {"status": "pass", "assertions": [{"id": "C001", "text": text}],
                "verdicts": [{"id": "C001", "status": "supported",
                              "evidence": [{"id": "B0040", "quote": quote}]}]}
    with pytest.raises(media._NoteQualityError, match="对应时段"):
        media._check_source_time_citations(bad, window, observed_positive(bad))
    correct = bad.replace("[13:30]", "[14:22]")
    media._check_source_time_citations(correct, window, observed_positive(correct))


def test_final_repair_system_separates_real_sources_from_errors_and_rechecks_supported_times(monkeypatch):
    context = {"window": {"start": 283.98, "end": 880.5},
               "evidence_blocks": [{"block_id": "B0040", "start": 862.17,
                                    "end": 880.5, "text": "关键期受干扰会造成严重后果。"}],
               "assertions": [{"id": "C001", "text": "关键期受干扰可能导致严重后果（录像时间：[13:30]）。"}],
               "verdicts": [{"id": "C001", "status": "supported", "evidence": [
                   {"id": "B0040", "quote": "关键期受干扰会造成严重后果"}], "reason": "旧审核认为支持"}]}
    calls = []
    def observe(_kind, prompt, _settings, *, system):
        calls.append((system, prompt))
        packet = json.loads(prompt)
        assert packet["source_time_context"] == context
        assert packet["audit_issues"] == ["孤立正文标题需成为完整陈述"]
        assert set(packet) == {"audit_issues", "source_time_context", "original_draft"}
        assert packet["original_draft"] == "原稿"
        assert prompt == json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
        assert "仅修复列出的硬错误" not in system
        assert "全部保留的录像时间引用" in system and "supported" in system
        assert "完整主语" in system and "真正的 Markdown 标题" in system
        assert "不能删除" in system and "同一复合句" in system
        constraints = system
        assert "全部保留的录像时间引用" in constraints
        assert "稀疏时间起点" in constraints and "另一主体" in constraints
        assert "完整陈述" in constraints and "不能删除" in constraints
        assert "response_contract" in system and "不能仅返回概念名词" in system
        assert "模型不得在 text 中写时间" in system and "source_ids" in system
        assert "不要生成 quote" in system and "existing-metadata" in system
        return {"content": ' \n{"schema_version":1,"revisions":[]}\n '}
    monkeypatch.setattr("pku_sync.platform.llm", observe)
    assert media._repair_factual_conflicts(None, "test", "稀疏转写 [13:30]", "原稿",
        ["孤立正文标题需成为完整陈述"], settings=SimpleNamespace(platform_token="test"),
        sources=[], source_time_context=context) == ' \n{"schema_version":1,"revisions":[]}\n '
    assert len(calls) == 1


def test_current_editor_keeps_old_paid_revision_without_replaying_it(tmp_path, monkeypatch):
    from pku_sync import note_editor, transcript_notes
    parts = tmp_path / ".notes-parts"
    parts.mkdir()
    old = parts / "old-v5-paid-repair.draft.md"
    old.write_text("旧真实付费原始稿保留", "utf-8")
    before = old.read_bytes()
    run, calls, _ = _organizer_run(tmp_path, monkeypatch, "常模用于比较参照群体的表现。")
    run()
    assert len(calls) == 1 and old.read_bytes() == before
    requests = list((parts / transcript_notes.POLICY).glob("*.json"))
    assert len(requests) == 1 and all(json.loads(p.read_text("utf-8"))["policy"] == transcript_notes.POLICY for p in requests)


@pytest.mark.parametrize("supported", [True, False])
def test_contextual_review_preserves_locator_draft_and_separates_completion_status(tmp_path, monkeypatch, supported):
    from pku_sync import note_editor, transcript_notes
    original = "常模用于比较参照群体的表现，见录像的一分十秒到一分二十秒。"
    run, calls, _ = _organizer_run(tmp_path, monkeypatch, original,
        replay_notices=[] if supported else ["这一引述仍需核对原音。"])
    target = run()
    assert len(calls) == 1 and original in target.read_text("utf-8")
    report = json.loads(note_editor.report_path(target).read_text("utf-8"))
    assert report["content_complete"] is None
    assert report["assessment"] == "organization-not-content-certification"


def _organizer_run(tmp_path, monkeypatch, notes, *, transcript=None, sources=None, replay_notices=None):
    """Use real plain-text transport and persistence; no simulated factual verdict."""
    from pku_sync import transcript_notes
    transcript = transcript or {"segments": [{"start": 60, "end": 120,
                                             "text": "课堂介绍常模用于比较参照群体的表现。"}]}
    cfg = SimpleNamespace(platform_token="offline", openai_api_key="", notes_model="offline")
    parts = transcript_notes.transcript_parts(transcript, 480, transcript_notes.TRANSCRIPT_CHARS)
    calls = []
    def ask(llm, model, prompt, system, settings):
        payload = json.loads(prompt)
        calls.append(payload)
        assert payload["transcript"] == parts[payload["part"] - 1]
        assert "courseware" not in payload and "draft" not in payload
        assert system == transcript_notes.SYSTEM
        result = notes(payload) if callable(notes) else notes
        if replay_notices and isinstance(result, str):
            result += "\n\n" + "\n".join(replay_notices)
        return result
    monkeypatch.setattr(media, "_recovery_llm", ask)
    target = tmp_path / "notes.md"
    return (lambda: media.write_notes(transcript, [], target, cfg, "课程", source_context=sources)), calls, transcript


def test_contextual_review_reuses_all_bound_responses_without_touching_prior_evidence(tmp_path, monkeypatch):
    from pku_sync import note_editor, transcript_notes
    revised = "常模用于比较参照群体的表现，为课堂比较提供参考（录像时间：01:10-01:20）。"
    run, calls, transcript = _organizer_run(tmp_path, monkeypatch, revised)
    target = run()
    assert len(calls) == 1 and calls[0]["transcript"] == transcript["segments"]
    cache = tmp_path / ".notes-parts" / transcript_notes.POLICY
    original = {path.name: path.read_bytes() for path in cache.iterdir()}
    body = target.read_bytes()
    target.unlink()
    assert run() == target and len(calls) == 1 and target.read_bytes() == body
    assert all((cache / name).read_bytes() == content for name, content in original.items())


def test_dense_model_incomplete_revision_retains_note_without_length_certification(tmp_path, monkeypatch):
    from pku_sync import note_editor, transcript_notes
    transcript = {"segments": [{"start": 60, "end": 660,
                               "text": "课堂介绍常模用于比较参照群体的表现。" * 120}]}
    run, calls, _ = _organizer_run(tmp_path, monkeypatch, "常模用于参照比较。", transcript=transcript,
        replay_notices=["主要例题步骤尚未充分还原。"])
    target = run()
    report = json.loads(note_editor.report_path(target).read_text("utf-8"))
    assert len(calls) > 1 and report["content_complete"] is None
    assert "".join(row["text"] for call in calls for row in call["transcript"]) == transcript["segments"][0]["text"]
    assert "常模用于参照比较" in target.read_text("utf-8")


def test_lost_cloud_window_result_resumes_same_billable_job(tmp_path, monkeypatch):
    import httpx
    from pku_sync import platform, note_editor
    cfg = SimpleNamespace(platform_token="offline", notes_model="qwen3.5-flash",
                          data_dir=tmp_path, cloud_transcribe_url="https://relay.example", llm_timeout=600)
    posted, jobs, lost = [], {}, []
    def post(url, **kwargs):
        body = kwargs["json"]
        posted.append(body)
        key = body["idempotency_key"]
        jobs.setdefault(key, body)
        return httpx.Response(202, json={"job_id": key})
    def get(url, **kwargs):
        key = url.rsplit("/", 1)[-1]
        part = json.loads(jobs[key]["user"])["part"]
        content = "常模用于比较参照群体的表现。" if part == 1 else "后续课堂例子。"
        if part == 2 and not lost:
            lost.append(key)
            content = None
        return httpx.Response(200, json={"job_id": key, "status": "succeeded", "content": content, "points_charged": .1})
    monkeypatch.setattr(platform, "_post", post)
    monkeypatch.setattr(platform, "_get", get)
    target = tmp_path / "notes.md"
    transcript = {"segments": [{"start": 60, "end": 120, "text": "课堂介绍常模。"},
                               {"start": 600, "end": 620, "text": "后续课堂例子。"}]}
    with pytest.raises(platform.PlatformError, match="云端返回异常"):
        media.write_notes(transcript, [], target, cfg, "课程")
    assert not target.exists() and len(jobs) == 2
    media.write_notes(transcript, [], target, cfg, "课程")
    assert len(posted) == 3 and posted[1] == posted[2] and len(jobs) == 2
    assert note_editor.review_record_issues(target, transcript) == []


def test_tampered_review_response_stops_before_model_or_publication(tmp_path, monkeypatch):
    from pku_sync import note_editor, transcript_notes
    run, calls, _ = _organizer_run(tmp_path, monkeypatch, "常模用于比较参照群体的表现。")
    target = run()
    target.unlink()
    caches = list((tmp_path / ".notes-parts" / transcript_notes.POLICY).glob("*.json"))
    raw = caches[0]
    value = json.loads(raw.read_text("utf-8"))
    value["response"] += "新增正文"
    raw.write_text(json.dumps(value, ensure_ascii=False), "utf-8")
    before = raw.read_bytes()
    # Both a partial-cache replay and a saved-note restore must stop here;
    # their wording differs, but neither may regenerate or replace evidence.
    with pytest.raises(RuntimeError):
        run()
    assert len(calls) == 1 and not target.exists() and raw.read_bytes() == before


def test_contextual_review_receives_both_speech_and_slide_when_draft_cites_audio(tmp_path, monkeypatch):
    source = {"title": "常模.pdf", "locator": "第 1 页", "status": "readable", "text": "常模用于比较参照群体的表现"}
    revised = "常模用于参照比较，课件第一页面有说明；课堂举例见一分十秒附近。"
    run, calls, transcript = _organizer_run(tmp_path, monkeypatch, revised, sources=[source])
    target = run()
    assert calls[0]["transcript"] == transcript["segments"] and "courseware" not in calls[0]
    assert revised in target.read_text("utf-8")


@pytest.mark.parametrize("bad_audit", ["partial", "provider_error"])
def test_empty_text_or_provider_failure_cannot_commit_note(tmp_path, monkeypatch, bad_audit):
    def notes(payload):
        if bad_audit == "provider_error":
            raise RuntimeError("organizer provider unavailable")
        return " "
    run, calls, _ = _organizer_run(tmp_path, monkeypatch, notes)
    with pytest.raises(RuntimeError):
        run()
    assert len(calls) == 1 and not (tmp_path / "notes.md").exists()
    if bad_audit == "partial":
        # A saved empty response is not repaired or charged again.
        with pytest.raises(RuntimeError):
            run()
        assert len(calls) == 1


@pytest.mark.parametrize("repaired,bad_audit", [
    ("", ""),
    ("### 常模\n\n常模用于比较参照群体的表现。", ""),
    ("### 常模\n\n常模用于比较参照群体的表现（录像时间：00:01）。", ""),
    ("### 常模\n\n常模用于比较参照群体的表现并作比较（录像时间：01:10）。", "unsupported"),
    ("### 常模\n\n常模用于比较参照群体的表现并作比较（录像时间：01:10）。", "quote"),
])
def test_model_disclosed_unresolved_revision_keeps_complete_status_false(tmp_path, monkeypatch, repaired, bad_audit):
    from pku_sync import note_editor, transcript_notes
    revised = repaired or "无法确定这一段，原课堂仍需核对。"
    run, calls, _ = _organizer_run(tmp_path, monkeypatch, revised,
        replay_notices=["这一修订的来源或解释仍待核实。"])
    target = run()
    before = target.read_bytes()
    assert run() == target and len(calls) == 1 and target.read_bytes() == before
    report = json.loads(note_editor.report_path(target).read_text("utf-8"))
    assert report["content_complete"] is None and revised in target.read_text("utf-8")


def test_free_form_provider_response_is_preserved_without_protocol_repair(tmp_path, monkeypatch):
    from pku_sync import transcript_notes
    raw = '### 常模\n\n常模用于比较参照群体的表现。\n> 原始响应中的备注。'
    run, calls, transcript = _organizer_run(tmp_path, monkeypatch, raw)
    for _ in range(2):
        target = run()
    assert len(calls) == 1 and target.read_text("utf-8") == "# 课程\n\n" + raw + "\n"
    records = [json.loads(path.read_text("utf-8")) for path in
        (tmp_path / ".notes-parts" / transcript_notes.POLICY).glob("*.json")]
    assert len(records) == 1 and records[0]["response"] == raw
    assert transcript_notes.record_issues(target, transcript) == []


def _revision_packet(context):
    return {"schema_version": 1, "revisions": [
        ({"id": row["id"], "kind": "existing-metadata"} if media._REVIEW_MARKER_RE.fullmatch(row["text"])
         else {"id": row["id"], "kind": "prose", "sentences": [{
            "text": "常模用于比较参照群体的表现，为课堂比较提供参考。",
            "source_ids": [block["block_id"] for block in context["evidence_blocks"]]}]})
        for row in context["assertions"]]}


@pytest.mark.parametrize("bad", ["missing_note", "missing_complete", "missing_uncertainties", "note_not_text", "empty_note", "complete_not_bool", "complete_integer", "uncertainties_not_list", "uncertainty_not_text", "empty_uncertainty", "extra_field", "duplicate_json_key"])
def test_literal_json_example_is_preserved_without_envelope_interpretation(tmp_path, monkeypatch, bad):
    from pku_sync import note_editor, transcript_notes
    packet = {"note": "常模用于比较参照群体的表现。", "complete": True, "uncertainties": []}
    if bad == "missing_note": del packet["note"]
    elif bad == "missing_complete": del packet["complete"]
    elif bad == "missing_uncertainties": del packet["uncertainties"]
    elif bad == "note_not_text": packet["note"] = {"正文": "内容"}
    elif bad == "empty_note": packet["note"] = " "
    elif bad == "complete_not_bool": packet["complete"] = "true"
    elif bad == "complete_integer": packet["complete"] = 1
    elif bad == "uncertainties_not_list": packet["uncertainties"] = "待核对"
    elif bad == "uncertainty_not_text": packet["uncertainties"] = [None]
    elif bad == "empty_uncertainty": packet["uncertainties"] = [" "]
    elif bad == "extra_field": packet["hidden_text"] = "隐藏的课堂内容"
    raw = json.dumps(packet, ensure_ascii=False)
    if bad == "duplicate_json_key": raw = raw.replace('"complete": true', '"complete": true,"complete": false')
    run, calls, _ = _organizer_run(tmp_path, monkeypatch, raw)
    for _ in range(2):
        target = run()
    assert len(calls) == 1 and target.read_text("utf-8") == "# 课程\n\n" + raw + "\n"
    from pku_sync import transcript_notes, note_editor
    records = [json.loads(path.read_text("utf-8")) for path in
        (tmp_path / ".notes-parts" / transcript_notes.POLICY).glob("*.json")]
    assert len(records) == 1 and records[0]["response"] == raw
    report = json.loads(note_editor.report_path(target).read_text("utf-8"))
    assert report["content_complete"] is None


def test_disjoint_speech_segments_reach_reviewer_with_original_times(tmp_path, monkeypatch):
    transcript = {"segments": [{"start": 60, "end": 120, "text": "课堂介绍常模。"},
                               {"start": 180.17, "end": 200.5, "text": "本段继续介绍参照群体的表现。"}]}
    revised = "课堂在一分钟附近介绍常模，在三分钟附近继续讲参照群体。"
    run, calls, _ = _organizer_run(tmp_path, monkeypatch, revised, transcript=transcript)
    target = run()
    assert calls[0]["transcript"] == transcript["segments"] and revised in target.read_text("utf-8")


def test_editor_receives_original_marker_with_its_paragraph_context(tmp_path, monkeypatch):
    original = "### 常模\n\n常模用于比较参照群体的表现。\n\n（回看 01:00）"
    run, calls, _ = _organizer_run(tmp_path, monkeypatch, original)
    target = run()
    assert len(calls) == 1 and original in target.read_text("utf-8")


@pytest.mark.parametrize("bad", ["prose_hidden_as_metadata", "metadata_new_text", "metadata_new_source",
                                 "metadata_empty_sentences"])
def test_literal_json_fields_cannot_hide_text_or_certify_notes(tmp_path, monkeypatch, bad):
    fields = {"prose_hidden_as_metadata": {"metadata": "常模用于参照比较。"},
              "metadata_new_text": {"retained_metadata": "回看 13:30"},
              "metadata_new_source": {"source_ids": ["B9999"]},
              "metadata_empty_sentences": {"sentences": []}}
    raw = json.dumps({"note": "常模用于参照比较。", "complete": True, "uncertainties": [], **fields[bad]}, ensure_ascii=False)
    run, calls, _ = _organizer_run(tmp_path, monkeypatch, raw)
    for _ in range(2):
        target = run()
    assert len(calls) == 1 and target.read_text("utf-8") == "# 课程\n\n" + raw + "\n"
    from pku_sync import transcript_notes, note_editor
    records = [json.loads(path.read_text("utf-8")) for path in
        (tmp_path / ".notes-parts" / transcript_notes.POLICY).glob("*.json")]
    assert len(records) == 1 and records[0]["response"] == raw
    report = json.loads(note_editor.report_path(target).read_text("utf-8"))
    assert report["content_complete"] is None


@pytest.mark.parametrize("marker", ["（回看 01:99）", "（回看 13:30）", "（回看 02:40）"])
def test_bad_time_marker_is_not_repaired_by_local_language_rules(tmp_path, monkeypatch, marker):
    original = "常模用于比较参照群体的表现。" + marker
    run, calls, transcript = _organizer_run(tmp_path, monkeypatch, original)
    target = run()
    assert len(calls) == 1 and calls[0]["transcript"] == transcript["segments"]
    assert target.read_text("utf-8") == "# 课程\n\n" + original + "\n"
    from pku_sync import note_editor
    report = json.loads(note_editor.report_path(target).read_text("utf-8"))
    assert report["content_complete"] is None

