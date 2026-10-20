"""Client account contracts, actual rendered auth UI, and ASR wire metadata."""
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pku_sync import auth, platform, transcription
from pku_sync.panel import recordings_api
from pku_sync.panel.platform_bridge import FakePlatformBridge, RealPlatformBridge
from pku_sync.panel.student_ui import ACCOUNT_COPY, AUTH_COPY, student_page

ROOT=Path(__file__).parents[1]


def settings(tmp_path, **kwargs):
    return SimpleNamespace(**dict({'data_dir':tmp_path,'platform_token':'session',
        'cloud_transcribe_url':'https://panel.test/v1/transcribe','transcription_backend':'cloud',
        'notes_strategy':'transcript','pku_username':'old','pku_password':'old-password'},**kwargs))


@pytest.mark.parametrize('raw,expected',[
    (' GIFT-123456 ','GIFT-123456'),('https://host/gift/GIFT-ABCD?x=1#claim','GIFT-ABCD'),
    ('https://host/invite/PKU-ABCD','PKU-ABCD'),('/redeem/ABCDEF','ABCDEF'),
    ('https://host/?gift=GIFT-ABCD','GIFT-ABCD'),('https://host/?a=b&invite=PKU-ABCD','PKU-ABCD'),
    ('https://host/?code=ABCDEF','ABCDEF'),('', '')])
def test_pasted_url_token_extraction(raw,expected):
    assert platform._extract_code_from_input(raw)==expected


def test_client_copy_describes_tiers_quotas_invites_and_password():
    assert AUTH_COPY['password_register_label']=='密码（至少 8 位）'
    assert '8' in AUTH_COPY['password_hint']
    assert all(tier in ACCOUNT_COPY['tier_labels'] for tier in ('lite','pro','max'))
    assert '1:1' in ACCOUNT_COPY['advanced_fallback_hint']
    assert '不扣 AI 点' in ACCOUNT_COPY['advanced_fallback_hint']
    assert '25' in ACCOUNT_COPY['weekly_rollover_hint']
    assert '10' in ACCOUNT_COPY['exchange_title'] and '1 小时' in ACCOUNT_COPY['exchange_title']
    assert '学号' in ACCOUNT_COPY['invite_help'] and '1 个月' in ACCOUNT_COPY['invite_help']
    assert 'Gift Link' in ACCOUNT_COPY['redeem_help']


def test_registration_password_hint_and_validation_in_real_browser():
    from playwright.sync_api import sync_playwright
    from browser_support import browser_launch_options
    script=(ROOT/'pku_sync/panel/static/app.js').read_text('utf-8')
    script=script.rstrip()[:-5]+'window.__tierTest={state:state,render:render,submitAuth:submitAuth};})();'
    posted=[]
    def serve(route):
        path=urlsplit(route.request.url).path
        if route.request.method=='POST':
            posted.append((path,route.request.post_data_json))
            route.fulfill(status=400,json={'detail':'test validation response'})
        elif path=='/app': route.fulfill(body=student_page(),content_type='text/html')
        elif path=='/app/app.js': route.fulfill(body=script,content_type='application/javascript')
        elif path=='/app/app.css': route.fulfill(body='',content_type='text/css')
        else: route.fulfill(json={'active':False})
    with sync_playwright() as driver:
        options = browser_launch_options()
        if Path("/Applications/Google Chrome.app").exists():
            options = {"channel": "chrome"}
        browser=driver.chromium.launch(headless=True,**options)
        try:
            page=browser.new_page()
            page.add_init_script("localStorage.setItem('pku-panel-welcome-v1','seen')")
            page.route('http://panel.test/**',serve)
            page.goto('http://panel.test/app')
            page.locator('.auth-mode-tabs [data-action="auth-mode-register"]').click()
            assert page.locator('label[for="auth-password"]').inner_text()=='密码（至少 8 位）'
            assert page.locator('#auth-password').get_attribute('minlength')=='8'
            assert page.locator('label[for="auth-invite"]').inner_text()=='同学邀请码（选填）'
            assert page.locator('#auth-invite').get_attribute('placeholder')=='如 PKU-4D034AEE（也可粘贴邀请链接）'
            page.locator('#auth-email').fill('student@example.com')
            page.locator('#auth-password').fill('1234567')
            page.locator('#auth-password-confirm').fill('1234567')
            page.evaluate('window.__tierTest.submitAuth()')
            assert posted==[]
            assert '8' in page.locator('#auth-help').inner_text()
            page.locator('#auth-password').fill('12345678')
            page.locator('#auth-password-confirm').fill('12345678')
            page.locator('#auth-invite').fill('https://panel.test/invite/PKU-ABCD')
            page.evaluate('window.__tierTest.submitAuth()')
            assert posted==[('/api/auth/register',{'email':'student@example.com','password':'12345678','invite_code':'https://panel.test/invite/PKU-ABCD'})]
        finally: browser.close()


@pytest.mark.parametrize('mobile,verified', [(False,True), (True,True), (False,False)])
def test_invite_code_and_link_copy_and_full_width_redeem_in_browser(mobile, verified):
    from playwright.sync_api import sync_playwright, expect
    from browser_support import browser_launch_options
    script=(ROOT/'pku_sync/panel/static/app.js').read_text('utf-8')
    script=script.rstrip()[:-5]+'window.__tierTest={state:state,render:render};})();'
    css=(ROOT/'pku_sync/panel/static/app.css').read_text('utf-8')
    def serve(route):
        path=urlsplit(route.request.url).path
        if path=='/app': route.fulfill(body=student_page(),content_type='text/html')
        elif path=='/app/app.js': route.fulfill(body=script,content_type='application/javascript')
        elif path=='/app/app.css': route.fulfill(body=css,content_type='text/css')
        else: route.fulfill(json={'active':False})
    with sync_playwright() as driver:
        options=browser_launch_options()
        if Path('/Applications/Google Chrome.app').exists(): options={'channel':'chrome'}
        browser=driver.chromium.launch(headless=True,**options)
        try:
            page=browser.new_page(viewport={'width':390 if mobile else 1280,'height':900})
            page.add_init_script("""localStorage.setItem('pku-panel-welcome-v1','seen');
                window.copied=[];Object.defineProperty(navigator,'clipboard',{
                    value:{writeText:async text=>window.copied.push(text)}});""")
            page.route('http://panel.test/**',serve)
            page.goto('http://panel.test/app')
            page.wait_for_function('window.__tierTest !== undefined')
            page.evaluate("""verified=>{let s=window.__tierTest.state;s.activated=true;
                s.quota={active:true,email:'student@example.test',email_verified:verified,
                    tier:'lite',invite_code:'PKU-4D034AEE',
                    invite_url:'https://relay.test/invite/PKU-4D034AEE'};window.__tierTest.render()}""", verified)
            suffix='-mobile' if mobile else ''
            menu=page.locator('[data-persistent-disclosure="'+('mobile-account' if mobile else 'desktop-account')+'"]')
            if menu.get_attribute('open') is None:
                menu.locator(':scope > summary').click()
            expect(page.locator('#account-invite-code'+suffix)).to_have_text('PKU-4D034AEE')
            page.locator('#copy-invite-code-btn'+suffix).click()
            page.locator('#copy-invite-btn'+suffix).click()
            assert page.evaluate('window.copied')==['PKU-4D034AEE','https://relay.test/invite/PKU-4D034AEE']
            if verified:
                field=page.locator('#code'+suffix)
                assert field.get_attribute('placeholder')=='输入兑换码、礼包链接或邀请码'
                assert page.locator('label[for="code'+suffix+'"]').inner_text()=='会员续费 / 兑换码'
                field.scroll_into_view_if_needed()
                input_box=field.bounding_box()
                row_box=page.locator('#redeem-form'+suffix+' .redeem-input-row').bounding_box()
                button_box=page.locator('#redeem-form'+suffix+' [data-action="redeem"]').bounding_box()
                assert abs(input_box['width']-row_box['width'])<2
                assert button_box['y']>=input_box['y']+input_box['height']
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        finally: browser.close()


def test_exchange_and_campus_binding_wire_contract(tmp_path,monkeypatch):
    calls=[]
    payload={'tier':'max','transcribe_seconds_remaining':3600,'basic_seconds_remaining':0,
             'advanced_seconds_remaining':3600,'weekly_rollover_seconds':90000,'llm_points_remaining':0,
             'campus_bound':True,'reward_granted':True}
    def post(url,**kwargs):
        calls.append((url,kwargs));return httpx.Response(200,json=payload)
    monkeypatch.setattr(platform,'_post',post)
    bridge=RealPlatformBridge(settings(tmp_path))
    result=bridge.exchange_quota(1,target='advanced')
    assert result['advanced_seconds_remaining']==3600 and result['tier']=='max'
    assert calls[-1][0]=='https://panel.test/v1/quota/exchange'
    assert calls[-1][1]['json']=={'hours':1,'target':'advanced'}
    bound=bridge.bind_campus(' 20260001 ')
    assert bound['bound'] and bound['reward_granted']
    assert calls[-1][0]=='https://panel.test/v1/auth/bind-campus'
    assert calls[-1][1]['json']=={'student_id':'20260001'}
    assert all(item[1]['headers']=={'Authorization':'Bearer session'} for item in calls)


@pytest.mark.parametrize('hours,target',[(0,'basic'),(-1,'basic'),(True,'basic'),(1.5,'basic'),(101,'basic'),(1,'wrong')])
def test_invalid_exchange_never_sends_request(tmp_path,monkeypatch,hours,target):
    monkeypatch.setattr(platform,'_post',lambda *a,**k:pytest.fail('invalid request sent'))
    with pytest.raises(platform.PlatformError):platform.exchange_quota(hours,settings(tmp_path),target=target)


def test_bind_campus_offline_anonymous_and_malformed(tmp_path,monkeypatch):
    assert platform.bind_campus('20260001',settings(tmp_path,platform_token=''))['reason']=='not_signed_in'
    monkeypatch.setattr(platform,'_post',lambda *a,**k:httpx.Response(200,json=[]))
    assert platform.bind_campus('20260001',settings(tmp_path))['reason']=='invalid_response'
    def offline(*a,**k):raise httpx.ConnectError('offline')
    monkeypatch.setattr(platform,'_post',offline)
    assert platform.bind_campus('20260001',settings(tmp_path))['reason']=='offline'


def test_redeem_and_register_extract_urls_and_preserve_tier(tmp_path,monkeypatch):
    calls=[]
    monkeypatch.setattr(platform,'env_file_path',lambda _:tmp_path/'.env')
    def post(url,**kwargs):
        calls.append((url,kwargs['json']))
        return httpx.Response(200,json={'platform_token':'new-session','email':'student@example.com',
            'email_verified':False,'tier':'lite','transcribe_seconds_remaining':90000,
            'basic_seconds_remaining':90000,'weekly_rollover_seconds':90000,'kind':'gift'})
    monkeypatch.setattr(platform,'_post',post)
    s=settings(tmp_path)
    assert platform.register('student@example.com','password1',s,invite_code='https://host/invite/PKU-ABCD')['tier']=='lite'
    assert calls[-1][1]['invite_code']=='PKU-ABCD'
    assert platform.redeem('https://host/gift/GIFT-ABCD',s)['basic_seconds_remaining']==90000
    assert calls[-1][1]=={'code':'GIFT-ABCD'}


@pytest.mark.parametrize('outcome',['verified','rejected','offline-bind'])
def test_campus_credentials_bind_after_verification_before_local_save(tmp_path,monkeypatch,outcome):
    s=settings(tmp_path)
    manager=recordings_api.RecordingWorkManager(s)
    events=[]
    def bind(student):
        events.append('bind')
        assert student=='20260001' and s.pku_username=='old'
        assert not (tmp_path/'.env').exists()
        if outcome=='offline-bind':raise platform.PlatformError('offline')
        return {'reward_granted':True}
    app=FastAPI();app.state.platform_service=SimpleNamespace(bind_campus=bind)
    recordings_api.add_recording_routes(app,manager)
    monkeypatch.setattr(recordings_api,'env_file_path',lambda _:tmp_path/'.env')
    def verify(**credentials):
        events.append('verify')
        if outcome=='rejected':raise auth.CampusLoginRejected('rejected')
        return SimpleNamespace(close=lambda:events.append('closed'))
    monkeypatch.setattr(auth,'get_session',verify)
    response=TestClient(app).post('/api/recordings/credentials',json={'username':'20260001','password':'campus-secret'})
    if outcome=='rejected':
        assert response.status_code==401 and events==['verify']
    else:
        assert response.status_code==200,response.text
        assert events==['verify','closed','bind']
        assert response.json().get('invite_reward_granted',False)==(outcome=='verified')


@pytest.mark.parametrize('tier,status',[('lite',403),('pro',403),('max',200),('',403)])
def test_local_materials_upload_requires_current_max_tier(tmp_path,tier,status):
    from test_panel_recordings import lecture_material_fixture
    client,folder,jobs=lecture_material_fixture(tmp_path)
    client.app.state.platform_service=SimpleNamespace(quota=lambda:{'tier':tier})
    key=recordings_api.recording_id(jobs[0])
    response=client.post(f'/api/campus/courses/course-1/recordings/{key}/local-materials',
        files={'files':('notes.md',b'# Local lecture courseware','text/markdown')})
    assert response.status_code==status,response.text
    if status==403:
        assert 'Max' in response.json()['detail']
        assert not (folder/'materials/.lecture-uploads').exists()


@pytest.mark.parametrize('direct',[False,True])
@pytest.mark.parametrize('strategy,mode',[('transcript','basic'),('topics','advanced')])
def test_cloud_transcription_sends_source_part_and_mode_for_both_transports(tmp_path,monkeypatch,direct,strategy,mode):
    requests=[]
    part=tmp_path/'part-0002.m4a';part.write_bytes(b'audio')
    monkeypatch.setattr(transcription,'_audio_duration',lambda _:1200)
    monkeypatch.setattr(transcription,'_extract_audio_parts',lambda *a,**k:[(part,0.0)])
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,url,**kwargs):
            requests.append((url,kwargs))
            if url.endswith('/direct/init'):
                return httpx.Response(200,json={'upload_id':'a'*32,'upload_url':'https://test.oss-cn-beijing.aliyuncs.com/audio'})
            return httpx.Response(200,json={'duration_seconds':10,'segments':[{'start':0,'end':10,'text':'课堂'}]})
        def put(self,url,**kwargs):
            assert 'Authorization' not in kwargs['headers']
            assert kwargs['content'].read()==b'audio'
            return httpx.Response(200)
    monkeypatch.setattr(httpx,'Client',Client)
    s=settings(tmp_path,notes_strategy=strategy,_metered_operation_context={'source_id':'course:lecture'})
    result=transcription.transcribe_cloud(tmp_path/'video.mp4',tmp_path/'transcript.json',s,direct_oss=direct)
    assert result['segments'][0]['text']=='课堂'
    for url,kwargs in requests:
        assert kwargs['headers']=={'Authorization':'Bearer session','X-PKU-Source-Id':'course:lecture',
            'X-PKU-Part-Index':'2','X-PKU-Transcribe-Mode':mode}
    if direct:
        assert requests[0][1]['json']=={'size_bytes':5,'source_id':'course:lecture','part_index':2,'transcribe_mode':mode}


def test_panel_exchange_error_status_and_invite_forwarding(tmp_path):
    from pku_sync.panel.webapi import create_app
    from pku_sync.config import Settings
    fake=FakePlatformBridge(activated=True,email_verified=True,llm_points=10)
    client=TestClient(create_app(settings=Settings(_env_file=None,data_dir=tmp_path),platform_service=fake))
    assert client.post('/api/platform/exchange',json={'hours':1,'target':'basic'}).status_code==200
    assert client.post('/api/platform/exchange',json={'hours':1,'target':'basic'}).status_code==402
    assert client.post('/api/platform/exchange',json={'hours':True}).status_code==422
    response=client.post('/api/auth/register',json={'email':'new@example.com','password':'password1','invite_code':'https://host/invite/PKU-ABCD'})
    assert response.status_code==200,response.text
    assert fake.auth_calls[-1]['invite_code']=='PKU-ABCD'


@pytest.mark.parametrize('tier,expected',[('lite','transcript'),('pro','topics'),('max','topics')])
def test_recording_workflow_enforces_lite_strategy_before_processing(tmp_path,monkeypatch,tier,expected):
    from pku_sync import media
    manager=recordings_api.RecordingWorkManager(settings(tmp_path))
    manager.platform_service=SimpleNamespace(quota=lambda:{'tier':tier})
    class ReachedModelSelection(Exception):pass
    def resolve(config,strategy):
        assert strategy==expected
        raise ReachedModelSelection
    monkeypatch.setattr(media,'resolve_notes_model',resolve)
    with pytest.raises(ReachedModelSelection):
        manager._process(None,'lecture',notes_strategy='topics')
    assert manager.settings.notes_strategy=='transcript'  # only this task is changed


def test_invite_link_uses_cloud_origin_not_local_panel(tmp_path,monkeypatch):
    monkeypatch.setattr(platform,'_get',lambda *a,**k:httpx.Response(200,json={
        'transcribe_seconds_remaining':100,'llm_points_remaining':0,'tier':'lite','invite_code':'PKU-ABCD'}))
    result=platform.quota(settings(tmp_path))
    assert result['invite_url']=='https://panel.test/invite/PKU-ABCD'


@pytest.mark.parametrize('tier,token,activated,blocked', [
    ('lite','session',False,True), ('lite','',True,True),
    ('lite','',False,False), ('pro','session',True,False), ('max','session',True,False),
])
@pytest.mark.parametrize('path', ['/api/campus/courses/course-1/summary', '/api/campus/notion/summaries'])
def test_summary_tier_guard_and_offline_compatibility(tmp_path, tier, token, activated, blocked, path):
    manager = recordings_api.RecordingWorkManager(settings(tmp_path, platform_token=token))
    calls = []
    manager.start_course_summaries = lambda *args: calls.append(args) or {'state':'running'}
    app = FastAPI()
    app.state.platform_service = SimpleNamespace(_activated=activated, quota=lambda:{'tier':tier})
    recordings_api.add_recording_routes(app, manager)
    response = TestClient(app).post(path)
    assert response.status_code == (403 if blocked else 200), response.text
    assert bool(calls) is not blocked
    if blocked:
        assert '课程总结为 Pro / Max 会员功能' in response.json()['detail']


def test_quota_copies_pending_campus_reward(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, '_get', lambda *a, **k: httpx.Response(200, json={
        'transcribe_seconds_remaining':0, 'llm_points_remaining':0,
        'invite_pending_campus_reward':True, 'campus_bound':False}))
    result = platform.quota(settings(tmp_path))
    assert result['invite_pending_campus_reward'] is True
    assert result['campus_bound'] is False


def test_tier_onboarding_and_course_gates_in_browser():
    from playwright.sync_api import sync_playwright, expect
    script = (ROOT/'pku_sync/panel/static/app.js').read_text('utf-8')
    script = script[:script.index('  window.setInterval(refreshAccount, 8000);')] + '''window.__ux={state:state,render:render,
        credentials:recordingCredentialsForm,setup:campusSetupGuide,noteStyle:recordingNoteStyle,
        summary:startCourseSummary,centers:campusOfficialSections,prepare:prepareCampusAssignment,
        analyze:analyzeCampusAssignmentRequirements,submit:submitCampusAssignment};})();'''
    # Use the real renderer with deterministic course data; network writes are observed.
    posted = []
    def serve(route):
        path = urlsplit(route.request.url).path
        if route.request.method != 'GET': posted.append(path)
        if path == '/app': route.fulfill(body=student_page(),content_type='text/html')
        elif path == '/app/app.js': route.fulfill(body=script,content_type='application/javascript')
        elif path == '/app/app.css': route.fulfill(body=(ROOT/'pku_sync/panel/static/app.css').read_bytes(),content_type='text/css')
        else: route.fulfill(json={'active':False})
    with sync_playwright() as driver:
        from browser_support import browser_launch_options
        options = {'channel':'chrome'} if Path('/Applications/Google Chrome.app').exists() else browser_launch_options()
        browser = driver.chromium.launch(headless=True, **options)
        try:
            page = browser.new_page()
            page.add_init_script("localStorage.setItem('pku-panel-welcome-v1','seen')")
            page.route('http://panel.test/**',serve)
            page.goto('http://panel.test/app')
            page.wait_for_function('window.__ux !== undefined')
            page.evaluate('''() => {
                let s=window.__ux.state;s.activated=true;
                s.quota={active:true,email_verified:true,tier:'lite',invite_pending_campus_reward:true};
                s.campus.configured=false;s.view='campus-account';window.__ux.render();
            }''')
            expect(page.locator('#recording-credentials-form .invite-reward-hint')).to_contain_text('你与邀请人将立即各得 1 个月 Lite')
            page.evaluate('''() => {
                let s=window.__ux.state;s.campus.configured=true;s.view='campus-course';
                s.campus.courseId='course';s.campus.section='home';
                s.campus.course={id:'course',title:'课程',lessons:[],unassigned_materials:[],assignments:[
                    {title:'作业',content_id:'a',instructions:'完成作业'}],announcements:[]};window.__ux.render();
            }''')
            expect(page.locator('[data-action="campus-course-summary"]')).to_have_text('Pro · 生成课程总结')
            assert page.locator('.center-title .tier-pro').count() == 2
            page.locator('[data-action="campus-course-summary"]').click()
            expect(page.locator('#toast')).to_contain_text('Pro / Max 会员')
            page.locator('[data-center="assignments"]').click()
            expect(page.locator('#code')).to_be_focused()
            expect(page.locator('#toast')).to_contain_text('作业中心')
            page.evaluate("window.__ux.state.campus.section='assignments';window.__ux.render()")
            expect(page.locator('#course-assignments .tier-feature-notice')).to_contain_text('当前为 Lite 等级')
            expect(page.locator('[data-action="campus-prepare-assignment"]')).to_be_disabled()
            page.evaluate('''() => {window.__ux.prepare();window.__ux.analyze();window.__ux.submit();}''')
            assert posted == []
            page.evaluate("window.__ux.state.campus.section='notices';window.__ux.render()")
            expect(page.locator('#course-notices .tier-feature-notice')).to_contain_text('当前为 Lite 等级')
            markup = page.evaluate("window.__ux.noteStyle('lecture',false)")
            page.locator('#app').evaluate('(el,html)=>el.innerHTML=html',markup)
            expect(page.locator('.recording-note-tier-hint')).to_contain_text('升级 Pro / Max')
            assert page.locator('option[value="topics"]').evaluate('el=>el.disabled')
            assert page.locator('select').input_value() == 'transcript'
            for tier in ('pro','max'):
                page.evaluate("tier=>{window.__ux.state.quota.tier=tier;window.__ux.state.campus.section='home';window.__ux.render()}",tier)
                expect(page.locator('[data-action="campus-course-summary"]')).to_have_text('生成课程总结')
                assert page.locator('.center-title .tier-pro').count() == 0
                assert 'recording-note-tier-hint' not in page.evaluate("window.__ux.noteStyle('lecture',false)")
        finally:
            browser.close()
