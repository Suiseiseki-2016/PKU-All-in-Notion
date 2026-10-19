"""Single-purpose text organization, source delivery and failure recovery."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pku_sync import transcript_notes as notes


def config(model="qwen3.5-flash"):
    return SimpleNamespace(notes_model=model, pku_username="isolated-student",
        _metered_operation_context={"purpose":"lecture-notes", "source_id":"pku:test:lecture"})


@pytest.mark.parametrize("name", ["SYSTEM", "TOPIC_SYSTEM", "MERGE_SYSTEM", "SOURCED_MERGE_SYSTEM"])
def test_prompts_correct_asr_without_losing_lecture_quality_or_factual_boundaries(name):
    system = getattr(notes, name)
    for rule in (
        "course", "previous_context", "courseware", "领域常识", "同音、近音", "英文单词",
        "统一术语拼写", "共行→共情", "来防者/白访→来访者", "机器关注→积极关注",
        "策证点→侧重点", "治疗师假/治疗医→治疗师甲/治疗师乙", "明确纠错不加（转写待回听）",
        "鄙视→被试", "慎少数→性少数", "四个杂购一个H→四个 W 一个 H（4W1H）",
        "不要炸去/No炸去→不要 judge", "大家晚上好", "是不是？、是吧？、对不对？",
        "保留实质问答与反问含义", "进入独立新主题或模块时必须加", "### 主题小标题",
        "仅当分段开头直接延续 previous_context 的同一段落/主题时省略开头标题",
        "previous_context 是只读的衔接上下文，其中的句子绝不能在输出中重复",
        "绝不用于普通词汇、口语语法修复", "外国人名", "符号/代码", "关键数字",
        "仍确实无法辨认", "同一不确定词最多标一次", "0细胞", "清晰连贯的大学课堂讲义",
        "推导步骤", "课堂问答", "作业考试安排", "不重复标题", "### 小标题",
        "要点列表/关键概念/总结/待核问题", "绝不编造课堂未提及", "普遍规律或因果结论",
        "个人猜测、未证实、研究分歧、仅供参考、了解即可、不考",
    ):
        assert rule in system
    assert "不要纠正或替换转写里的名词" not in system
    assert "只能保留原词" not in system
    assert len(system) < 2000
    assert "JSON" in notes.SYSTEM and "轻度编辑" in notes.TOPIC_SYSTEM
    assert "优先使用课件中的准确写法" in notes.SOURCED_MERGE_SYSTEM


@pytest.mark.parametrize("previous_system", [notes.PREVIOUS_SYSTEM, notes.LEGACY_SYSTEM])
def test_previous_transcript_prompt_receipts_restore_and_reuse_paid_parts(tmp_path, monkeypatch, previous_system):
    from pku_sync.note_editor import report_path, review_record_issues

    target = tmp_path / "old.md"
    with monkeypatch.context() as patch:
        patch.setattr(notes, "SYSTEM", previous_system)
        notes.write_notes(original(), target, config(), "课程", ask=lambda *a: "原付费正文。")
    saved = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    fail = lambda *a: pytest.fail("previous transcript response billed again")
    assert notes.record_issues(target, original()) == []
    assert review_record_issues(target, original()) == []
    notes.write_notes(original(), target, config(), "课程", ask=fail)
    target.unlink()
    notes.write_notes(original(), target, config(), "课程", ask=fail)
    assert all(p.read_bytes() == raw for p, raw in saved.items())
    # A new output without a final report must discover old prompt cache keys.
    fresh = tmp_path / "new.md"
    notes.write_notes(original(), fresh, config(), "课程", ask=fail)
    assert fresh.read_bytes() == target.read_bytes()
    old_report = json.loads(report_path(target).read_text("utf-8"))
    new_report = json.loads(report_path(fresh).read_text("utf-8"))
    assert new_report["requests"] == old_report["requests"]
    assert notes.record_issues(fresh, original()) == []


@pytest.mark.parametrize("merge_name", [
    "LEGACY_MERGE_SYSTEM", "LEGACY_SOURCED_MERGE_SYSTEM", "PREVIOUS_SOURCED_MERGE_SYSTEM",
])
def test_previous_topic_prompts_restore_notes_and_reuse_paid_parts(tmp_path, monkeypatch, merge_name):
    from pku_sync.note_editor import review_record_issues

    sourced = merge_name != "LEGACY_MERGE_SYSTEM"
    sources = [{"title": "课件", "text": "本课术语", "status": "readable"}] if sourced else []
    target = tmp_path / "old.md"
    with monkeypatch.context() as patch:
        patch.setattr(notes, "TOPIC_SYSTEM", notes.LEGACY_TOPIC_SYSTEM)
        patch.setattr(notes, "SOURCED_MERGE_SYSTEM" if sourced else "MERGE_SYSTEM",
                      getattr(notes, merge_name))
        notes.write_topic_notes(original(), target, config(), "课程", source_context=sources,
                                ask=lambda *a: "原付费分段。", merge_ask=lambda *a: "原合并正文。")
    saved = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    fail = lambda *a: pytest.fail("previous topic response billed again")
    assert notes.topic_record_issues(target, original(), sources) == []
    assert review_record_issues(target, original(), sources) == []
    notes.write_topic_notes(original(), target, config(), "课程", source_context=sources,
                            ask=fail, merge_ask=fail)
    target.unlink()
    notes.write_topic_notes(original(), target, config(), "课程", source_context=sources,
                            ask=fail, merge_ask=fail)
    calls = []
    fresh = tmp_path / "new.md"
    def merge(system, prompt):
        assert system == (notes.SOURCED_MERGE_SYSTEM if sourced else notes.MERGE_SYSTEM)
        calls.append(json.loads(prompt))
        return "新合并正文。"
    notes.write_topic_notes(original(), fresh, config(), "课程", source_context=sources,
                            ask=fail, merge_ask=merge)
    assert len(calls) == 1
    assert [r["note"] for r in calls[0]["section_drafts"]] == ["原付费分段。"] * 2
    assert notes.topic_record_issues(fresh, original(), sources) == []
    assert all(p.read_bytes() == raw for p, raw in saved.items())


@pytest.mark.parametrize("strategy", ["topics", "transcript"])
@pytest.mark.parametrize("model", [None, "qwen3.7-plus", "qwen3.5-flash", "qwen3.8-max", "test-model"])
@pytest.mark.parametrize("via_settings", [False, True])
def test_notes_model_routing_preserves_explicit_settings(monkeypatch, strategy, model, via_settings):
    from pku_sync.config import Settings
    from pku_sync.media import resolve_notes_model

    monkeypatch.delenv("NOTES_MODEL", raising=False)
    monkeypatch.delenv("NOTES_STRATEGY", raising=False)
    options = {} if model is None else {"notes_model": model}
    if via_settings:
        options["notes_strategy"] = strategy
    cfg = Settings(_env_file=None, **options)
    expected = model or ("qwen3.5-flash" if strategy == "transcript" else "qwen3.7-plus")
    assert resolve_notes_model(cfg, None if via_settings else strategy) == expected
    assert cfg.notes_model == (model or "qwen3.7-plus")


@pytest.mark.parametrize("strategy", ["topics", "transcript"])
@pytest.mark.parametrize("model", [None, "qwen3.7-plus", "qwen3.5-flash"])
def test_media_uses_routed_model_without_mutating_settings(tmp_path, monkeypatch, strategy, model):
    from pku_sync import media, knowledge_workflow
    from pku_sync.config import Settings

    monkeypatch.delenv("NOTES_MODEL", raising=False)
    options = {} if model is None else {"notes_model": model}
    cfg = Settings(_env_file=None, platform_token="offline", **options)
    fields_set = cfg.model_fields_set.copy()
    calls = []

    def ask(llm, selected_model, prompt, system, configured):
        calls.append((selected_model, configured.notes_model))
        if system == knowledge_workflow.PLAN_SYSTEM:
            return json.dumps({"units": [
                {"title": "课堂内容", "first_segment": 0, "reference_segments": [], "courseware_indices": []}]})
        return "完整课堂笔记。"

    monkeypatch.setattr(media, "_recovery_llm", ask)
    target = tmp_path / "notes.md"
    assert media.write_notes(original(), [], target, cfg, "课程", strategy=strategy) == target
    expected = model or ("qwen3.5-flash" if strategy == "transcript" else "qwen3.7-plus")
    assert calls and all(call == (expected, expected) for call in calls)
    assert cfg.notes_model == (model or "qwen3.7-plus")
    assert cfg.model_fields_set == fields_set


def legacy_paid_note(transcript, target, cfg, title, strategy, sources):
    """Create authentic old-policy receipts for restart compatibility checks."""
    from pku_sync import media
    ask = lambda system, prompt: media._recovery_llm(None, cfg.notes_model, prompt, system, cfg)
    if strategy == 'topics':
        return notes.write_topic_notes(transcript, target, cfg, title,
            ask=ask, merge_ask=ask, source_context=sources)
    return notes.write_notes(transcript, target, cfg, title, ask=ask, source_context=sources)


def test_topic_workers_really_overlap_but_merge_keeps_original_order(tmp_path):
    import threading
    rendezvous = threading.Barrier(3)
    lecture = {"segments": [
        {"start": n * 600, "end": n * 600 + 25, "text": f"第{n+1}段讲述与例子。"}
        for n in range(3)]}
    def ask(system, prompt):
        packet = json.loads(prompt)
        assert system == notes.TOPIC_SYSTEM
        rendezvous.wait(timeout=3)
        return f"## 知识点{packet['part']}\n\n保留本段例子。"
    merged = []
    def merge(system, prompt):
        assert system == notes.MERGE_SYSTEM
        merged.append(json.loads(prompt))
        return "## 跨段知识点\n\n合并后的完整讲述与例子。"
    target = tmp_path / "notes.md"
    notes.write_topic_notes(lecture, target, config(), "课程", ask=ask, merge_ask=merge)
    assert merged[0]["original_transcript"] == lecture["segments"]
    assert [p["part"] for p in merged[0]["section_drafts"]] == [1, 2, 3]
    assert [p["note"].splitlines()[0] for p in merged[0]["section_drafts"]] == [
        "## 知识点1", "## 知识点2", "## 知识点3"]
    cache = tmp_path / ".notes-parts" / notes.TOPIC_POLICY
    records = [json.loads(p.read_text("utf-8")) for p in cache.glob("*.json")]
    middle = next(r for r in records if r["stage"] == "topic" and r["input"]["part"] == 2)
    assert middle["input"]["previous_context"] == lecture["segments"][:1]
    assert middle["input"]["following_context"] == lecture["segments"][2:]
    assert notes.topic_record_issues(target, lecture) == []
    assert "[" not in target.read_text("utf-8")  # optional time labels are not a quality gate


def test_topic_failure_stops_new_submissions_and_retry_reuses_paid_parts(tmp_path):
    lecture = {"segments": original()["segments"] + [{"start": 1200, "end": 1225, "text": "第三段。"}]}
    calls, fail = [], [True]
    def ask(system, prompt):
        part = json.loads(prompt)["part"]
        calls.append(part)
        if part == 2 and fail[0]:
            fail[0] = False
            raise RuntimeError("response lost")
        return f"知识点{part}，包含例子。"
    target = tmp_path / "notes.md"
    with pytest.raises(RuntimeError, match="response lost"):
        notes.write_topic_notes(lecture, target, config(), "课程", ask=ask,
            merge_ask=lambda *a: pytest.fail("incomplete parts merged"), workers=1)
    assert calls == [1, 2] and not target.exists()
    saved = {p: p.read_bytes() for p in (tmp_path / ".notes-parts" / notes.TOPIC_POLICY).glob("*.json")}
    notes.write_topic_notes(lecture, target, config(), "课程", ask=ask,
        merge_ask=lambda *a: "完整笔记。", workers=1)
    assert calls == [1, 2, 2, 3] and all(p.read_bytes() == value for p, value in saved.items())
    assert notes.topic_record_issues(target, lecture) == []


def test_topic_failure_drains_inflight_paid_response_before_returning(tmp_path):
    import threading
    lecture = {"segments": [{"start": n * 600, "end": n * 600 + 20, "text": f"原文{n}"}
        for n in range(4)]}
    both = threading.Barrier(2)
    failed, release = threading.Event(), threading.Event()
    calls, errors = [], []
    def ask(system, prompt):
        part = json.loads(prompt)["part"]
        calls.append(part)
        both.wait(timeout=3)
        if part == 1:
            failed.set()
            raise RuntimeError("owned first worker failed")
        assert release.wait(3)
        return "第二段已计费响应。"
    def run():
        try:
            notes.write_topic_notes(lecture, tmp_path / "notes.md", config(), "课程",
                ask=ask, merge_ask=lambda *a: pytest.fail("failed run merged"), workers=2)
        except Exception as exc:
            errors.append(exc)
    caller = threading.Thread(target=run)
    caller.start()
    try:
        assert failed.wait(3)
        caller.join(0.05)
        assert caller.is_alive()  # the owned second request must be collected
    finally:
        release.set()
        caller.join(3)
    assert not caller.is_alive() and len(errors) == 1 and "first worker failed" in str(errors[0])
    assert sorted(calls) == [1, 2] and not (tmp_path / "notes.md").exists()
    records = list((tmp_path / ".notes-parts" / notes.TOPIC_POLICY).glob("*.json"))
    assert len(records) == 1 and json.loads(records[0].read_text("utf-8"))["response"] == "第二段已计费响应。"


def test_topic_merge_failure_reuses_parts_and_completed_merge_on_file_retry(tmp_path, monkeypatch):
    parts, merges, failed = [], [], [False]
    target = tmp_path / "notes.md"
    def ask(system, prompt):
        parts.append(json.loads(prompt)["part"])
        return "分段讲述。"
    def merge(system, prompt):
        merges.append(prompt)
        if len(merges) == 1:
            raise RuntimeError("merge response lost")
        return "## 统一知识点\n\n有条件的解释与例子。"
    with pytest.raises(RuntimeError, match="merge response lost"):
        notes.write_topic_notes(original(), target, config(), "课程", ask=ask, merge_ask=merge)
    replace = Path.replace
    def fail_commit(path, destination):
        if destination == target:
            failed[0] = True
            raise OSError("disk full")
        return replace(path, destination)
    monkeypatch.setattr(Path, "replace", fail_commit)
    with pytest.raises(OSError, match="disk full"):
        notes.write_topic_notes(original(), target, config(), "课程", ask=ask, merge_ask=merge)
    assert failed[0] and sorted(parts) == [1, 2] and len(merges) == 2
    monkeypatch.setattr(Path, "replace", replace)
    notes.write_topic_notes(original(), target, config(), "课程",
        ask=lambda *a: pytest.fail("part billed again"), merge_ask=lambda *a: pytest.fail("merge billed again"))
    assert notes.topic_record_issues(target, original()) == []


def test_sourced_prompt_upgrade_preserves_paid_notes_and_reuses_parts(tmp_path, monkeypatch):
    sources=[{'title':'本课课件','locator':'第1页','text':'原课件内容','status':'readable'}]
    current=notes.SOURCED_MERGE_SYSTEM
    target=tmp_path/'old.md'
    monkeypatch.setattr(notes,'SOURCED_MERGE_SYSTEM',notes.LEGACY_SOURCED_MERGE_SYSTEM)
    notes.write_topic_notes(original(),target,config(),'课程',source_context=sources,
        ask=lambda *a:'已付费分段。',merge_ask=lambda *a:'原付费正文。')
    saved={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    monkeypatch.setattr(notes,'SOURCED_MERGE_SYSTEM',current)
    assert notes.topic_record_issues(target,original(),sources)==[]
    notes.write_topic_notes(original(),target,config(),'课程',source_context=sources,
        ask=lambda *a:pytest.fail('old part billed'),merge_ask=lambda *a:pytest.fail('old note billed'))
    calls=[]
    def merge(system,prompt):
        assert system==current
        calls.append(json.loads(prompt))
        return '修订后的正文。'
    fresh=tmp_path/'new.md'
    notes.write_topic_notes(original(),fresh,config(),'课程',source_context=sources,
        ask=lambda *a:pytest.fail('part billed again'),merge_ask=merge)
    assert len(calls)==1 and calls[0]['original_transcript']==original()['segments']
    assert len(calls[0]['section_drafts'])==2
    assert notes.topic_record_issues(fresh,original(),sources)==[]
    assert all(p.read_bytes()==raw for p,raw in saved.items())


def test_matched_courseware_is_delivered_in_full_to_merge_without_rebilling_topic_parts(tmp_path):
    # A saved older unsourced merge stays readable. A deliberate new output
    # can reuse its paid topic drafts while adding actual courseware input.
    first = tmp_path / "old.md"
    sources = [{"title":"课堂课件.pdf", "locator":"第54页", "text":"Cell Stem Cell\n" + "原课件" * 1200,
                "status":"partial", "path":"materials/private-local-path.pdf"},
               {"title":"课堂课件.pdf", "locator":"第55页", "text":"", "status":"unreadable"}]
    notes.write_topic_notes(original(), first, config(), "课程",
        ask=lambda *a: "已付费分段。", merge_ask=lambda *a: "旧合并正文。", source_context=None)
    saved = {p:p.read_bytes() for p in (tmp_path / ".notes-parts" / notes.TOPIC_POLICY).glob("*.json")}
    calls = []
    def merge(system,prompt):
        assert system == notes.SOURCED_MERGE_SYSTEM
        payload=json.loads(prompt); calls.append(payload)
        assert payload['original_transcript']==original()['segments']
        assert payload['courseware']==[{k:s.get(k,'') for k in ('title','locator','text','status')} for s in sources]
        assert len(payload['section_drafts'])==2
        return "结合课件整理的正文，不要求固定引用。"
    target=tmp_path/'new.md'
    notes.write_topic_notes(original(),target,config(),'课程',source_context=sources,
        ask=lambda *a:pytest.fail('saved topic billed again'),merge_ask=merge)
    assert len(calls)==1 and all(p.read_bytes()==raw for p,raw in saved.items())
    assert notes.topic_record_issues(first,original(),[])==[]
    assert notes.topic_record_issues(target,original(),sources)==[]
    assert notes.topic_record_issues(target,original())==[]
    notes.write_topic_notes(original(),target,config(),'课程',source_context=sources,
        ask=lambda *a:pytest.fail('part repeated'),merge_ask=lambda *a:pytest.fail('merge repeated'))
    assert len(calls)==1
    changed=[dict(s) for s in sources]; changed[0]['text']+='学生校正'
    assert notes.topic_record_issues(target,original(),changed)


def test_courseware_overflow_is_rejected_before_any_paid_part_without_cropping(tmp_path):
    with pytest.raises(RuntimeError,match='超过模型或传输容量'):
        notes.write_topic_notes(original(),tmp_path/'notes.md',config(),'课程',
            source_context=[{'title':'完整课件','text':'中文课件' * 90000,'status':'readable'}],
            ask=lambda *a:pytest.fail('oversized courseware billed'),
            merge_ask=lambda *a:pytest.fail('courseware silently cropped'))
    assert not (tmp_path/'.notes-parts').exists()


def test_impossible_whole_source_merge_is_rejected_before_any_charge(tmp_path):
    lecture = {"segments": [{"start": 0, "end": 1200, "text": "原文" * 45000}]}
    with pytest.raises(RuntimeError, match="超过模型或传输容量"):
        notes.write_topic_notes(lecture, tmp_path / "notes.md", config(), "课程",
            ask=lambda *a: pytest.fail("impossible merge billed parts"),
            merge_ask=lambda *a: pytest.fail("source truncated"), merge_model="qwen3.7-plus")
    assert not (tmp_path / ".notes-parts").exists()


@pytest.mark.parametrize('model',['qwen3.7-plus','qwen3.8-max','qwen3.8-max-0902'])
def test_topic_merge_and_transcript_capacity_accept_full_inputs_above_30000(model):
    payload={'transcript':[{'start':0,'end':1200,'text':'原文'*16000}]}
    expected=json.dumps(payload,ensure_ascii=False)
    assert notes._topic_capacity(notes.MERGE_SYSTEM,payload,model,{})==expected
    assert notes._capacity(payload,model,{})==expected


@pytest.mark.parametrize("change", ["source", "part", "merge", "body"])
def test_topic_receipt_integrity_without_semantic_grading(tmp_path, change):
    target = tmp_path / "notes.md"
    notes.write_topic_notes(original(), target, config(), "课程", ask=lambda *a: "分段知识点。",
        merge_ask=lambda *a: "正文没有固定引用格式，专业词保留待回听。")
    lecture = original()
    report = json.loads(notes.report_path(target).read_text("utf-8"))
    if change == "source":
        lecture["segments"][0]["text"] += "学生修正。"
    elif change == "body":
        target.write_text(target.read_text("utf-8") + "学生补充。", "utf-8")
    else:
        key = report["requests"][0] if change == "part" else report["merge_request"]
        path = tmp_path / ".notes-parts" / notes.TOPIC_POLICY / (key + ".json")
        record = json.loads(path.read_text("utf-8"))
        record["response"] += "外部修改。"
        path.write_text(json.dumps(record), "utf-8")
    assert notes.topic_record_issues(target, lecture)


@pytest.mark.parametrize("merge_model", ["qwen3.7-plus", "qwen3.8-max-0902", "unused-old-merge-selection"])
@pytest.mark.parametrize('via_settings',[False,True])
def test_topic_entry_keeps_selected_unit_model_and_skips_configured_merge_model(tmp_path, monkeypatch, merge_model, via_settings):
    from pku_sync import media, note_editor, knowledge_workflow
    cfg = config(); cfg.platform_token = "offline"; cfg.openai_api_key = ""
    if via_settings:
        cfg.notes_strategy='topics'; cfg.notes_merge_model=merge_model
    options={} if via_settings else {'strategy':'topics','merge_model':merge_model}
    calls = []
    def ask(llm, model, prompt, system, configured):
        calls.append((model, configured.notes_model, system))
        if system == knowledge_workflow.PLAN_SYSTEM:
            return json.dumps({'units': [
                {'title': '第一知识点', 'first_segment': 0, 'reference_segments': [], 'courseware_indices': []},
                {'title': '第二知识点', 'first_segment': 1, 'reference_segments': [], 'courseware_indices': []}]})
        if system == knowledge_workflow.MERGE_SYSTEM:
            return json.dumps({'sections': [{'title': '知识点', 'first_unit': 0,
                'last_unit': 1, 'body': '完整笔记正文。'}]})
        return "完整笔记正文。"
    monkeypatch.setattr(media, "_recovery_llm", ask)
    target = tmp_path / "notes.md"
    media.write_notes(original(), [], target, cfg, "课程", **options)
    assert len(calls) == 3
    assert [c[2] for c in calls] == [knowledge_workflow.PLAN_SYSTEM] + [knowledge_workflow.UNIT_SYSTEM] * 2
    assert all(c[:2] == ("qwen3.5-flash", "qwen3.5-flash") for c in calls)
    assert cfg.notes_model == "qwen3.5-flash"
    assert note_editor.review_record_issues(target, original()) == []
    media.write_notes(original(), [], target, cfg, "课程", strategy="topics", merge_model=merge_model)
    assert len(calls) == 3
    cfg.notes_model = 'unsupported-model'
    with pytest.raises(ValueError, match="尚未支持"):
        media.write_notes(original(), [], tmp_path / "unsupported.md", cfg, "课程",
            strategy="topics", merge_model="max")
    assert len(calls) == 3


def original():
    return {"segments":[{"start":0,"end":20,"text":"这是原讲述，含有可能和一般的限定。"},
        {"start":600,"end":625,"text":"老师的第二段例子。"}]}


def test_exact_source_order_one_request_per_window_and_no_model_quality_verdict(tmp_path):
    calls = []
    raw = ['# 原讲述\n\n老师说“可能”。', '## 例子\n\n仍需回听的内容。']
    references = [{"title":"完整课件.pdf","text":"另外的课件内容。","status":"readable"}]
    def ask(system, prompt):
        calls.append(json.loads(prompt))
        assert system == notes.SYSTEM and "JSON" in system
        return raw[len(calls)-1]
    target = tmp_path / "notes.md"
    assert notes.write_notes(original(), target, config(), "课程", ask=ask, source_context=references) == target
    assert len(calls) == 2 and [p["transcript"] for p in calls] == [[s] for s in original()["segments"]]
    assert calls[0]["previous_context"] == [] and calls[1]["previous_context"] == [original()["segments"][0]]
    assert all("draft" not in p and "source_reviews" not in p and "courseware" not in p for p in calls)
    assert target.read_text("utf-8") == "# 课程\n\n" + "\n\n".join(raw) + "\n"
    report = json.loads(notes.report_path(target).read_text("utf-8"))
    assert report["status"] == "organized" and report["content_complete"] is None
    assert report["assessment"] == "organization-not-content-certification"
    assert notes.record_issues(target, original(), references) == []


@pytest.mark.parametrize("raw", ['老师说“可能”。\n不采用固定引用写法。',
    '```json\n{"note":"这是老师举的代码例子"}\n```',
    '# 短标题\n\n一行课堂笔记。'])
def test_plain_text_is_retained_verbatim_without_format_repair_or_text_rules(tmp_path, raw):
    calls = []
    target = tmp_path / "notes.md"
    notes.write_notes({"segments":original()["segments"][:1]}, target, config(), "课程",
        ask=lambda *a: calls.append(a) or raw)
    assert len(calls) == 1 and raw in target.read_text("utf-8")
    records = list((tmp_path / ".notes-parts" / notes.POLICY).glob("*.json"))
    assert len(records) == 1 and json.loads(records[0].read_text("utf-8"))["response"] == raw


def test_completed_topic_is_not_resubmitted_as_a_full_previous_window(tmp_path):
    lecture = {"segments": [
        {"start": 0, "end": 10, "text": "先前已经完成的独立主题。"},
        {"start": 20, "end": 30, "text": "前一主题的详细例子。"},
        {"start": 400, "end": 420, "text": "上一段最后的解释。"},
        {"start": 430, "end": 450, "text": "衔接下一段的问句。"},
        {"start": 600, "end": 620, "text": "当前回答，继续本段讲述。"}]}
    calls = []
    def ask(system, prompt):
        packet = json.loads(prompt)
        calls.append(packet)
        return "保存当前段落。"
    target = tmp_path / "notes.md"
    notes.write_notes(lecture, target, config(), "课程", ask=ask)
    assert len(calls) == 2
    assert calls[0]["transcript"] == lecture["segments"][:4]
    assert calls[1]["transcript"] == lecture["segments"][4:]
    assert calls[1]["previous_context"] == lecture["segments"][2:4]
    assert list(calls[1])[-1] == "transcript"
    assert notes.record_issues(target, lecture) == []


def test_interrupted_later_window_reuses_original_paid_response(tmp_path):
    calls, failed = [], []
    def ask(system, prompt):
        packet = json.loads(prompt)
        calls.append(packet["part"])
        if packet["part"] == 2 and not failed:
            failed.append(1)
            raise RuntimeError("lost response")
        return f"第{packet['part']}段原始响应。"
    target = tmp_path / "notes.md"
    with pytest.raises(RuntimeError, match="lost response"):
        notes.write_notes(original(), target, config(), "课程", ask=ask)
    cache = tmp_path / ".notes-parts" / notes.POLICY
    saved = {p:p.read_bytes() for p in cache.iterdir()}
    assert len(saved) == 1 and not target.exists()
    notes.write_notes(original(), target, config(), "课程", ask=ask)
    assert calls == [1,2,2] and all(p.read_bytes() == b for p,b in saved.items())
    assert notes.record_issues(target, original()) == []


@pytest.mark.parametrize("raw", ["", "  \n\t"])
def test_empty_saved_response_is_not_rebilled_or_committed(tmp_path, raw):
    calls = []
    for _ in range(2):
        with pytest.raises(RuntimeError, match="没有返回文字"):
            notes.write_notes({"segments":original()["segments"][:1]}, tmp_path / "notes.md",
                config(), "课程", ask=lambda *a: calls.append(a) or raw)
    assert len(calls) == 1 and not (tmp_path / "notes.md").exists()


def test_existing_unbound_student_note_is_preserved_before_any_generation(tmp_path):
    target = tmp_path / "notes.md"
    target.write_bytes(b"student-original\n")
    with pytest.raises(RuntimeError, match="原文件保留"):
        notes.write_notes(original(), target, config(), "课程", ask=lambda *a:pytest.fail("existing note rewritten"))
    assert target.read_bytes() == b"student-original\n"


@pytest.mark.parametrize("change", ["source", "response", "body", "missing", "quality_flag"])
def test_changed_receipts_cannot_claim_saved_note_integrity(tmp_path, change):
    target = tmp_path / "notes.md"
    notes.write_notes(original(), target, config(), "课程", ask=lambda *a:"原始响应。")
    transcript = original()
    report_path = notes.report_path(target)
    report = json.loads(report_path.read_text("utf-8"))
    request = tmp_path / ".notes-parts" / notes.POLICY / (report["requests"][0] + ".json")
    if change == "source":
        transcript["segments"][0]["text"] = "已校正的原文。"
    elif change == "response":
        record = json.loads(request.read_text("utf-8"));record["response"] += "变化"
        request.write_text(json.dumps(record),"utf-8")
    elif change == "body":
        target.write_text(target.read_text("utf-8") + "变化", "utf-8")
    elif change == "missing":
        request.unlink()
    else:
        report["content_complete"] = True
        report_path.write_text(json.dumps(report), "utf-8")
    assert notes.record_issues(target, transcript)


def test_multibyte_capacity_is_checked_before_first_paid_window(tmp_path):
    calls=[]
    with pytest.raises(RuntimeError, match="任务未提交"):
        notes.write_notes(original(), tmp_path / "notes.md", config(), "课"*90000,
            ask=lambda *a:calls.append(a))
    assert calls == [] and not (tmp_path / ".notes-parts").exists()


@pytest.mark.parametrize("model", ["qwen3.5-flash", "qwen3.7-plus", "qwen3.8-max-0902", "qwen3.8-max"])
def test_large_individual_segment_keeps_every_original_character(tmp_path, model):
    text='"\\原讲述' * 7000
    transcript={"segments":[{"start":0,"end":1200,"text":text}]}
    calls=[]
    def ask(system,prompt):
        calls.append(json.loads(prompt));return "整理的文字。"
    target=tmp_path/"notes.md"
    notes.write_notes(transcript,target,config(model),"课程",ask=ask)
    assert ''.join(s['text'] for p in calls for s in p['transcript']) == text
    assert notes.record_issues(target,transcript) == []


@pytest.mark.parametrize('saved_strategy,legacy_prompt', [('transcript',False),('topics',False),('topics',True)])
def test_missing_final_note_restores_paid_output_after_restart(tmp_path, monkeypatch, saved_strategy, legacy_prompt):
    from pku_sync import media
    current=notes.SOURCED_MERGE_SYSTEM
    if legacy_prompt:
        monkeypatch.setattr(notes,'SOURCED_MERGE_SYSTEM',notes.LEGACY_SOURCED_MERGE_SYSTEM)
    cfg=config();cfg.platform_token='offline';cfg.openai_api_key=''
    sources=[{'title':'本课课件','text':'已关联来源','status':'readable'}]
    calls=[]
    monkeypatch.setattr(media,'_recovery_llm',lambda *a,**k:calls.append(a) or '已付费正文。')
    target=tmp_path/'notes.md'
    replace=Path.replace
    def fail(path,destination):
        if destination==target:raise OSError('disk full')
        return replace(path,destination)
    monkeypatch.setattr(Path,'replace',fail)
    with pytest.raises(OSError,match='disk full'):
        legacy_paid_note(original(),target,cfg,'课程',saved_strategy,sources)
    assert not target.exists() and notes.report_path(target).exists()
    saved={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    monkeypatch.setattr(Path,'replace',replace)
    monkeypatch.setattr(notes,'SOURCED_MERGE_SYSTEM',current)
    cfg.notes_strategy='transcript' if saved_strategy=='topics' else 'topics'
    cfg.notes_model='model-no-longer-selected';cfg.platform_token=''
    monkeypatch.setattr(media,'_recovery_llm',lambda *a,**k:pytest.fail('completed response billed again'))
    assert media.write_notes(original(),[],target,cfg,'new display title')==target
    assert target.read_text('utf-8')==notes._body('课程',['已付费正文。'] if saved_strategy=='topics' else ['已付费正文。']*2)
    check=notes.topic_record_issues if saved_strategy=='topics' else notes.record_issues
    assert check(target,original(),sources)==[]
    assert len(calls)==(3 if saved_strategy=='topics' else 2)
    assert all(p.read_bytes()==raw for p,raw in saved.items())


@pytest.mark.parametrize('saved_strategy',['transcript','topics'])
@pytest.mark.parametrize('damage',['source','sources','response','report'])
def test_missing_note_with_changed_receipts_never_bills_or_overwrites(tmp_path, monkeypatch, saved_strategy, damage):
    from pku_sync import media
    cfg=config();cfg.platform_token='offline';cfg.openai_api_key=''
    sources=[{'title':'原课件','text':'已提取文字','status':'readable'}]
    monkeypatch.setattr(media,'_recovery_llm',lambda *a,**k:'原付费正文。')
    target=tmp_path/'notes.md'
    legacy_paid_note(original(),target,cfg,'课程',saved_strategy,sources)
    target.unlink()
    transcript=original();supplied=None
    report_path=notes.report_path(target)
    report=json.loads(report_path.read_text('utf-8'))
    if damage=='source':
        transcript['segments'][0]['text']+='修改后的原转写'
    elif damage=='sources':
        supplied=[]
    elif damage=='response':
        cache=tmp_path/'.notes-parts'/report['policy']/(report['requests'][0]+'.json')
        record=json.loads(cache.read_text('utf-8'));record['response']+='外部改动'
        cache.write_text(json.dumps(record),'utf-8')
    else:
        report['policy']='unknown-policy'
        report_path.write_text(json.dumps(report),'utf-8')
    saved={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    cfg.notes_strategy='transcript' if saved_strategy=='topics' else 'topics'
    monkeypatch.setattr(media,'_recovery_llm',lambda *a,**k:pytest.fail('damaged output rebilled'))
    with pytest.raises(RuntimeError,match='原文件保留'):
        media.write_notes(transcript,[],target,cfg,'课程',source_context=supplied)
    assert not target.exists() and all(p.read_bytes()==raw for p,raw in saved.items())


def test_failed_final_file_commit_reuses_all_saved_responses(tmp_path, monkeypatch):
    calls=[]
    target=tmp_path/"notes.md"
    replace=Path.replace
    def fail(path,destination):
        if destination == target:raise OSError("disk full")
        return replace(path,destination)
    monkeypatch.setattr(Path,"replace",fail)
    with pytest.raises(OSError,match="disk full"):
        notes.write_notes(original(),target,config(),"课程",ask=lambda *a:calls.append(a) or "原响应。")
    assert len(calls)==2 and notes.report_path(target).is_file() and not target.exists()
    monkeypatch.setattr(Path,"replace",replace)
    notes.write_notes(original(),target,config(),"课程",ask=lambda *a:pytest.fail("paid response repeated"))
    assert notes.record_issues(target,original()) == []


@pytest.mark.parametrize("saved_strategy", ["transcript", "topics"])
def test_current_media_paths_preserve_prose_and_reuse_own_paid_policy(tmp_path, monkeypatch, saved_strategy):
    from pku_sync import media, note_editor, knowledge_workflow
    cfg=config();cfg.platform_token="offline";cfg.openai_api_key=""
    calls=[]
    def ask(llm, model, prompt, system, configured):
        calls.append(json.loads(prompt))
        assert system in ({notes.SYSTEM} if saved_strategy == "transcript"
                          else {knowledge_workflow.PLAN_SYSTEM, knowledge_workflow.UNIT_SYSTEM, knowledge_workflow.MERGE_SYSTEM})
        if system == knowledge_workflow.PLAN_SYSTEM:
            return json.dumps({'units': [
                {'title':'第一知识点', 'first_segment':0, 'reference_segments':[], 'courseware_indices':[]},
                {'title':'第二知识点', 'first_segment':1, 'reference_segments':[], 'courseware_indices':[]}]})
        if system == knowledge_workflow.MERGE_SYSTEM:
            return json.dumps({'sections':[{'title':'知识点', 'first_unit':0,
                'last_unit':1, 'body':'老师说“通常”。具体含混处待回听。'}]})
        return '老师说“通常”。具体含混处待回听。'
    monkeypatch.setattr(media,"_recovery_llm",ask)
    monkeypatch.setattr(note_editor,"write_notes",lambda *a,**k:pytest.fail("old multi-stage editor called"))
    target=tmp_path/"notes.md"
    sources=[{"title":"原课件", "locator":"第1页", "text":"本讲内容", "status":"readable"}]
    media.write_notes(original(),[],target,cfg,"课程",strategy=saved_strategy,source_context=sources)
    expected=2 if saved_strategy == "transcript" else 3
    assert len(calls)==expected and note_editor.review_record_issues(target,original(),sources)==[]
    saved={path:path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    # A restart has default settings and may not have fetched courseware.
    # Paid output keeps its own policy, even after the selected model changes.
    cfg.notes_strategy="topics" if saved_strategy == "transcript" else "transcript"
    cfg.notes_model="model-no-longer-selected"
    cfg.notes_merge_model="model-no-longer-selected"
    cfg.platform_token=""
    media.write_notes(original(),[],target,cfg,"课程")
    direct=notes.write_notes if saved_strategy == "transcript" else knowledge_workflow.write_notes
    callbacks={"ask":lambda *a:pytest.fail("paid response repeated")}
    if saved_strategy == "topics":
        callbacks["merge_ask"]=callbacks["ask"]
    assert direct(original(),target,cfg,"课程",**callbacks)==target
    with pytest.raises(RuntimeError,match="原文件保留"):
        media.write_notes(original(),[],target,cfg,"课程",source_context=[])
    assert len(calls)==expected and all(path.read_bytes()==raw for path,raw in saved.items())


def test_publication_checks_receipts_without_needing_model_completeness(tmp_path, monkeypatch):
    from pku_sync.panel import recordings_api, transcript_review
    target=tmp_path/"notes.md"
    notes.write_notes(original(),target,config(),"课程",ask=lambda *a:'可读正文，待回听。')
    (tmp_path/"transcript.json").write_text(json.dumps(original()),"utf-8")
    monkeypatch.setattr(transcript_review,"review_pending",lambda job:False)
    monkeypatch.setattr(recordings_api,"assignment_catalog_publish_issues",lambda job:[])
    job=SimpleNamespace(directory=tmp_path)
    assert recordings_api.note_publish_issues(job,target)==[]
    changed=original();changed['segments'][0]['text']='校正后的讲述。'
    (tmp_path/"transcript.json").write_text(json.dumps(changed),"utf-8")
    assert recordings_api.note_publish_issues(job,target)


def test_default_cloud_path_recovers_lost_result_without_second_billable_job(tmp_path, monkeypatch):
    import httpx
    from pku_sync import media, platform
    cfg=config();cfg.platform_token="offline";cfg.openai_api_key=""
    cfg.data_dir=tmp_path;cfg.cloud_transcribe_url="https://relay.example";cfg.llm_timeout=600
    sent,jobs,lost=[],{},[]
    def post(url,**kwargs):
        body=kwargs['json'];key=body['idempotency_key'];sent.append(body);jobs.setdefault(key,body)
        return httpx.Response(202,json={'job_id':key})
    def get(url,**kwargs):
        key=url.rsplit('/',1)[-1];part=json.loads(jobs[key]['user'])['part']
        content=f'第{part}段的原始响应。'
        if part==2 and not lost:
            lost.append(key);content=None
        return httpx.Response(200,json={'job_id':key,'status':'succeeded','content':content,'points_charged':.01})
    monkeypatch.setattr(platform,'_post',post);monkeypatch.setattr(platform,'_get',get)
    target=tmp_path/'notes.md'
    with pytest.raises(platform.PlatformError,match='云端返回异常'):
        media.write_notes(original(),[],target,cfg,'课程')
    assert not target.exists() and len(jobs)==2
    media.write_notes(original(),[],target,cfg,'课程')
    assert len(sent)==3 and len(jobs)==2 and sent[1]==sent[2]
    assert notes.record_issues(target,original())==[]


@pytest.mark.parametrize("responses, expected", [
    (["正文一。", " \n<!-- 本段无新增授课内容 -->\n", "正文二。"], "正文一。\n\n正文二。"),
    (["<!-- 本段无新增授课内容 -->"], "<!-- 本段无新增授课内容 -->"),
])
def test_assembly_omits_break_placeholders_unless_all_parts_are_empty(responses, expected):
    assert notes._body("课程", responses) == "# 课程\n\n" + expected + "\n"
