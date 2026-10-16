"""Beta regressions: isolated catalog failures, local reading and MCP setup."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pku_sync.config import Settings
from pku_sync.mcp_tools import LocalCourseTools
from pku_sync.panel import notion_home, notion_workspace, recordings_api
from pku_sync.panel.student_ui import PANEL_COPY
from pku_sync.pipeline import collect_jobs


def seed(root, name='Networks', course_id='healthy'):
    folder = root / name
    (folder / 'recordings').mkdir(parents=True)
    (folder / 'course.json').write_text(json.dumps({'course_id': course_id, 'name': name, 'code': name}), 'utf8')
    (folder / 'recordings/index.json').write_text(json.dumps([
        {'course_id': course_id, 'title': '第1讲 TCP', 'recorded_at': '2026-10-01 08:00:00'}]), 'utf8')
    job = next(job for job in collect_jobs(root, course_id) if job.course_dir == folder)
    job.directory.mkdir(parents=True, exist_ok=True)
    (job.directory / 'notes.md').write_text(
        '## 课堂重点\n### TCP 可靠传输\n- 确认与重传\n  - 滑动窗口\n'
        '## 作业与考试口头线索\n- 下周复习拥塞控制\n', 'utf8')
    return folder, job


def api(root):
    settings = Settings(_env_file=None, data_dir=root, notion_token='fixture-notion', platform_token='')
    app = FastAPI()
    recordings_api.add_recording_routes(app, SimpleNamespace(settings=settings))
    return TestClient(app), settings


def test_corrupted_course_does_not_poison_healthy_courses(tmp_path, monkeypatch):
    seed(tmp_path)
    broken, _ = seed(tmp_path, 'Unprojected', 'unprojected')
    damaged, _ = seed(tmp_path, 'Corrupted', 'corrupted')
    (damaged / 'course.json').write_text('{broken', 'utf8')
    for section in ('materials', 'assignments', 'announcements'):
        (broken / section).mkdir()
        (broken / section / 'index.json').write_text('{broken', 'utf8')
    # A valid JSON scalar must degrade just as safely as invalid JSON.
    (damaged / 'recordings/index.json').write_text('null', 'utf8')
    client, settings = api(tmp_path)
    assert client.get('/api/campus/courses').status_code == 200
    assert client.get('/api/campus/courses/healthy').json()['title'] == 'Networks'
    assert client.get('/api/campus/courses/healthy/summary-preview').status_code == 200
    assert client.get('/api/campus/courses/unprojected').status_code == 200
    assert LocalCourseTools(tmp_path).get_course_summary('healthy')['summarized_lessons'] == 1

    workspace = {'schema': 2, 'active': True, 'id': 'home', 'parent_id': 'parent',
                 'courses': {'unprojected': {'id': 'old', 'lectures': {'retained': {}}}}}
    notion_workspace.manifest_path(tmp_path).write_text(json.dumps(workspace), 'utf8')
    from pku_sync import catalog_snapshots
    monkeypatch.setattr(catalog_snapshots, 'activate_account', lambda _: None)
    monkeypatch.setattr(catalog_snapshots, 'cached', lambda *args: None)
    # Only projection eligibility needs a snapshot; catalog endpoints retain real local reads.
    monkeypatch.setattr(notion_workspace, '_bind_history', lambda *args: ({'lessons': []}, None))
    monkeypatch.setattr(notion_workspace, '_verify_home', lambda *args: None)
    monkeypatch.setattr(notion_workspace, '_verify_historical_course', lambda *args: None)
    monkeypatch.setattr(notion_workspace, '_classify_course_children',
                        lambda *args: ({'每节课': {'id': 'center'}}, []))
    observed = []

    class Notion:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get_page(self, key): return {'id': key}
        def list_children(self, key): return []
        def list_child_pages(self, key):
            return [{'id': 'center', 'title': '每节课'}] if key == 'course-good' else []

    def course_page(client, home, row, **kwargs):
        observed.append(row['id'])
        if row['id'] == 'unprojected':
            raise ValueError('Course projection missing')
        return {'id': 'course-good'}

    monkeypatch.setattr(notion_workspace, 'NotionClient', lambda _: Notion())
    monkeypatch.setattr(notion_workspace, '_course_page', course_page)
    monkeypatch.setattr(notion_workspace, 'campus_courses', lambda _: {'courses': [
        {'id': 'unprojected'}, {'id': 'healthy'}]})
    monkeypatch.setattr(notion_workspace, 'campus_course', lambda *args: {'lessons': []})
    monkeypatch.setattr(catalog_snapshots, 'cached', lambda *args: {'projection_fixture': True})
    monkeypatch.setattr(notion_home, 'available_parents', lambda _: [{'id': 'parent'}])
    with pytest.raises(ValueError, match='projection missing'):
        notion_workspace.read_projection(settings)
    response = client.get('/api/campus/notion/parents')
    assert response.status_code == 200, response.text
    result = response.json()['workspace']
    assert result['courses']['healthy']['centers']['每节课']['id'] == 'center'
    assert result['courses']['unprojected'] == workspace['courses']['unprojected']
    assert observed[-2:] == ['unprojected', 'healthy']


def test_in_app_note_and_summary_preview_endpoints(tmp_path):
    _, job = seed(tmp_path)
    client, _ = api(tmp_path)
    key = recordings_api.recording_id(job)
    note = client.get(f'/api/campus/courses/healthy/recordings/{key}/note-preview')
    assert note.status_code == 200
    assert note.json()['available'] is True
    assert note.json()['topics'] == ['TCP 可靠传输']
    assert note.json()['clues'] == ['下周复习拥塞控制']
    assert '滑动窗口' in note.json()['markdown']
    summary = client.get('/api/campus/courses/healthy/summary-preview').json()
    assert summary['total_lessons'] == summary['summarized_lessons'] == 1
    assert 'TCP 可靠传输' in summary['markdown']
    assert client.get(f'/api/campus/courses/other/recordings/{key}/note-preview').status_code == 404
    assert client.get('/api/campus/courses/missing/summary-preview').status_code == 404
    (job.directory / 'notes.md').unlink()
    assert client.get(f'/api/campus/courses/healthy/recordings/{key}/note-preview').json()['available'] is False


def test_catalog_note_topics_are_bounded_and_tolerate_unreadable_notes(tmp_path, monkeypatch):
    from pku_sync.panel.campus_catalog import campus_course
    _, job = seed(tmp_path)
    notes = job.directory / 'notes.md'
    notes.write_text('## 课堂重点\n### TCP\n### 滑动窗口\n### 拥塞控制\n', 'utf8')
    assert campus_course(tmp_path, 'healthy')['lessons'][0]['note_topic'] == 'TCP · 滑动窗口'
    read_text = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == notes:
            raise OSError('note temporarily unavailable')
        return read_text(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'read_text', unreadable)
        assert campus_course(tmp_path, 'healthy')['lessons'][0]['note_topic'] == ''
    notes.unlink()
    assert campus_course(tmp_path, 'healthy')['lessons'][0]['note_topic'] == ''


def test_mcp_semantic_tools_and_panel_hub(tmp_path):
    folder, job = seed(tmp_path)
    tools = LocalCourseTools(tmp_path)
    for query in ('healthy', 'Networks', 'network'):
        assert tools.get_course_summary(query)['summarized_lessons'] == 1
    assert tools.get_course_summary()['courses'][0]['total_lessons'] == 1
    assert tools.get_lecture_notes('healthy', '2026-10-01')[0]['title'] == job.recording.title
    assert tools.get_lecture_notes('healthy', 'absent') == []
    assert tools.search_course_knowledge('TCP')[0]['source_type'] == 'notes'
    for section in ('assignments', 'announcements'):
        (folder / section).mkdir()
        (folder / section / 'index.json').write_text(json.dumps([
            {'title': 'TCP deadline', 'instructions': 'TCP exercises', 'private_url': 'do-not-expose'}]), 'utf8')
    (job.directory / 'transcript.json').write_text(json.dumps({'segments': [{'text': 'TCP lecture'}]}), 'utf8')
    hits = tools.search_course_knowledge('tcp')
    assert {row['source_type'] for row in hits} == {'notes', 'assignments', 'announcements', 'transcript'}
    assert 'do-not-expose' not in json.dumps(hits)
    assert tools.search_course_knowledge(' ') == []
    client, _ = api(tmp_path)
    status = client.get('/api/mcp/status').json()
    assert status['ok'] and status['courses_count'] == status['notes_count'] == 1
    assert {host['id'] for host in status['hosts']} == {'codex', 'claude', 'antigravity', 'oh-my-pi', 'opencode'}
    assert '\\' not in status['launcher_path'] + status['python_path']
    for host in status['hosts']:
        assert tmp_path.as_posix() in host['config_snippet']
        if host['id'] != 'codex':
            json.loads(host['config_snippet'])
    import tomllib
    codex = next(host for host in status['hosts'] if host['id'] == 'codex')
    assert tomllib.loads(codex['config_snippet'])['mcp_servers']['pku-sync']['env']['DATA_DIR'] == tmp_path.as_posix()


@pytest.mark.parametrize('metadata', ['catalog', 'recording', 'missing', 'malformed', 'scalar'])
def test_knowledge_search_lecture_identity_and_source_priority(tmp_path, metadata):
    folder = tmp_path / '计算机网络'
    recordings = folder / 'recordings'
    recordings.mkdir(parents=True)
    (folder / 'course.json').write_text(json.dumps({'course_id': 'net', 'name': '计算机网络'}), 'utf8')
    entry = {'course_id': 'net', 'title': '第三讲：可靠传输', 'recorded_at': '2026-10-08 08:00:00'}
    (recordings / 'index.json').write_text(json.dumps([entry] if metadata == 'catalog' else []), 'utf8')
    directory = collect_jobs(tmp_path, 'net')[0].directory if metadata == 'catalog' else recordings / 'opaque-id'
    directory.mkdir(parents=True)
    if metadata in {'recording', 'malformed', 'scalar'}:
        value = json.dumps(entry) if metadata == 'recording' else '{broken' if metadata == 'malformed' else 'null'
        (directory / 'recording.json').write_text(value, 'utf8')
    text = '滑动窗口 可靠传输 TCP'
    (directory / 'notes.md').write_text(text, 'utf8')
    (directory / 'transcript.json').write_text(json.dumps({'segments': [{'text': text}]}), 'utf8')
    for section in ('assignments', 'announcements'):
        (folder / section).mkdir()
        (folder / section / 'index.json').write_text(json.dumps([{'instructions': text}]), 'utf8')
    tools = LocalCourseTools(tmp_path)
    hits = tools.search_course_knowledge('滑动窗口，可靠传输；tcp', 'net')
    assert [hit['source_type'] for hit in hits] == ['notes', 'assignments', 'announcements', 'transcript']
    for hit in (hits[0], hits[-1]):
        assert hit['title'] == (entry['title'] if metadata in {'catalog', 'recording'} else directory.name)
        if metadata in {'catalog', 'recording'}:
            assert hit['date'] == '2026-10-08'
        else:
            assert 'date' not in hit
    # Content relevance still takes priority over the source-type tie breaker.
    (directory / 'transcript.json').write_text(json.dumps({'text': text + ' ' + text}), 'utf8')
    assert tools.search_course_knowledge('TCP')[0]['source_type'] == 'transcript'


@pytest.mark.parametrize('separator', [' ', '\n', ',', '，', '、', '。', ';', '；', ':', '：', '!', '！', '?', '？', '/', '|'])
def test_knowledge_search_splits_chinese_query_punctuation(tmp_path, separator):
    _, job = seed(tmp_path)
    (job.directory / 'notes.md').write_text('滑动窗口和可靠传输', 'utf8')
    tools = LocalCourseTools(tmp_path)
    hits = tools.search_course_knowledge(separator.join(['滑动窗口', '可靠传输']))
    assert len(hits) == 1 and hits[0]['source_type'] == 'notes'
    assert hits[0]['title'] == job.recording.title
    assert tools.search_course_knowledge(separator * 2) == []


def test_knowledge_search_source_priority_across_courses(tmp_path):
    first, first_job = seed(tmp_path, 'Alpha', 'alpha')
    second, second_job = seed(tmp_path, 'Beta', 'beta')
    for job in (first_job, second_job):
        (job.directory / 'notes.md').write_text('可靠传输', 'utf8')
    (first_job.directory / 'transcript.json').write_text(json.dumps({'text': '可靠传输'}), 'utf8')
    for folder, section in ((first, 'assignments'), (second, 'announcements')):
        (folder / section).mkdir()
        (folder / section / 'index.json').write_text(json.dumps([{'text': '可靠传输'}]), 'utf8')
    hits = LocalCourseTools(tmp_path).search_course_knowledge('可靠传输')
    assert [(hit['source_type'], hit['course']) for hit in hits] == [
        ('notes', 'Alpha'), ('notes', 'Beta'), ('assignments', 'Alpha'),
        ('announcements', 'Beta'), ('transcript', 'Alpha')]


def test_in_place_lesson_filter_in_edge():
    pw = pytest.importorskip('playwright.sync_api')
    from browser_support import EDGE, browser_launch_options
    if not EDGE.is_file():
        pytest.skip('Microsoft Edge is required')
    static = Path(__file__).parents[1] / 'pku_sync/panel/static'
    script = (static / 'app.js').read_text('utf8')
    script = script[:script.index('  window.setInterval(refreshAccount, 8000);')] + (
        'window.__beta = {state: state, render: render};})();')
    errors, requests = [], []

    def serve(route):
        path = urlsplit(route.request.url).path
        if path == '/':
            route.fulfill(content_type='text/html; charset=utf-8', body=
                '<!doctype html><meta charset="utf-8"><meta name="viewport" '
                'content="width=device-width, initial-scale=1"><div id="app"></div><div id="toast"></div>')
        else:
            requests.append(path)
            route.fulfill(json={})

    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(**browser_launch_options(), headless=True)
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.add_init_script("localStorage.setItem('pku-panel-welcome-v1','seen')")
            page.route('http://panel.test/**', serve)
            page.goto('http://panel.test/')
            page.evaluate('copy => window.PANEL_COPY = copy', PANEL_COPY)
            page.add_style_tag(content=(static / 'app.css').read_text('utf8'))
            page.add_script_tag(content=script)
            page.evaluate('''() => {
                const s = window.__beta.state;
                s.activated = true; s.quota = {active:true};
                s.connection.connected = true; s.campus.configured = true;
                s.view = 'campus-course'; s.campus.courseId = 'course'; s.campus.section = 'home';
                s.campus.course = {id:'course', title:'计算机网络', lessons:[],
                    unassigned_materials:[], all_materials:[], assignments:[],
                    announcements:[], announcement_tasks:[], summary:{}};
                window.__beta.render();
            }''')
            search = page.locator('#course-lessons > .section-label > [data-campus-lesson-filter]')
            cards = page.locator('#course-lessons [data-lesson-card]')
            lessons = [
                {'id': 'date', 'title': '开学导论', 'date': '2026-09-10', 'materials': [],
                 'notes_available': False, 'notion_note_url': 'https://www.notion.so/legacy-note'},
                {'id': 'topic', 'title': '第二讲', 'date': '2026-09-17', 'note_topic': 'TCP 可靠传输', 'materials': []},
                {'id': 'material', 'title': '第三讲', 'date': '2026-09-24', 'materials': [{'title': '滑动窗口 Slides.pdf'}, {}]},
                {'id': 'title', 'title': 'DNS 协议', 'materials': []},
            ]
            for count in (0, 1, 2, 3, 4, 16, 32):
                rows = lessons[:count] if count <= 4 else [dict(lessons[n % 4], id=f'lesson-{n}') for n in range(count)]
                page.evaluate('rows => {window.__beta.state.campus.course.lessons = rows; window.__beta.render()}', rows)
                pw.expect(search).to_have_count(int(count >= 4))
                pw.expect(cards).to_have_count(count)
            page.evaluate('rows => {window.__beta.state.campus.course.lessons = rows; window.__beta.render()}', lessons)
            pw.expect(page.locator('[data-lesson-card="date"] .recording-published')).to_have_count(0)
            pw.expect(search).to_have_attribute('aria-label', '筛选讲次')
            for query, lesson_id in [('09-10', 'date'), (' tcp ', 'topic'), ('sLiDeS', 'material'), ('滑动窗口', 'material'), ('dns', 'title')]:
                search.fill(query)
                pw.expect(search).to_be_focused()
                pw.expect(search).to_have_value(query)
                pw.expect(cards).to_have_count(1)
                pw.expect(cards).to_have_attribute('data-lesson-card', lesson_id)
            search.fill('')
            search.press_sequentially('TCP')
            assert search.evaluate('(el) => [el.selectionStart, el.selectionEnd]') == [3, 3]
            search.press('ArrowLeft')
            search.press('Backspace')
            pw.expect(search).to_have_value('TP')
            assert search.evaluate('(el) => [el.selectionStart, el.selectionEnd]') == [1, 1]
            search.fill('TCP')
            search.evaluate("el => el.setSelectionRange(0, 2, 'backward')")
            page.evaluate('window.__beta.render()')
            pw.expect(search).to_be_focused()
            assert search.evaluate('(el) => [el.selectionStart, el.selectionEnd, el.selectionDirection]') == [0, 2, 'backward']
            for query in ('不存在的主题', '\"><img src=x onerror=alert(1)>'):
                search.fill(query)
                pw.expect(cards).to_have_count(0)
                pw.expect(search).to_be_visible()
                pw.expect(search).to_be_focused()
                pw.expect(search).to_have_value(query)
                pw.expect(page.locator('#course-lessons .empty-materials')).to_have_text('没有找到匹配的讲次。')
                pw.expect(page.locator('#course-lessons img')).to_have_count(0)
                assert '暂时没有录像' not in page.locator('#course-lessons').inner_text()
            page.set_viewport_size({'width': 390, 'height': 900})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert search.evaluate('(el) => el.getBoundingClientRect().right <= innerWidth')
            search.fill('   ')
            pw.expect(cards).to_have_count(4)
            pw.expect(page.locator('#course-lessons .empty-materials')).to_have_count(0)
            search.fill('')
            pw.expect(cards).to_have_count(4)
            page.evaluate("window.__beta.state.campus.dateQuery = 'absent'; window.__beta.state.campus.lessonId = 'date'")
            for section in ('lecture', 'materials', 'assignments', 'notices', 'settings'):
                page.evaluate('section => {window.__beta.state.campus.section = section; window.__beta.render()}', section)
                pw.expect(page.locator('[data-campus-lesson-filter]')).to_have_count(0)
                if section == 'lecture':
                    pw.expect(page.locator('#course-lessons .recording-row')).to_have_count(1)
            page.evaluate('''() => {
                const s = window.__beta.state;
                s.view = 'dashboard';
                s.campus.courses = [{id:'course', title:'计算机网络', code:'CS101', recordings:4}];
                window.__beta.render();
            }''')
            course_search = page.locator('[data-campus-search]')
            course_search.fill('计算机网络')
            for start, end, direction in ((1, 1, 'forward'), (1, 4, 'backward'), (0, 5, 'forward')):
                course_search.evaluate('(el, range) => el.setSelectionRange(...range)', [start, end, direction])
                page.evaluate('window.__beta.render()')
                pw.expect(course_search).to_be_focused()
                pw.expect(course_search).to_have_value('计算机网络')
                assert course_search.evaluate('(el) => [el.selectionStart, el.selectionEnd, el.selectionDirection]') == [start, end, direction]
            course_search.press('x')
            pw.expect(course_search).to_have_value('x')
            pw.expect(course_search).to_be_focused()
            assert requests == []
            assert errors == []
        finally:
            browser.close()


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_local_reading_search_and_material_filter_in_edge(width):
    pw = pytest.importorskip('playwright.sync_api')
    from browser_support import EDGE, browser_launch_options
    from grading_workspace import PANEL_FORBIDDEN_STRINGS
    if not EDGE.is_file():
        pytest.skip('Microsoft Edge is required')
    static = Path(__file__).parents[1] / 'pku_sync/panel/static'
    script = (static / 'app.js').read_text('utf8')
    script = script[:script.index('  window.setInterval(refreshAccount, 8000);')] + (
        'window.__beta = {state, render, openCampusCourse};})();')
    markdown = ('## TCP theorem\n' + 'Local context.\n' * 35 +
                'TCP tcp **TCP**\nUse a+b, <term> & formula.\n' + 'Background.\n' * 35 +
                '![TCP diagram](/assets/TCP-diagram.svg)\n## Details\nLocal review.')
    course = {
        'id': 'course', 'title': '计算机网络',
        'lessons': [{'id': 'first', 'title': '可靠传输', 'notes_available': True,
                     'transcript_available': True, 'materials': [
                         {'index': 0, 'title': '窗口讲义', 'body_text': '流量控制', 'files': ['Transport.pdf']}]}],
        'unassigned_materials': [
            {'index': 1, 'title': '课程大纲', 'category': 'information', 'body_text': '考核权重', 'files': []},
            {'index': 2, 'title': '补充练习', 'body_text': '拥塞算法', 'files': []},
            {'index': 3, 'title': '参考附件', 'files': ['Review-Handout.pdf']}],
        'assignments': [], 'announcements': [], 'announcement_tasks': [], 'summary': {},
    }
    errors, requests = [], []

    def serve(route):
        path = urlsplit(route.request.url).path
        if path == '/':
            route.fulfill(content_type='text/html; charset=utf-8', body=
                '<!doctype html><meta charset="utf-8"><meta name="viewport" '
                'content="width=device-width, initial-scale=1"><div id="app"></div><div id="toast"></div>')
        elif path.endswith(('/summary-preview', '/note-preview')):
            requests.append(path)
            route.fulfill(json={'markdown': markdown, 'visuals': []})
        elif path == '/assets/TCP-diagram.svg':
            route.fulfill(content_type='image/svg+xml', body=
                '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80"></svg>')
        elif path == '/api/campus/courses/course':
            requests.append(path)
            route.fulfill(json=course)
        else:
            requests.append(path)
            route.fulfill(json={})

    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(**browser_launch_options(), headless=True)
        try:
            page = browser.new_page(viewport={'width': width, 'height': 900})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.add_init_script("localStorage.setItem('pku-panel-welcome-v1','seen')")
            page.route('http://panel.test/**', serve)
            page.goto('http://panel.test/')
            page.evaluate('copy => window.PANEL_COPY = copy', PANEL_COPY)
            page.add_style_tag(content=(static / 'app.css').read_text('utf8'))
            page.add_script_tag(content=script)
            page.evaluate('''course => {
                const s = window.__beta.state;
                s.activated = true; s.quota = {active:true};
                s.connection.connected = true; s.campus.configured = true;
                s.view = 'campus-course'; s.campus.courseId = course.id;
                s.campus.course = course; s.campus.section = 'materials';
                window.__beta.render();
            }''', course)
            material_search = page.locator('[data-campus-material-filter]')
            materials = page.locator('[data-materials-linked] > ul > li, '
                                     '[data-materials-info] > ul > li, [data-materials-unassigned] > ul > li')
            # The threshold includes linked and unassigned materials together.
            for count in (0, 1, 2, 3):
                page.evaluate('''rows => {
                    window.__beta.state.campus.course.unassigned_materials = rows;
                    window.__beta.render();
                }''', course['unassigned_materials'][:count])
                pw.expect(material_search).to_have_count(int(count == 3))
            for query, title in [('窗口', '窗口讲义'), ('流量', '窗口讲义'), ('transport.PDF', '窗口讲义'),
                                 ('可靠传输', '窗口讲义'), ('大纲', '课程大纲'), ('考核权重', '课程大纲'),
                                 ('拥塞', '补充练习'), ('review-HANDOUT', '参考附件')]:
                material_search.fill(' ' + query + ' ')
                pw.expect(material_search).to_be_focused()
                pw.expect(materials).to_have_count(1)
                pw.expect(materials.locator('strong')).to_have_text(title)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            material_search.fill('')
            material_search.press_sequentially('Transport')
            pw.expect(material_search).to_have_value('Transport')
            for start, end, direction in ((2, 2, 'forward'), (1, 5, 'backward')):
                material_search.evaluate('(el, range) => el.setSelectionRange(...range)', [start, end, direction])
                page.evaluate('window.__beta.render()')
                pw.expect(material_search).to_be_focused()
                assert material_search.evaluate('(el) => [el.selectionStart, el.selectionEnd, el.selectionDirection]') == [start, end, direction]
            material_search.press('x')
            pw.expect(material_search).to_have_value('Txport')
            for query in ('无此资料', '\"><img src=x onerror=alert(1)>'):
                material_search.fill(query)
                pw.expect(materials).to_have_count(0)
                pw.expect(page.locator('.empty-materials')).to_have_text('没有找到匹配的课程资料。')
                pw.expect(material_search).to_be_focused()
                pw.expect(page.locator('img')).to_have_count(0)
            material_search.fill('   ')
            pw.expect(materials).to_have_count(4)
            pw.expect(page.locator('.empty-materials')).to_have_count(0)
            summary_toggle = page.locator('.materials-summary-panel [data-action="campus-preview-course-summary"]')
            reader = page.locator('.materials-summary-panel .lecture-note-reader')
            summary_toggle.click()
            pw.expect(reader.locator('.study-markdown')).to_contain_text('Local review.')
            pw.expect(summary_toggle).to_have_text('收起课程总结')
            assert page.evaluate('window.__beta.state.campus.section') == 'materials'
            assert requests == ['/api/campus/courses/course/summary-preview']
            summary_toggle.click()
            pw.expect(reader).to_have_count(0)
            summary_toggle.click()
            pw.expect(reader.locator('[data-reader-search]')).to_be_visible()
            # Both local summary and lecture readers share safe highlighting and focus handling.
            for section in ('materials', 'lecture'):
                if section == 'lecture':
                    page.evaluate('''() => {
                        window.__beta.state.campus.section = 'home'; window.__beta.render();
                    }''')
                    page.locator('[data-action="campus-open-lesson"]').click()
                reader = page.locator('.lecture-note-reader')
                search = reader.locator('[data-reader-search]')
                content = reader.locator('.study-markdown')
                image_attributes = content.locator('img').evaluate('(el) => Array.from(el.attributes, a => [a.name, a.value])')
                search.fill('')
                search.press_sequentially('tCp')
                pw.expect(search).to_be_focused()
                pw.expect(search).to_have_value('tCp')
                assert search.evaluate('(el) => [el.selectionStart, el.selectionEnd]') == [3, 3]
                pw.expect(content.locator('mark.study-search-hit')).to_have_count(5)
                pw.expect(reader.locator('[data-reader-search-count]')).to_have_text('找到 5 处')
                hits = content.locator('mark.study-search-hit')
                active = content.locator('mark.study-search-hit-active[data-active-search-hit="true"]')
                pw.expect(active).to_have_count(1)
                pw.expect(hits.nth(0)).to_have_class('study-search-hit study-search-hit-active')
                for key, index in [('Enter', 1), ('Enter', 2), ('Shift+Enter', 1),
                                   ('Shift+Enter', 0), ('Shift+Enter', 4), ('Enter', 0)]:
                    search.press(key)
                    pw.expect(search).to_be_focused()
                    pw.expect(search).to_have_value('tCp')
                    pw.expect(active).to_have_count(1)
                    pw.expect(hits.nth(index)).to_have_attribute('data-active-search-hit', 'true')
                    assert active.evaluate('''el => {
                        const r = el.getBoundingClientRect();
                        return r.top >= 0 && r.bottom <= innerHeight;
                    }''')
                search.press('Shift+Enter')
                search.fill('TCP ')
                pw.expect(hits.nth(0)).to_have_attribute('data-active-search-hit', 'true')
                search.press('Escape')
                pw.expect(search).to_have_value('')
                pw.expect(search).to_be_focused()
                pw.expect(content.locator('mark')).to_have_count(0)
                pw.expect(reader.locator('[data-reader-search-count]')).to_have_count(0)
                search.fill('tCp')
                pw.expect(hits.nth(0)).to_have_attribute('data-active-search-hit', 'true')
                assert active.evaluate('el => getComputedStyle(el).backgroundColor') == 'rgb(250, 204, 21)'
                for start, end, direction in ((1, 1, 'forward'), (0, 2, 'backward')):
                    search.evaluate('(el, range) => el.setSelectionRange(...range)', [start, end, direction])
                    page.evaluate('window.__beta.render()')
                    pw.expect(search).to_be_focused()
                    assert search.evaluate('(el) => [el.selectionStart, el.selectionEnd, el.selectionDirection]') == [start, end, direction]
                search.press('x')
                pw.expect(search).to_have_value('xp')
                for query, count in [(' TCP ', 5), ('a+b', 1), ('<term>', 1), ('src', 0),
                                     ('alt', 0), ('note-sec', 0), ('img', 0), ('[.*', 0),
                                     ('\"><img onerror=alert(1)>', 0)]:
                    search.fill(query)
                    pw.expect(search).to_be_focused()
                    pw.expect(content.locator('mark.study-search-hit')).to_have_count(count)
                    pw.expect(reader.locator('[data-reader-search-count]')).to_have_text(f'找到 {count} 处' if count else '无匹配')
                    search.press('Enter')
                    pw.expect(search).to_be_focused()
                    pw.expect(active).to_have_count(int(count > 0))
                    assert content.locator('img').evaluate('(el) => Array.from(el.attributes, a => [a.name, a.value])') == image_attributes
                    pw.expect(content.locator('#note-sec-1')).to_have_text('TCP theorem')
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                for query in ('', '   '):
                    search.fill(query)
                    pw.expect(content.locator('mark')).to_have_count(0)
                    pw.expect(reader.locator('[data-reader-search-count]')).to_have_count(0)
                for needle in PANEL_FORBIDDEN_STRINGS:
                    assert needle not in page.locator('#app').inner_text()
                assert page.locator('#app a[href^="http"]').count() == 0
            # Opening a course clears the material filter for its next visit.
            page.evaluate("window.__beta.state.campus.materialQuery = 'old'; window.__beta.openCampusCourse('course')")
            pw.expect(page.locator('.course-summary-spotlight')).to_be_visible()
            assignment_center = page.locator('.campus-centers [data-center="assignments"]')
            pw.expect(assignment_center.locator('.center-count')).to_have_text('0 项记录')
            assert assignment_center.get_attribute('data-urgent-assignments') is None
            assert page.evaluate('window.__beta.state.campus.materialQuery') == ''
            assert errors == []
        finally:
            browser.close()


def seed_keyframe_materials(root):
    from test_slide_alignment import _write_pdf, _deck_rectangles, _projector_capture
    cv2 = pytest.importorskip('cv2')
    pdfium = pytest.importorskip('pypdfium2')
    folder, old_job = seed(root)
    recording = old_job.recording.model_copy(update={
        'title': '2026-10-08第3-4节', 'recorded_at': '2026-10-08 08:00:00'})
    (folder / 'recordings/index.json').write_text(json.dumps([recording.model_dump()]), 'utf8')
    job = collect_jobs(root, 'healthy')[0]
    job.directory.mkdir(parents=True, exist_ok=True)
    (job.directory / 'notes.md').write_bytes((old_job.directory / 'notes.md').read_bytes())
    materials = folder / 'materials'
    materials.mkdir()
    names = ['ch01-绪论.pdf', 'ch02-TCP滑动窗口.pdf']
    _write_pdf(materials / names[0], [[(350, 10, 15, 200), (20, 10, 15, 200)]])
    _write_pdf(materials / names[1], _deck_rectangles(2))
    rows = [{'path': name, 'title': name, 'kind': '课件', 'files': [name],
             'attachments': [{'filename': name, 'path': f'/bbcswebdav/{i}'}]}
            for i, name in enumerate(names)]
    (materials / 'index.json').write_text(json.dumps(rows), 'utf8')
    frames = job.directory / 'keyframes'
    frames.mkdir()
    document = pdfium.PdfDocument(str(materials / names[1]))
    try:
        page = document[0]
        bitmap = page.render(scale=2)
        try:
            gray = cv2.cvtColor(bitmap.to_numpy(), cv2.COLOR_RGB2GRAY)
            cv2.imencode('.jpg', _projector_capture(gray))[1].tofile(str(frames / 'frame.jpg'))
        finally:
            bitmap.close()
            page.close()
    finally:
        document.close()
    (frames / 'index.json').write_text(json.dumps([{'file': 'frame.jpg', 'timestamp': 0}]), 'utf8')
    return folder, job


def test_post_download_keyframe_slide_matching(tmp_path):
    from pku_sync.lecture_source_download import prepare_lesson_sources
    from pku_sync.panel.campus_catalog import campus_course
    folder, job = seed_keyframe_materials(tmp_path)
    client, settings = api(tmp_path)
    key = recordings_api.recording_id(job)
    endpoint = f'/api/campus/courses/healthy/recordings/{key}/match-slides'
    assert client.post(endpoint.replace('/healthy/', '/other/')).status_code == 404
    response = client.post(endpoint)
    assert response.status_code == 200, response.text
    assert response.json()['keyframe_matched_count'] == 1
    overrides = json.loads((folder / 'materials/matches.json').read_text('utf8'))
    assert overrides == {'ch02-TCP滑动窗口.pdf': key}
    course = campus_course(tmp_path, 'healthy')
    lesson = course['lessons'][0]
    assert lesson['materials'][0]['match_source'] == 'keyframe'
    assert lesson['materials'][0]['matched_frames'] == 1
    assert course['unassigned_materials'][0]['path'] == 'ch01-绪论.pdf'
    paths, failures = prepare_lesson_sources(job, key, settings)
    assert len(paths) == 1 and paths[0].is_file() and not failures
    assert client.post(endpoint).json()['keyframe_matched_count'] == 1
    (job.directory / 'keyframes/index.json').unlink()
    assert client.post(endpoint).status_code == 409


def test_download_automatically_matches_keyframes(tmp_path):
    folder, job = seed_keyframe_materials(tmp_path)
    # The download manager also matches a recording already saved locally.
    job.video.write_bytes(b'cached video')
    _, settings = api(tmp_path)
    manager = recordings_api.RecordingWorkManager(settings)
    key = recordings_api.recording_id(job)
    assert manager._download(job, key) == f'/api/recordings/{key}/video'
    assert json.loads((folder / 'materials/matches.json').read_text('utf8')) == {
        'ch02-TCP滑动窗口.pdf': key}


@pytest.mark.parametrize('override', ['ignore', 'another-recording'])
def test_keyframe_matching_preserves_explicit_assignments(tmp_path, override):
    from pku_sync.lecture_source_download import match_materials_by_keyframes
    folder, job = seed_keyframe_materials(tmp_path)
    overrides = {'ch02-TCP滑动窗口.pdf': override}
    path = folder / 'materials/matches.json'
    path.write_text(json.dumps(overrides), 'utf8')
    assert match_materials_by_keyframes(job, recordings_api.recording_id(job)) == []
    assert json.loads(path.read_text('utf8')) == overrides


def test_keyframe_matching_without_material_index_is_noop(tmp_path, monkeypatch):
    from pku_sync import lecture_source_download as sources
    _, job = seed(tmp_path)
    job.video.write_bytes(b'dummy video')
    def unexpected(*args, **kwargs):
        pytest.fail('no materials must not trigger extraction or network access')
    monkeypatch.setattr(sources, 'extract_keyframes', unexpected)
    monkeypatch.setattr('pku_sync.auth.get_session', unexpected)
    assert sources.match_materials_by_keyframes(job, recordings_api.recording_id(job)) == []


def test_source_preparation_matches_existing_keyframes_offline(tmp_path):
    from pku_sync.lecture_source_download import prepare_lesson_sources
    folder, job = seed_keyframe_materials(tmp_path)
    _, settings = api(tmp_path)
    paths, failures = prepare_lesson_sources(job, recordings_api.recording_id(job), settings)
    assert len(paths) == 1 and paths[0].is_file() and not failures
    assert paths[0].is_relative_to(folder / 'materials/.lecture-sources')


def test_inline_lecture_pager_and_note_outline_in_edge():
    pw = pytest.importorskip('playwright.sync_api')
    from browser_support import EDGE, browser_launch_options
    if not EDGE.is_file():
        pytest.skip('Microsoft Edge is required')
    static = Path(__file__).parents[1] / 'pku_sync/panel/static'
    script = (static / 'app.js').read_text('utf8')
    script = script[:script.index('  window.setInterval(refreshAccount, 8000);')] + (
        'window.__beta = {state: state, render: render};})();')
    long_heading = '章节内容' * 40 + ' <img onerror="window.noteXss = true"> & 复习'
    notes = {
        'first': '## **基础**\n正文\n### `例题`\n正文\n#### 细节\n```\n## 代码中的标题\n```\n'
                 '```python\nif a < b:\n    print("<button> & 代码")',
        'middle': '## **基础**\n' + '正文\n\n' * 35 + '### `例题`\n正文\n## ' + long_heading,
        'last': '只有正文，没有章节标题。',
    }
    errors = []

    def serve(route):
        path = urlsplit(route.request.url).path
        if path == '/':
            route.fulfill(content_type='text/html; charset=utf-8', body=
                '<!doctype html><meta charset="utf-8"><meta name="viewport" '
                'content="width=device-width, initial-scale=1"><div id="app"></div><div id="toast"></div>')
        elif path.endswith('/note-preview'):
            route.fulfill(json={'markdown': notes[path.split('/')[-2]], 'visuals': []})
        else:
            route.fulfill(json={})

    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(**browser_launch_options(), headless=True)
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.add_init_script("localStorage.setItem('pku-panel-welcome-v1','seen')")
            page.add_init_script("Object.defineProperty(navigator, 'clipboard', {value:{writeText: async text => {window.copiedStudyText = text;}}})")
            page.route('http://panel.test/**', serve)
            page.goto('http://panel.test/')
            page.evaluate('copy => window.PANEL_COPY = copy', PANEL_COPY)
            page.add_style_tag(content=(static / 'app.css').read_text('utf8'))
            page.add_script_tag(content=script)
            page.evaluate('''() => {
                const s = window.__beta.state;
                s.activated = true; s.quota = {active:true};
                s.connection.connected = true; s.campus.configured = true;
                s.view = 'campus-course'; s.campus.courseId = 'course';
                s.campus.course = {id:'course', title:'测试课程',
                    lessons:[{id:'first', title:'第一讲', materials:[], notes_available:true,
                        transcript_available:true}], unassigned_materials:[], all_materials:[],
                    assignments:[], announcements:[], announcement_tasks:[], summary:{}};
                s.campus.section = 'lecture'; s.campus.lessonId = 'first';
                window.__beta.render();
            }''')
            pw.expect(page.locator('.breadcrumb-pager')).to_have_count(0)
            page.evaluate('''() => {
                const s = window.__beta.state;
                s.campus.course.lessons.push({...s.campus.course.lessons[0], id:'middle', title:'第二讲'});
                s.campus.section = 'home'; window.__beta.render();
            }''')
            pw.expect(page.locator('.breadcrumb-pager')).to_have_count(0)
            page.locator('[data-action="campus-open-lesson"][data-recording="first"]').click()
            pager = page.locator('.breadcrumb > .breadcrumb-pager')
            prev = pager.get_by_role('button', name='上一讲', exact=True)
            next_ = pager.get_by_role('button', name='下一讲', exact=True)
            pw.expect(prev).to_be_disabled()
            pw.expect(prev).to_have_attribute('data-recording', '')
            pw.expect(next_).to_be_enabled()
            pw.expect(next_).to_have_attribute('data-recording', 'middle')
            pw.expect(prev).to_have_attribute('title', '上一讲 ([)')
            pw.expect(next_).to_have_attribute('title', '下一讲 (])')
            pw.expect(page.locator('.study-markdown h3')).to_have_text('例题')
            pw.expect(page.locator('.note-outline')).to_have_count(0)
            pw.expect(page.locator('.note-outline-footer')).to_have_count(0)
            pw.expect(page.locator('.study-markdown [id^="note-sec-"]')).to_have_count(2)
            code_wraps = page.locator('.study-markdown .study-code-wrap')
            pw.expect(code_wraps).to_have_count(2)
            code_copy = code_wraps.last.locator('[data-action="campus-copy-code"]')
            pw.expect(code_wraps.last.locator('pre > code')).to_have_text('if a < b:\n    print("<button> & 代码")')
            pw.expect(code_wraps.last.locator('code button')).to_have_count(0)
            pw.expect(code_copy).to_have_css('opacity', '0')
            code_wraps.last.hover()
            pw.expect(code_copy).to_have_css('opacity', '1')
            code_copy.click()
            pw.expect(page.locator('#toast')).to_have_text('已复制代码，可直接粘贴')
            assert page.evaluate('window.copiedStudyText') == 'if a < b:\n    print("<button> & 代码")\n'
            code_wraps.first.hover()
            code_wraps.first.locator('[data-action="campus-copy-code"]').click()
            assert page.evaluate('window.copiedStudyText') == '## 代码中的标题\n'
            page.mouse.move(0, 0)
            page.keyboard.press('Tab')
            code_copy.focus()
            pw.expect(code_copy).to_have_css('opacity', '1')
            touch_page = browser.new_page(has_touch=True, viewport={'width': 390, 'height': 900})
            try:
                touch_page.set_content(code_wraps.last.evaluate('(el) => el.outerHTML'))
                touch_page.add_style_tag(content=(static / 'app.css').read_text('utf8'))
                assert touch_page.evaluate('matchMedia("(hover: none)").matches')
                pw.expect(touch_page.locator('.study-code-copy')).to_have_css('opacity', '1')
            finally:
                touch_page.close()
            page.evaluate('document.activeElement.blur()')
            page.keyboard.press('[')
            assert page.evaluate('window.__beta.state.campus.lessonId') == 'first'
            page.keyboard.press(']')
            pw.expect(page.locator('.note-outline')).to_be_visible()
            assert page.evaluate('window.__beta.state.campus.lessonId') == 'middle'
            page.keyboard.press('[')
            pw.expect(code_wraps).to_have_count(2)
            assert page.evaluate('window.__beta.state.campus.lessonId') == 'first'
            next_.click()
            pw.expect(next_).to_be_disabled()
            pw.expect(next_).to_have_attribute('data-recording', '')
            pw.expect(prev).to_be_enabled()
            outline = page.get_by_role('navigation', name='笔记章节导航')
            pw.expect(outline).to_be_visible()
            links = outline.locator('.note-outline-link')
            pw.expect(links).to_have_text(['基础', '例题', long_heading])
            pw.expect(outline.locator('strong, code, img')).to_have_count(0)
            assert page.evaluate('window.noteXss === undefined')
            assert page.locator('.study-markdown h2, .study-markdown h3').evaluate_all(
                '(items) => items.map(item => item.id)') == ['note-sec-1', 'note-sec-2', 'note-sec-3']
            page.evaluate('''() => {
                const scroll = Element.prototype.scrollIntoView;
                Element.prototype.scrollIntoView = function(options) {
                    window.noteScroll = {id:this.id, reader:this.matches('.lecture-note-reader'), options};
                    scroll.call(this, options);
                };
            }''')
            links.last.click()
            assert page.evaluate('window.noteScroll') == {
                'id': 'note-sec-3', 'reader': False, 'options': {'block': 'start'}}
            pw.expect(page.locator('#note-sec-3')).to_be_in_viewport()
            footer = page.locator('.study-markdown > .note-outline-footer:last-child')
            pw.expect(footer).to_have_count(1)
            footer.get_by_role('button', name='返回目录与工具栏', exact=True).click()
            assert page.evaluate('window.noteScroll') == {
                'id': '', 'reader': True, 'options': {'block': 'start'}}
            pw.expect(page.locator('.lecture-note-reader .reader-toolbar')).to_be_in_viewport()
            pw.expect(outline).to_be_in_viewport()
            page.set_viewport_size({'width': 390, 'height': 900})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert outline.evaluate('(el) => el.scrollWidth <= el.clientWidth')
            assert links.evaluate_all('(items) => items.every(el => el.scrollWidth <= el.clientWidth)')
            prev.click()
            pw.expect(page.locator('.note-outline')).to_have_count(0)
            pw.expect(page.locator('.note-outline-footer')).to_have_count(0)
            next_.click()
            pw.expect(outline).to_be_visible()
            assert links.evaluate_all('(items) => items.map(el => el.dataset.target)') == [
                'note-sec-1', 'note-sec-2', 'note-sec-3']
            page.evaluate('''() => {
                const s = window.__beta.state;
                s.campus.course.lessons.push({...s.campus.course.lessons[0], id:'last', title:'第三讲'});
                window.__beta.render();
            }''')
            pw.expect(prev).to_be_enabled()
            pw.expect(next_).to_be_enabled()
            pw.expect(next_).to_have_attribute('data-recording', 'last')
            for markup in ('<input>', '<textarea></textarea>', '<select><option>课程</option></select>',
                           '<div contenteditable="true"><span tabindex="0">编辑笔记</span></div>'):
                page.evaluate('''markup => {
                    const editor = document.createElement('div'); editor.id = 'shortcut-editor';
                    editor.innerHTML = markup; document.body.appendChild(editor);
                    (editor.querySelector('span') || editor.firstElementChild).focus();
                }''', markup)
                for key in ('[', ']'):
                    page.keyboard.press(key)
                    assert page.evaluate('window.__beta.state.campus.lessonId') == 'middle'
                page.locator('#shortcut-editor').evaluate('(el) => el.remove()')
            page.evaluate('''() => {
                const dialog = document.createElement('dialog'); dialog.id = 'shortcut-dialog';
                dialog.innerHTML = '<button>关闭</button>'; document.body.appendChild(dialog); dialog.show();
            }''')
            for key in ('[', ']'):
                page.keyboard.press(key)
                assert page.evaluate('window.__beta.state.campus.lessonId') == 'middle'
            page.locator('#shortcut-dialog').evaluate('(el) => el.remove()')
            for options in ({'ctrlKey': True}, {'metaKey': True}, {'altKey': True}, {'prevented': True}):
                for key in ('[', ']'):
                    page.evaluate('''({key, options}) => {
                        const event = new KeyboardEvent('keydown', {key, bubbles:true, cancelable:true, ...options});
                        if (options.prevented) event.preventDefault();
                        document.body.dispatchEvent(event);
                    }''', {'key': key, 'options': options})
                    assert page.evaluate('window.__beta.state.campus.lessonId') == 'middle'
            next_.click()
            pw.expect(page.locator('.study-markdown')).to_contain_text('只有正文')
            pw.expect(page.locator('.note-outline')).to_have_count(0)
            pw.expect(page.locator('.note-outline-footer')).to_have_count(0)
            pw.expect(next_).to_be_disabled()
            page.evaluate('document.activeElement.blur()')
            page.keyboard.press(']')
            assert page.evaluate('window.__beta.state.campus.lessonId') == 'last'
            page.evaluate("window.__beta.state.campus.lessonId = 'missing'; window.__beta.render()")
            pw.expect(pager).to_have_count(0)
            for section in ('home', 'materials', 'assignments', 'notices', 'settings'):
                page.evaluate('section => {window.__beta.state.campus.section = section; window.__beta.render()}', section)
                pw.expect(pager).to_have_count(0)
            assert errors == []
        finally:
            browser.close()


def test_assignment_format_suggestions_and_filename_template_in_edge():
    pw = pytest.importorskip('playwright.sync_api')
    from browser_support import EDGE, browser_launch_options
    if not EDGE.is_file():
        pytest.skip('Microsoft Edge is required')
    static = Path(__file__).parents[1] / 'pku_sync/panel/static'
    script = (static / 'app.js').read_text('utf8')
    script = script[:script.index('  window.setInterval(refreshAccount, 8000);')] + (
        'window.__beta = {state: state, render: render};})();')
    analysis = {'requirements': [{'quote': '请按要求提交答案'}],
                'file_formats': [{'value': 'PDF', 'quote': '只上传 PDF'}],
                'filename_rules': [{'quote': '文件名为“学号_姓名.zip”。'}], 'uncertainties': []}
    calls, errors = [], []

    def serve(route):
        path = urlsplit(route.request.url).path
        if path == '/':
            route.fulfill(content_type='text/html; charset=utf-8', body=
                          '<!doctype html><meta charset="utf-8"><div id="app"></div><div id="toast"></div>')
        elif path.endswith('/requirements'):
            assert route.request.method == 'POST'
            assert route.request.post_data_json == {'confirmed': True}
            calls.append(path)
            route.fulfill(json={'analysis': analysis, 'points_charged': .01})
        else:
            route.fulfill(json={})

    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(**browser_launch_options(), headless=True)
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.add_init_script("localStorage.setItem('pku-panel-welcome-v1','seen')")
            page.route('http://panel.test/**', serve)
            page.goto('http://panel.test/')
            page.evaluate('copy => window.PANEL_COPY = copy', PANEL_COPY)
            page.add_style_tag(content=(static / 'app.css').read_text('utf8'))
            page.add_script_tag(content=script)
            page.evaluate('''() => {
                const s = window.__beta.state;
                s.activated = true; s.quota = {active:true}; s.connection.connected = true;
                s.campus.configured = true; s.view = 'campus-course'; s.campus.courseId = 'course';
                s.campus.course = {id:'course', title:'测试课程', lessons:[],
                    unassigned_materials:[], all_materials:[], announcements:[], announcement_tasks:[],
                    assignments:[{content_id:'assignment', title:'正式作业'}], summary:{}};
                s.campus.section = 'assignments'; s.campus.submission.selectedId = 'assignment';
                window.__beta.render();
            }''')
            analyze = page.get_by_role('button', name='AI 识别作业要求', exact=True)
            mode = page.locator('#assignment-prepare-form [name="submission_mode"]')
            apply_rule = page.locator('[data-assignment-requirements] [data-action="campus-apply-filename-rule"]')
            archive = page.locator('#assignment-prepare-form [name="archive_name"]')

            def recognize():
                with page.expect_response('**/requirements'):
                    analyze.click()
                pw.expect(analyze).to_be_enabled()

            pw.expect(mode).to_have_value('zip')
            recognize()
            pw.expect(mode).to_have_value('single_file')
            pw.expect(apply_rule).to_have_count(0)
            pw.expect(page.locator('[data-assignment-requirements] li')).to_have_text([
                '请按要求提交答案', '文件名为“学号_姓名.zip”。'])
            mode.select_option('zip')
            pw.expect(apply_rule).to_have_text('套用命名规则')
            pw.expect(apply_rule).to_have_attribute('data-filename-template', '学号_姓名.zip')
            apply_rule.click()
            pw.expect(archive).to_have_value('学号_姓名.zip')
            pw.expect(archive).to_be_focused()
            archive.fill('123_张同学.zip')
            recognize()
            pw.expect(mode).to_have_value('zip')
            pw.expect(archive).to_have_value('123_张同学.zip')

            archive.fill('')
            page.locator('[name="attachments"]').set_input_files(
                {'name': 'answer.pdf', 'mimeType': 'application/pdf', 'buffer': b'answer'})
            recognize()
            pw.expect(mode).to_have_value('zip')
            pw.expect(page.locator('[data-assignment-attachments]')).to_contain_text('answer.pdf')
            page.locator('[name="attachments"]').set_input_files([])
            mode.select_option('single_file')
            analysis['file_formats'].append({'value': 'ZIP', 'quote': '或打包 ZIP'})
            recognize()
            pw.expect(mode).to_have_value('zip')

            for quote, expected in [
                ('文件名为学号姓名.zip', '学号姓名.zip'),
                ('“学号_姓名_第1次作业”。', '学号_姓名_第1次作业.zip'),
                ('命名为「学号_姓名.ZIP」。', '学号_姓名.ZIP'),
                ('文件名为“学号_<姓名>.zip”。', '学号_<姓名>.zip'),
            ]:
                analysis['filename_rules'] = [{'quote': ''}, {'quote': quote}]
                recognize()
                pw.expect(apply_rule).to_have_attribute('data-filename-template', expected)
                pw.expect(page.locator('[data-assignment-requirements] li').last).to_have_text(quote)
                apply_rule.click()
                pw.expect(archive).to_have_value(expected)
                pw.expect(archive).to_be_focused()
                archive.fill('')

            analysis['file_formats'] = []
            analysis['filename_rules'] = []
            mode.select_option('single_file')
            recognize()
            pw.expect(mode).to_have_value('single_file')
            mode.select_option('zip')
            pw.expect(apply_rule).to_have_count(0)
            assert len(calls) == 9
            assert errors == []
        finally:
            browser.close()


def test_summary_reading_and_mcp_navigation_in_edge(tmp_path):
    pw = pytest.importorskip('playwright.sync_api')
    from browser_support import EDGE, browser_launch_options
    if not EDGE.is_file():
        pytest.skip('Microsoft Edge is required')
    _, job = seed_keyframe_materials(tmp_path)
    (job.directory / 'keyframes/slide-alignment.json').write_text(json.dumps({'anchors': [
        {'start': 0, 'end': 60, 'title': 'ch02-TCP滑动窗口.pdf', 'page': 1}]}), 'utf8')
    client, _ = api(tmp_path)
    static = Path(__file__).parents[1] / 'pku_sync/panel/static'
    errors = []
    free_actions = []
    background_sync = {
        'state': 'partial',
        'stage': '部分课程更新失败；已完成课程可用，请检查失败结果后恢复',
        'course_results': [
            {'course_id': f'course-{index}', 'title': f'测试课程 {index}',
             'state': 'done' if index < 10 else 'failed',
             'metadata_state': 'confirmed' if index < 10 else 'unconfirmed',
             'projection_state': 'confirmed' if index < 10 else 'unconfirmed',
             'summary_created': 1 if index < 10 else 0,
             'summary_existing': 2 if index < 10 else 0,
             'error': '' if index < 10 else '课程已关闭，暂时无法读取',
             'failure_id': '' if index < 10 else f'failure-{index}'}
            for index in range(12)
        ],
    }

    def decorate_course(body):
        body['title'] = 'Networks/CS:101 (CS101)'
        body['summary'] = {'title': 'Networks 课程总结', 'url': 'https://www.notion.so/course-summary'}
        body['unassigned_materials'].append({'index': 99, 'title': '课程基本信息',
                                             'category': 'information', 'files': [],
                                             'body_text': '安全设计 <基础> 与课程考核说明'})
        body['assignments'] = [
            {'title': '正文截止作业', 'instructions': '10月22日23：30 前提交'},
            {'title': '日历测试', 'source': 'calendar', 'instructions': '',
             'due_at': (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()},
            *[{'title': title, 'due_at': (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()}
              for title, hours in [('今天截止作业', 12), ('两天后截止作业', 48), ('远期作业', 96), ('过期作业', -1)]],
            {'title': '无效日期作业', 'due_at': 'invalid'}]
        return body

    def serve(route):
        path = urlsplit(route.request.url).path
        if path == '/':
            route.fulfill(content_type='text/html; charset=utf-8', body=
                '<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/app.css">'
                '</head><body><div id="app"></div><div id="toast"></div><script>window.PANEL_COPY=' +
                json.dumps(PANEL_COPY) + ';</script><script src="/app.js"></script></body></html>')
        elif path in ('/app.js', '/app.css'):
            route.fulfill(path=str(static / path[1:]), content_type='text/css' if path.endswith('css') else 'application/javascript')
        elif '/frames/' in path or path.endswith('/slide-page'):
            response = client.get(route.request.url.replace('http://panel.test', ''))
            route.fulfill(status=response.status_code, body=response.content, content_type=response.headers.get('content-type'))
        elif path.endswith('/match-slides'):
            response = client.post(path)
            route.fulfill(status=response.status_code, json=decorate_course(response.json()))
        elif path == '/api/open-notion':
            route.fulfill(json={'opened': True})
        elif route.request.method == 'POST' and path.endswith(('/download', '/summary')):
            free_actions.append(path)
            route.fulfill(json={'state': 'idle'})
        elif path in ('/api/campus/courses', '/api/campus/courses/healthy', '/api/mcp/status') or path.endswith(('/summary-preview', '/note-preview')):
            response = client.get(path)
            body = response.json()
            if path == '/api/campus/courses':
                body['notion_home'] = {'id': 'home', 'schema': 2}
                body['campus_configured'] = True
            if path == '/api/campus/courses/healthy':
                decorate_course(body)
            route.fulfill(status=response.status_code, json=body)
        else:
            values = {
                '/api/platform/quota': {'active': True, 'available': True, 'email': 'beta@example.com', 'email_verified': True},
                '/api/auth/me': {'available': True, 'email': 'beta@example.com', 'email_verified': True},
                '/api/connection': {'connected': True, 'state': 'connected'},
                '/api/update': {'status': 'desktop_managed'},
                '/api/recordings/task': {'state': 'idle', 'background_sync': background_sync},
                '/api/campus/notion/parents': {'pages': [], 'home': {'id': 'home', 'schema': 2}},
            }
            route.fulfill(json=values.get(path, {}))

    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(**browser_launch_options(), headless=True)
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('console', lambda msg: errors.append(msg.text) if msg.type == 'error' else None)
            page.add_init_script("localStorage.setItem('pku-panel-welcome-v1','seen')")
            page.add_init_script("Object.defineProperty(navigator, 'clipboard', {value:{writeText: async text => {window.copiedStudyText = text;}}})")
            page.route('http://panel.test/**', serve)
            page.goto('http://panel.test/')
            banner = page.locator('.background-sync-banner[data-background-state="partial"]')
            pw.expect(banner).to_be_visible()
            pw.expect(banner).to_have_attribute('role', 'status')
            pw.expect(banner).to_have_attribute('aria-live', 'polite')
            pw.expect(banner.locator('.background-sync-counts')).to_have_text('已完成 10 / 12 门 · 2 门未完成')
            details = banner.locator('[data-sync-details]')
            assert not details.evaluate('(element) => element.open')
            assert banner.bounding_box()['height'] < 110
            rows = banner.locator('[data-sync-course]')
            pw.expect(rows).to_have_count(12)
            assert not rows.first.is_visible()
            assert '完成教学网' not in banner.text_content()
            assert '失败教学网' not in banner.text_content()
            details.locator('summary').click()
            pw.expect(rows.first).to_be_visible()
            assert all(rows.nth(index).is_visible() for index in range(12))
            assert rows.evaluate_all('(items) => items.map(item => item.dataset.syncState)') == ['failed'] * 2 + ['done'] * 10
            pw.expect(rows.first).to_contain_text('失败 · 教学网：尚未核对')
            pw.expect(rows.first).to_contain_text('课程已关闭，暂时无法读取')
            pw.expect(rows.first).to_contain_text('故障编号：failure-10')
            pw.expect(rows.nth(2)).to_contain_text('本轮新建总结 1 页')
            pw.expect(rows.nth(2)).to_contain_text('复用已有总结 2 页')
            page.locator('[data-action="open-campus-course"]').first.click()
            pw.expect(page.locator('.lesson-title-date')).to_be_hidden()
            pw.expect(page.locator('.lesson-topic-line')).to_have_text('TCP 可靠传输')
            assert '核心主业' not in page.content()
            assert '扩展工具：' not in page.content()
            pw.expect(page.locator('[data-action="campus-open-summary"]')).to_have_count(0)
            assert 'https://www.notion.so/course-summary' not in page.content()
            pw.expect(page.locator('.course-summary-spotlight')).to_be_visible()
            with page.expect_response(lambda response: response.url.endswith('/healthy/summary')):
                page.locator('[data-action="campus-course-summary"]').click()
            assert page.locator('[role="dialog"]').count() == 0
            assert free_actions == ['/api/campus/courses/healthy/summary']
            page.locator('[data-action="campus-preview-course-summary"]').click()
            pw.expect(page.locator('.study-markdown')).to_contain_text('TCP 可靠传输')
            pw.expect(page.locator('[data-action="campus-preview-course-summary"]')).to_have_text('收起课程总结')
            page.locator('[data-action="campus-preview-course-summary"]').click()
            pw.expect(page.locator('.study-markdown')).to_have_count(0)
            pw.expect(page.locator('[data-action="campus-preview-course-summary"]')).to_have_text('阅读课程总结')
            page.locator('[data-action="campus-preview-course-summary"]').click()
            pw.expect(page.locator('.study-markdown')).to_contain_text('TCP 可靠传输')
            assert page.locator('[data-action="campus-copy-markdown"]').is_visible()
            page.locator('[data-action="campus-copy-markdown"]').click()
            assert 'TCP' in page.evaluate('window.copiedStudyText')
            download_button = page.locator('.reader-toolbar [data-action="campus-download-markdown"]')
            pw.expect(download_button).to_be_visible()
            pw.expect(download_button).to_have_text('下载 .md')
            with page.expect_download() as download_event:
                download_button.click()
            summary_download = download_event.value
            assert summary_download.suggested_filename == 'Networks-CS-101-课程总结.md'
            assert Path(summary_download.path()).read_text('utf8') == page.evaluate('window.copiedStudyText')
            pw.expect(page.locator('#toast')).to_have_text('已下载 Markdown 文件')
            assert page.locator('.campus-centers').bounding_box()['height'] < 65
            page.screenshot(path=str(tmp_path / 'summary-workbench.png'), full_page=True)
            page.locator('[data-action="campus-toggle-preview"]').click()
            page.locator('[data-action="campus-open-lesson"]').first.click()
            pw.expect(page.locator('.study-markdown')).to_contain_text('滑动窗口')
            images = page.locator('.study-markdown .note-visual-card img')
            pw.expect(images).to_have_count(2)
            images.last.scroll_into_view_if_needed()
            page.wait_for_function("Array.from(document.querySelectorAll('.study-markdown img')).every(img => img.complete && img.naturalWidth > 0)")
            boxes = [images.nth(i).bounding_box() for i in range(2)]
            assert abs(boxes[0]['y'] - boxes[1]['y']) < 2
            assert boxes[1]['x'] > boxes[0]['x']
            page.locator('[data-action="campus-copy-markdown"]').click()
            assert page.evaluate('window.copiedStudyText') == (job.directory / 'notes.md').read_text('utf8')
            pw.expect(download_button).to_be_visible()
            with page.expect_download() as download_event:
                download_button.click()
            note_download = download_event.value
            lesson_title = client.get('/api/campus/courses/healthy').json()['lessons'][0]['title']
            assert note_download.suggested_filename == f'Networks-CS-101-{lesson_title}.md'
            assert Path(note_download.path()).read_bytes() == page.evaluate('window.copiedStudyText').encode('utf8')
            pw.expect(page.locator('#toast')).to_have_text('已下载 Markdown 文件')
            page.set_viewport_size({'width': 390, 'height': 900})
            images.last.scroll_into_view_if_needed()
            assert images.last.bounding_box()['y'] > images.first.bounding_box()['y']
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.set_viewport_size({'width': 1440, 'height': 1000})
            pw.expect(page.locator('.lesson-title-date')).to_be_visible()
            pw.expect(page.locator('.lesson-topic-line')).to_have_text('TCP 可靠传输')
            with page.expect_response(lambda response: response.url.endswith('/download')):
                page.locator('[data-action="recording-download"]').click()
            assert free_actions[-1].endswith('/download')
            assert page.locator('[role="dialog"]').count() == 0
            page.locator('[data-action="campus-match-slides-keyframes"]').click()
            pw.expect(page.locator('.status-badge.indexed')).to_contain_text('关键帧匹配')
            pw.expect(page.locator('.study-markdown')).to_contain_text('滑动窗口')
            pw.expect(page.locator('[data-action="campus-preview-lecture-note"]')).to_have_text('收起本讲总结')
            page.locator('[data-action="campus-preview-lecture-note"]').click()
            pw.expect(page.locator('.study-markdown')).to_have_count(0)
            pw.expect(page.locator('[data-action="campus-preview-lecture-note"]')).to_have_text('阅读本讲总结')
            page.locator('[data-action="campus-preview-lecture-note"]').click()
            pw.expect(page.locator('.study-markdown')).to_contain_text('滑动窗口')
            page.locator('[data-action="campus-copy-markdown"]').click()
            pw.expect(page.locator('#toast')).to_contain_text('已复制笔记 Markdown，可直接粘贴或发送给 AI')
            # HTML in generated notes remains literal text.
            (job.directory / 'notes.md').write_text(
                '# TCP\n<script>window.noteXss = true</script>\n'
                '- 拥塞控制\n  - 慢开始与拥塞避免\n'
                '吞吐率公式 $\\alpha + \\beta \\leq \\frac{W}{RTT}$：\n'
                '$$CWND_{n+1} = CWND_n + \\frac{1}{CWND_n}$$\n'
                '| 阶段 | 窗口变化 |\n| --- | --- |\n| 慢开始 | 指数增长 |\n'
                '![标准图片](/api/campus/courses/healthy/recordings/' + recordings_api.recording_id(job) + '/frames/frame.jpg)\n'
                '![恶意图片](javascript:alert(1))\n```\n![代码图片](/do-not-load.jpg)\n```',
                'utf8',
            )
            page.locator('[data-action="campus-toggle-preview"]').click()
            page.locator('[data-action="campus-preview-lecture-note"]').click()
            pw.expect(page.locator('.study-markdown')).to_contain_text('<script>')
            assert page.evaluate('window.noteXss === undefined')
            pw.expect(page.locator('.study-markdown .study-subitem')).to_contain_text('慢开始与拥塞避免')
            pw.expect(page.locator('.study-markdown .math-inline .math-frac')).to_have_count(1)
            pw.expect(page.locator('.study-markdown .math-block .math-frac')).to_have_count(1)
            pw.expect(page.locator('.study-markdown .study-table th').first).to_have_text('阶段')
            pw.expect(page.locator('.study-markdown .study-table td').nth(1)).to_have_text('指数增长')
            pw.expect(page.locator('.study-markdown .note-visual-card img')).to_have_count(1)
            pw.expect(page.locator('.study-markdown img')).to_have_attribute('alt', '标准图片')
            assert 'javascript:' not in page.locator('.study-markdown img').get_attribute('src')
            page.locator('.study-markdown .note-visual-card img').click()
            pw.expect(page.locator('dialog.note-lightbox[open]')).to_be_visible()
            page.locator('dialog.note-lightbox').click()
            pw.expect(page.locator('dialog.note-lightbox[open]')).to_have_count(0)
            page.locator('[data-action="campus-course-home"]').click()
            page.locator('[data-center="materials"]').click()
            pw.expect(page.locator('[data-materials-info]')).to_contain_text('课程基本信息')
            pw.expect(page.locator('[data-materials-info] .material-match-disclosure')).to_have_count(0)
            pw.expect(page.locator('[data-materials-info] .material-body-text')).to_have_text('安全设计 <基础> 与课程考核说明')
            assert '课程基本信息' not in page.locator('[data-materials-unassigned]').inner_text()
            pw.expect(page.locator('.materials-summary-panel .course-summary-control')).to_be_visible()
            pw.expect(page.locator('.materials-summary-copy')).to_contain_text('汇总已关联课件、课堂笔记与作业要求')
            page.locator('[data-action="campus-course-home"]').click()
            assignment_center = page.locator('.campus-centers [data-center="assignments"]')
            pw.expect(assignment_center.locator('.center-count')).to_have_text('7 项记录 · 2 项即将截止')
            pw.expect(assignment_center).to_have_attribute('data-urgent-assignments', '2')
            page.locator('[data-center="assignments"]').click()
            pw.expect(page.locator('#course-assignments')).to_contain_text('截止：10月22日23：30 前提交（正文提及）')
            pw.expect(page.locator('#course-assignments .status-badge.focus')).to_have_count(0)
            pw.expect(page.locator('[data-deadline-urgency="today"]')).to_have_text('今天截止')
            pw.expect(page.locator('[data-deadline-urgency="soon"]')).to_have_text('即将截止')
            for title in ('正文截止作业', '日历测试', '远期作业', '过期作业', '无效日期作业'):
                pw.expect(page.locator('#course-assignments li').filter(has_text=title).locator('[data-deadline-urgency]')).to_have_count(0)
            calendar = page.locator('#course-assignments li').filter(has_text='日历测试')
            pw.expect(calendar).to_contain_text('来自教学网日历提醒，尚未关联到正式作业')
            assert '尚无可验证的提交目标' not in calendar.inner_text()
            page.locator('.sidebar [data-action="open-mcp-hub"]').click()
            pw.expect(page.locator('.mcp-hub h1')).to_have_text('AI 工具连接')
            pw.expect(page.locator('.reader-toolbar [data-action="copy-mcp-command"]')).to_be_visible()
            for host in ('codex', 'claude', 'antigravity', 'oh-my-pi', 'opencode'):
                page.locator(f'[data-host="{host}"]').click()
                assert page.locator('[data-action="copy-mcp-snippet"]').is_visible()
                page.locator('[data-action="copy-mcp-snippet"]').click()
                assert 'pku-sync' in page.evaluate('window.copiedStudyText')
                page.locator('[data-action="copy-mcp-prompt"]').click()
                assert 'MCP' in page.evaluate('window.copiedStudyText')
            page.screenshot(path=str(tmp_path / 'mcp-hub.png'), full_page=True)
            for width in (390, 800, 1440):
                page.set_viewport_size({'width': width, 'height': 900})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert page.locator('.mcp-hub pre').evaluate_all('(items) => items.every(item => item.scrollWidth <= item.clientWidth)')
            assert errors == []
        finally:
            browser.close()


@pytest.mark.parametrize('index_shape', ['dict', 'list'])
def test_note_visuals_serve_board_and_render_slide_by_catalog_title(tmp_path, index_shape):
    from test_slide_alignment import _write_pdf, _deck_rectangles
    import cv2
    import numpy as np
    folder, job = seed(tmp_path)
    frames = job.directory / 'keyframes'
    frames.mkdir()
    # A dark board deliberately scores below the publication cutoff.
    board = np.full((180, 320, 3), (38, 65, 42), dtype=np.uint8)
    cv2.putText(board, 'CIA', (25, 80), cv2.FONT_HERSHEY_SIMPLEX, 1, (230, 240, 230), 2)
    (frames / 'board.png').write_bytes(cv2.imencode('.png', board)[1].tobytes())
    rows = [{'file': 'board.png', 'timestamp': 755}]
    (frames / 'index.json').write_text(json.dumps({'keyframes': rows} if index_shape == 'dict' else rows), 'utf8')
    title = '01-信息安全引论与CIA三元组.pdf'
    materials = folder / 'materials'
    materials.mkdir()
    _write_pdf(materials / 'download.pdf', _deck_rectangles(2))
    (materials / 'index.json').write_text(json.dumps([{'title': title, 'files': ['download.pdf']}]), 'utf8')
    (frames / 'slide-alignment.json').write_text(json.dumps({'anchors': [
        {'start': 700, 'end': 800, 'title': title, 'page': 2}]}), 'utf8')
    (job.directory / 'notes.md').write_text('## 课堂重点\n### CIA [12:00–14:00]\n- 威胁矩阵\n## 复习建议\n- 复习', 'utf8')
    client, _ = api(tmp_path)
    key = recordings_api.recording_id(job)
    base = f'/api/campus/courses/healthy/recordings/{key}'
    response = client.get(base + '/note-preview')
    assert response.status_code == 200, response.text
    visual, = response.json()['visuals']
    assert visual['heading'] == 'CIA [12:00–14:00]'
    assert visual['time'] == '12:35' and visual['timestamp'] == 755
    assert visual['slide_title'] == title and visual['slide_page'] == 2
    assert client.get(visual['frame_url']).content == (frames / 'board.png').read_bytes()
    slide = client.get(visual['slide_url'])
    assert slide.status_code == 200 and slide.headers['content-type'] == 'image/jpeg'
    assert cv2.imdecode(np.frombuffer(slide.content, dtype=np.uint8), cv2.IMREAD_COLOR).shape[0] > 100
    # Second request is served from the generated cache, even offline.
    (materials / 'download.pdf').unlink()
    assert client.get(visual['slide_url']).content == slide.content
    assert client.get(base + '/slide-page', params={'file': title, 'page': 0}).status_code == 400
    assert client.get(base + '/slide-page', params={'file': title, 'page': 999}).status_code == 404
    assert client.get(base + '/frames/index.json').status_code == 400
    assert client.get(base + '/frames/missing.png').status_code == 404
    assert client.get(base + '/frames/..%5Cboard.png').status_code == 400
    assert client.get(visual['frame_url'].replace('/healthy/', '/other/')).status_code == 404
    assert client.get(visual['slide_url'].replace('/healthy/', '/other/')).status_code == 404
    outside = tmp_path / 'outside.pdf'
    _write_pdf(outside, _deck_rectangles(1))
    assert client.get(base + '/slide-page', params={'file': str(outside)}).status_code == 404
    # Pre-rendered slide pages work without the source PDF as well.
    from pku_sync.panel.note_images import _slide_cache
    cache = Path(str(_slide_cache(job, title, 6)) + '.png')
    cache.write_bytes((frames / 'board.png').read_bytes())
    assert client.get(base + '/slide-page', params={'file': title, 'page': 6}).content == cache.read_bytes()


def test_visual_sections_order_windows_matches_and_malformed_cache(tmp_path, monkeypatch):
    from pku_sync.panel import note_images
    _, job = seed(tmp_path)
    frames = job.directory / 'keyframes'
    frames.mkdir()
    for name in ('a.jpg', 'b.png', 'c.jpg'):
        (frames / name).write_bytes(b'fixture')
    rows = [{'file': 'c.jpg', 'timestamp': 120}, {'file': 'b.png', 'timestamp': 30},
            {'file': 'a.jpg', 'timestamp': 10}, {'file': '../private.jpg', 'timestamp': 5}]
    (frames / 'index.json').write_text(json.dumps({'keyframes': rows}), 'utf8')
    monkeypatch.setattr(note_images, '_visual_score', lambda p: 5 if p.name == 'b.png' else 1)
    build = lambda text: note_images.build_note_visual_sections(job, 'healthy', 'recording', text)
    items = build('### First\n### Second\n## 复习建议')
    assert [v['timestamp'] for v in items] == [10, 30]
    items = build('### First [00:00-01:00]\n### Second [02:00–03:00]')
    assert [v['timestamp'] for v in items] == [30, 120]
    assert build('### Empty [05:00–06:00]') == []
    (frames / 'keyframe-matches.json').write_text(json.dumps({'matches': [
        {'frame_file': 'b.png', 'title': 'Deck.pdf', 'page': 3}]}), 'utf8')
    item, = build('### First [00:00-01:00]')
    assert item['slide_title'] == 'Deck.pdf' and item['slide_page'] == 3 and not item['slide_url']
    # Exact saved note sections take precedence over chronological distribution.
    text = '### First\n### Second'
    monkeypatch.setattr(note_images, '_saved_visual_windows', lambda *args: [
        {'markdown': '### First', 'start': 100, 'end': 180},
        {'markdown': '### Second', 'start': 0, 'end': 60}])
    assert [v['timestamp'] for v in build(text)] == [120, 30]
    for malformed in ('{bad', 'null', '{"keyframes": null}'):
        (frames / 'index.json').write_text(malformed, 'utf8')
        assert build(text) == []


def test_direct_slide_anchor_and_supplemental_frame_preview(tmp_path):
    from pku_sync.panel.note_images import build_note_visual_sections, _slide_cache
    _, job = seed(tmp_path)
    frames = job.directory / 'keyframes'
    frames.mkdir()
    supplemental = job.directory / 'note-frames'
    supplemental.mkdir()
    (supplemental / 'board.webp').write_bytes(b'cached supplemental frame')
    (frames / 'index.json').write_text(json.dumps({'keyframes': [
        {'file': 'board.webp', 'timestamp': 12, 'anchor': {'title': 'Slides.pdf', 'page': 6}}]}), 'utf8')
    image = Path(str(_slide_cache(job, 'Slides.pdf', 6)) + '.png')
    image.parent.mkdir()
    image.write_bytes(b'pre-rendered slide')
    key = recordings_api.recording_id(job)
    # A single level-two heading is itself a content section.
    item, = build_note_visual_sections(job, 'healthy', key, '## 课堂重点\n课堂内容')
    assert item['heading'] == '课堂重点' and item['slide_page'] == 6 and item['slide_url']
    client, _ = api(tmp_path)
    assert client.get(item['frame_url']).content == (supplemental / 'board.webp').read_bytes()
    assert client.get(item['slide_url']).content == image.read_bytes()
    (frames / 'slide-alignment.json').write_text('null', 'utf8')
    (frames / 'keyframe-matches.json').write_text('{"matches":null}', 'utf8')
    assert build_note_visual_sections(job, 'healthy', key, '## Topic')[0]['slide_title'] == 'Slides.pdf'
