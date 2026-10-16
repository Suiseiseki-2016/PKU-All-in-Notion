import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from pku_sync import knowledge_workflow as notes
from pku_sync.note_editor import report_path


def config():
    return SimpleNamespace(notes_model='qwen3.5-flash', pku_username='isolated-student',
        _metered_operation_context={'purpose': 'lecture-notes', 'source_id': 'pku:test:source'})


def lecture():
    return {'segments': [{'start': 6326.1, 'end': 6400, 'text': '最初的解释与条件。'},
        {'start': 6500, 'end': 6610, 'text': '实验步骤与观察。'},
        {'start': 6997.25, 'end': 7013.1, 'text': '不同结果与个人经历。'}]}


def plan(total=3):
    return json.dumps({'units': [{'title': f'知识点{i}', 'first_segment': i,
        'reference_segments': [], 'courseware_indices': []}
        for i in range(total)]})


def merge(total=3, body='原讲述的详细正文，不需要固定引用。'):
    return json.dumps({'sections': [{'title': '相邻知识点', 'first_unit': 0,
        'last_unit': total - 1, 'body': body}]})


def normal(system, prompt):
    if system == notes.PLAN_SYSTEM:
        return plan()
    return f"第{json.loads(prompt)['unit']}个知识点的原始响应。"


def test_full_lecture_merge_keeps_every_owned_original_once_and_restores_paid_output(tmp_path, monkeypatch):
    original = {'segments': [
        {'start': i * 6.0, 'end': i * 6.0 + 5,
         'text': '这段完整的原始课堂讲述包括实验方法、对照条件及可能的研究结果。'}
        for i in range(1087)]}
    sources = [{'title': '同课件', 'locator': f'第{i + 1}页', 'status': 'readable',
                'text': '本页是用于核对原讲述的课堂材料。' * 5} for i in range(61)]
    units = [{'title': '前半讲述', 'first_segment': 0, 'last_segment': 599,
              'reference_segments': [], 'courseware_indices': []},
             {'title': '后半讲述', 'first_segment': 600, 'last_segment': 1086,
              'reference_segments': [], 'courseware_indices': []}]
    cfg = config()
    from pku_sync.platform import llm_request_body_size, LLM_REQUEST_MAX_BYTES
    duplicate = notes._merge_input('课程', original['segments'], units,
        [{'response': '前半草稿'}, {'response': '后半草稿'}], notes._courseware(sources), 'identity')
    assert llm_request_body_size('notes', notes.MERGE_SYSTEM, json.dumps(duplicate, ensure_ascii=False),
                                model=cfg.notes_model) > LLM_REQUEST_MAX_BYTES
    target, calls = tmp_path / 'notes.md', []
    def ask(system, prompt):
        payload = json.loads(prompt)
        if system == notes.PLAN_SYSTEM:
            calls.append('plan')
            return json.dumps({'units': units}, ensure_ascii=False)
        calls.append(payload['unit'])
        owned = units[payload['unit']]
        assert payload['original_transcript'] == original['segments'][
            owned['first_segment']:owned['last_segment'] + 1]
        return f"第{payload['unit']}份付费草稿。"
    def combine(system, prompt):
        calls.append('merge')
        payload = json.loads(prompt)
        assert payload['source_layout'] == 'per-unit-original-transcript'
        assert 'original_transcript' not in payload
        assert payload['source_segment_count'] == 1087
        assert [s for unit in payload['unit_drafts'] for s in unit['original_transcript']] == original['segments']
        assert payload['courseware'] == notes._courseware(sources)
        assert llm_request_body_size('notes', system, prompt, model=cfg.notes_model) <= LLM_REQUEST_MAX_BYTES
        return merge(total=2, body='模型返回的合并正文，原始文字保留。')
    replace = Path.replace
    def fail_commit(path, dest):
        if dest == target:
            raise OSError('disk full after paid merge')
        return replace(path, dest)
    monkeypatch.setattr(Path, 'replace', fail_commit)
    with pytest.raises(OSError, match='disk full after paid merge'):
        notes.write_notes(original, target, cfg, '课程', ask=ask, merge_ask=combine,
                          source_context=sources, workers=1, assembly="model-merge")
    assert calls == ['plan', 0, 1, 'merge'] and not target.exists()
    paid = {p: p.read_bytes() for p in tmp_path.rglob('*.json')}
    monkeypatch.setattr(Path, 'replace', replace)
    notes.write_notes(original, target, cfg, '课程',
        ask=lambda *a: pytest.fail('rebilled planning or editing'),
        merge_ask=lambda *a: pytest.fail('rebilled merge'), source_context=sources, assembly="model-merge")
    assert all(p.read_bytes() == raw for p, raw in paid.items())
    assert notes.saved_sections(target, original, sources)[0] == target.read_text('utf-8')


def test_unavoidably_oversized_complete_source_stops_before_any_paid_request(tmp_path):
    original = {'segments': [{'start': 0, 'end': 6000, 'text': '不裁剪原始讲述' * 20000}]}
    target = tmp_path / 'notes.md'
    before = json.dumps(original, ensure_ascii=False)
    with pytest.raises(RuntimeError, match='输入超过'):
        notes.write_notes(original, target, config(), '课程',
            ask=lambda *a: pytest.fail('paid work before complete-source capacity check'),
            merge_ask=lambda *a: pytest.fail('oversized source was sent'))
    assert json.dumps(original, ensure_ascii=False) == before
    assert not target.exists() and not list(tmp_path.rglob('*.json'))


def test_parallel_units_receive_only_selected_original_lookups_then_merge_in_source_order(tmp_path):
    both = threading.Barrier(3)
    original = lecture()
    sources = [{'title': '对应原课件', 'locator': '第54页', 'text': '原课件全部文字。',
        'status': 'partial', 'path': 'never-send-local-path.pdf'},
        {'title': '图像页', 'locator': '第55页', 'text': '', 'status': 'unreadable'}]
    seen = []
    def ask(system, prompt):
        payload = json.loads(prompt)
        if system == notes.PLAN_SYSTEM:
            assert payload['indexed_transcript'] == [
                {'segment_index': i, 'segment': s} for i,s in enumerate(original['segments'])]
            assert payload['last_segment_index'] == len(original['segments']) - 1
            rows=json.loads(plan())['units']
            rows[0]['reference_segments']=[2]
            rows[1]['courseware_indices']=[0]
            return json.dumps({'units':rows})
        assert payload['reference_context'] == ([original['segments'][2]] if payload['unit']==0 else [])
        assert payload['original_transcript'] == [original['segments'][payload['unit']]]
        assert payload['courseware'] == ([{k: sources[0].get(k, '') for k in ('title','locator','text','status')}]
                                         if payload['unit']==1 else [])
        assert list(payload)[-1]=='original_transcript'
        both.wait(3)
        return f"原始正文{payload['unit']}，老师说可能。"
    def combine(system, prompt):
        seen.append(json.loads(prompt))
        return merge(body='## 模型自己写的错误时间 [70:00–75:00]\n\n仍保留正文。')
    target = tmp_path / 'notes.md'
    notes.write_notes(original, target, config(), '课程', ask=ask, merge_ask=combine, source_context=sources, assembly="model-merge")
    assert [r['unit'] for r in seen[0]['unit_drafts']] == [0, 1, 2]
    assert [r['note'] for r in seen[0]['unit_drafts']] == [f'原始正文{i}，老师说可能。' for i in range(3)]
    assert seen[0]['original_transcript'] == original['segments']
    assert [r['original_transcript'] for r in seen[0]['unit_drafts']]==[[s] for s in original['segments']]
    body, rendered = notes.saved_sections(target, original, sources)
    assert '## 相邻知识点 [105:26–116:53]' in body
    assert rendered[0]['start'] == 6326.1 and rendered[0]['end'] == 7013.1
    assert '[70:00–75:00]' in body  # raw prose is never rewritten by lexical rules
    assert notes.record_issues(target, original, sources) == []


def test_unit_failure_stops_new_work_and_retry_reuses_completed_paid_parts(tmp_path):
    calls, fail = [], [True]
    def ask(system, prompt):
        if system == notes.PLAN_SYSTEM:
            calls.append('plan')
            return plan()
        i = json.loads(prompt)['unit']
        calls.append(i)
        if i == 1 and fail[0]:
            fail[0] = False
            raise RuntimeError('lost-result')
        return f'已付费正文{i}'
    target = tmp_path / 'notes.md'
    with pytest.raises(RuntimeError, match='lost-result'):
        notes.write_notes(lecture(), target, config(), '课程', ask=ask,
            merge_ask=lambda *a: pytest.fail('incomplete merge'), workers=1)
    assert calls == ['plan', 0, 1]
    saved = {p: p.read_bytes() for p in tmp_path.rglob('*.json')}
    notes.write_notes(lecture(), target, config(), '课程', ask=ask,
        merge_ask=lambda *a: merge(), workers=1)
    assert calls == ['plan', 0, 1, 1, 2]
    assert all(p.read_bytes() == raw for p, raw in saved.items())
    assert notes.record_issues(target, lecture()) == []


def test_failure_waits_for_owned_inflight_response_and_never_schedules_next_unit(tmp_path):
    both, failed, release = threading.Barrier(2), threading.Event(), threading.Event()
    calls, errors = [], []
    def ask(system, prompt):
        if system == notes.PLAN_SYSTEM:
            return plan()
        i = json.loads(prompt)['unit']; calls.append(i)
        both.wait(3)
        if i == 0:
            failed.set()
            raise RuntimeError('first failed')
        assert release.wait(3)
        return '第二个已付费响应。'
    def run():
        try:
            notes.write_notes(lecture(), tmp_path/'notes.md', config(), '课程', ask=ask,
                merge_ask=lambda *a: pytest.fail('failed run merged'), workers=2)
        except Exception as exc:
            errors.append(exc)
    caller = threading.Thread(target=run); caller.start()
    try:
        assert failed.wait(3)
        caller.join(.05)
        assert caller.is_alive()
    finally:
        release.set(); caller.join(3)
    assert not caller.is_alive() and len(errors) == 1 and sorted(calls) == [0, 1]
    saved = [json.loads(p.read_text('utf-8')) for p in (tmp_path/'.notes-parts'/notes.POLICY).glob('*.json')]
    assert [r['response'] for r in saved if r['stage'] == 'unit'] == ['第二个已付费响应。']


def test_disk_full_after_merge_restores_exact_output_without_new_model_call(tmp_path, monkeypatch):
    target = tmp_path/'notes.md'
    replace = Path.replace
    def fail_commit(path, dest):
        if dest == target:
            raise OSError('disk full')
        return replace(path, dest)
    monkeypatch.setattr(Path, 'replace', fail_commit)
    with pytest.raises(OSError, match='disk full'):
        notes.write_notes(lecture(), target, config(), '课程', ask=normal, merge_ask=lambda *a: merge(), assembly="model-merge")
    assert not target.exists() and report_path(target).exists()
    saved = {p:p.read_bytes() for p in tmp_path.rglob('*.json')}
    monkeypatch.setattr(Path, 'replace', replace)
    from pku_sync import media
    cfg = config(); cfg.platform_token=''; cfg.notes_strategy='transcript'; cfg.notes_model='old-selection'
    assert media.write_notes(lecture(), [], target, cfg, '课程') == target
    assert notes.record_issues(target, lecture()) == []
    assert all(p.read_bytes() == raw for p, raw in saved.items())


@pytest.mark.parametrize('bad', [
    {'units':[{'title':'布尔编号', 'first_segment':False, 'reference_segments':[], 'courseware_indices':[]}]},
    {'units':[{'title':'越界', 'first_segment':3, 'reference_segments':[], 'courseware_indices':[]}]},
    {'units':[{'title':'重复', 'first_segment':0, 'reference_segments':[], 'courseware_indices':[]},
              {'title':'重叠','first_segment':0,'reference_segments':[], 'courseware_indices':[]}]}])
def test_invalid_plan_has_one_durable_repair_before_any_unit_work(tmp_path, bad):
    calls=[]
    def ask(system, prompt):
        calls.append(system)
        if system == notes.PLAN_SYSTEM:
            return json.dumps(bad)
        if system == notes.REPAIR_SYSTEM:
            return plan()
        return '完整知识点正文。'
    target=tmp_path/'notes.md'
    notes.write_notes(lecture(), target, config(), '课程', ask=ask, merge_ask=lambda *a: merge())
    assert calls[:2] == [notes.PLAN_SYSTEM, notes.REPAIR_SYSTEM] and len(calls) == 5
    assert notes.record_issues(target, lecture()) == []
    notes.write_notes(lecture(), target, config(), '课程', ask=lambda *a:pytest.fail('rebilled'),
        merge_ask=lambda *a:pytest.fail('rebilled'))


def test_plan_repair_with_leading_podium_segments_anchors_first_unit_to_zero_and_reuses_paid_cache(tmp_path):
    original = {'segments': [
        {'start': 65.96, 'end': 75.86, 'text': '欢迎使用多媒体教学系统，请开启讲台设备。'},
        {'start': 71.77, 'end': 75.87, 'text': '讲台设备已准备就绪。'},
        {'start': 310.12, 'end': 321.74, 'text': '下面继续讲解可靠数据传输协议 RDT3.0。'}]}
    raw = json.dumps({'units': [{'title': 'RDT3.0 可靠数据传输协议', 'first_segment': 2,
        'reference_segments': [], 'courseware_indices': []}]}, ensure_ascii=False)
    target, cfg, calls = tmp_path / 'notes.md', config(), []
    cache = tmp_path / '.notes-parts' / notes.POLICY
    identity = notes.fingerprint({'campus_account': cfg.pku_username,
                                  'operation': cfg._metered_operation_context})
    payload = notes._plan_input('课程', original['segments'], [], identity)

    def paid_plan(system, prompt):
        calls.append(system)
        assert system in {notes.PLAN_SYSTEM, notes.REPAIR_SYSTEM}
        return raw

    # Reproduce the old failure and leave both already-paid responses on disk.
    with pytest.raises(RuntimeError, match='未能恢复'):
        notes._structured(cache, 'plan', payload, cfg.notes_model, identity,
            cfg._metered_operation_context, paid_plan,
            lambda response: notes._units(response, len(original['segments']), 0))
    assert calls == [notes.PLAN_SYSTEM, notes.REPAIR_SYSTEM]
    paid = {path: path.read_bytes() for path in cache.glob('*.json')}
    assert len(paid) == 2

    def ask(system, prompt):
        calls.append(system)
        assert system == notes.UNIT_SYSTEM
        unit = json.loads(prompt)
        assert unit['unit'] == 0
        assert unit['original_transcript'] == original['segments']
        return 'RDT3.0 的完整课堂讲解。'

    def combine(system, prompt):
        calls.append(system)
        assert system == notes.MERGE_SYSTEM
        unit = json.loads(prompt)['unit_drafts'][0]
        assert (unit['first_segment'], unit['last_segment']) == (0, 2)
        assert unit['original_transcript'] == original['segments']
        return merge(total=1)

    assert notes.write_notes(original, target, cfg, '课程', ask=ask, merge_ask=combine,
                             assembly='model-merge') == target
    assert calls == [notes.PLAN_SYSTEM, notes.REPAIR_SYSTEM, notes.UNIT_SYSTEM, notes.MERGE_SYSTEM]
    assert all(path.read_bytes() == content for path, content in paid.items())
    report = json.loads(report_path(target).read_text('utf-8'))
    primary, units = notes._resolved(cache, report['plan_request'], report['plan_response'], 'plan',
        lambda response: notes._units(response, 3, 0, allow_leading_gap=True),
        repair_validate=lambda response: notes._units(response, 3, 0, allow_leading_gap=True))
    assert primary['response'] == raw
    assert report['plan_request'] == report['plan_response']
    assert units[0]['first_segment'] == 0 and units[0]['last_segment'] == 2
    assert notes.record_issues(target, original) == []
    body = target.read_bytes()
    saved = {path: path.read_bytes() for path in tmp_path.rglob('*.json')}
    target.unlink()

    def forbidden(*args, **kwargs):
        pytest.fail('already-paid knowledge notes were submitted again')

    assert notes.write_notes(original, target, cfg, '课程', ask=forbidden, merge_ask=forbidden,
                             assembly='model-merge') == target
    assert target.read_bytes() == body
    assert all(path.read_bytes() == content for path, content in saved.items())
    assert notes.record_issues(target, original) == []


def test_failed_merge_format_repair_never_rebills_primary_response(tmp_path):
    merges=[]
    def combine(system, prompt):
        merges.append(system)
        return 'not-json'
    target=tmp_path/'notes.md'
    for _ in range(2):
        with pytest.raises(RuntimeError,match='未能恢复'):
            notes.write_notes(lecture(), target, config(), '课程', ask=normal, merge_ask=combine, assembly="model-merge")
    assert merges == [notes.MERGE_SYSTEM, notes.REPAIR_SYSTEM] and not target.exists()


def test_standard_json_fence_and_bare_array_do_not_trigger_paid_format_repair(tmp_path):
    calls=[]
    def ask(system,prompt):
        calls.append(system)
        if system==notes.PLAN_SYSTEM:
            return '```json\n'+json.dumps(json.loads(plan())['units'])+'\n```'
        return '原始正文。'
    target=tmp_path/'notes.md'
    body='```python\nprint("课堂代码例子")\n```\n\n正文仍保留。'
    notes.write_notes(lecture(),target,config(),'课程',ask=ask,
        merge_ask=lambda *a:'```json\n'+merge(body=body)+'\n```', assembly="model-merge")
    assert calls==[notes.PLAN_SYSTEM]+[notes.UNIT_SYSTEM]*3
    assert body in target.read_text('utf-8')
    assert notes.record_issues(target,lecture())==[]


def test_explicit_indices_repair_out_of_range_guess_without_changing_source(tmp_path):
    sent=[]
    def ask(system,prompt):
        payload=json.loads(prompt);sent.append((system,payload))
        if system==notes.PLAN_SYSTEM:
            return json.dumps({'units':[{'title':'错误计数','first_segment':30,
                                        'reference_segments':[], 'courseware_indices':[]}]})
        if system==notes.REPAIR_SYSTEM:
            assert payload['original_input']['last_segment_index']==2
            assert [s['segment_index'] for s in payload['original_input']['indexed_transcript']]==[0,1,2]
            return plan()
        return '已整理原讲述。'
    target=tmp_path/'notes.md'
    notes.write_notes(lecture(),target,config(),'课程',ask=ask,merge_ask=lambda *a:merge())
    assert sent[0][1]['indexed_transcript']==[
        {'segment_index':i,'segment':row} for i,row in enumerate(lecture()['segments'])]
    assert notes.record_issues(target,lecture())==[]


@pytest.mark.parametrize('change',['source','unit','merge','body'])
def test_provenance_changes_cannot_claim_paid_result_integrity(tmp_path, change):
    target=tmp_path/'notes.md'
    notes.write_notes(lecture(), target, config(), '课程', ask=normal, merge_ask=lambda *a:merge(), assembly="model-merge")
    original=lecture(); report=json.loads(report_path(target).read_text('utf-8'))
    if change=='source': original['segments'][0]['text']+='学生改正。'
    elif change=='body': target.write_text(target.read_text('utf-8')+'外部修改。','utf-8')
    else:
        key=report['requests'][0] if change=='unit' else report['merge_request']
        path=tmp_path/'.notes-parts'/notes.POLICY/(key+'.json')
        row=json.loads(path.read_text('utf-8')); row['response']+='外部修改。'
        path.write_text(json.dumps(row),'utf-8')
    assert notes.record_issues(target, original)


def test_impossible_complete_source_merge_refuses_before_first_charge(tmp_path):
    original={'segments':[{'start':0,'end':1200,'text':'原讲述'*30000}]}
    with pytest.raises(RuntimeError,match='超过模型或传输容量'):
        notes.write_notes(original, tmp_path/'notes.md', config(), '课程',
            ask=lambda *a:pytest.fail('oversized billed'), merge_ask=lambda *a:pytest.fail('oversized billed'),
            merge_model='qwen3.7-plus', assembly="model-merge")
    assert not (tmp_path/'.notes-parts').exists()


def test_large_merge_format_repair_keeps_full_raw_body_and_restores_without_rebilling(tmp_path, monkeypatch):
    original = {'segments': [{'start': i * 6.0, 'end': i * 6.0 + 5,
        'text': '这段完整的原始课堂讲述包括实验方法、对照条件及可能的研究结果。'}
        for i in range(1087)]}
    sources = [{'title': '原课件', 'locator': f'第{i + 1}页', 'status': 'readable',
                'text': '本页是用于核对原讲述的课堂材料。' * 5} for i in range(61)]
    body = '老师说"课堂原话"，可能还需要比较实验条件。\n' * 750
    valid = json.dumps(json.loads(merge(1, body)), ensure_ascii=False)
    raw = valid.replace('\\"课堂原话\\"', '"课堂原话"')
    assert raw != valid
    target, calls = tmp_path / 'notes.md', []
    def ask(system, prompt):
        calls.append(system)
        if system == notes.PLAN_SYSTEM:
            return json.dumps({'units': [{'title': '完整讲述', 'first_segment': 0,
                'reference_segments': [], 'courseware_indices': []}]})
        return body
    def combine(system, prompt):
        payload = json.loads(prompt)
        calls.append(system)
        if system == notes.MERGE_SYSTEM:
            return raw
        assert system == notes.REPAIR_SYSTEM
        assert payload['repair_layout'] == 'structure-only'
        assert payload['raw_response'] == raw
        assert payload['original_input'] == {'unit_drafts': [{'unit': 0, 'title': '完整讲述',
            'first_segment': 0, 'last_segment': 1086}]}
        primary = [json.loads(p.read_text('utf-8')) for p in (tmp_path/'.notes-parts'/notes.POLICY).glob('*.json')
                   if json.loads(p.read_text('utf-8'))['stage'] == 'merge'][0]
        assert payload['original_request_sha256'] == primary['request_sha256']
        from pku_sync.platform import llm_request_body_size, LLM_REQUEST_MAX_BYTES
        old = notes._repair_input('merge', primary['input'], raw, notes.POLICY, primary['request_sha256'])
        assert llm_request_body_size('notes', notes.REPAIR_SYSTEM, json.dumps(old, ensure_ascii=False),
                                    model=config().notes_model) > LLM_REQUEST_MAX_BYTES
        assert llm_request_body_size('notes', notes.REPAIR_SYSTEM, prompt,
                                    model=config().notes_model) <= LLM_REQUEST_MAX_BYTES
        return valid
    real_replace = Path.replace
    def disk_full(path, other):
        if Path(other) == target:
            raise OSError('disk full after paid format recovery')
        return real_replace(path, other)
    monkeypatch.setattr(Path, 'replace', disk_full)
    with pytest.raises(OSError, match='disk full'):
        notes.write_notes(original, target, config(), '课程', ask=ask, merge_ask=combine, source_context=sources, assembly="model-merge")
    saved = {p: p.read_bytes() for p in (tmp_path/'.notes-parts'/notes.POLICY).glob('*.json')}
    assert calls == [notes.PLAN_SYSTEM, notes.UNIT_SYSTEM, notes.MERGE_SYSTEM, notes.REPAIR_SYSTEM]
    monkeypatch.setattr(Path, 'replace', real_replace)
    def forbidden(*a, **k):
        pytest.fail('completed paid format recovery was submitted again')
    assert notes.write_notes(original, target, config(), '课程', ask=forbidden, merge_ask=forbidden,
                             source_context=sources, assembly="model-merge") == target
    assert body in target.read_text('utf-8')
    assert notes.record_issues(target, original, sources) == []
    assert all(p.read_bytes() == raw_bytes for p, raw_bytes in saved.items())
    report = json.loads(report_path(target).read_text('utf-8'))
    repair_path = tmp_path/'.notes-parts'/notes.POLICY/(report['merge_response']+'.json')
    repair = json.loads(repair_path.read_text('utf-8'))
    repair['input']['original_request_sha256'] = '0' * 64
    repair_path.write_text(json.dumps(repair), 'utf-8')
    assert notes.record_issues(target, original, sources)


def test_large_plan_format_repair_retains_all_source_indices_and_paid_response():
    from pku_sync.platform import llm_request_body_size, LLM_REQUEST_MAX_BYTES
    payload = notes._plan_input('课程', [{'start': i, 'end': i + 1, 'text': '原始课堂讲述。' * 7}
        for i in range(1087)], [{'title': '原课件', 'text': '核对材料。', 'locator': '第1页', 'status': 'readable'}], 'identity')
    raw = '模型已经生成的完整知识点划分响应。' * 2500
    primary = {'input': payload, 'response': raw, 'request_sha256': notes._key(
        'plan', payload, config().notes_model, 'identity')}
    repair = notes._repair_payload(Path(notes.POLICY), 'plan', primary, config().notes_model, {})
    assert repair['repair_layout'] == 'structure-only' and repair['raw_response'] == raw
    assert repair['original_request_sha256'] == primary['request_sha256']
    assert repair['original_input']['indexed_transcript'] == [{'segment_index': i} for i in range(1087)]
    assert repair['original_input']['last_segment_index'] == 1086
    assert repair['original_input']['courseware'] == [{'courseware_index': 0}]
    assert llm_request_body_size('notes', notes.REPAIR_SYSTEM, json.dumps(repair, ensure_ascii=False),
                                model=config().notes_model) <= LLM_REQUEST_MAX_BYTES


def test_images_follow_saved_source_even_with_false_model_times_and_student_heading(tmp_path, monkeypatch):
    from pku_sync.panel import note_images
    from pku_sync.notion import markdown_to_blocks
    target=tmp_path/'notes.md'
    notes.write_notes(lecture(), target, config(), '课程', ask=normal,
        merge_ask=lambda *a:merge(body='### 模型时间 [70:00–75:00]\n\n详细正文。'), assembly="model-merge")
    frame={'path':tmp_path/'right.jpg','time':'110:00','timestamp':6600,'score':2}
    monkeypatch.setattr(note_images,'_frames',lambda directory:[frame])
    blocks=markdown_to_blocks('发布提示\n\n'+target.read_text('utf-8'))
    for i,b in enumerate(blocks):b['id']=f'b{i}'
    blocks.append({'id':'student',**markdown_to_blocks('## 批注 [70:00–75:00]')[0]})
    intervals=[]
    def window(frames,start,end,directory):
        intervals.append((start,end));return frame
    monkeypatch.setattr(note_images,'_window_frame',window)
    uploads=[]
    class Notion:
        def list_children(self,page):return blocks
        def upload_file(self,path):uploads.append(path);return 'file-upload://image'
        def append_blocks(self,page,values,*,after,retry):
            assert after=='b2' and retry is False
            blocks.extend(values)
    notion=Notion()
    assert note_images.publish_note_images(notion,'note',tmp_path,notes_path=target,body_offset=1)==1
    assert intervals==[(6326.1,7013.1)] and uploads==[frame['path']]
    assert note_images.publish_note_images(notion,'note',tmp_path,notes_path=target,body_offset=1)==0
    assert len(uploads)==1 and any(b.get('id')=='student' for b in blocks)


def test_no_frame_in_source_interval_without_video_does_not_attach_an_earlier_topic(tmp_path, monkeypatch):
    from pku_sync.panel import note_images
    from pku_sync.notion import markdown_to_blocks
    target=tmp_path/'notes.md'
    notes.write_notes(lecture(),target,config(),'课程',ask=normal,merge_ask=lambda *a:merge(), assembly="model-merge")
    stale={'path':tmp_path/'earlier.jpg','time':'104:10','timestamp':6250,'score':2}
    monkeypatch.setattr(note_images,'_frames',lambda directory:[stale])
    blocks=markdown_to_blocks(target.read_text('utf-8'))
    for i,b in enumerate(blocks):b['id']=f'b{i}'
    class Notion:
        def list_children(self,page):return blocks
        def upload_file(self,path):pytest.fail('an earlier knowledge point was attached')
    assert note_images.publish_note_images(Notion(),'note',tmp_path,notes_path=target,body_offset=0)==0


def test_inline_images_do_not_shift_source_section_attachment_offsets(tmp_path, monkeypatch):
    from pku_sync.panel import note_images
    from pku_sync.notion import markdown_to_blocks
    target=tmp_path/'notes.md'
    combined=json.dumps({'sections':[
        {'title':'第一节','first_unit':0,'last_unit':0,'body':'说明。\n\n![示意图](https://example.invalid/a.png)'},
        {'title':'后两节','first_unit':1,'last_unit':2,'body':'后续讲述。'}]})
    notes.write_notes(lecture(),target,config(),'课程',ask=normal,merge_ask=lambda *a:combined, assembly="model-merge")
    frames=[{'path':tmp_path/'first.jpg','time':'106:00','timestamp':6360,'score':2},
            {'path':tmp_path/'second.jpg','time':'110:00','timestamp':6600,'score':2}]
    monkeypatch.setattr(note_images,'_frames',lambda directory:frames)
    blocks=markdown_to_blocks(target.read_text('utf-8'))
    for i,b in enumerate(blocks):b['id']=f'b{i}'
    insertions=[]
    class Notion:
        def list_children(self,page):return blocks
        def upload_file(self,path):return 'file-upload://'+path.stem
        def append_blocks(self,page,values,*,after,retry):insertions.append(after)
    assert note_images.publish_note_images(Notion(),'note',tmp_path,notes_path=target,body_offset=0)==2
    headings=[b['id'] for b in blocks if b['type']=='heading_2']
    assert insertions==headings


def test_starts_define_complete_inclusive_ranges_without_model_end_arithmetic():
    raw=json.dumps({'units':[
        {'title':'解释及过程','first_segment':0,'reference_segments':[],'courseware_indices':[]},
        {'title':'不同结果','first_segment':2,'reference_segments':[0],'courseware_indices':[1]}]})
    units=notes._units(raw,3,2)
    assert [(r['first_segment'],r['last_segment']) for r in units]==[(0,1),(2,2)]
    first=notes._unit_input('课程',lecture()['segments'],units[0],0,[{'text':'甲'},{'text':'乙'}],'owner')
    second=notes._unit_input('课程',lecture()['segments'],units[1],1,[{'text':'甲'},{'text':'乙'}],'owner')
    assert first['original_transcript']==lecture()['segments'][:2]
    assert second['original_transcript']==lecture()['segments'][2:]
    assert second['reference_context']==lecture()['segments'][:1]
    assert second['courseware']==[{'text':'乙'}]


@pytest.mark.parametrize('field,values',[
    ('reference_segments',[True]),('reference_segments',[3]),('reference_segments',[0]),
    ('reference_segments','1'),('courseware_indices',[False]),('courseware_indices',[1]),
    ('courseware_indices',[0,0]),('reference_segments',None),('courseware_indices',None),
    ('reference_segments',[{},[],True,-1,3,0]),('courseware_indices',[{},[],False,-1,1])])
def test_lookup_metadata_is_healed_without_repeating_paid_work(tmp_path,field,values):
    row={'title':'完整知识点','first_segment':0,'reference_segments':[],'courseware_indices':[]}
    row[field]=values
    calls=[]
    def ask(system,prompt):
        calls.append(system)
        if system == notes.PLAN_SYSTEM:
            return json.dumps({'units':[row]})
        if system == notes.REPAIR_SYSTEM:
            return plan(total=1)
        payload=json.loads(prompt)
        assert payload['original_transcript']==lecture()['segments']
        assert payload['reference_context']==[]
        assert len(payload['courseware'])==(1 if values==[0,0] else 0)
        return '完整知识点正文。'
    target=tmp_path/'notes.md'
    notes.write_notes(lecture(),target,config(),'课程',ask=ask,source_context=[{'text':'原课件'}])
    expected=[notes.PLAN_SYSTEM]+([notes.REPAIR_SYSTEM] if isinstance(values,str) else [])+[notes.UNIT_SYSTEM]
    assert calls==expected
    assert notes.record_issues(target,lecture(),[{'text':'原课件'}])==[]
    target.unlink()
    notes.write_notes(lecture(),target,config(),'课程',ask=lambda *a:pytest.fail('rebilled'),
                      source_context=[{'text':'原课件'}])
    assert calls==expected


def test_primary_plan_sanitizes_titles_anchors_start_and_preserves_lookup_order():
    raw=json.dumps({'units':[
        {'title':'  引入\r\n说明  ','first_segment':1,
         'reference_segments':[4,4,3,0,True,-1,5,'2'], 'courseware_indices':[2,0,2,False,3]},
        {'title':'后续','first_segment':3,'reference_segments':None,'courseware_indices':None}]})
    units=notes._units(raw,5,3,allow_leading_gap=True)
    assert units==[
        {'title':'引入  说明','first_segment':0,'last_segment':2,
         'reference_segments':[4,3],'courseware_indices':[2,0]},
        {'title':'后续','first_segment':3,'last_segment':4,
         'reference_segments':[],'courseware_indices':[]}]


@pytest.mark.parametrize('window,expected', [(480,[(0,2),(3,4),(5,5)]),(200,[(0,1),(2,3),(4,4),(5,5)])])
def test_fallback_units_cover_every_source_in_elapsed_time_windows(window,expected):
    original=[{'start':t,'end':t+20,'text':str(i)} for i,t in enumerate([65,100,350,545,550,1100])]
    units=notes._fallback_units(original,window)
    assert [(u['first_segment'],u['last_segment']) for u in units]==expected
    assert [u['title'] for u in units]==[f'课堂讲述（第 {i+1} 部分）' for i in range(len(units))]
    assert all(u['reference_segments']==u['courseware_indices']==[] for u in units)
    assert notes._fallback_units([])==[]


@pytest.mark.parametrize('raw,repair', [('not-json','still not-json'),('', ''),
    (json.dumps({'units':[{'title':'错误编号','first_segment':True}]}),'{}')])
def test_failed_plan_and_repair_fall_back_and_restore_without_rebilling(tmp_path,monkeypatch,raw,repair):
    target=tmp_path/'notes.md'
    original={'segments':[{'start':i*200,'end':i*200+100,'text':f'原讲述{i}'} for i in range(5)]}
    calls=[]
    def ask(system,prompt):
        calls.append(system)
        if system==notes.PLAN_SYSTEM:return raw
        if system==notes.REPAIR_SYSTEM:return repair
        assert system==notes.UNIT_SYSTEM
        payload=json.loads(prompt)
        assert payload['original_transcript']==original['segments'][payload['unit']:payload['unit']+1]
        return f"已整理原讲述{payload['unit']}"
    replace=Path.replace
    def fail(path,dest):
        if dest==target:raise OSError('disk full after fallback')
        return replace(path,dest)
    monkeypatch.setattr(Path,'replace',fail)
    with pytest.raises(OSError,match='disk full'):
        notes.write_notes(original,target,config(),'课程',ask=ask,window_seconds=200,workers=1)
    assert calls==[notes.PLAN_SYSTEM,notes.REPAIR_SYSTEM]+[notes.UNIT_SYSTEM]*5
    paid={p:p.read_bytes() for p in tmp_path.rglob('*.json')}
    monkeypatch.setattr(Path,'replace',replace)
    notes.write_notes(original,target,config(),'课程',ask=lambda *a:pytest.fail('rebilled fallback'),window_seconds=480)
    assert notes.record_issues(target,original)==[]
    assert all(p.read_bytes()==content for p,content in paid.items())
    report=json.loads(report_path(target).read_text('utf-8'))
    assert report['plan_window_seconds']==200
    cache=tmp_path/'.notes-parts'/notes.POLICY
    assert json.loads((cache/(report['plan_request']+'.json')).read_text('utf-8'))['response']==raw
    assert json.loads((cache/(report['plan_response']+'.json')).read_text('utf-8'))['response']==repair
    damaged=cache/(report['plan_response']+'.json')
    damaged.unlink()
    assert notes.record_issues(target,original)


@pytest.mark.parametrize('model',['qwen3.7-plus','qwen3.8-max','qwen3.8-max-0902'])
def test_full_lecture_plus_and_max_plan_and_merge_accept_more_than_30000_characters(tmp_path,model):
    original={'segments':[{'start':0,'end':1200,'text':'原讲述'*12000}]}
    cfg=config();cfg.notes_model=model
    calls=[]
    def ask(system,prompt):
        calls.append(system)
        assert len(prompt)>30000
        if system==notes.PLAN_SYSTEM:return plan(total=1)
        if system==notes.MERGE_SYSTEM:return merge(total=1)
        return '完整正文。'
    target=tmp_path/'notes.md'
    notes.write_notes(original,target,cfg,'课程',ask=ask,merge_ask=ask,assembly='model-merge')
    assert calls==[notes.PLAN_SYSTEM,notes.UNIT_SYSTEM,notes.MERGE_SYSTEM]
    assert notes.record_issues(target,original)==[]


def test_paid_plan_with_more_original_lookups_resumes_without_rebilling_or_dropping_sources(tmp_path):
    original = {'segments': [{'start': i * 10, 'end': i * 10 + 9,
                              'text': f'原始课堂片段{i}'} for i in range(8)]}
    sources = [{'title': '原课件', 'locator': f'第{i + 1}页', 'text': f'原页{i}',
                'status': 'readable'} for i in range(5)]
    units = [{'title': '引入', 'first_segment': 0,
              'reference_segments': list(range(1, 8)), 'courseware_indices': list(range(5))},
             {'title': '解释', 'first_segment': 1,
              'reference_segments': [], 'courseware_indices': []}]
    target = tmp_path / 'notes.md'
    identity = notes.fingerprint({'campus_account': config().pku_username,
                                  'operation': config()._metered_operation_context})
    cache = target.parent / '.notes-parts' / notes.POLICY
    payload = notes._plan_input('课程', original['segments'], notes._courseware(sources), identity)
    saved = notes._response(cache, 'plan', payload, config().notes_model, identity,
                            config()._metered_operation_context,
                            lambda *a: json.dumps({'units': units}, ensure_ascii=False))
    receipt = cache / (saved['request_sha256'] + '.json')
    before = receipt.read_bytes()
    seen = []
    def ask(system, prompt):
        assert system == notes.UNIT_SYSTEM  # a paid plan must not trigger another repair
        value = json.loads(prompt)
        seen.append(value)
        return f"第{value['unit']}段正文"
    notes.write_notes(original, target, config(), '课程', ask=ask,
                      merge_ask=lambda *a: merge(total=2), source_context=sources, workers=1)
    assert seen[0]['reference_context'] == original['segments'][1:]
    assert seen[0]['courseware'] == notes._courseware(sources)
    assert receipt.read_bytes() == before
    paid = {p: p.read_bytes() for p in tmp_path.rglob('*.json')}
    body = target.read_bytes()
    target.unlink()
    notes.write_notes(original, target, config(), '课程',
                      ask=lambda *a: pytest.fail('rebilled cached request'),
                      merge_ask=lambda *a: pytest.fail('rebilled cached merge'), source_context=sources)
    assert target.read_bytes() == body and all(p.read_bytes() == raw for p, raw in paid.items())
    assert notes.record_issues(target, original, sources) == []


def legacy_plan():
    return json.dumps({'units':[{'title':f'旧知识点{i}','first_segment':i,'last_segment':i} for i in range(3)]})


def test_v1_paid_note_remains_readable_and_restores_after_new_policy_selection(tmp_path):
    from pku_sync import media
    target=tmp_path/'notes.md'
    def ask(system,prompt):
        if system==notes.LEGACY_PLAN_SYSTEM:return legacy_plan()
        payload=json.loads(prompt)
        assert payload['reference_context']==lecture()['segments']
        return f"旧付费正文{payload['unit']}"
    notes.write_notes(lecture(),target,config(),'课程',ask=ask,merge_ask=lambda *a:merge(),_policy=notes.LEGACY_POLICY)
    original=target.read_bytes();saved={p:p.read_bytes() for p in tmp_path.rglob('*.json')}
    cfg=config();cfg.platform_token='';cfg.notes_strategy='topics';cfg.notes_model='new-model'
    assert media.write_notes(lecture(),[],target,cfg,'新选择')==target
    assert notes.record_issues(target,lecture())==[]
    target.unlink()
    assert media.write_notes(lecture(),[],target,cfg,'新选择')==target
    assert target.read_bytes()==original and all(p.read_bytes()==raw for p,raw in saved.items())
    assert json.loads(report_path(target).read_text('utf-8'))['policy']==notes.LEGACY_POLICY


@pytest.mark.parametrize('empty_new_cache',[False,True])
def test_unfinished_v1_keeps_original_paid_cache_and_resumes_its_original_policy(tmp_path,empty_new_cache):
    target=tmp_path/'notes.md';calls=[];fail=[True]
    def ask(system,prompt):
        if system==notes.LEGACY_PLAN_SYSTEM:
            calls.append('plan');return legacy_plan()
        assert system==notes.LEGACY_UNIT_SYSTEM
        index=json.loads(prompt)['unit'];calls.append(index)
        if index==1 and fail[0]:
            fail[0]=False;raise RuntimeError('temporary service failure')
        return f'旧版已付费正文{index}'
    with pytest.raises(RuntimeError,match='temporary service failure'):
        notes.write_notes(lecture(),target,config(),'课程',ask=ask,merge_ask=lambda *a:merge(),
            workers=1,_policy=notes.LEGACY_POLICY)
    saved={p:p.read_bytes() for p in tmp_path.rglob('*.json')}
    if empty_new_cache:
        (tmp_path/'.notes-parts'/notes.POLICY).mkdir()
    def combine(system,prompt):
        assert system==notes.LEGACY_MERGE_SYSTEM
        assert all('original_transcript' not in row for row in json.loads(prompt)['unit_drafts'])
        return merge()
    notes.write_notes(lecture(),target,config(),'课程',ask=ask,merge_ask=combine,workers=1)
    assert calls==['plan',0,1,1,2]
    assert not list((tmp_path/'.notes-parts'/notes.POLICY).glob('*.json'))
    assert all(p.read_bytes()==raw for p,raw in saved.items())
    assert notes.record_issues(target,lecture())==[]


def test_merge_quote_repair_preserves_raw_paid_response_and_exact_body(tmp_path):
    raw='{"sections":[{"title":"相邻知识点","first_unit":0,"last_unit":2,"body":"原词"SalesandSale"待回听"}]}'
    body='原词"SalesandSale"待回听';calls=[]
    def combine(system,prompt):
        calls.append(system)
        if system==notes.MERGE_SYSTEM:return raw
        payload=json.loads(prompt)
        assert payload['raw_response']==raw and payload['response_contract']==notes.MERGE_SYSTEM
        assert all('original_transcript' in row for row in payload['original_input']['unit_drafts'])
        return merge(body=body)
    target=tmp_path/'notes.md'
    notes.write_notes(lecture(),target,config(),'课程',ask=normal,merge_ask=combine, assembly="model-merge")
    assert calls==[notes.MERGE_SYSTEM,notes.REPAIR_SYSTEM]
    report=json.loads(report_path(target).read_text('utf-8'))
    primary=json.loads((tmp_path/'.notes-parts'/notes.POLICY/(report['merge_request']+'.json')).read_text('utf-8'))
    assert primary['response']==raw and body in target.read_text('utf-8')
    assert notes.record_issues(target,lecture())==[]
