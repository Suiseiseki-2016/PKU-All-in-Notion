"""Renderer regression only; installed native red/green is recorded separately."""
from pathlib import Path

import pytest
from browser_support import browser_launch_options
from playwright.sync_api import sync_playwright

from pku_sync.panel.student_ui import PANEL_COPY

ROOT = Path(__file__).resolve().parents[1]
EDGE = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


@pytest.mark.parametrize('transcript,notes,remote_note', [
    (False, False, False), (True, False, False), (False, True, False),
    (False, False, True), (True, True, False), (True, True, True),
])
def test_projected_lecture_does_not_replace_first_processing_with_regeneration(transcript, notes, remote_note):
    script = (ROOT / 'pku_sync/panel/static/app.js').read_text('utf-8')
    script = script[:script.index('  window.setInterval(refreshAccount, 8000);')] + (
        'window.__layout = {state: state, render: render};})();')
    with sync_playwright() as driver:
        browser = driver.chromium.launch(**browser_launch_options(), headless=True)
        try:
            page = browser.new_page()
            page.add_init_script("localStorage.setItem('pku-panel-welcome-v1', 'seen')")
            page.route('**/*', lambda route: route.fulfill(body='<div id="app"></div><div id="toast"></div>', content_type='text/html'))
            page.goto('http://renderer-regression.test')
            page.evaluate('copy => {window.PANEL_COPY=copy}', PANEL_COPY)
            page.add_script_tag(content=script)
            page.evaluate('''flags => {
                const s=window.__layout.state;
                s.activated=true; s.quota={active:true}; s.connection.connected=true;
                s.view='campus-course'; s.campus.configured=true; s.campus.courseId='official-course';
                s.campus.home={id:'test-home'}; s.campus.section='lecture'; s.campus.lessonId='lesson';
                s.campus.course={id:'official-course',title:'真实来源讲次',lessons:[{
                    id:'lesson', title:'讲次', materials:[], video_available:true,
                    notion_lecture_url:'https://www.notion.so/lecture',
                    notion_note_url:flags.remote ? 'https://www.notion.so/notes' : '',
                    transcript_available:flags.transcript,notes_available:flags.notes
                }],unassigned_materials:[]};
                window.__layout.render();
            }''', {'transcript': transcript, 'notes': notes, 'remote': remote_note})
            can_regenerate = transcript and notes
            assert page.locator('[data-action="campus-process"]').count() == int(not can_regenerate)
            assert page.locator('[data-action="campus-regenerate"]').count() == int(can_regenerate)
            assert page.locator('[data-action="campus-preview-lecture-note"]').count() == int(notes)
            assert page.locator('[data-action="campus-open-lecture"]').count() == 0
        finally:
            browser.close()
