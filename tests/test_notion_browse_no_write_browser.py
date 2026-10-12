"""Development browser regression; not an installed native E2E verdict."""
import json
from pathlib import Path

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
                     'home': {'id': 'home', 'parent_id': 'parent'},
                     'workspace': {'id': 'home', 'active': True}}
        else:
            value = {'state': 'done'}
        route.fulfill(body=json.dumps(value), content_type='application/json')
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe', headless=True)
        try:
            page = browser.new_page()
            page.route('**/*', serve)
            page.goto('http://panel.test')
            page.set_content('<div id="app"></div><div id="toast"></div>')
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
