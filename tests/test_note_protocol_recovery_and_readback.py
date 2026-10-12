"""Offline integration audits; no model charges or remote writes."""
import copy
import json
from types import SimpleNamespace
import pytest
from pku_sync import note_editor, notion
from pku_sync.models import Recording
from pku_sync.pipeline import RecordingJob
from pku_sync.panel import recordings_api, note_images, note_videos

@pytest.mark.parametrize('damage', ['none', 'missing-row', 'missing-cell', 'wrong-cell', 'extra-row'])
def test_nested_table_readback_compares_actual_cells_not_just_table_width(tmp_path, monkeypatch, damage):
    job=RecordingJob('课程',tmp_path/'课程',Recording(course_id='course-1',title='第一讲',recorded_at='2026-10-09 09:00:00'))
    job.directory.mkdir(parents=True)
    (job.directory/'notes.md').write_text('# 课堂比较\n\n| 概念 | 条件 |\n| --- | --- |\n| 模逆元 | 与模数互素 |\n','utf-8')
    state={'blocks':[], 'table':[], 'archived':[]}
    class Notion:
        def __init__(self, token): pass
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def list_child_pages(self,_): return []
        def create_page(self,parent,title,*,children,retry):
            state['blocks']=copy.deepcopy(children)
            for i,block in enumerate(state['blocks']):
                block['id']=f'block-{i}'
                if block['type']=='table':
                    state['table']=block['table'].pop('children')
                    block['has_children']=True
                    block['id']='table-id'
            if damage=='missing-row': state['table'].pop()
            if damage=='missing-cell': state['table'][1]['table_row']['cells'].pop()
            if damage=='extra-row': state['table'].append(copy.deepcopy(state['table'][1]))
            if damage=='wrong-cell':
                state['table'][1]['table_row']['cells'][1][0]['text']['content']='任意整数都有逆元'
            # Real Notion adds convenience fields and default annotations.
            for row in state['table']:
                for cell in row['table_row']['cells']:
                    for part in cell:
                        if part['type']=='text':
                            part.update(plain_text=part['text']['content'],href=None,
                                        annotations={'bold':False,'italic':False,'strikethrough':False,'underline':False,'code':False,'color':'default'})
            return {'id':'note-id','url':'https://www.notion.so/note-id'}
        def list_children(self,identifier):
            return state['table'] if identifier=='table-id' else state['blocks'] if identifier=='note-id' else []
        def archive_page(self,identifier,*,retry): state['archived'].append(identifier)
    monkeypatch.setattr(notion,'NotionClient',Notion)
    monkeypatch.setattr(recordings_api,'formal_assignment_blocks',lambda *_: [])
    monkeypatch.setattr(note_images,'publish_note_images',lambda *_,**kw:0)
    monkeypatch.setattr(note_videos,'publish_note_videos',lambda *_,**kw:0)
    if damage=='none':
        assert recordings_api.publish_notes(job,'lecture-id','fake-token').endswith('note-id')
        assert state['archived']==[]
    else:
        with pytest.raises(ValueError,match='回读内容与本次提交不一致'):
            recordings_api.publish_notes(job,'lecture-id','fake-token')
        assert state['archived']==['note-id']
        assert not (job.directory/'published-note-version.json').exists()


def test_malformed_review_recovers_once_with_original_context_and_keeps_every_response(tmp_path):
    transcript={'segments':[{'start':0,'end':10,'text':'模逆元存在的条件是与模数互素。'}]}
    sources=[{'title':'本讲.pdf','locator':'第 1 页','text':'a 与 n 互素时存在模逆元。','status':'readable'}]
    malformed='{"note": "模逆元存在的条件是与模数互素。", "complete": true, "uncertainties": []'
    good=json.dumps({'note':'模逆元存在的条件是与模数互素。','complete':True,'uncertainties':[]},ensure_ascii=False)
    calls=[]
    def ask(system,prompt):
        payload=json.loads(prompt); calls.append(payload)
        assert payload['transcript']==transcript['segments'] and payload['courseware']==sources
        if len(calls)==2: return malformed
        if len(calls)==3:
            assert payload['format_repair']['raw_response']==malformed
            assert payload['format_repair']['stage']=='review'
        return good
    target=tmp_path/'notes.md'
    cfg=SimpleNamespace(notes_model='test-model')
    note_editor.write_notes(transcript,target,cfg,'课程',ask=ask,source_context=sources)
    assert len(calls)==3 and note_editor.review_record_issues(target,transcript,sources)==[]
    records=[json.loads(p.read_text('utf-8')) for p in (tmp_path/'.notes-parts'/note_editor.POLICY).glob('*.json')]
    assert len(records)==4 and malformed in [r['response'] for r in records]
    target.unlink()
    note_editor.write_notes(transcript,target,cfg,'课程',ask=ask,source_context=sources)
    assert len(calls)==3


@pytest.mark.parametrize('failed_stage', ['write', 'review'])
def test_interrupted_format_recovery_reuses_original_response_and_same_request(tmp_path, failed_stage):
    transcript={'segments':[{'start':0,'end':10,'text':'课堂说明存在的条件。'}]}
    cfg=SimpleNamespace(notes_model='test-model')
    target=tmp_path/'notes.md'
    good=json.dumps({'note':'课堂说明存在的条件。','complete':True,'uncertainties':[]})
    malformed='{"note": "课堂说明存在的条件。"'
    calls=[]
    def ask(system,prompt):
        payload=json.loads(prompt); calls.append((system,payload))
        if 'format_repair' in payload and sum('format_repair' in p for _,p in calls)==1:
            raise RuntimeError('format transport interrupted')
        if 'format_repair' in payload:
            assert payload['format_repair']['raw_response']==malformed
            return good
        is_review='审校编辑' in system
        return malformed if is_review==(failed_stage=='review') else good
    with pytest.raises(RuntimeError,match='format transport interrupted'):
        note_editor.write_notes(transcript,target,cfg,'课程',ask=ask)
    original_records={p.name:p.read_bytes() for p in (tmp_path/'.notes-parts'/note_editor.POLICY).glob('*.json')}
    assert malformed in [json.loads(raw)['response'] for raw in original_records.values()]
    note_editor.write_notes(transcript,target,cfg,'课程',ask=ask)
    repairs=[p for _,p in calls if 'format_repair' in p]
    assert len(calls)==4 and repairs[0]==repairs[1]
    assert note_editor.review_record_issues(target,transcript)==[]
    assert all((tmp_path/'.notes-parts'/note_editor.POLICY/name).read_bytes()==raw for name,raw in original_records.items())
    target.unlink()
    note_editor.write_notes(transcript,target,cfg,'课程',ask=ask)
    assert len(calls)==4


@pytest.mark.parametrize('field', ['chapter-complete', 'chapter-uncertainty', 'content-complete', 'uncertainties'])
def test_saved_assessment_cannot_be_upgraded_without_model_response(tmp_path,field):
    transcript={'segments':[{'start':0,'end':10,'text':'具体日期听不清。'}]}
    cfg=SimpleNamespace(notes_model='test-model')
    target=tmp_path/'notes.md'
    raw=json.dumps({'note':'日期需要回看。','complete':False,'uncertainties':['日期待核。']})
    note_editor.write_notes(transcript,target,cfg,'课程',ask=lambda *_:raw)
    assert note_editor.review_record_issues(target,transcript)==[]
    original=target.read_bytes()
    report=json.loads(note_editor.report_path(target).read_text('utf-8'))
    if field=='chapter-complete': report['chapters'][0]['model_content_complete']=True
    if field=='chapter-uncertainty': report['chapters'][0]['uncertainties']=[]
    if field=='content-complete': report['content_complete']=True
    if field=='uncertainties': report['uncertainties']=[]
    note_editor.write_json(note_editor.report_path(target),report)
    assert note_editor.review_record_issues(target,transcript)
    assert target.read_bytes()==original


@pytest.fixture(autouse=True)
def isolated_source_reading_provider(monkeypatch):
    """Keep these writer/review fault fixtures independent of the reading model.

    The real request/cache/receipt code still runs. Dedicated source-reading
    tests exercise its provider, locality, faults and metered recovery.
    """
    from pku_sync import note_editor
    original = note_editor._request
    def request(ask, **kwargs):
        if kwargs["stage"] == "source-review":
            ask = lambda *args: json.dumps({"note": "原材料阅读的独立离线响应。",
                "complete": False, "uncertainties": []}, ensure_ascii=False)
        return original(ask, **kwargs)
    monkeypatch.setattr(note_editor, "_request", request)
