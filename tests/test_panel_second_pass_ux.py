"""Product states for concise quotas, summary availability and lecture actions."""
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from pku_sync.panel.student_ui import student_page
from browser_support import browser_launch_options

pw = pytest.importorskip('playwright.sync_api')
ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize('width', [390, 1440])
def test_quota_summary_and_lecture_actions(width):
    source = (ROOT / 'pku_sync/panel/static/app.js').read_text()
    source = source[:source.index('  window.setInterval(refreshAccount, 8000);')] + '''
    window.__ux = {state:state, render:render, previews:readingPreviews}; render();})();'''
    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, **browser_launch_options())
        try:
            page = browser.new_page(viewport={'width':width, 'height':1000})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            def serve(route):
                path = urlsplit(route.request.url).path
                if path == '/app': route.fulfill(body=student_page(), content_type='text/html')
                elif path == '/app/app.js': route.fulfill(body=source, content_type='text/javascript')
                elif path == '/app/app.css': route.fulfill(body=(ROOT/'pku_sync/panel/static/app.css').read_bytes(), content_type='text/css')
                else: route.fulfill(json={})
            page.route('http://panel.test/**', serve)
            page.goto('http://panel.test/app')
            pw.expect(page.locator('#auth-form')).to_be_visible()
            pw.expect(page.locator('.auth-switches button')).to_have_count(1)
            page.locator('[data-action="auth-mode-register"]').click()
            pw.expect(page.locator('[data-password-hint]')).to_be_visible()
            page.evaluate('''() => {
                let s=__ux.state;s.activated=true;
                s.quota={active:true,email_verified:true,email:'lite@example.test',tier:'lite',
                    transcribe_seconds_remaining:90000,basic_seconds_remaining:90000,
                    advanced_seconds_remaining:0,llm_points_remaining:0,weekly_rollover_seconds:90000};
                s.update={version:'0.1.62',status:'up_to_date'};
                s.campus.configured=true;s.view='campus-course';s.campus.section='home';s.campus.courseId='course';
                s.campus.course={id:'course',title:'课程',has_summary:false,lessons:[
                    {id:'lesson',title:'第二讲',date:'2026-09-23',video_available:false,video_size:'0',
                        notes_available:true,transcript_available:true,materials:[]}],
                    unassigned_materials:[],assignments:[],announcements:[]};__ux.render();
            }''')
            quota = page.locator('.sidebar [data-block="quota"]')
            if width == 390:
                page.locator('.mobile-account-menu > summary').click()
                quota = page.locator('.mobile-account-content [data-block="quota"]')
            else:
                pw.expect(quota).to_contain_text('可用转写 1500 分钟')
                assert '高级转写' not in quota.inner_text()
                assert 'AI 点 0' not in quota.inner_text()
                quota.locator('.account-actions > summary').click()
            pw.expect(quota).to_contain_text('高级转写 0 分钟')
            update = page.locator('.mobile-account-content' if width==390 else '.sidebar').locator('[data-block="update"]')
            pw.expect(update).to_contain_text('版本 v0.1.62')
            pw.expect(update).to_contain_text('已是最新版本')
            pw.expect(page.locator('[data-action="campus-preview-course-summary"]')).to_have_count(0)
            page.evaluate("__ux.state.campus.section='materials';__ux.render()")
            pw.expect(page.locator('[data-action="campus-preview-course-summary"]')).to_have_count(0)
            page.evaluate("__ux.state.campus.course.summary={title:'已保存的总结'};__ux.render()")
            pw.expect(page.locator('[data-action="campus-preview-course-summary"]')).to_be_visible()
            page.evaluate('''() => {
                __ux.state.campus.section='lecture';__ux.state.campus.lessonId='lesson';
                __ux.previews['course:lesson']={open:true,markdown:'# 本讲笔记'};__ux.render();
            }''')
            row = page.locator('.recording-row')
            assert '本地视频' not in row.inner_text()
            pw.expect(row).to_contain_text('在线回放')
            pw.expect(row.get_by_role('button', name='收起本讲笔记')).to_be_visible()
            pw.expect(row.get_by_role('button', name='收起阅读')).to_have_count(0)
            pw.expect(row.locator('.recording-primary-bar [data-action="campus-regenerate"]')).to_be_visible()
            pw.expect(row.locator('[data-action="campus-review-term"]')).to_be_hidden()
            row.locator('.lecture-more-actions > summary').click()
            pw.expect(row.locator('[data-action="campus-review-term"]')).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors
        finally:
            browser.close()
