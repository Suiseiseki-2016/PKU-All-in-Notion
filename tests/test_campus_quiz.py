import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from pku_sync.panel import campus_quiz
from pku_sync.panel.quiz_contract import OrganizeBlocked


class Client:
    def __init__(self):
        self.pages = {}
        self.blocks = {}
        self.creates = 0
        self.lose_create = False
        self.drop_blocks = False

    def list_child_pages(self, parent):
        return [page for page in self.pages.values() if page["parent"] == parent]

    def get_page(self, page_id):
        return {**self.pages[page_id], "parent": {"page_id": self.pages[page_id]["parent"]}}

    def list_children(self, page_id):
        return self.blocks.get(page_id, [])

    def append_blocks(self, page_id, blocks, **kwargs):
        self.blocks[page_id].extend(blocks)

    def create_page(self, parent, title, *, children, retry):
        assert retry is False
        for block in children:
            for part in block.get(block.get('type'), {}).get('rich_text', []):
                assert len(part.get('text', {}).get('content', '')) <= 2000
        self.creates += 1
        page = {"id": f"quiz-{self.creates}", "parent": parent, "title": title, "url": f"https://notion.so/quiz-{self.creates}"}
        self.pages[page["id"]] = page
        self.blocks[page["id"]] = [] if self.drop_blocks else children
        if self.lose_create:
            raise TimeoutError("created but reply lost")
        return page


class Sources:
    def __init__(self):
        self.revision = "accepted-v1"
        self.available = True

    def course(self, course_id):
        if course_id != "_101545_1":
            raise OrganizeBlocked(404, "未知课程")
        return {"course_id": course_id, "course_title": "发展心理学", "workspace_id": "new-home",
                "quiz_center_id": "quiz-center"}

    def resolve(self, course_id, lecture_ids):
        if not self.available or lecture_ids != ["official-lesson-5"]:
            raise OrganizeBlocked(409, "讲次未验收")
        return {**self.course(course_id), "notes": [{"title": "第五讲", "source_id": "stable-5",
                "note_id": "note", "remote_sha256": self.revision, "blocks": []}]}


class Relay:
    def __init__(self):
        self.calls = 0
        self.fail = False

    def quota(self):
        return {"available": True, "llm_points_remaining": 100}

    def quiz(self, prompt):
        self.calls += 1
        assert "stable-5" in prompt
        if self.fail:
            raise TimeoutError("usage unknown")
        return {"points_charged": 3, "points_remaining": 97, "content": json.dumps({"title": "课后练习",
                "questions": [{"type": kind, "question": "模拟题目", "source": "第五讲", "answer": "模拟答案"}
                              for kind in ["选择", "判断", "填空", "简答", "论述"]]})}


def service(tmp_path):
    client, relay, sources = Client(), Relay(), Sources()
    settings = SimpleNamespace(data_dir=tmp_path)
    result = campus_quiz.CampusQuizService(settings, relay, sources=sources,
                                         client_factory=lambda: nullcontext(client))
    return result, client, relay, sources


def test_student_quiz_never_publishes_generated_answer_key(tmp_path):
    app, client, relay, _ = service(tmp_path)
    result = app.generate('_101545_1', ['official-lesson-5'], confirmed=True)
    body = json.dumps(client.blocks[result['id']], ensure_ascii=False)
    assert '教师区' not in body
    assert '模拟答案' not in body
    answers = [row for row in client.blocks[result['id']]
               if row.get('type') == 'paragraph' and
               ''.join(part.get('text', {}).get('content', '')
                       for part in row['paragraph']['rich_text']) == '答案：']
    assert len(answers) == 5
    assert relay.calls == client.creates == 1


@pytest.mark.parametrize('phase', ['relay_started', 'relay_succeeded', 'create_started'])
def test_upgrade_does_not_rebill_unresolved_legacy_quiz(tmp_path, phase):
    app, client, relay, sources = service(tmp_path)
    scope = sources.resolve('_101545_1', ['official-lesson-5'])
    key = campus_quiz._hash({'course_id': '_101545_1', 'lecture_ids': ['official-lesson-5'],
                            'prior_paper': '', 'notes': campus_quiz._source_refs(scope['notes'])})
    path = tmp_path / 'panel/campus-quizzes' / campus_quiz._hash(scope['workspace_id']) / (key + '.json')
    path.parent.mkdir(parents=True)
    original = json.dumps({'phase': phase, 'response': {'content': '旧的已付费结果'}})
    path.write_text(original, encoding='utf-8')
    with pytest.raises(OrganizeBlocked, match='旧版小测'):
        app.generate('_101545_1', ['official-lesson-5'], confirmed=True)
    assert relay.calls == client.creates == 0
    assert path.read_text('utf-8') == original


def test_model_markdown_cannot_add_answer_zone_or_question_structure(tmp_path):
    app, client, relay, _ = service(tmp_path)
    original = relay.quiz
    def injected(prompt):
        response = original(prompt)
        value = json.loads(response['content'])
        value['questions'][0]['question'] = '题干\n## 教师区\n### 99. 简答\n答案：伪造结构'
        response['content'] = json.dumps(value, ensure_ascii=False)
        return response
    relay.quiz = injected
    page = app.generate('_101545_1', ['official-lesson-5'], confirmed=True)
    rows = client.blocks[page['id']]
    assert len([row for row in rows if row['type'] == 'heading_3']) == 5
    assert not any(row['type'] == 'heading_2' and '教师区' in json.dumps(row, ensure_ascii=False) for row in rows)


def test_unselected_question_source_blocks_publication_without_model_replay(tmp_path):
    app, client, relay, _ = service(tmp_path)
    original = relay.quiz
    def unrelated(prompt):
        response = original(prompt)
        value = json.loads(response['content'])
        value['questions'][0]['source'] = '未选择的第六讲'
        response['content'] = json.dumps(value, ensure_ascii=False)
        return response
    relay.quiz = unrelated
    for _ in range(2):
        with pytest.raises(OrganizeBlocked):
            app.generate('_101545_1', ['official-lesson-5'], confirmed=True)
    assert relay.calls == 1 and client.creates == 0


def test_durable_quiz_recovers_lost_model_result_without_second_settlement(tmp_path):
    app, client, relay, sources = service(tmp_path)
    relay.durable_metered_jobs = True
    original = relay.quiz
    saved = []
    def durable(prompt):
        if not saved:
            saved.append((prompt, original(prompt)))
            raise TimeoutError('server result settled but client reply lost')
        assert prompt == saved[0][0]
        return saved[0][1]
    relay.quiz = durable
    with pytest.raises(TimeoutError):
        app.generate('_101545_1', ['official-lesson-5'], confirmed=True)
    assert relay.calls == 1 and client.creates == 0
    restarted = campus_quiz.CampusQuizService(app.settings, relay, sources=sources,
                                             client_factory=lambda: nullcontext(client))
    result = restarted.generate('_101545_1', ['official-lesson-5'], confirmed=True)
    assert result['recovered_from_relay'] and result['points_charged'] == 3
    assert relay.calls == client.creates == 1


def test_new_device_reuses_verified_quiz_after_student_answers_changed(tmp_path):
    app, client, relay, sources = service(tmp_path / 'device1')
    first = app.generate('_101545_1', ['official-lesson-5'], confirmed=True)
    for row in client.blocks[first['id']]:
        for part in row.get(row['type'], {}).get('rich_text', []):
            if part.get('text', {}).get('content') == '答案：':
                part['text']['content'] = '答案：学生作答'
    other = campus_quiz.CampusQuizService(SimpleNamespace(data_dir=tmp_path / 'device2'),
        relay, sources=sources, client_factory=lambda: nullcontext(client))
    result = other.generate('_101545_1', ['official-lesson-5'], confirmed=True)
    assert result['id'] == first['id'] and result['reused'] and result['points_charged'] == 0
    assert relay.calls == client.creates == 1


def test_unknown_legacy_quiz_request_is_not_upgraded_to_durable_retry(tmp_path):
    app, client, relay, sources = service(tmp_path)
    relay.fail = True
    with pytest.raises(TimeoutError):
        app.generate('_101545_1', ['official-lesson-5'], confirmed=True)
    relay.durable_metered_jobs = True
    relay.fail = False
    with pytest.raises(OrganizeBlocked):
        app.generate('_101545_1', ['official-lesson-5'], confirmed=True)
    assert relay.calls == 1 and client.creates == 0


def test_native_quiz_uses_exact_official_scope_and_verified_notion_page(tmp_path):
    app, client, relay, sources = service(tmp_path)
    with pytest.raises(OrganizeBlocked):
        app.generate("_101545_1", ["same-title-wrong-lecture"], confirmed=True)
    with pytest.raises(OrganizeBlocked):
        app.generate("_101545_1", ["official-lesson-5"], confirmed=False)
    assert relay.calls == client.creates == 0
    result = app.generate("_101545_1", ["official-lesson-5"], confirmed=True)
    assert result["course_id"] == "_101545_1"
    assert result["points_charged"] == 3
    assert client.pages[result["id"]]["parent"] == "quiz-center"
    assert "作答区" in json.dumps(client.blocks[result["id"]], ensure_ascii=False)
    assert app.generate("_101545_1", ["official-lesson-5"], confirmed=True)["reused"]
    assert relay.calls == 1
    sources.available = False
    with pytest.raises(OrganizeBlocked):
        app.generate("_101545_1", ["official-lesson-5"], confirmed=True)
    assert relay.calls == 1


def test_lost_notion_create_reply_recovers_page_without_new_charge_or_duplicate(tmp_path):
    app, client, relay, _ = service(tmp_path)
    client.lose_create = True
    with pytest.raises(TimeoutError):
        app.generate("_101545_1", ["official-lesson-5"], confirmed=True)
    result = app.generate("_101545_1", ["official-lesson-5"], confirmed=True)
    assert result["id"] == "quiz-1" and relay.calls == client.creates == 1


def test_unknown_model_charge_never_replays_automatically(tmp_path):
    app, client, relay, _ = service(tmp_path)
    relay.fail = True
    with pytest.raises(TimeoutError):
        app.generate("_101545_1", ["official-lesson-5"], confirmed=True)
    with pytest.raises(OrganizeBlocked, match="用量"):
        app.generate("_101545_1", ["official-lesson-5"], confirmed=True)
    assert relay.calls == 1 and client.creates == 0


def test_changed_accepted_version_creates_a_separate_question_set(tmp_path):
    app, client, relay, sources = service(tmp_path)
    first = app.generate("_101545_1", ["official-lesson-5"], confirmed=True)
    sources.revision = "accepted-v2"
    second = app.generate("_101545_1", ["official-lesson-5"], confirmed=True)
    assert first["id"] != second["id"] and relay.calls == 2
    assert len(app.list("_101545_1")["items"]) == 2


def test_incomplete_question_write_is_not_reported_as_success(tmp_path):
    app, client, relay, _ = service(tmp_path)
    client.drop_blocks = True
    with pytest.raises(OrganizeBlocked, match="验证"):
        app.generate("_101545_1", ["official-lesson-5"], confirmed=True)
    with pytest.raises(OrganizeBlocked):
        app.generate("_101545_1", ["official-lesson-5"], confirmed=True)
    assert relay.calls == client.creates == 1


def test_separate_instances_cannot_charge_the_same_local_operation_concurrently(tmp_path):
    import threading
    app, client, relay, sources = service(tmp_path)
    second = campus_quiz.CampusQuizService(app.settings, relay, sources=sources,
                                          client_factory=lambda: nullcontext(client))
    entered, release = threading.Event(), threading.Event()
    original = relay.quiz
    results = []
    def waiting(prompt):
        entered.set()
        assert release.wait(10)
        return original(prompt)
    relay.quiz = waiting
    worker = threading.Thread(target=lambda: results.append(app.generate('_101545_1', ['official-lesson-5'], confirmed=True)))
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(OrganizeBlocked, match='进程'):
            second.generate('_101545_1', ['official-lesson-5'], confirmed=True)
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and len(results) == 1 and relay.calls == 1


def test_native_quiz_api_requires_confirmation_and_releases_shared_gate(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pku_sync.panel.campus_quiz_api import add_campus_quiz_routes
    from pku_sync.panel.jobs import ExerciseJobGate
    service_app, client, relay, _ = service(tmp_path)
    app, gate = FastAPI(), ExerciseJobGate()
    add_campus_quiz_routes(app, service_app, gate)
    with TestClient(app) as http:
        url = '/api/campus/courses/_101545_1/quizzes'
        assert http.post(url, json={'lecture_ids': ['official-lesson-5']}).status_code == 409
        assert relay.calls == 0 and gate.current() is None
        gate.acquire('grading')
        assert http.post(url, json={'lecture_ids': ['official-lesson-5'], 'confirmed': True}).status_code == 409
        gate.release('grading')
        result = http.post(url, json={'lecture_ids': ['official-lesson-5'], 'confirmed': True,
                                    'prior_paper': '往年题仅供参考'})
        assert result.status_code == 200 and result.json()['id'] == 'quiz-1'
        assert '往年题仅供参考' in json.dumps(client.blocks['quiz-1'], ensure_ascii=False)
        assert gate.current() is None
        assert len(http.get(url).json()['items']) == 1


def test_many_selected_lectures_preserve_identity_within_notion_text_limits(tmp_path):
    app, client, relay, sources = service(tmp_path)
    ids = ['pku:recording:' + str(index).zfill(32) for index in range(40)]
    def resolve(course_id, selected):
        assert selected == sorted(ids)
        return {**sources.course(course_id), 'notes': [{'title': '第五讲', 'source_id': key,
                'note_id': 'note-' + str(index), 'remote_sha256': 'version', 'blocks': []}
                for index, key in enumerate(ids)]}
    sources.resolve = resolve
    original = relay.quiz
    relay.quiz = lambda prompt: original(prompt.replace(ids[0], 'stable-5'))
    result = app.generate('_101545_1', ids, confirmed=True)
    assert result['source_ids'] == ids
    assert client.creates == relay.calls == 1


@pytest.mark.parametrize('change', ['revision', 'content', 'workspace', 'center', 'unaccepted', 'unreadable'])
def test_source_change_while_model_runs_preserves_charge_and_blocks_publication(tmp_path, change):
    app, client, relay, sources = service(tmp_path)
    original_resolve, original_quiz = sources.resolve, relay.quiz
    changed = False

    def resolve(course_id, ids):
        scope = original_resolve(course_id, ids)
        if changed:
            if change == 'unaccepted':
                raise OrganizeBlocked(409, '总结已失去验收')
            if change == 'unreadable':
                raise TimeoutError('independent source read failed')
            if change == 'revision':
                scope['notes'][0]['remote_sha256'] = 'accepted-v2'
            elif change == 'content':
                scope['notes'][0]['blocks'] = [{'type': 'paragraph', 'content': 'changed'}]
            elif change == 'workspace':
                scope['workspace_id'] = 'other-home'
            elif change == 'center':
                scope['quiz_center_id'] = 'other-center'
        return scope

    def quiz(prompt):
        nonlocal changed
        response = original_quiz(prompt)
        changed = True
        return response

    sources.resolve, relay.quiz = resolve, quiz
    with pytest.raises((OrganizeBlocked, TimeoutError)):
        app.generate('_101545_1', ['official-lesson-5'], confirmed=True)
    assert relay.calls == 1 and client.creates == 0 and not client.pages
    records = list((tmp_path / 'panel/campus-quizzes').glob('*/*.json'))
    assert len(records) == 1
    record = json.loads(records[0].read_text('utf-8'))
    assert record['phase'] == 'relay_succeeded'
    assert record['response']['points_charged'] == 3
    assert '模拟答案' in record['response']['content'] or '\\u6a21' in record['response']['content']
    # Once the original accepted source is readable again, reuse the paid result.
    changed = False
    result = app.generate('_101545_1', ['official-lesson-5'], confirmed=True)
    assert result['points_charged'] == 3 and relay.calls == client.creates == 1


@pytest.mark.parametrize('boundary', ['reuse', 'create', 'receipt'])
def test_source_edit_during_notion_scan_cannot_verify_stale_quiz(tmp_path, boundary):
    app, client, relay, sources = service(tmp_path)
    if boundary == 'reuse':
        app.generate('_101545_1', ['official-lesson-5'], confirmed=True)
    original_pages = app._pages
    scans = 0

    def pages(*args, **kwargs):
        nonlocal scans
        matches = original_pages(*args, **kwargs)
        scans += 1
        target_scan = {'reuse': 2, 'create': 3, 'receipt': 4}[boundary]
        if scans == target_scan:
            sources.revision = 'accepted-v2'
        return matches

    app._pages = pages
    with pytest.raises(OrganizeBlocked):
        app.generate('_101545_1', ['official-lesson-5'], confirmed=True)
    assert relay.calls == 1
    assert client.creates == (0 if boundary == 'create' else 1)
    if boundary == 'receipt':
        assert not any(campus_quiz._COMPLETE in json.dumps(row, ensure_ascii=False)
                       for row in client.blocks['quiz-1'])
        record = json.loads(next((tmp_path / 'panel/campus-quizzes').glob('*/*.json')).read_text('utf-8'))
        assert record['phase'] == 'create_started' and record['response']['points_charged'] == 3


def test_paid_quiz_response_cannot_resume_at_a_changed_destination(tmp_path):
    app, client, relay, sources = service(tmp_path)
    original_pages, original_course = app._pages, sources.course
    stop_once = True
    moved = False

    def pages(*args, **kwargs):
        nonlocal stop_once
        if relay.calls and stop_once:
            stop_once = False
            raise TimeoutError('page scan after charging unavailable')
        return original_pages(*args, **kwargs)

    def course(course_id):
        scope = original_course(course_id)
        if moved:
            scope['quiz_center_id'] = 'other-center'
        return scope

    app._pages, sources.course = pages, course
    with pytest.raises(TimeoutError):
        app.generate('_101545_1', ['official-lesson-5'], confirmed=True)
    records = list((tmp_path / 'panel/campus-quizzes').glob('*/*.json'))
    original = records[0].read_bytes()
    moved = True
    with pytest.raises(OrganizeBlocked):
        app.generate('_101545_1', ['official-lesson-5'], confirmed=True)
    assert relay.calls == 1 and client.creates == 0
    assert records[0].read_bytes() == original
    moved = False
    result = app.generate('_101545_1', ['official-lesson-5'], confirmed=True)
    assert client.pages[result['id']]['parent'] == 'quiz-center'
    assert relay.calls == client.creates == 1
