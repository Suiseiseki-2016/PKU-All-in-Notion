"""Third-pass account, upgrade guidance, sync feedback and cached-course behavior."""
from pathlib import Path
import runpy
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pku_sync import auth
from pku_sync.panel import recordings_api
from pku_sync.panel.student_ui import student_page
from browser_support import browser_launch_options

pw = pytest.importorskip('playwright.sync_api')
ROOT = Path(__file__).parents[1]


@pytest.fixture
def browser():
    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, **browser_launch_options())
        yield browser
        browser.close()


def panel_page(browser, width):
    script = (ROOT/'pku_sync/panel/static/app.js').read_text()
    script = script[:script.index('  window.setInterval(refreshAccount, 8000);')] + '''
    window.__ux={state:state,render:render,poll:pollRecordingTask};render();})();'''
    page = browser.new_page(viewport={'width':width,'height':1000})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
    def serve(route):
        path = urlsplit(route.request.url).path
        if path == '/app': route.fulfill(body=student_page(),content_type='text/html')
        elif path == '/app/app.js': route.fulfill(body=script,content_type='text/javascript')
        elif path == '/app/app.css': route.fulfill(body=(ROOT/'pku_sync/panel/static/app.css').read_bytes(),content_type='text/css')
        else: route.fulfill(json={})
    page.route('http://panel.test/**',serve)
    page.goto('http://panel.test/app')
    page.evaluate('''() => {
        let s=__ux.state;s.activated=true;
        s.quota={active:true,email_verified:true,email:'student@example.test',tier:'lite',
            transcribe_seconds_remaining:90000,basic_seconds_remaining:90000,advanced_seconds_remaining:0,
            llm_points_remaining:0,weekly_rollover_seconds:90000,tier_expires_at:Date.now()/1000+30*86400,
            invite_code:'PKU-TEST'};
        s.campus.configured=true;s.view='campus-course';s.campus.section='home';s.campus.courseId='course';
        s.campus.courses=[{id:'course',title:'课程',recordings:1,synced_at:new Date().toISOString()}];
        s.campus.course={id:'course',title:'课程',has_summary:false,lessons:[
            {id:'lesson',title:'第一讲',materials:[],notes_available:false}],
            unassigned_materials:[],assignments:[],announcements:[]};__ux.render();
    }''')
    return page, errors


@pytest.mark.parametrize('width',[390,1440])
@pytest.mark.parametrize('tier,points,exchange',[('free',0,False),('lite',0,False),('lite',5,True),('pro',0,True),('max',0,True)])
def test_account_expiry_details_exchange_and_renewal(browser,width,tier,points,exchange):
    page,errors=panel_page(browser,width)
    page.evaluate('''([tier,points])=>{__ux.state.quota.tier=tier;__ux.state.quota.llm_points_remaining=points;__ux.render()}''',[tier,points])
    if width==390:
        page.locator('.mobile-account-menu > summary').click()
        card=page.locator('.mobile-account-content [data-block="quota"]')
    else:
        card=page.locator('.sidebar [data-block="quota"]')
        assert card.locator(':scope > p').count()==2
        assert card.locator(':scope > [data-dual-quota]').count()==0
        card.locator('.account-drawer > summary').click()
        drawer=card.locator('.account-drawer')
        assert drawer.locator('[data-membership-expiry], .quota-available').count()==0
    pw.expect(card.locator('[data-membership-expiry]')).to_contain_text('有效期至 ')
    assert card.inner_text().count('每周充能 25 小时自动滚存')==1
    pw.expect(card).to_contain_text('student@example.test · 邮箱已验证')
    pw.expect(card.locator('[data-quota-exchange]')).to_have_count(int(exchange))
    field=card.locator('[data-redeem-form] input')
    assert field.get_attribute('placeholder')=='输入兑换码、礼包链接或邀请码'
    assert field.evaluate('''e=>{let c=document.createElement('canvas').getContext('2d'),s=getComputedStyle(e);c.font=s.font;return c.measureText(e.placeholder).width <= e.clientWidth-parseFloat(s.paddingLeft)-parseFloat(s.paddingRight)}''')
    pw.expect(card.get_by_role('button',name='兑换 / 续费',exact=True)).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors
    page.close()


@pytest.mark.parametrize('width',[390,1440])
@pytest.mark.parametrize('tier',['lite','free'])
def test_locked_features_open_and_focus_renewal(browser,width,tier):
    page,errors=panel_page(browser,width)
    page.evaluate('(tier)=>{__ux.state.quota.tier=tier;__ux.render()}',tier)
    field=page.locator('#code-mobile' if width==390 else '#code')
    for action in ['campus-course-summary','campus-open-center']:
        page.locator('[data-action="'+action+'"]').first.click()
        pw.expect(field).to_be_focused()
        pw.expect(page.locator('#toast')).to_contain_text('Pro / Max')
        pw.expect(page.locator('#toast')).to_contain_text('会员续费 / 兑换码')
        assert page.evaluate('__ux.state.campus.section')=='home'
        if width==390:page.locator('.mobile-account-menu > summary').click()
    page.evaluate("__ux.state.campus.section='lecture';__ux.state.campus.lessonId='lesson';__ux.render()")
    page.locator('[data-action="membership-upgrade"]').press('Enter')
    pw.expect(field).to_be_focused()
    pw.expect(page.locator('#toast')).to_contain_text('Max')
    pw.expect(page.locator('#toast')).to_contain_text('上传补充材料')
    assert not errors
    page.close()


def test_expiry_aliases_and_no_fixed_expiry(browser):
    page,errors=panel_page(browser,1440)
    for key,value,expected in [('tier_expires_at',1893456000,'2030-01-01'),('membership_expires_at','2030-01-01T12:00:00Z','2030-01-01'),
        ('subscription_expires_at',1893456000000,'2030-01-01'),('expires_at','2030-01-01','2030-01-01'),('active_until','2030-01-01','2030-01-01')]:
        page.evaluate('''([key,value])=>{let q=__ux.state.quota;for(let k of ['tier_expires_at','membership_expires_at','subscription_expires_at','expires_at','active_until']) delete q[k];q[key]=value;__ux.render()}''',[key,value])
        pw.expect(page.locator('.sidebar [data-membership-expiry]')).to_contain_text(expected)
    page.evaluate('delete __ux.state.quota.active_until;__ux.render()')
    pw.expect(page.locator('.sidebar [data-membership-expiry]')).to_contain_text('按周充能')
    page.evaluate('__ux.state.quota.weekly_rollover_seconds=0;__ux.render()')
    pw.expect(page.locator('.sidebar [data-membership-expiry]')).to_have_text('长期有效')
    assert not errors


@pytest.mark.parametrize('width',[390,1440])
def test_sync_metadata_success_toast_and_cached_connect_action(browser,width):
    page,errors=panel_page(browser,width)
    page.evaluate("__ux.state.view='dashboard';__ux.render()")
    pw.expect(page.locator('.course-list-meta')).to_contain_text('1 门课程 · 上次更新：刚刚')
    pw.expect(page.locator('.course-list-actions > button')).to_have_count(2)
    # A completed job survives navigation but no longer occupies any page.
    for section in ['home','lecture','materials']:
        page.evaluate('''section=>{let s=__ux.state;s.view='campus-course';s.campus.section=section;s.campus.lessonId='lesson';
            s.recordings.task={kind:'sync',state:'done',stage:'课程目录已更新'};
            s.recordings.background={state:'done',stage:'课程目录已更新',course_results:[{state:'done'}]};__ux.render()}''',section)
        pw.expect(page.locator('.recording-task, .background-sync-banner')).to_have_count(0)
    page.evaluate("__ux.state.recordings.task={kind:'sync',state:'running',stage:'更新课程目录'};__ux.state.recordings.background={state:'idle'};__ux.render()")
    pw.expect(page.locator('.recording-task')).to_contain_text('更新课程目录')
    page.route('http://panel.test/api/recordings/task',lambda route:route.fulfill(json={'kind':'sync','state':'done','stage':'课程目录已更新'}))
    page.evaluate('__ux.poll(true)')
    pw.expect(page.locator('#toast')).to_have_text('课程目录已更新')
    pw.expect(page.locator('.recording-task, .background-sync-banner')).to_have_count(0)
    page.evaluate("__ux.state.recordings.task={kind:'sync',state:'done',stage:'更新完成',warnings:['部分资料需重试']};__ux.render()")
    pw.expect(page.locator('.recording-task')).to_contain_text('部分资料需重试')
    page.evaluate("__ux.state.campus.configured=false;__ux.state.campus.course.read_only=true;__ux.render()")
    notice=page.locator('section[role="status"]').filter(has_text='当前展示本机已缓存的课程内容')
    pw.expect(notice).to_contain_text('如需同步最新课件与录像，请先连接教学网。')
    notice.locator('[data-action="open-campus-setup"]').click()
    pw.expect(page.locator('#recording-credentials-form')).to_be_visible()
    pw.expect(page.locator('#recording-username')).to_be_focused()
    assert not errors


@pytest.mark.parametrize('fallback',[False,True])
def test_gift_copy_primary_claim_and_success_cleanup(browser,fallback):
    render=runpy.run_path(str(ROOT/'release-work/pku-server-current/server/auth_pages.py'))['gift_landing_page']
    page=browser.new_page(viewport={'width':390,'height':844})
    errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.add_init_script('''window.copied=[];document.execCommand=()=>{window.copied.push(document.querySelector('textarea').value);return true};
        Object.defineProperty(navigator,'clipboard',{value:{writeText:async value=>{''' +
        ("throw Error('denied')" if fallback else "window.copied.push(value)") + '''}}});''')
    def serve(route):
        route.fulfill(body=render(token='GIFT-COPY-TEST',gift_summary='Pro 会员 30 天',ok_message='已成功绑定' if route.request.method=='POST' else ''),content_type='text/html')
    page.route('http://gift.test/**',serve)
    page.goto('http://gift.test/gift/GIFT-COPY-TEST')
    assert page.locator('#copy-gift-code').evaluate('e=>e.offsetWidth') < 100
    page.locator('#copy-gift-code').click()
    pw.expect(page.locator('#copy-gift-code')).to_have_text('已复制')
    assert page.evaluate('window.copied')==['GIFT-COPY-TEST']
    page.locator('#email').fill('student@example.test')
    page.locator('#password').fill('LocalTest2026!')
    page.get_by_role('button',name='登录并直接领取').click()
    pw.expect(page.locator('form, #gift-code, #copy-gift-code, .gift-desktop-hint')).to_have_count(0)
    assert not errors


def test_conflicting_student_id_preserves_local_credentials(tmp_path,monkeypatch):
    from test_tier_invite_gift_client import settings
    s=settings(tmp_path)
    manager=recordings_api.RecordingWorkManager(s)
    app=FastAPI()
    app.state.platform_service=SimpleNamespace(bind_campus=lambda student:{'bound':False,'status_code':409,
        'detail':'当前账号已绑定教学网学号 20****1234，请使用该学号连接。'})
    recordings_api.add_recording_routes(app,manager)
    monkeypatch.setattr(recordings_api,'env_file_path',lambda _:tmp_path/'.env')
    monkeypatch.setattr(auth,'get_session',lambda **kwargs:SimpleNamespace(close=lambda:None))
    response=TestClient(app).post('/api/recordings/credentials',json={'username':'2026995678','password':'new-password'})
    assert response.status_code==409 and '20****1234' in response.json()['detail']
    assert s.pku_username=='old' and s.pku_password=='old-password'
    assert not (tmp_path/'.env').exists()


def test_campus_reward_refreshes_balance_and_expiry_before_next_poll(browser):
    page,errors=panel_page(browser,1440)
    page.evaluate("let s=__ux.state;s.view='campus-account';s.campus.configured=false;s.quota.transcribe_seconds_remaining=0;s.quota.basic_seconds_remaining=0;s.quota.tier_expires_at=0;__ux.render()")
    page.route('http://panel.test/api/recordings/credentials',lambda route:route.fulfill(json={
        'configured':True,'verified':True,'invite_reward_granted':True}))
    quota=page.evaluate('__ux.state.quota')
    quota.update(transcribe_seconds_remaining=90000,basic_seconds_remaining=90000,tier_expires_at=1893456000)
    page.route('http://panel.test/api/platform/quota',lambda route:route.fulfill(json=quota))
    page.locator('#recording-username').fill('2026991234')
    page.locator('#recording-password').fill('CampusPassword')
    page.locator('[data-action="recording-save-credentials"]').click()
    # Timers are disabled in this renderer: the credential flow must render its own refresh.
    pw.expect(page.locator('.sidebar .quota-available')).to_have_text('可用转写 1500 分钟')
    pw.expect(page.locator('.sidebar [data-membership-expiry]')).to_have_text('有效期至 2030-01-01')
    assert not errors
