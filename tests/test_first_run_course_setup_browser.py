"""Whole first-run UI journey with simulated services, not live-account E2E."""
import json
from urllib.parse import urlsplit

import pytest
from browser_support import browser_launch_options
from pku_sync.panel.student_ui import student_page, STATIC_DIR

playwright = pytest.importorskip('playwright.sync_api')


@pytest.mark.parametrize('width', [390, 1280])
@pytest.mark.parametrize('failed_catalog', ['empty', 'read_only'])
def test_empty_account_can_sync_retry_and_use_local_courses_from_welcome(width, failed_catalog, tmp_path):
    configured, synced, home = False, False, None
    sync_attempts, writes = 0, []
    course = {'id': '_fixture_1', 'title': '测试课程', 'recordings': 0,
              'synced_at': '2026-10-14', 'read_only': False}

    def serve(route):
        nonlocal configured, synced, sync_attempts, home
        path = urlsplit(route.request.url).path
        if path == '/app':
            route.fulfill(body=student_page(), content_type='text/html')
            return
        if path in ('/app/app.js', '/app/app.css'):
            route.fulfill(body=(STATIC_DIR / path.rsplit('/', 1)[1]).read_bytes(),
                          content_type='text/javascript' if path.endswith('.js') else 'text/css')
            return
        status, body = 200, {}
        if route.request.method == 'POST':
            writes.append(path)
        if path == '/api/platform/quota':
            body = {'active': True, 'available': True, 'email': 'new@example.test', 'email_verified': True}
        elif path == '/api/campus/courses':
            rows = [course] if synced else [dict(course, read_only=True)] if sync_attempts and failed_catalog == 'read_only' else []
            body = {'campus_configured': configured, 'courses': rows, 'notion_home': home}
        elif path == '/api/connection':
            body = {'connected': True, 'state': 'connected'}
        elif path == '/api/directory':
            body = {'courses': [], 'exercises': []}
        elif path == '/api/campus/notion/parents':
            body = {'pages': [{'id': 'parent', 'title': '学习空间'}],
                    'home': home, 'workspace': home, 'home_sync': {'status': 'pulled' if home else 'absent'}}
        elif path == '/api/recordings/credentials':
            configured = True
            body = {'configured': True, 'verified': True}
        elif path == '/api/recordings/sync':
            sync_attempts += 1
            body = {'id': str(sync_attempts), 'scope': 'background', 'kind': 'sync', 'state': 'running'}
        elif path == '/api/recordings/task':
            body = {'state': 'idle'}
            if sync_attempts:
                synced = sync_attempts > 1
                body['background_sync'] = {'id': str(sync_attempts), 'scope': 'background',
                    'kind': 'sync', 'state': 'done' if synced else 'failed',
                    'stage': '课程目录已更新' if synced else '教学网暂时无法读取，请重试',
                    'course_results': [{'course_id': course['id'], 'state': 'done', 'metadata_state': 'confirmed'}] if synced else []}
        elif path == '/api/campus/notion/workspace':
            if not synced:
                status, body = 409, {'detail': '尚无已验证的教学网课程快照。'}
            else:
                home = {'id': 'home', 'parent_id': 'parent', 'active': True, 'schema': 2,
                        'url': 'https://www.notion.so/fixture'}
                body = home
        route.fulfill(status=status, body=json.dumps(body), content_type='application/json')

    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(**browser_launch_options(), headless=True)
        try:
            page = browser.new_page(viewport={'width': width, 'height': 900})
            page.route('http://panel.test/**', serve)
            page.goto('http://panel.test/app')
            playwright.expect(page.locator('.welcome-screen')).to_be_visible()
            page.locator('[data-action="welcome-start"]').click()
            page.locator('#recording-username').fill('fixture-student')
            page.locator('#recording-password').fill('fixture-password')
            page.locator('[data-action="recording-save-credentials"]').click()
            playwright.expect(page.locator('.campus-setup')).to_contain_text('下一步：课程目录')
            sync = page.locator('.campus-setup [data-action="recording-sync"]')
            playwright.expect(sync).to_be_enabled()
            create = page.locator('[data-action="campus-create-workspace"]')
            assert create.count() == 0
            sync.click()
            playwright.expect(page.locator('[data-background-sync]')).to_contain_text('教学网暂时无法读取')
            playwright.expect(sync).to_be_enabled()
            assert create.count() == 0
            assert configured and writes == ['/api/recordings/credentials', '/api/recordings/sync']
            page.screenshot(path=str(tmp_path / f'failed-sync-{width}.png'), full_page=True)
            sync.click()
            playwright.expect(page.locator('.campus-setup')).to_contain_text('已就绪')
            assert create.count() == 0
            assert 'Notion' not in page.locator('body').inner_text()
            assert writes == ['/api/recordings/credentials', '/api/recordings/sync', '/api/recordings/sync']
            assert page.locator('#auth-form').count() == 0
            page.screenshot(path=str(tmp_path / f'home-ready-{width}.png'), full_page=True)
            page.reload()
            playwright.expect(page.get_by_role('heading', name='我的课程', exact=True)).to_be_visible()
            assert page.locator('.welcome-screen').count() == 0
        finally:
            browser.close()
