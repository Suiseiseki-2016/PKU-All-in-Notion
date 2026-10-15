"""Development renderer checks; these do not certify native file saving."""
from pathlib import Path

import pytest
from browser_support import browser_launch_options
from playwright.sync_api import sync_playwright
from pku_sync.panel.student_ui import PANEL_COPY


@pytest.fixture
def panel():
    script = (Path(__file__).resolve().parents[1] / 'pku_sync/panel/static/app.js').read_text('utf-8')
    script = script[:script.index('  window.setInterval(refreshAccount, 8000);')] + (
        'window.__draft = {state: state, render: render};})();')
    with sync_playwright() as driver:
        browser = driver.chromium.launch(**browser_launch_options(), headless=True)
        page = browser.new_page()
        page.add_init_script("localStorage.setItem('pku-panel-welcome-v1', 'seen')")
        try:
            page.route('http://panel.test/', lambda route: route.fulfill(body='<div id="app"></div><div id="toast"></div>', content_type='text/html'))
            page.goto('http://panel.test/')
            page.evaluate('copy => {window.PANEL_COPY = copy;}', PANEL_COPY)
            page.add_script_tag(content=script)
            page.evaluate('''() => {
                window.pendingChecks = []; window.downloads = [];
                window.fetch = (url, opts) => new Promise(resolve => pendingChecks.push({url, opts, resolve}));
                const click = HTMLAnchorElement.prototype.click;
                HTMLAnchorElement.prototype.click = function () {
                    if (this.hasAttribute('download') && !this.hasAttribute('data-action')) downloads.push(this.getAttribute('href'));
                    else click.call(this);
                };
                const s = __draft.state;
                s.view = 'campus-course'; s.activated = true;
                s.campus.courseId = '_1_1'; s.campus.section = 'assignments';
                s.campus.course = {id:'_1_1', title:'Course', lessons:[], unassigned_materials:[], assignments:[{content_id:'_2_1',title:'Assignment'}]};
                s.campus.submission.selectedId = '_2_1';
                s.campus.submission.draft = {draft_id:'a'.repeat(32),sha256:'b'.repeat(64),
                    download_url:'/api/campus/submission-drafts/'+'a'.repeat(32)+'/file',
                    archive_name:'answer.zip',files:[],state:'prepared'};
                __draft.render();
            }''')
            yield page
        finally:
            browser.close()


def resolve(page, body, status=200):
    page.evaluate('''arg => pendingChecks[0].resolve(new Response(JSON.stringify(arg.body), {
        status:arg.status, headers:{'Content-Type':'application/json'}}))''', {'body': body, 'status': status})
    page.wait_for_function('!__draft.state.campus.submission.downloadBusy')


def test_course_center_transition_preserves_native_document_identity(panel):
    panel.evaluate('() => { __draft.state.campus.section = "home"; __draft.render(); }')
    original_url = panel.url
    panel.locator('[data-action="campus-open-center"][data-center="assignments"]').click()
    assert panel.evaluate('__draft.state.campus.section') == 'assignments'
    assert panel.locator('[data-action="campus-download-draft"]').is_visible()
    assert panel.url == original_url


def test_account_settings_reads_current_workspace_after_directory_update(panel):
    panel.evaluate('''() => {
        const s = __draft.state;
        s.campus.configured = true;
        s.connection.connected = true;
        s.campus.home = null; s.campus.workspace = null; s.campus.parents = [];
        __draft.render();
    }''')
    panel.locator('.sidebar [data-action="campus-account-settings"]').click()
    assert panel.evaluate('pendingChecks.length') == 1
    assert panel.evaluate('pendingChecks[0].url') == '/api/campus/notion/parents'
    panel.evaluate('''() => pendingChecks[0].resolve(new Response(JSON.stringify({
        pages: [{id:'a'.repeat(32),title:'E2E parent'}],
        home: {schema:2,active:true,id:'b'.repeat(32),parent_id:'a'.repeat(32),url:'https://www.notion.so/'+'b'.repeat(32)},
        workspace: {schema:2,id:'b'.repeat(32),parent_id:'a'.repeat(32),active:true,url:'https://www.notion.so/'+'b'.repeat(32)},
        legacy_home: {id:'c'.repeat(32),url:'https://www.notion.so/'+'c'.repeat(32)}
    }), {status:200,headers:{'Content-Type':'application/json'}}))''')
    link = panel.locator('.home-setup-ready [data-action="campus-open-home"]')
    link.wait_for(state='visible')
    assert link.get_attribute('data-url') == 'https://www.notion.so/' + 'b' * 32
    assert panel.get_by_role('heading', name='连接设置', exact=True).is_visible()


def test_download_double_click_shares_preflight_then_uses_exact_preview_hash(panel):
    anchor = panel.locator('[data-action="campus-download-draft"]')
    anchor.dblclick()
    assert panel.evaluate('pendingChecks.length') == 1
    url = panel.evaluate('pendingChecks[0].url')
    assert 'sha256=' + 'b' * 64 in url and 'course_id=_1_1' in url and 'content_id=_2_1' in url
    resolve(panel, {'valid': True, 'draft_id': 'a' * 32, 'sha256': 'b' * 64})
    assert panel.evaluate('downloads') == ['/api/campus/submission-drafts/' + 'a' * 32 + '/file?sha256=' + 'b' * 64]


@pytest.mark.parametrize('body,status', [
    ({'detail': '草稿文件与预览时不同，请重新准备。'}, 409),
    ({'valid': True, 'draft_id': 'a' * 32, 'sha256': 'c' * 64}, 200),
])
def test_failed_or_mismatched_preflight_shows_error_without_download(panel, body, status):
    panel.locator('[data-action="campus-download-draft"]').click()
    resolve(panel, body, status)
    assert panel.locator('.assignment-preparation [role="alert"]').is_visible()
    assert '预览' in panel.locator('.assignment-preparation [role="alert"]').inner_text()
    assert panel.evaluate('downloads') == []


def test_changing_assignment_while_check_pending_cannot_download_previous_target(panel):
    panel.locator('[data-action="campus-download-draft"]').click()
    panel.evaluate('__draft.state.campus.submission.selectedId = "_3_1"')
    resolve(panel, {'valid': True, 'draft_id': 'a' * 32, 'sha256': 'b' * 64})
    assert panel.evaluate('downloads') == []


@pytest.mark.parametrize('outcome', ['saved', 'cancelled', 'mismatched'])
def test_native_save_bridge_keeps_exact_identity_and_requires_a_verified_save_receipt(panel, outcome):
    panel.evaluate('''outcome => {
        window.nativeSaveCalls = [];
        window.__TAURI_INTERNALS__ = {invoke: (command, args) => {
            nativeSaveCalls.push({command,args});
            return Promise.resolve(outcome === 'cancelled' ? {state:'cancelled'} :
              {state:'saved',filename:'answer.zip',sha256:outcome === 'mismatched' ? 'c'.repeat(64) : args.sha256});
        }};
    }''', outcome)
    panel.locator('[data-action="campus-download-draft"]').click()
    resolve(panel, {'valid': True, 'draft_id': 'a' * 32, 'sha256': 'b' * 64})
    calls = panel.evaluate('nativeSaveCalls')
    assert calls == [{'command': 'save_reviewed_draft', 'args': {
        'draftId': 'a' * 32, 'sha256': 'b' * 64, 'courseId': '_1_1',
        'contentId': '_2_1', 'archiveName': 'answer.zip'}}]
    assert panel.evaluate('downloads') == []
    assert not panel.locator('#assignment-reviewed').is_checked()
    if outcome == 'mismatched':
        assert '保存结果未通过核验' in panel.locator('.assignment-preparation [role="alert"]').inner_text()
        assert not panel.locator('[data-native-save="saved"]').count()
    else:
        status = panel.locator('.assignment-download-status')
        assert status.get_attribute('data-native-save') == outcome
        assert status.get_attribute('data-saved-sha256') == ('b' * 64 if outcome == 'saved' else '')
