"""Small offline driver-contract checks, not evidence of native E2E passing."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from product_e2e import learning_journeys as journeys
from product_e2e.core import ResourceBlocked, need


def test_course_navigation_does_not_accept_previous_courses_still_visible_lessons():
    current = {'course': '_old_1'}
    actions = []
    waits = []
    def visible(selector):
        if selector.startswith('.course-hero'):
            return ('data-course-id="' + current['course'] + '"') in selector
        return True
    def wait(predicate):
        waits.append(predicate)
        if len(waits) == 1:
            assert predicate() is True  # Target navigation button already exists.
            return
        assert predicate() is False, 'Old course content must not satisfy new course navigation'
        current['course'] = '_target_1'
        assert predicate() is True
    def click(selector):
        actions.append((selector, current['course']))
    ui = SimpleNamespace(visible=visible, wait=wait, click=click,
                         request_read=lambda path: {'id': '_target_1'})
    run = SimpleNamespace(session=lambda name: ui, scenario={'id': 'JOURNEY-04'})
    _, course = journeys._open_course(run, '_target_1')
    assert course['id'] == '_target_1'
    assert len(waits) == 2
    assert actions[-1][1] == '_target_1'


def test_mismatched_native_course_payload_is_rejected_before_paid_work():
    ui = SimpleNamespace(visible=lambda selector: True, wait=lambda predicate: predicate(),
                         click=lambda selector: None, request_read=lambda path: {'id': '_other_1'})
    run = SimpleNamespace(session=lambda name: ui, scenario={'id': 'JOURNEY-03'})
    with pytest.raises(AssertionError, match='course response identity'):
        journeys._open_course(run, '_target_1')


def test_learning_assertions_exactly_match_mandatory_manifest_and_no_write_api_bypass():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "product_e2e/learning_journeys.py").read_text("utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assertions = {call.args[0].value for call in calls if isinstance(call.func, ast.Attribute)
                  and call.func.attr == "check" and isinstance(call.args[0], ast.Constant)}
    manifest = json.loads((root / "docs/PRODUCT_E2E_SCENARIOS.json").read_text("utf-8"))
    owned = {"JOURNEY-03", "JOURNEY-04", "JOURNEY-05", "XPLAT-01"}
    expected = {key for row in manifest["scenarios"] if row["id"] in owned for key in row["assertions"]}
    expected |= {replacement for row in manifest["scenarios"] if row["id"] in owned
                 for replacement in row.get("deferrals", {}).get("windows", {})
                 .get("assertion_replacements", {}).values()}
    assert assertions == expected
    forbidden = {"post", "put", "patch", "delete", "request", "evaluate"}
    assert not [call for call in calls if isinstance(call.func, ast.Attribute) and call.func.attr in forbidden]


def test_native_click_precedes_explicit_dialog_confirmation():
    actions = []
    ui = SimpleNamespace(click=lambda selector: actions.append(("click", selector)),
                         prepare_dialog=lambda value: actions.append(("arm", value)),
                         confirm=lambda value: actions.append(("confirm", value)))
    journeys._confirm(None, ui, "#submit")
    assert actions == [("arm", True), ("click", "#submit"), ("confirm", True)]


def test_summary_hash_is_independent_of_marker_claim_and_changes_with_body():
    fact = {"type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": "source fact"},
                                                                  "plain_text": "source fact"}]}}
    marker = {"type": "paragraph", "paragraph": {"rich_text": [{"text": {
        "content": 'PKU 总结验收版本 · {"schema":2,"remote_sha256":"not trusted"}'}}]}}
    calls = []
    def get(service, path, params):
        calls.append((service, path, params))
        return {"results": [fact, marker], "has_more": False}
    run = SimpleNamespace(oracle=SimpleNamespace(get=get))
    before = journeys._note_fingerprint(run, "page")
    fact["paragraph"]["rich_text"][0]["text"]["content"] = "changed fact"
    assert journeys._note_fingerprint(run, "page") != before
    assert all(service == "notion" and "?" not in path and params == {"page_size": 100}
               for service, path, params in calls)


def test_answer_key_leak_cannot_pass_student_page():
    row = {"type": "heading_2", "heading_2": {"rich_text": [{"plain_text": "教师区（答案）"}]}}
    with pytest.raises(AssertionError, match="Answer key leaked"):
        journeys._quiz_questions([row])


@pytest.mark.parametrize("corruption", ["outside-name", "wrong-hash"])
def test_actual_draft_cannot_escape_sandbox_or_lie_about_hash(tmp_path, corruption):
    cfg = {"test_root": str(tmp_path), "instances": {"primary": {"app_dir": str(tmp_path / "app")}}}
    run = SimpleNamespace(need=lambda key: need(cfg, key))
    folder = tmp_path / "app/data/submission-drafts/draft"
    folder.mkdir(parents=True)
    row = {"course_id": "_1_1", "content_id": "_2_1", "archive_name": "answer.zip", "sha256": "0" * 64}
    (folder / "answer.zip").write_bytes(b"actual bytes")
    if corruption == "outside-name":
        row["archive_name"] = "../../outside.zip"
    (folder / "draft.json").write_text(json.dumps(row), "utf-8")
    with pytest.raises(AssertionError):
        journeys._draft_probe(run, "_1_1", "_2_1")


def test_current_account_quota_authority_does_not_authorize_official_assignment_submit():
    cfg = {"assignment": {"course_id": "_1_1", "content_id": "_2_1"},
           "budget": {"approved": True, "max_points": 100}, "submission_authority": {"approved": False}}
    run = SimpleNamespace(need=lambda key: need(cfg, key), context={"target": "windows"},
                         scenario={"assertions": ["designated-test-assignment-official-history-and-file-readback"]},
                         session=lambda *args: pytest.fail("UI must not open"))
    with pytest.raises(ResourceBlocked, match="designated_resubmittable_assignment_not_approved"):
        journeys.run_assignment(run)


def test_windows_preview_scope_does_not_confirm_or_report_deferred_submission(monkeypatch):
    from scripts.product_e2e_gate import load_manifest, required_cases
    scenario = required_cases(load_manifest(Path(__file__).resolve().parents[1]), "windows")[("JOURNEY-04", "live")]
    cfg = {"assignment": {"course_id": "_1_1", "content_id": "_2_1"},
           "submission_authority": {"approved": False}}
    official = {"course_id": "_1_1", "content_id": "_2_1", "source_id": "official", "due_at": "date",
                "instructions": "requirements", "source": "teaching-portal"}
    digest, operation = 'a' * 64, 'b' * 64
    receipt = {'schema': 1, 'course_id': '_1_1', 'content_id': '_2_1',
               'requirements_sha256': digest, 'operation_id': operation,
               'official_instructions': official['instructions'], 'due_at': official['due_at'],
               'source_grounding_verified': True, 'reused': False,
               'points_charged': 0.004, 'receipt_points_charged': 0.004,
               'analysis': {'requirements': [{'quote': 'requirements'}], 'filename_rules': [],
                            'file_formats': [], 'uncertainties': []}}
    job = {'id': 'job-1', 'operation_id': 'job-1', 'client_operation_id': operation,
           'purpose': 'assignment-requirements', 'course_id': '_1_1', 'content_id': '_2_1',
           'source_id': digest, 'status': 'succeeded', 'points_charged': 0.004}
    settlement = {'id': 'llm:1', 'operation_id': 'job-1', 'charged_points': 0.004}
    checks, snapshots = {}, iter([{'complete': True, 'charged_points': 20,
                                  'jobs': [], 'settlements': [], 'submission_posts': []},
                                 {'complete': True, 'charged_points': 20.004,
                                  'jobs': [job], 'settlements': [settlement], 'submission_posts': []},
                                 {'submission_posts': []}])
    ui = SimpleNamespace(visible=lambda selector: False, screenshot=lambda name: None,
                         text=lambda selector: '本次用量：0.004 AI 点',
                         page=SimpleNamespace(locator=lambda selector:
                              SimpleNamespace(all_text_contents=lambda: ['requirements'])))
    run = SimpleNamespace(need=lambda key: need(cfg, key), context={"target": "windows"},
                          scenario=scenario, mode="live", actions=[],
                          oracle=SimpleNamespace(historical_assignment=lambda cid: {"read_only": True}),
                          check=lambda key, value, detail: checks.update({key: value}))
    monkeypatch.setattr(journeys, "_open_course", lambda *args: (ui, {"id": "_1_1", "assignments": [official]}))
    monkeypatch.setattr(journeys, "_official_submission", lambda *args, **kwargs: official)
    monkeypatch.setattr(journeys, "_ledger", lambda *args: next(snapshots))
    monkeypatch.setattr(journeys, "_prepare", lambda *args: {'sha256': 'actual-test-digest',
        'course_id': '_1_1', 'content_id': '_2_1', 'requirements_sha256': digest,
        'native_requirements_response': receipt})
    monkeypatch.setattr(journeys, "_preview", lambda *args: {"content_match": True, "source_requirements_match": True, "sha256": "actual-test-digest"})
    monkeypatch.setattr(journeys, "_unconfirmed_trial", lambda *args: True)
    monkeypatch.setattr(journeys, "_confirm", lambda *args: pytest.fail("deferred submission cannot be confirmed"))
    journeys.run_assignment(run)
    assert set(checks) == set(scenario["assertions"]) and all(checks.values())
    assert run.actions == [{"event": "official_submission_deferred", "status": "deferred-unverified"}]


def test_absent_driver_capability_is_not_mislabeled_missing_account_resource():
    with pytest.raises(ResourceBlocked, match="software_capability_missing_upload"):
        journeys._capability(None, object(), "upload")


def test_grade_compares_real_history_json_to_objective_scores_and_subjective_bounds():
    rows = [{"number": number, "score": 20 if number <= 3 else 12} for number in range(1, 6)]
    payload = {"score": sum(row["score"] for row in rows), "questions": rows}
    code = {"type": "code", "code": {"rich_text": [{"plain_text": json.dumps(payload)}]}}
    run = SimpleNamespace(oracle=SimpleNamespace(get=lambda *args: {"results": [code], "has_more": False}))
    cfg = {"objective_expected": {str(number): 20 for number in range(1, 4)},
           "subjective_bounds": {"4": [10, 15], "5": [10, 15]}}
    assert journeys._grade_check(run, "history", cfg)
    cfg["objective_expected"]["1"] = 0
    assert not journeys._grade_check(run, "history", cfg)


def test_prepared_file_sha_is_computed_from_real_bytes(tmp_path):
    cfg = {"test_root": str(tmp_path), "instances": {"primary": {"app_dir": str(tmp_path / "app")}}}
    run = SimpleNamespace(need=lambda key: need(cfg, key))
    folder = tmp_path / "app/data/submission-drafts/draft"
    folder.mkdir(parents=True)
    raw = b"actual prepared file"
    digest = hashlib.sha256(raw).hexdigest()
    (folder / "answer.zip").write_bytes(raw)
    (folder / "draft.json").write_text(json.dumps({"course_id": "_1_1", "content_id": "_2_1",
                                                  "archive_name": "answer.zip", "sha256": digest}), "utf-8")
    assert journeys._draft_probe(run, "_1_1", "_2_1")["sha256"] == digest


def reviewed_questions():
    return [{'number': n, 'type': kind, 'question': 'actual independent question ' + str(n),
             'source': 'frozen official source', 'answer_block_id': str(n)*32}
            for n, kind in enumerate(['选择','判断','填空','简答','论述'], 1)]


def reviewed_fixture():
    return {'quiz_id':'a'*32, 'exact_questions_sha256':'b'*64, 'source_ids':['official-source'],
        'source_facts_sha256':'c'*64, 'answer_texts':['A','错误','端脑','independent response','independent essay'],
        'objective_expected':{'1':20,'2':0,'3':20}, 'subjective_bounds':{'4':[16,20],'5':[16,20]},
        'expected_question_anchors':['actual independent question'], 'changed_answer':'B', 'review_note':'reviewed'}


@pytest.mark.parametrize('field,value', [('quiz_id','d'*32),('exact_questions_sha256','d'*64),
    ('source_ids',['other-source']),('source_facts_sha256','d'*64),('answer_texts',['A']),
    ('objective_expected',{'1':20}),('subjective_bounds',{'4':[0,20],'5':[16,20]}),
    ('expected_question_anchors',[]),('changed_answer','A')])
def test_reviewed_oracle_cannot_accept_wrong_questions_incomplete_scores_or_unbounded_rubric(field,value):
    fixture = reviewed_fixture()
    fixture[field] = value
    with pytest.raises(ResourceBlocked):
        journeys._validate_reviewed_quiz_answers(fixture, reviewed_questions(), 'a'*32, 'b'*64, ['official-source'],'c'*64)


def test_reviewed_oracle_binds_every_actual_question_and_all_five_score_types():
    assert journeys._validate_reviewed_quiz_answers(reviewed_fixture(),reviewed_questions(),'a'*32,'b'*64,['official-source'],'c'*64)['objective_expected']['2'] == 0


def reviewed_student_rows():
    rows = []
    def block(kind, text):
        return {'id': str(len(rows)+1).zfill(32), 'type':kind,
                kind:{'rich_text':[{'type':'text','text':{'content':text},'plain_text':text}]}}
    for question in reviewed_questions():
        for kind, text in [('heading_3',str(question['number'])+'. '+question['type']),
                           ('paragraph',question['question']),('paragraph','来源：'+question['source']),
                           ('paragraph','答案：')]:
            rows.append(block(kind,text))
    rows.append(block('heading_2','学生上传的往年题参考'))
    rows.append(block('paragraph','unrelated prior paper'))
    return rows


def test_reviewed_oracle_question_digest_preserves_actual_content_but_allows_answer_edit():
    rows = reviewed_student_rows()
    original = journeys._reviewed_quiz_questions(rows)[1]
    rows[3]['paragraph']['rich_text'][0]['plain_text'] = '答案：student answer'
    assert journeys._reviewed_quiz_questions(rows)[1] == original
    rows[1]['paragraph']['rich_text'][0]['text']['content'] = 'changed option without plain text change'
    assert journeys._reviewed_quiz_questions(rows)[1] != original


def test_reviewed_oracle_private_path_and_pre_model_source_binding(tmp_path):
    private = tmp_path/'private'; private.mkdir()
    facts = tmp_path/'goal-facts.json'
    facts.write_text(json.dumps({'course_id':'course','source_id':'source'}),'utf-8')
    cfg = {'course_id':'course','source_ids':['source'],'reviewed_answers_path':str(private/'answers.json'),
           'source_facts_path':str(facts),'source_facts_sha256':hashlib.sha256(facts.read_bytes()).hexdigest()}
    run = SimpleNamespace(config={'instances':{'primary':{'app_dir':str(private/'app')}}},
                          need=lambda key:str(private))
    assert journeys._reviewed_quiz_preflight(run,cfg)['reused'] is False
    cfg['reviewed_answers_path'] = str(tmp_path/'outside.json')
    with pytest.raises(ResourceBlocked):
        journeys._reviewed_quiz_preflight(run,cfg)
    cfg['reviewed_answers_path'] = str(private/'app'/'oracle.json')
    with pytest.raises(ResourceBlocked):
        journeys._reviewed_quiz_preflight(run,cfg)
    cfg['reviewed_answers_path'] = str(private/'answers.json')
    cfg['source_ids'] = ['wrong-source']
    with pytest.raises(ResourceBlocked):
        journeys._reviewed_quiz_preflight(run,cfg)


def test_reviewed_oracle_exact_preexisting_fixture_reusable_but_later_change_rejected(tmp_path,monkeypatch):
    private = tmp_path/'private'; private.mkdir()
    case_dir = tmp_path/'case'; case_dir.mkdir()
    facts = tmp_path/'facts.json'
    facts.write_text(json.dumps({'course_id':'course','source_id':'official-source'}),'utf-8')
    rows = reviewed_student_rows()
    questions, digest = journeys._reviewed_quiz_questions(rows)
    value = reviewed_fixture(); value['exact_questions_sha256'] = digest
    value['source_facts_sha256'] = hashlib.sha256(facts.read_bytes()).hexdigest()
    path = private/'answers.json'; path.write_text(json.dumps(value),'utf-8')
    cfg = {'course_id':'course','source_ids':['official-source'],'reviewed_answers_path':str(path),
           'source_facts_path':str(facts),'source_facts_sha256':value['source_facts_sha256']}
    run = SimpleNamespace(config={},need=lambda key:str(private),case_dir=case_dir,actions=[])
    binding = journeys._reviewed_quiz_preflight(run,cfg)
    ui = SimpleNamespace(wait=lambda predicate,**kwargs:predicate())
    actual = journeys._reviewed_quiz_answers(run,ui,cfg,{'id':'a'*32},rows,binding)
    monkeypatch.setattr(journeys,'_blocks',lambda *args:rows)
    journeys._assert_reviewed_quiz_unchanged(run,actual,{'id':'a'*32},binding)
    assert run.actions[-1]['reused'] is True
    assert actual['objective_expected']['2'] == 0
    value['subjective_bounds']['4'] = [17,20]
    path.write_text(json.dumps(value),'utf-8')
    with pytest.raises(ResourceBlocked):
        journeys._assert_reviewed_quiz_unchanged(run,actual,{'id':'a'*32},binding)


@pytest.mark.parametrize('fault', ['none','actual-app','url-host','evil-subdomain','app-wrong-path','url-identity','source','notes','parent','remote','duplicate'])
def test_unaccepted_published_note_requires_unique_local_version_and_independent_remote_identity(tmp_path,monkeypatch,fault):
    app=tmp_path/'primary'; folder=app/'data/course/recordings/one'; folder.mkdir(parents=True)
    source='official-source'; note='3f491b6f53e18113b65fedde2cab928f'; lecture='b'*32
    (folder/'recording.json').write_text(json.dumps({'source_id':source,'course_id':'course'}),'utf-8')
    (folder/'notes.md').write_bytes(b'real reviewed notes')
    version={'note_id':note,'lecture_id':lecture,'source_id':source,
             'notes_sha256':hashlib.sha256((folder/'notes.md').read_bytes()).hexdigest(),'remote_sha256':'c'*64}
    if fault=='source':version['source_id']='wrong'
    (folder/'published-note-version.json').write_text(json.dumps(version),'utf-8')
    if fault=='notes':(folder/'notes.md').write_bytes(b'changed')
    if fault=='duplicate':
        duplicate=app/'data/course/recordings/two'; duplicate.mkdir()
        (duplicate/'recording.json').write_text((folder/'recording.json').read_text('utf-8'),'utf-8')
    cfg={'instances':{'primary':{'app_dir':str(app)}},'test_root':str(tmp_path)}
    run=SimpleNamespace(need=lambda key:need(cfg,key))
    lesson={'source_id':source,'notion_note_url':'https://www.notion.so/'+note,'summary_acceptance':{'accepted':False}}
    if fault=='actual-app':lesson['notion_note_url']='https://app.notion.com/p/2026-09-30-2026-09-30-5-6-3f491b6f53e18113b65fedde2cab928f'
    if fault=='url-host':lesson['notion_note_url']='https://attacker.invalid/'+note
    if fault=='evil-subdomain':lesson['notion_note_url']='https://app.notion.com.attacker.invalid/p/'+note
    if fault=='app-wrong-path':lesson['notion_note_url']='https://app.notion.com/unknown/'+note
    if fault=='url-identity':lesson['notion_note_url']='https://www.notion.so/'+'d'*32
    page={'object':'page','id':note,'parent':{'page_id':lecture if fault!='parent' else 'd'*32}}
    monkeypatch.setattr(journeys,'_page',lambda *args: (page, fault!='parent'))
    monkeypatch.setattr(journeys,'_note_fingerprint',lambda *args:'d'*64 if fault=='remote' else 'c'*64)
    marker={'type':'paragraph','paragraph':{'rich_text':[{'text':{'content':'查看教学网讲次来源',
        'link':{'url':'https://course.pku.edu.cn/webapps/bb-streammedia-hqy-BBLEARN/videoList.action#official-source'}}}]}}
    monkeypatch.setattr(journeys,'_blocks',lambda *args:[marker])
    if fault in {'none','actual-app'}:
        assert journeys._published_note_id(run,lesson,lecture,'course') == note
    else:
        with pytest.raises(ResourceBlocked):journeys._published_note_id(run,lesson,lecture,'course')
