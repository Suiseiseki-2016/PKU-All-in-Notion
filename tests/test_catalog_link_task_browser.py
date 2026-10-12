"""Developer browser regressions for actual save-link handlers; not native E2E."""
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from browser_support import browser_launch_options
from playwright.sync_api import sync_playwright
from pku_sync.panel.student_ui import PANEL_COPY

EDGE = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'


@pytest.mark.parametrize('accepted_state', ['running', 'failed'])
def test_save_link_receives_catalog_task_and_exposes_actual_failure(accepted_state):
    script = (Path(__file__).parents[1] / 'pku_sync/panel/static/app.js').read_text('utf-8')
    assert script.rstrip().endswith('})();')
    # This fixture tests the save-link handler and its actual scheduled task poll.
    # Await the separate startup pipeline before seeding an authenticated view.
    assert script.count('  loadQuota()\n') == 1
    script = script.replace('  loadQuota()\n', '  var __candidateStartup = loadQuota()\n', 1)
    script = script.replace('  window.setInterval(refreshAccount, 8000);', '')
    script = script.rstrip()[:-5] + 'window.__candidateTest={state:state,render:render,ready:__candidateStartup};})();'
    course = {'id': '_101545_1', 'title': '测试课程', 'code': 'TEST', 'term': '测试学期',
        'lessons': [], 'unassigned_materials': [], 'announcements': [], 'announcement_tasks': [],
        'assignments': [], 'all_materials': [], 'summary': {},
        'existing_notion_url': 'https://www.notion.so/test-course'}
    failure = {'state': 'failed', 'kind': 'catalog', 'stage': '读取账号课程快照',
        'error': '账号暂不允许读取课程快照；已有课程缓存已保留。请检查账号权限后重试。',
        'failure_id': 'D1A2B3C4'}
    requests = []
    def serve(route):
        path = urlsplit(route.request.url).path
        requests.append((route.request.method, path))
        if path == '/':
            route.fulfill(status=200, body='<div id="app"></div><div id="toast"></div>', content_type='text/html')
            return
        if path == '/api/campus/courses/_101545_1/existing-notion':
            body = course
        elif path == '/api/campus/notion/sync':
            body = failure if accepted_state == 'failed' else {
                'state': 'running', 'kind': 'catalog', 'stage': '读取账号课程快照'}
        elif path == '/api/recordings/task':
            body = failure
        elif path == '/api/campus/courses':
            body = {'courses': [course]}
        elif path == '/api/campus/courses/_101545_1':
            body = course
        else:
            route.fulfill(status=404, body='{}', content_type='application/json')
            return
        route.fulfill(status=200, body=json.dumps(body), content_type='application/json')
    with sync_playwright() as driver:
        browser = driver.chromium.launch(**browser_launch_options(), headless=True)
        try:
            page = browser.new_page()
            page.route('http://panel.test/**', serve)
            page.goto('http://panel.test/')
            page.evaluate('copy => { window.PANEL_COPY=copy; }', PANEL_COPY)
            page.add_script_tag(content=script)
            page.wait_for_function('Boolean(window.__candidateTest)')
            page.evaluate('() => window.__candidateTest.ready')
            page.evaluate('''course => {
                const s=window.__candidateTest.state;
                s.activated=true; s.quota={active:true}; s.view='campus-course';
                s.connection.connected=true; s.campus.courseId=course.id;
                s.campus.course=course; s.campus.configured=true; s.campus.section='settings';
                s.campus.home={id:'test-home',url:'https://www.notion.so/test-home'};
                s.recordings.task={state:'idle'};
                window.__candidateTest.render();
            }''', course)
            page.locator('#campus-existing-notion-url').fill(course['existing_notion_url'])
            page.locator('[data-action="campus-save-existing-notion"]').click()
            page.wait_for_function("window.__candidateTest.state.recordings.task.failure_id==='D1A2B3C4'", timeout=5000)
            assert failure['error'] in page.locator('.recording-task').inner_text()
            assert '故障编号：D1A2B3C4' in page.locator('.recording-task').inner_text()
            assert requests.count(('POST', '/api/campus/notion/sync')) == 1
            assert requests.count(('POST', '/api/campus/courses/_101545_1/existing-notion')) == 1
            assert (requests.count(('GET', '/api/recordings/task')) >= 1) is (accepted_state == 'running')
        finally:
            browser.close()
