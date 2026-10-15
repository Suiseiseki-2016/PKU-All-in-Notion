"""Development browser regression; not an installed native E2E verdict."""
import json
from pathlib import Path
from browser_support import browser_launch_options

from playwright.sync_api import sync_playwright
from pku_sync.panel.student_ui import PANEL_COPY


def test_reading_connected_notion_parents_does_not_start_directory_write():
    root = Path(__file__).resolve().parents[1]
    script = (root / 'pku_sync/panel/static/app.js').read_text('utf-8')
    script = script[:script.index('  window.setInterval(refreshAccount, 8000);')] + (
        'window.__browse = {state: state, load: loadNotionParents};})();')
    writes = []
    def serve(route):
        if route.request.method != 'GET':
            writes.append(route.request.url)
        if route.request.url.endswith('/api/campus/notion/parents'):
            value = {'pages': [{'id': 'parent', 'title': 'test'}],
                     'home': {'schema': 2, 'active': True, 'id': 'home', 'parent_id': 'parent'},
                     'workspace': {'schema': 2, 'id': 'home', 'active': True}}
        else:
            value = {'state': 'done'}
        route.fulfill(body=json.dumps(value), content_type='application/json')
    with sync_playwright() as driver:
        browser = driver.chromium.launch(**browser_launch_options(), headless=True)
        try:
            page = browser.new_page()
            page.add_init_script("localStorage.setItem('pku-panel-welcome-v1', 'seen')")
            page.route('**/*', serve)
            page.goto('http://panel.test')
            page.route('http://panel.test/', lambda route: route.fulfill(body='<div id="app"></div><div id="toast"></div>', content_type='text/html'))
            page.goto('http://panel.test/')
            page.evaluate('copy => {window.PANEL_COPY = copy;}', PANEL_COPY)
            page.add_script_tag(content=script)
            page.evaluate('''async () => {
                const s = window.__browse.state;
                s.activated = true;
                s.connection.connected = true;
                s.campus.courses = [{id:'official-course'}];
                s.recordings.task = {state:'idle'};
                await window.__browse.load();
            }''')
            assert page.evaluate('window.__browse.state.campus.home.id') == 'home'
            assert writes == []
        finally:
            browser.close()
