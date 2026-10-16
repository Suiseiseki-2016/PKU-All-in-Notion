"""Beta regressions: isolated catalog failures, local reading and MCP setup."""
from __future__ import annotations

import json
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
        body['summary'] = {'url': 'https://www.notion.so/course-summary'}
        body['unassigned_materials'].append({'index': 99, 'title': '课程基本信息',
                                             'category': 'information', 'files': [],
                                             'body_text': '安全设计 <基础> 与课程考核说明'})
        body['assignments'] = [
            {'title': '正文截止作业', 'instructions': '10月22日23：30 前提交'},
            {'title': '日历测试', 'source': 'calendar', 'instructions': ''}]
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
            page.locator('[data-action="campus-open-summary"]').click()
            pw.expect(page.locator('#toast')).to_contain_text(PANEL_COPY['launch']['opened_external'])
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
            (job.directory / 'notes.md').write_text('# TCP\n<script>window.noteXss = true</script>\n![标准图片](/api/campus/courses/healthy/recordings/' + recordings_api.recording_id(job) + '/frames/frame.jpg)\n![恶意图片](javascript:alert(1))\n```\n![代码图片](/do-not-load.jpg)\n```', 'utf8')
            page.locator('[data-action="campus-toggle-preview"]').click()
            page.locator('[data-action="campus-preview-lecture-note"]').click()
            pw.expect(page.locator('.study-markdown')).to_contain_text('<script>')
            assert page.evaluate('window.noteXss === undefined')
            pw.expect(page.locator('.study-markdown .note-visual-card img')).to_have_count(1)
            pw.expect(page.locator('.study-markdown img')).to_have_attribute('alt', '标准图片')
            assert 'javascript:' not in page.locator('.study-markdown img').get_attribute('src')
            page.locator('[data-action="campus-course-home"]').click()
            page.locator('[data-center="materials"]').click()
            pw.expect(page.locator('[data-materials-info]')).to_contain_text('课程基本信息')
            pw.expect(page.locator('[data-materials-info] .material-match-disclosure')).to_have_count(0)
            pw.expect(page.locator('[data-materials-info] .material-body-text')).to_have_text('安全设计 <基础> 与课程考核说明')
            assert '课程基本信息' not in page.locator('[data-materials-unassigned]').inner_text()
            pw.expect(page.locator('.materials-summary-panel .course-summary-control')).to_be_visible()
            pw.expect(page.locator('.materials-summary-copy')).to_contain_text('汇总已关联课件、课堂笔记与作业要求')
            page.locator('[data-action="campus-course-home"]').click()
            page.locator('[data-center="assignments"]').click()
            pw.expect(page.locator('#course-assignments')).to_contain_text('截止：10月22日23：30 前提交（正文提及）')
            pw.expect(page.locator('#course-assignments .status-badge.focus')).to_have_count(0)
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
