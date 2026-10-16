"""A new user's campus setup works without workspace preparation."""
import json
from urllib.parse import urlsplit

import pytest
from browser_support import browser_launch_options
from pku_sync.panel.student_ui import student_page, STATIC_DIR

playwright = pytest.importorskip('playwright.sync_api')


def test_new_account_can_sync_and_browse_without_workspace_preparation():
    configured = False
    synced = False
    writes = []
    def serve(route):
        nonlocal configured, synced
        path = urlsplit(route.request.url).path
        status, body = 200, {}
        if path == '/app':
            route.fulfill(status=200, body=student_page(), content_type='text/html')
            return
        if path in {'/app/app.js', '/app/app.css'}:
            route.fulfill(status=200, body=(STATIC_DIR / path.rsplit('/', 1)[1]).read_bytes(),
                          content_type='text/javascript' if path.endswith('.js') else 'text/css')
            return
        if route.request.method == 'POST':
            writes.append(path)
        if path == '/api/platform/quota':
            body = {'active': True, 'available': True, 'email': 'new@example.test',
                    'email_verified': True, 'llm_points_remaining': 10, 'transcribe_seconds_remaining': 600}
        elif path == '/api/campus/courses':
            body = {'campus_configured': configured, 'courses':
                [{'id': 'fixture-course', 'title': '测试课程', 'synced_at': '2026-10-14'}] if synced else [], 'notion_home': None}
        elif path == '/api/connection':
            body = {'state': 'connected', 'connected': True}
        elif path == '/api/recordings/task':
            body = {'state': 'idle'}
            if synced:
                body['background_sync'] = {'scope': 'background', 'kind': 'sync', 'state': 'done', 'id': 'sync'}
        elif path == '/api/recordings/sync':
            synced = True
            body = {'scope': 'background', 'kind': 'sync', 'state': 'running', 'id': 'sync'}
        elif path == '/api/directory':
            body = {'courses': [], 'exercises': []}
        elif path == '/api/campus/notion/parents':
            body = {'pages': [{'id': 'parent', 'title': '学习空间'}], 'home_sync': {'status': 'absent'}}
        elif path == '/api/recordings/credentials':
            configured = True
            body = {'configured': True, 'verified': True}
        elif path == '/api/campus/notion/workspace':
            status, body = 409, {'detail': '课程准备读取已过期、快照变化或课序不一致。'}
        else:
            status = 404
        route.fulfill(status=status, body=json.dumps(body), content_type='application/json')

    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(**browser_launch_options(), headless=True)
        try:
            page = browser.new_page(viewport={'width': 1280, 'height': 900})
            page.add_init_script("localStorage.setItem('pku-panel-welcome-v1','seen')")
            page.route('http://panel.test/**', serve)
            page.goto('http://panel.test/app')
            page.locator('.sidebar [data-action="campus-account-settings"]').click()
            page.locator('#recording-username').fill('fixture-student')
            page.locator('#recording-password').fill('fixture-password')
            page.locator('[data-action="recording-save-credentials"]').click()
            playwright.expect(page.locator('[data-action="recording-edit-credentials"]')).to_be_visible()
            page.locator('.campus-setup [data-action="recording-sync"]').click()
            assert page.locator('[data-action="campus-create-workspace"]').count() == 0
            assert 'Notion' not in page.locator('body').inner_text()
            assert configured and page.locator('#auth-form').count() == 0
            page.locator('.sidebar [data-action="open-dashboard"]').click()
            playwright.expect(page.get_by_role('heading', name='我的课程', exact=True)).to_be_visible()
            playwright.expect(page.locator('[data-action="recording-sync"]')).to_be_enabled()
            page.reload()
            playwright.expect(page.get_by_role('heading', name='我的课程', exact=True)).to_be_visible()
            assert page.locator('#auth-form').count() == 0
            page.locator('.sidebar [data-action="campus-account-settings"]').click()
            playwright.expect(page.locator('[data-action="recording-edit-credentials"]')).to_be_visible()
            assert page.locator('[data-action="campus-create-workspace"]').count() == 0
            assert 'Notion' not in page.locator('body').inner_text()
            assert writes == ['/api/recordings/credentials', '/api/recordings/sync']
        finally:
            browser.close()
