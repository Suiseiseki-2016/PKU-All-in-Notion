"""Model editing contracts, transport failures and preservation of originals."""
import json
from types import SimpleNamespace

import pytest

from pku_sync import media, note_editor, transcript_notes
from pku_sync.panel import recordings_api


def settings():
    return SimpleNamespace(platform_token="test-account", notes_model="test-model",
                           pku_username="test-campus-account", openai_api_key="")


def response(note="### 主题\n\n按上下文整理的课堂讲解。", *, complete=True, uncertainties=None):
    return note


def transcript(text="老师讲到三成，而课件用 30% 表示同一比例。", end=120):
    return {"segments": [{"start": 0, "end": end, "text": text}]}


def two_windows():
    return {"segments": [{"start": 0, "end": 120, "text": "原始第一段"},
                         {"start": 500, "end": 620, "text": "原始第二段"}]}


@pytest.mark.parametrize("note", [
    "### 考核\n\n考试占三成，参见讲义第七页。",
    "### 课堂说明\n\n老师说‘没有作业’是举例说明否定命题，不是宣布本课没有作业。",
    "### 分析\n\n本节指出‘密钥保密就一定安全’这个说法不成立。",
    "### 术语\n\n转写识别成 RAC，结合课堂上下文暂理解为 RSA，仍需回看核对。",
])
def test_production_writer_leaves_language_and_citations_to_model(tmp_path, monkeypatch, note):
    calls = []
    def ask(llm, model, prompt, system, configured):
        calls.append((json.loads(prompt), system))
        return response(note)
    monkeypatch.setattr(media, "_recovery_llm", ask)
    for name in ("_filter_unsupported_claims", "_ledger_sentence_guard", "_chapter_content_gap",
                 "_normalize_window_summary", "_check_source_time_citations"):
        monkeypatch.setattr(media, name, lambda *a, **k: pytest.fail("text rule entered production"))
    target = tmp_path / "notes.md"
    sources = [{"title": "讲义.pdf", "locator": "第 7 页", "status": "readable", "text": "考试占30%。"}]
    assert media.write_notes(transcript(), [], target, settings(), "课程", source_context=sources) == target
    assert note in target.read_text("utf-8")
    assert len(calls) == 1
    assert calls[0][0]["transcript"] == transcript()["segments"]
    assert "courseware" not in calls[0][0] and "draft" not in calls[0][0]
    assert "轻度编辑" in calls[0][1]
    assert note_editor.review_record_issues(target, transcript(), sources) == []


def test_uncertain_content_survives_as_readable_note_with_explicit_review_status(tmp_path, monkeypatch):
    note = "### 老师的安排\n\n老师提到下周提交，具体日期需要回看录像。"
    monkeypatch.setattr(media, "_recovery_llm", lambda *a: response(
        note, complete=False, uncertainties=["提交日期在转写中含糊，请回看。 "]))
    target = tmp_path / "notes.md"
    media.write_notes(transcript(), [], target, settings(), "课程")
    body = target.read_text("utf-8")
    assert body == "# 课程\n\n" + note + "\n"
    report = json.loads(note_editor.report_path(target).read_text("utf-8"))
    assert report["status"] == "organized" and report["content_complete"] is None
    assert report["assessment"] == "organization-not-content-certification"
    assert note_editor.review_record_issues(target, transcript()) == []


def test_original_reference_text_is_preserved_for_separate_term_review(tmp_path, monkeypatch):
    original = "完全不同的专业术语，只有理解上下文才知道关联。" * 1400
    sources = [{"title": "长讲义.pdf", "locator": "第 2 页", "status": "readable", "text": original}]
    calls = []
    def ask(llm, model, prompt, *args):
        calls.append(json.loads(prompt))
        return response()
    monkeypatch.setattr(media, "_recovery_llm", ask)
    media.write_notes(transcript(), [], tmp_path / "notes.md", settings(), "课程", source_context=sources)
    assert len(calls) == 1 and calls[0]["transcript"] == transcript()["segments"]
    assert "courseware" not in calls[0]
    report = json.loads(note_editor.report_path(tmp_path / "notes.md").read_text("utf-8"))
    assert report["sources_sha256"] == note_editor.fingerprint(sources)
    assert sources[0]["text"] == original


def test_later_courseware_does_not_trigger_implicit_rewrite(tmp_path, monkeypatch):
    sources = [{"title": "讲义.pdf", "status": "readable", "text": "前页文字" * 6000},
               {"title": "讲义.pdf", "status": "readable", "text": "后页给出了具体提交日期。"}]
    original_sources = json.dumps(sources, ensure_ascii=False)
    doubt = "提交日期仍不确定，需要回看。"
    calls = []
    def ask(llm, model, prompt, system, configured):
        calls.append(json.loads(prompt))
        return doubt
    monkeypatch.setattr(media, "_recovery_llm", ask)
    target = tmp_path / "notes.md"
    media.write_notes(transcript(), [], target, settings(), "课程", source_context=sources)
    media.write_notes(transcript(), [], target, settings(), "课程", source_context=sources)
    assert len(calls) == 1 and "courseware" not in calls[0]
    assert target.read_text("utf-8") == "# 课程\n\n" + doubt + "\n"
    report = json.loads(note_editor.report_path(target).read_text("utf-8"))
    assert report["content_complete"] is None
    assert report["sources_sha256"] == note_editor.fingerprint(sources)
    assert json.dumps(sources, ensure_ascii=False) == original_sources
    assert note_editor.review_record_issues(target, transcript(), sources) == []


def test_response_loss_retry_reuses_finished_original_windows(tmp_path, monkeypatch):
    calls = []
    def ask(llm, model, prompt, system, configured):
        calls.append(system)
        if len(calls) == 2:
            raise RuntimeError("review transport interrupted")
        return response()
    monkeypatch.setattr(media, "_recovery_llm", ask)
    target = tmp_path / "notes.md"
    with pytest.raises(RuntimeError, match="transport interrupted"):
        media.write_notes(two_windows(), [], target, settings(), "课程")
    assert not target.exists()
    media.write_notes(two_windows(), [], target, settings(), "课程")
    assert len(calls) == 3
    assert note_editor.review_record_issues(target, two_windows()) == []
    media.write_notes(two_windows(), [], target, settings(), "课程")
    assert len(calls) == 3


def test_interrupted_retry_rejects_changed_request_metadata_before_another_charge(tmp_path, monkeypatch):
    calls = []
    def ask(*args):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("review interrupted")
        return response()
    monkeypatch.setattr(media, "_recovery_llm", ask)
    target = tmp_path / "notes.md"
    with pytest.raises(RuntimeError, match="review interrupted"):
        media.write_notes(two_windows(), [], target, settings(), "课程")
    cache = next((tmp_path / ".notes-parts" / transcript_notes.POLICY).glob("*.json"))
    record = json.loads(cache.read_text("utf-8"))
    record["input"]["transcript"][0]["text"] = "缓存中的来源已被改写。"
    cache.write_text(json.dumps(record, ensure_ascii=False), "utf-8")
    original = cache.read_bytes()
    with pytest.raises(RuntimeError, match="请求或响应发生变化"):
        media.write_notes(two_windows(), [], target, settings(), "课程")
    assert len(calls) == 2 and not target.exists()
    assert cache.read_bytes() == original


@pytest.mark.parametrize("raw", ["not JSON", '{"note":"草稿"}',
    '{"note":"正文","complete":true,"complete":false,"uncertainties":[]}'])
def test_literal_json_and_plain_prose_are_preserved_without_format_rebilling(tmp_path, monkeypatch, raw):
    calls = []
    monkeypatch.setattr(media, "_recovery_llm", lambda *a: calls.append(1) or raw)
    target = tmp_path / "notes.md"
    for _ in range(2):
        media.write_notes(transcript(), [], target, settings(), "课程")
    assert len(calls) == 1
    assert target.read_text("utf-8") == "# 课程\n\n" + raw + "\n"
    records = list((tmp_path / ".notes-parts" / transcript_notes.POLICY).glob("*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text("utf-8"))["response"] == raw
    assert note_editor.review_record_issues(target, transcript()) == []


def test_existing_legacy_note_and_changed_transcript_are_preserved(tmp_path, monkeypatch):
    target = tmp_path / "notes.md"
    target.write_text("原笔记和学生批注。", "utf-8")
    monkeypatch.setattr(media, "_recovery_llm", lambda *a: pytest.fail("implicit paid rewrite"))
    with pytest.raises(RuntimeError, match="原文件保留"):
        media.write_notes(transcript(), [], target, settings(), "课程")
    assert target.read_text("utf-8") == "原笔记和学生批注。"
    assert note_editor.review_record_issues(target, transcript())


def test_review_record_does_not_accept_edited_note_or_new_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "_recovery_llm", lambda *a: response())
    target = tmp_path / "notes.md"
    media.write_notes(transcript(), [], target, settings(), "课程")
    assert note_editor.review_record_issues(target, transcript("其他课堂原文"))
    target.write_text(target.read_text("utf-8") + "学生新编辑。", "utf-8")
    assert note_editor.review_record_issues(target, transcript())


def test_publication_checks_review_binding_without_language_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "_recovery_llm", lambda *a: response(
        "### 否定命题\n\n‘没有作业’在这里是逻辑例句。"))
    target = tmp_path / "notes.md"
    media.write_notes(transcript(), [], target, settings(), "课程")
    (tmp_path / "transcript.json").write_text(json.dumps(transcript()), "utf-8")
    monkeypatch.setattr(recordings_api, "assignment_catalog_publish_issues", lambda *a: [])
    monkeypatch.setattr("pku_sync.panel.transcript_review.review_pending", lambda *a: False)
    monkeypatch.setattr("pku_sync.note_quality.audit_note", lambda *a, **k: pytest.fail("text rules at publish"))
    job = SimpleNamespace(directory=tmp_path)
    assert recordings_api.note_publish_issues(job, target) == []
    target.write_text(target.read_text("utf-8") + "内容已变化。", "utf-8")
    assert recordings_api.note_publish_issues(job, target)


def test_transport_partition_preserves_all_characters_and_time_metadata():
    source = "原始课堂正文" * 3000
    original = transcript(source)
    parts = note_editor.transcript_parts(original, 480)
    assert "".join(row["text"] for part in parts for row in part) == source
    assert all(row["start"] == 0 and row["end"] == 120 for part in parts for row in part)
    assert original == transcript(source)


def test_plus_model_requests_fit_relay_budget_without_losing_source_text(tmp_path, monkeypatch):
    import httpx
    from pku_sync import platform
    cfg = settings()
    cfg.notes_model = "qwen3.7-plus"
    cfg.data_dir = tmp_path
    cfg.cloud_transcribe_url = "https://relay.example"
    cfg.llm_timeout = 600
    original = transcript("完整课堂正文。" * 1600)
    sources = [{"title": "本讲.pdf", "status": "readable", "text": "完整课件页面文字。" * 1600}]
    submitted = []
    jobs = {}
    def post(url, **kwargs):
        body = kwargs["json"]
        assert len(body["system"]) + len(body["user"]) <= 30000
        submitted.append((json.loads(body["user"]), body["system"]))
        job_id = body["idempotency_key"]
        jobs[job_id] = response("模型整理后的连贯讲义。" * 700)
        return httpx.Response(202, json={"job_id": job_id})
    def get(url, **kwargs):
        job_id = url.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"job_id": job_id, "status": "succeeded",
                                       "content": jobs[job_id], "points_charged": .01})
    monkeypatch.setattr(platform, "_post", post)
    monkeypatch.setattr(platform, "_get", get)
    target = tmp_path / "notes.md"
    media.write_notes(original, [], target, cfg, "课程", source_context=sources)
    parts = note_editor.transcript_parts(original, 480, transcript_notes.PLUS_TRANSCRIPT_CHARS)
    assert len(submitted) == len(parts)
    for index, (part, (payload, system)) in enumerate(zip(parts, submitted)):
        assert payload["transcript"] == part
        assert payload["previous_context"] == (parts[index - 1][-2:] if index else [])
        assert "courseware" not in payload and system == transcript_notes.SYSTEM
    assert "".join(row["text"] for part in parts for row in part) == original["segments"][0]["text"]
    assert note_editor.review_record_issues(target, original, sources) == []
    count = len(submitted)
    target.unlink()
    media.write_notes(original, [], target, cfg, "课程", source_context=sources)
    assert len(submitted) == count


def test_plus_budget_includes_json_escaping_and_metadata_before_submission(tmp_path):
    calls = []
    cfg = settings()
    cfg.notes_model = "qwen3.7-plus"
    # Reference metadata is no longer submitted; the actual course title is.
    with pytest.raises(RuntimeError, match="任务未提交"):
        transcript_notes.write_notes(transcript(), tmp_path / "notes.md", cfg, "t" * 201000,
                                     ask=lambda *a: calls.append(a) or response())
    assert calls == [] and not (tmp_path / "notes.md").exists()


def test_note_commit_failure_reuses_paid_responses_and_preserves_review_record(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(media, "_recovery_llm", lambda *a: calls.append(1) or response())
    target = tmp_path / "notes.md"
    from pathlib import Path
    original_replace = Path.replace
    def fail_note(path, destination):
        if destination == target:
            raise OSError("disk write interrupted")
        return original_replace(path, destination)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail_note)
        with pytest.raises(OSError, match="disk write interrupted"):
            media.write_notes(transcript(), [], target, settings(), "课程")
    assert not target.exists() and note_editor.report_path(target).exists()
    media.write_notes(transcript(), [], target, settings(), "课程")
    assert len(calls) == 1
    assert note_editor.review_record_issues(target, transcript()) == []


@pytest.mark.parametrize("damage", ["response", "missing-request", "missing-record", "source-change"])
def test_changed_review_or_evidence_cannot_reuse_completed_note(tmp_path, monkeypatch, damage):
    calls = []
    monkeypatch.setattr(media, "_recovery_llm", lambda *a: calls.append(1) or response())
    target = tmp_path / "notes.md"
    media.write_notes(transcript(), [], target, settings(), "课程")
    original = target.read_bytes()
    report = json.loads(note_editor.report_path(target).read_text("utf-8"))
    sources = []
    if damage == "response":
        cache = target.parent / ".notes-parts" / transcript_notes.POLICY / (report["requests"][-1] + ".json")
        value = json.loads(cache.read_text("utf-8"))
        value["response"] = response("缓存被外部改写。")
        cache.write_text(json.dumps(value), "utf-8")
    elif damage == "missing-request":
        report["requests"].pop()
        note_editor.write_json(note_editor.report_path(target), report)
    elif damage == "missing-record":
        note_editor.report_path(target).unlink()
    else:
        sources = [{"title": "不同课件.pdf", "text": "新的证据", "status": "readable"}]
    with pytest.raises(RuntimeError, match="保留"):
        media.write_notes(transcript(), [], target, settings(), "课程", source_context=sources)
    assert len(calls) == 1 and target.read_bytes() == original


def test_unmatched_file_is_not_supplied_as_lecture_evidence(tmp_path, monkeypatch):
    calls = []
    def ask(llm, model, prompt, *args):
        calls.append(json.loads(prompt))
        return response()
    monkeypatch.setattr(media, "_recovery_llm", ask)
    source = {"title": "另一周.pdf", "text": "与本讲未建立关联的内容", "status": "unmatched"}
    media.write_notes(transcript(), [], tmp_path / "notes.md", settings(), "课程", source_context=[source])
    assert all("courseware" not in call for call in calls)


def test_explicit_rebuild_is_new_work_but_publication_retry_reuses_paid_stages(tmp_path, monkeypatch):
    from pku_sync.models import Recording
    from pku_sync.pipeline import RecordingJob
    from pku_sync.catalog_snapshots import recording_source_id
    job = RecordingJob("课程", tmp_path / "课程", Recording(
        course_id="course-1", title="第一讲", recorded_at="2026-10-09 09:00:00",
        play_url="https://campus.example/recording"))
    job.directory.mkdir(parents=True)
    target = job.directory / "notes.md"
    cfg = settings()
    cfg.data_dir, cfg.notion_token = tmp_path, "fake-notion"
    source_id = recording_source_id(job.recording)
    cfg._metered_operation_context = {"purpose": "lecture-notes", "course_id": "course-1",
                                     "source_id": source_id, "client_operation_id": source_id}
    calls = []
    def ask(llm, model, prompt, *args):
        calls.append(json.loads(prompt))
        return response("原课堂笔记。" if len(calls) == 1 else "模型重整后的课堂笔记。")
    monkeypatch.setattr(media, "_recovery_llm", ask)
    media.write_notes(transcript(), [], target, cfg, job.label)
    original = target.read_bytes()
    (job.directory / "transcript.json").write_text(json.dumps(transcript()), "utf-8")
    monkeypatch.setattr(recordings_api, "assignment_catalog_publish_issues", lambda *a: [])
    monkeypatch.setattr("pku_sync.lecture_source_download.prepare_lesson_sources", lambda *a, **k: ([], []))
    monkeypatch.setattr("pku_sync.note_sources.collect_note_sources", lambda *a, **k: [])
    attempts = []
    def publish(actual_job, lecture_id, token, *, note_path, revision):
        assert recordings_api.note_publish_issues(job, note_path) == []
        attempts.append(note_path.read_bytes())
        if len(attempts) == 1:
            raise RuntimeError("Notion unavailable")
        return "https://notion.example/new-note"
    monkeypatch.setattr(recordings_api, "publish_notes", publish)
    manager = recordings_api.RecordingWorkManager(cfg)
    with pytest.raises(ValueError, match="原笔记及旧页面均已保留"):
        manager._process(job, "lecture", regenerate=True)
    assert target.read_bytes() == original and len(calls) == 2
    assert calls[0]["request_context"] != calls[1]["request_context"]
    manager._process(job, "lecture", regenerate=True)
    assert len(calls) == 2 and attempts[0] == attempts[1]
    assert "模型重整后的课堂笔记" in target.read_text("utf-8")
    assert next(job.directory.glob("notes.previous-*.md")).read_bytes() == original


@pytest.mark.parametrize("local_commit_failure", [False, True])
def test_actual_rebuild_promotes_bound_review_or_restores_original_files(tmp_path, monkeypatch, local_commit_failure):
    from pathlib import Path
    from pku_sync.models import Recording
    from pku_sync.pipeline import RecordingJob

    job = RecordingJob("课程", tmp_path / "课程", Recording(
        course_id="course-1", title="第一讲", recorded_at="2026-10-09 09:00:00",
        play_url="https://campus.example/recording"))
    job.directory.mkdir(parents=True)
    target = job.directory / "notes.md"
    target.write_text("原笔记和学生批注。", "utf-8")
    old_report = {"policy": "previous-editor", "original": "学生原记录"}
    note_editor.write_json(note_editor.report_path(target), old_report)
    (job.directory / "transcript.json").write_text(json.dumps(transcript()), "utf-8")
    frames = job.directory / "keyframes"
    frames.mkdir()
    (frames / "frame.jpg").write_bytes(b"existing-frame")
    (frames / "index.json").write_text(json.dumps({"keyframes": [
        {"file": "frame.jpg", "timestamp": 0, "time": "00:00"}]}), "utf-8")
    cfg = settings()
    cfg.data_dir, cfg.notion_token = tmp_path, "fake-notion"
    monkeypatch.setattr(media, "_recovery_llm", lambda *a: response("### 概念\n\n模型修订后的课堂笔记。"))
    monkeypatch.setattr(recordings_api, "assignment_catalog_publish_issues", lambda *a: [])
    monkeypatch.setattr("pku_sync.lecture_source_download.prepare_lesson_sources", lambda *a, **k: ([], []))
    monkeypatch.setattr("pku_sync.note_sources.collect_note_sources", lambda *a, **k: [])
    publications = []
    def publish(actual_job, lecture_id, token, *, note_path, revision):
        assert actual_job is job and revision and lecture_id == "lecture"
        assert recordings_api.note_publish_issues(job, note_path) == []
        publications.append(note_path)
        return "https://notion.example/new-note"
    monkeypatch.setattr(recordings_api, "publish_notes", publish)
    original_replace = Path.replace
    def fail_promotion(path, destination):
        if destination == target and path.name.startswith("notes.rebuild-"):
            raise OSError("local promotion interrupted")
        return original_replace(path, destination)
    manager = recordings_api.RecordingWorkManager(cfg)
    if local_commit_failure:
        monkeypatch.setattr(Path, "replace", fail_promotion)
        with pytest.raises(OSError, match="local promotion interrupted"):
            manager._process(job, "lecture", regenerate=True)
        assert target.read_text("utf-8") == "原笔记和学生批注。"
        assert json.loads(note_editor.report_path(target).read_text("utf-8")) == old_report
        assert note_editor.report_path(publications[0]).exists()
    else:
        assert manager._process(job, "lecture", regenerate=True) == "https://notion.example/new-note"
        assert "模型修订后的课堂笔记" in target.read_text("utf-8")
        assert note_editor.review_record_issues(target, transcript()) == []
        backups = list(job.directory.glob("notes.previous-*.md"))
        assert len(backups) == 1 and backups[0].read_text("utf-8") == "原笔记和学生批注。"
        assert json.loads(note_editor.report_path(backups[0]).read_text("utf-8")) == old_report
    assert len(publications) == 1

