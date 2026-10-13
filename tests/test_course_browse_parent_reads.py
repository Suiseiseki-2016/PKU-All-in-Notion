"""Developer renderer check: slow parent reads cannot occupy course browsing."""
import json
from pathlib import Path
from browser_support import browser_launch_options

from playwright.sync_api import sync_playwright
from pku_sync.panel.student_ui import PANEL_COPY

STATIC = Path(__file__).resolve().parents[1] / 'pku_sync/panel/static'
EDGE = Path(r'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


def test_course_navigation_does_not_read_full_notion_tree_and_settings_coalesces_reads():
    script = (STATIC / 'app.js').read_text('utf-8')
    script = script[:script.index('  window.setInterval(refreshAccount, 8000);')] + (
        'window.__browse={state:state,open:openCampusCourse,parents:loadNotionParents};})();')
    parent_reads = []
    course = {'id': '_101508_1', 'title': 'E2E课程', 'lessons': [], 'unassigned_materials': [],
              'all_materials': [], 'assignments': [], 'announcements': [], 'announcement_tasks': [], 'summary': {}}
    with sync_playwright() as driver:
        browser = driver.chromium.launch(**browser_launch_options(), headless=True)
        try:
            page = browser.new_page()
            page.add_init_script("localStorage.setItem('pku-panel-welcome-v1', 'seen')")
            def serve(route):
                path = route.request.url.split('panel.test')[-1]
                if path == '/':
                    route.fulfill(body='<div id="app"></div><div id="toast"></div>',content_type='text/html')
                elif path == '/api/campus/notion/parents':
                    parent_reads.append(route)
                    # Intentionally held; no synthetic completed tree response.
                elif path == '/api/campus/courses/_101508_1':
                    route.fulfill(body=json.dumps(course), content_type='application/json')
                else:
                    route.fulfill(body=json.dumps({'items': [], 'pending': [], 'lectures': [], 'histories': []}),
                                  content_type='application/json')
            page.route('**/*', serve)
            page.goto('http://panel.test/')
            page.evaluate('copy => {window.PANEL_COPY=copy}', PANEL_COPY)
            page.add_script_tag(content=script)
            page.evaluate('''() => {let s=window.__browse.state;s.activated=true;
              s.quota={active:true,email:'e2e@example.test',email_verified:true};
              s.connection.connected=true;s.campus.configured=true;}''')
            for _ in range(5):
                page.evaluate("window.__browse.open('_101508_1')")
                page.locator('.course-hero[data-course-id="_101508_1"] h1').wait_for()
            assert parent_reads == [], 'Course browsing started slow full-tree Notion reads'
            page.locator('[data-action="campus-course-settings"]').click()
            page.wait_for_timeout(100)
            assert len(parent_reads) == 1, 'Explicit settings must read fresh Notion parents'
            page.evaluate('''() => {window.__browse.parents();window.__browse.parents();}''')
            page.wait_for_timeout(100)
            assert len(parent_reads) == 1, 'Concurrent settings reads exhausted browser connections'
        finally:
            browser.close()
