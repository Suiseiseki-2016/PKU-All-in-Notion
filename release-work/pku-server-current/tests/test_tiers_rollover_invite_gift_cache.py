"""Account entitlements through real routes/SQLite, with isolated mail and vendors."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from types import SimpleNamespace
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from server import accounts, mail, product_auth, relay, transcript_store
from server.llm import Completion

NOW = 1_800_000_000.0
WEEK = accounts.WEEK_SECONDS
ADMIN = 'test-admin-' + 'a' * 32


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(accounts.time, 'time', lambda: clock[0])
    db_path = tmp_path / 'accounts.db'
    conn = accounts.connect(db_path)
    calls = []
    def asr(audio):
        calls.append(audio)
        return {'duration_seconds': 90, 'language': 'zh',
                'segments': [{'start': 0, 'end': 90, 'text': audio.decode()}]}
    config = mail.MailConfig(provider='log', api_key='', mail_from='test@example.com',
        app_public_url='https://panel.test', token_secret='test-secret', token_ttl_seconds=1800)
    sender = SimpleNamespace(send=lambda **kwargs: mail.SentMail(provider='test', to=kwargs['to'], subject=kwargs['subject'], id='test'))
    app = relay.create_relay_app(db_path, asr_fn=asr, admin_token=ADMIN,
        llm_fn=lambda *args: Completion('answer', 10, 20, 7, 'qwen3.5-flash'),
        mail_config=config, mail_sender=sender)
    with TestClient(app) as client:
        yield SimpleNamespace(db=conn, path=db_path, client=client, app=app, clock=clock, calls=calls)
    conn.close()


def user(e, name='student', tier='lite', basic=0, advanced=0, points=10000, expiry=0, weekly=0):
    uid = accounts.create_account(e.db, name)
    e.db.execute('UPDATE accounts SET tier=?, basic_seconds_remaining=?, advanced_seconds_remaining=?, '
                 'transcribe_seconds_remaining=?, llm_millipoints_remaining=?, tier_expires_at=?, '
                 'weekly_rollover_seconds=?, last_weekly_refill_at=? WHERE id=?',
                 (tier, basic, advanced, basic+advanced, points, expiry, weekly, e.clock[0], uid))
    e.db.commit()
    token = accounts.issue_session(e.db, uid, ttl=365*86400)
    return uid, {'Authorization': 'Bearer ' + token}


def quota(e, headers):
    response = e.client.get('/v1/quota', headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def upload(e, headers, data=b'lecture', source='course:lecture-1', part=0, mode='basic'):
    return e.client.post('/v1/transcribe', headers={**headers, 'X-PKU-Source-Id': source,
        'X-PKU-Part-Index': str(part), 'X-PKU-Transcribe-Mode': mode}, files={'audio': ('part.m4a', data, 'audio/mp4')})


@pytest.mark.parametrize('tier', accounts.VALID_TIERS)
def test_tier_api_and_advanced_access(env, tier):
    uid, headers = user(env, tier=tier, basic=300, advanced=300)
    assert quota(env, headers)['tier'] == tier
    response = upload(env, headers, mode='advanced')
    assert response.status_code == (403 if tier == 'lite' else 200)
    if tier != 'lite':
        q = quota(env, headers)
        assert (q['basic_seconds_remaining'], q['advanced_seconds_remaining']) == (300, 210)
    assert upload(env, headers, data=b'basic', source='course:basic').status_code == 200


@pytest.mark.parametrize('seconds,expected', [(20,(80,50)), (100,(0,50)), (130,(0,20)), (150,(0,0))])
def test_basic_spends_basic_then_advanced_one_to_one(env, seconds, expected):
    uid, headers = user(env, basic=100, advanced=50)
    accounts.record_transcription(env.db, uid, seconds, mode='basic')
    q = quota(env, headers)
    assert (q['basic_seconds_remaining'], q['advanced_seconds_remaining']) == expected
    assert q['transcribe_seconds_remaining'] == sum(expected)
    assert env.db.execute('SELECT seconds FROM usage').fetchone()[0] == seconds


def test_advanced_cannot_spend_basic_and_invalid_usage_is_rejected(env):
    uid, headers = user(env, tier='pro', basic=500)
    assert upload(env, headers, mode='advanced').status_code == 402
    for seconds, mode in [(-1,'basic'), (True,'basic'), (1,'bad')]:
        with pytest.raises(ValueError):
            accounts.record_transcription(env.db, uid, seconds, mode=mode)
    assert quota(env, headers)['transcribe_seconds_remaining'] == 500


@pytest.mark.parametrize('operation', ['notes', 'summary', 'qa'])
@pytest.mark.parametrize('entitlement', ['quota', 'membership', 'rollover'])
def test_bundled_operations_are_free_with_zero_points(env, operation, entitlement):
    uid, headers = user(env, points=0, basic=100 if entitlement=='quota' else 0,
        expiry=NOW+30*86400 if entitlement!='quota' else 0,
        weekly=90000 if entitlement=='rollover' else 0)
    response = env.client.post('/v1/llm', headers=headers, json={'operation':operation,'system':'system','user':'notes'})
    assert response.status_code == 200, response.text
    assert response.json()['points_charged'] == 0
    assert quota(env, headers)['llm_points_remaining'] == 0
    assert env.db.execute('SELECT millipoints FROM llm_usage').fetchone()[0] == 0


def test_expired_empty_plan_does_not_bundle_and_quiz_still_costs(env):
    uid, headers = user(env, points=0, expiry=NOW-1, weekly=90000)
    assert not accounts.is_bundled_llm_operation(env.db, uid, 'qa')
    assert env.client.post('/v1/llm', headers=headers, json={'operation':'qa','system':'s','user':'u'}).status_code == 402
    env.db.execute('UPDATE accounts SET tier_expires_at=?, llm_millipoints_remaining=10 WHERE id=?', (NOW+100,uid))
    env.db.commit()
    response = env.client.post('/v1/llm', headers=headers, json={'operation':'quiz','system':'s','user':'u'})
    assert response.json()['points_charged'] == .007


@pytest.mark.parametrize('target', ['basic', 'advanced'])
def test_exchange_ten_points_per_hour_atomic(env, target):
    uid, headers = user(env, points=20000)
    response = env.client.post('/v1/quota/exchange', headers=headers, json={'hours':2,'target':target})
    assert response.status_code == 200, response.text
    assert response.json()[target+'_seconds_remaining'] == 7200
    assert response.json()['llm_points_remaining'] == 0
    before = quota(env, headers)
    assert env.client.post('/v1/quota/exchange', headers=headers, json={'hours':1,'target':target}).status_code in (400,402)
    assert quota(env, headers) == before


@pytest.mark.parametrize('body', [{'hours':0},{'hours':-1},{'hours':True},{'hours':1.5},{'hours':101},{'hours':'1'},{'target':'wrong'}])
def test_invalid_exchange_does_not_change_balances(env, body):
    uid, headers = user(env)
    before = quota(env, headers)
    assert env.client.post('/v1/quota/exchange', headers=headers, json=body).status_code in (400,422)
    assert quota(env, headers) == before


@pytest.mark.parametrize('tier,bucket', [('lite','basic'),('pro','advanced'),('max','advanced')])
def test_weekly_rollover_accumulates_elapsed_weeks_until_expiry(env, tier, bucket):
    uid, headers = user(env, tier=tier, basic=123, weekly=90000, expiry=NOW+30*86400)
    env.clock[0] += WEEK-1
    assert quota(env, headers)['transcribe_seconds_remaining'] == 123
    env.clock[0] += 1
    assert quota(env, headers)[bucket+'_seconds_remaining'] == 90000+(123 if bucket=='basic' else 0)
    env.clock[0] = NOW+3*WEEK+100
    assert quota(env, headers)['transcribe_seconds_remaining'] == 270123
    assert quota(env, headers)['transcribe_seconds_remaining'] == 270123
    env.clock[0] = NOW+60*86400
    assert quota(env, headers)['transcribe_seconds_remaining'] == 360123
    assert quota(env, headers)['tier'] == 'lite'


def test_rollover_requires_expiry_and_renewal_does_not_fill_inactive_gap(env):
    uid, headers = user(env, weekly=90000, expiry=0)
    env.clock[0] += 10*WEEK
    assert quota(env, headers)['transcribe_seconds_remaining'] == 0
    gift = accounts.create_gift_link(env.db, tier='lite', weekly_rollover_seconds=90000, duration_days=30)
    assert accounts.claim_gift_link(env.db, gift, uid)
    assert quota(env, headers)['transcribe_seconds_remaining'] == 0
    env.clock[0] += WEEK
    assert quota(env, headers)['basic_seconds_remaining'] == 90000


@pytest.mark.parametrize('bind_first', [False,True])
def test_invite_url_binding_rewards_both_once_and_preserves_id_history(env, bind_first):
    inviter, _ = user(env, 'inviter')
    invitee, headers = user(env, 'invitee')
    code = accounts.ensure_invite_code(env.db, inviter)
    assert env.client.get('/invite/'+code).status_code == 200
    assert env.client.get('/invite/missing').status_code == 404
    if bind_first:
        assert env.client.post('/v1/auth/bind-campus', headers=headers, json={'student_id':'20260001'}).json()['reward_granted'] is False
    response = env.client.post('/v1/redeem', headers=headers, json={'code':'https://panel.test/invite/'+code+'?x=1'})
    assert response.status_code == 200, response.text
    assert response.json()['kind'] == 'invite'
    response = env.client.post('/v1/auth/bind-campus', headers=headers, json={'student_id':'20260001'})
    assert response.json()['reward_granted'] is (not bind_first)
    for uid in (inviter,invitee):
        q = accounts.quota_breakdown(env.db,uid)
        assert q['tier'] == 'lite'
        assert q['basic_seconds_remaining'] == q['weekly_rollover_seconds'] == 90000
        assert q['tier_expires_at'] == NOW+30*86400
    assert not env.client.post('/v1/auth/bind-campus', headers=headers, json={'student_id':'20260001'}).json()['reward_granted']
    # A conflicting identity cannot replace the bound student ID.
    conflict = env.client.post('/v1/auth/bind-campus', headers=headers, json={'student_id':'20260002'})
    assert conflict.status_code == 409
    assert '20****0001' in conflict.json()['detail']
    other, other_headers = user(env,'other')
    accounts.apply_invite_code(env.db,other,code)
    duplicate = env.client.post('/v1/auth/bind-campus', headers=other_headers, json={'student_id':'20260001'}).json()
    assert duplicate['already_used'] and not duplicate['campus_bound'] and not duplicate['reward_granted']
    assert accounts.quota_breakdown(env.db,inviter)['basic_seconds_remaining'] == 90000
    assert not accounts.apply_invite_code(env.db,inviter,code)
    assert not accounts.apply_invite_code(env.db,invitee,code)


def test_concurrent_student_binding_rewards_only_one_account(env):
    inviter,_ = user(env,'inviter')
    users=[user(env,'student'+str(i))[0] for i in range(2)]
    for uid in users:
        accounts.apply_invite_code(env.db,uid,accounts.ensure_invite_code(env.db,inviter))
    barrier=threading.Barrier(2)
    def bind(uid):
        db=accounts.connect(env.path)
        try:
            barrier.wait(5)
            return accounts.bind_campus_account(db,uid,'20268888')
        finally:
            db.close()
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(bind,users))
    assert sum(r['reward_granted'] for r in results)==1
    assert accounts.quota_breakdown(env.db,inviter)['basic_seconds_remaining']==90000


def test_registration_accepts_full_invite_url(env):
    inviter,_ = user(env,'inviter')
    code=accounts.ensure_invite_code(env.db,inviter)
    result=env.client.post('/v1/auth/register',json={'email':'new@example.com','password':'password1','invite_code':'https://panel.test/invite/'+code})
    assert result.status_code==200,result.text
    assert result.json()['tier']=='lite'
    row=env.db.execute('SELECT invited_by FROM accounts WHERE email=?',('new@example.com',)).fetchone()
    assert row[0]==inviter
    assert env.client.post('/v1/auth/register',json={'email':'short@example.com','password':'1234567'}).status_code==400


def web_account(env, name):
    response = env.client.post('/v1/auth/register', json={'email': name+'@example.com', 'password': 'password1'})
    assert response.status_code == 200, response.text
    uid = env.db.execute('SELECT id FROM accounts WHERE email=?', (name+'@example.com',)).fetchone()[0]
    return uid, {'Authorization': 'Bearer '+response.json()['platform_token']}


def test_invite_web_landing_masks_email_and_offers_both_modes(env):
    inviter, _ = web_account(env, 'inviter')
    code = accounts.ensure_invite_code(env.db, inviter)
    response = env.client.get('/invite/'+code)
    assert response.status_code == 200
    assert 'i***@example.com' in response.text and 'inviter@example.com' not in response.text
    for text in (code, '已安装客户端？邀请码', '1 个月 Lite 普通转写额度', '每周 25 小时自动滚存',
                 '已有账号直接绑定邀请', '新用户网页直接注册并绑定邀请',
                 '密码（至少 8 位）', 'minlength="8"', 'name="mode"', 'value="register"',
                 'action="/invite/'+code+'/claim"', 'method="post"'):
        assert text in response.text
    assert env.client.get('/invite/missing').status_code == 404
    assert env.client.post('/invite/missing/claim', data={'mode':'register','email':'not-created@example.com','password':'password1'}).status_code == 404
    assert product_auth.find_by_email(env.db, 'not-created@example.com') is None


@pytest.mark.parametrize('mode,bind_first', [('login', False), ('login', True), ('register', False)])
def test_invite_web_claim_immediate_or_deferred_rewards_once(env, mode, bind_first):
    inviter, _ = web_account(env, 'inviter')
    code = accounts.ensure_invite_code(env.db, inviter)
    if mode == 'login':
        invitee, headers = web_account(env, 'invitee')
        if bind_first:
            assert not env.client.post('/v1/auth/bind-campus', headers=headers, json={'student_id':'20261234'}).json()['reward_granted']
    sessions = env.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
    url = '/invite/'+code+'/claim'
    response = env.client.post(url, data={'mode':mode, 'email':'invitee@example.com', 'password':'password1'})
    assert response.status_code == 200, response.text
    assert 'role="status"' in response.text and '邀请已' in response.text
    for obsolete in ('id="invite-code"', 'id="copy-invite-code"', 'id="invite-mode"', '<form'):
        assert obsolete not in response.text
    assert '验证邮箱' in response.text and '下载桌面客户端' in response.text
    assert 'no-store' in response.headers['cache-control']
    assert 'value="password1"' not in response.text
    row = product_auth.find_by_email(env.db, 'invitee@example.com')
    invitee = row['id']
    assert row['invited_by'] == inviter
    assert (row['invite_rewarded_at'] is not None) is bind_first
    assert env.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == sessions
    if mode == 'register':
        assert row['email_verified_at'] is None
        assert '注册成功' in response.text and '邮箱验证邮件' in response.text
        login = env.client.post('/v1/auth/login', json={'email':'invitee@example.com','password':'password1'})
        headers = {'Authorization':'Bearer '+login.json()['platform_token']}
    if not bind_first:
        assert '在客户端连接新的北大教学网学号后' in response.text
        for uid in (inviter, invitee):
            assert accounts.quota_breakdown(env.db, uid)['basic_seconds_remaining'] == 0
        bound = env.client.post('/v1/auth/bind-campus', headers=headers, json={'student_id':'20261234'})
        assert bound.status_code == 200 and bound.json()['reward_granted']
    else:
        assert '奖励已到账' in response.text
    # Reloading or submitting again must not create another reward.
    repeated = env.client.post(url, data={'email':'invitee@example.com','password':'password1'})
    assert repeated.status_code == 200
    for uid in (inviter, invitee):
        q = accounts.quota_breakdown(env.db, uid)
        assert q['tier'] == 'lite'
        assert q['basic_seconds_remaining'] == q['weekly_rollover_seconds'] == 90000
        assert q['tier_expires_at'] == NOW+30*86400


@pytest.mark.parametrize('mode,email,password,status', [
    ('login','invitee@example.com','wrong',401),
    ('register','new@example.com','1234567',400),
    ('register','invitee@example.com','password1',409),
    ('invalid','new@example.com','password1',400),
    ('login','','',400),
])
def test_invite_web_errors_render_form_without_binding(env, mode, email, password, status):
    inviter, _ = web_account(env, 'inviter')
    invitee, _ = web_account(env, 'invitee')
    code = accounts.ensure_invite_code(env.db, inviter)
    response = env.client.post('/invite/'+code+'/claim', data={'mode':mode,'email':email,'password':password})
    assert response.status_code == status, response.text
    assert 'role="alert"' in response.text and '<form method="post"' in response.text
    assert product_auth.find_by_email(env.db, 'invitee@example.com')['invited_by'] is None
    assert product_auth.find_by_email(env.db, 'new@example.com') is None
    assert accounts.quota_breakdown(env.db, inviter)['basic_seconds_remaining'] == 0
    if mode == 'register':
        assert '<option value="register" selected>' in response.text


def test_invite_web_rejects_self_and_other_inviter_and_renders_rate_limit(env):
    inviter, _ = web_account(env, 'inviter')
    other, _ = web_account(env, 'other')
    invitee, _ = web_account(env, 'invitee')
    code = accounts.ensure_invite_code(env.db, inviter)
    accounts.apply_invite_code(env.db, invitee, accounts.ensure_invite_code(env.db, other))
    for email in ('inviter@example.com', 'invitee@example.com'):
        response = env.client.post('/invite/'+code+'/claim', data={'email':email,'password':'password1'})
        assert response.status_code == 400 and 'role="alert"' in response.text
    assert product_auth.find_by_email(env.db, 'invitee@example.com')['invited_by'] == other
    from server.rate_limit import SlidingWindowRateLimiter
    env.app.state.limiter_auth = SlidingWindowRateLimiter(0, 60)
    response = env.client.post('/invite/'+code+'/claim', data={'email':'invitee@example.com','password':'password1'})
    assert response.status_code == 429 and '操作过于频繁' in response.text


@pytest.mark.parametrize('tier',accounts.VALID_TIERS)
def test_admin_gift_issue_and_redeem_full_url(env,tier):
    uid,headers=user(env)
    payload={'tier':tier,'basic_minutes':60,'advanced_minutes':120,'llm_points':12.5,'max_claims':2}
    assert env.client.post('/admin/api/gifts',json=payload).status_code==401
    response=env.client.post('/admin/api/gifts',headers={'Authorization':'Bearer '+ADMIN},json=payload)
    assert response.status_code==200,response.text
    token=response.json()['tokens'][0]
    landing=env.client.get('/gift/'+token)
    assert landing.status_code==200 and 'password' in landing.text and token in landing.text
    response=env.client.post('/v1/redeem',headers=headers,json={'code':'https://panel.test/gift/'+token})
    assert response.status_code==200,response.text
    q=response.json()
    assert q['tier']==tier and q['basic_seconds_remaining']==3600 and q['advanced_seconds_remaining']==7200
    assert q['weekly_rollover_seconds']==90000 and q['llm_points_remaining']==22.5
    assert env.client.post('/v1/redeem',headers=headers,json={'code':token}).status_code==400
    assert env.db.execute('SELECT claimed_count FROM gift_links WHERE token=?',(token,)).fetchone()[0]==1


@pytest.mark.parametrize('payload',[{'tier':'gold'},{'duration_days':-1},{'basic_minutes':-1},{'advanced_minutes':'bad'}, {'count':True},{'max_claims':0},{'llm_points':'NaN'},{'llm_points':.0001}])
def test_invalid_gift_issuance_is_422_without_rows(env,payload):
    response=env.client.post('/admin/api/gifts',headers={'Authorization':'Bearer '+ADMIN},json=payload)
    assert response.status_code==422,response.text
    assert env.db.execute('SELECT COUNT(*) FROM gift_links').fetchone()[0]==0


def test_gift_login_claim_wrong_password_unverified_and_exhausted(env):
    token=accounts.create_gift_link(env.db,tier='max',basic_seconds=90000)
    env.client.post('/v1/auth/register',json={'email':'gift@example.com','password':'password1'})
    url='/gift/'+token
    assert env.client.post(url,data={'email':'gift@example.com','password':'wrong'}).status_code==401
    assert env.client.post(url,data={'email':'gift@example.com','password':'password1'}).status_code==403
    env.db.execute("UPDATE accounts SET email_verified_at=? WHERE email='gift@example.com'",(NOW,));env.db.commit()
    result=env.client.post(url,data={'email':'gift@example.com','password':'password1'})
    assert result.status_code==200 and '已成功绑定' in result.text
    assert env.client.post(url,data={'email':'gift@example.com','password':'password1'}).status_code==400
    uid,_=user(env,'another')
    assert not accounts.claim_gift_link(env.db,token,uid)
    expired=accounts.create_gift_link(env.db,expires_at=NOW)
    assert not accounts.claim_gift_link(env.db,expired,uid)
    assert env.client.get('/gift/missing').status_code==404


def test_same_lecture_different_bytes_cross_account_cache_is_free_and_part_scoped(env):
    a,ha=user(env,'a',basic=500)
    b,hb=user(env,'b',basic=500)
    first=upload(env,ha,b'encoding-a',part=2)
    second=upload(env,hb,b'encoding-b',part=2)
    assert first.status_code==second.status_code==200
    assert first.json()['seconds_charged']==90
    assert second.json()['reused'] and second.json()['seconds_charged']==0
    assert second.json()['segments']==first.json()['segments']
    assert env.calls==[b'encoding-a'] and quota(env,hb)['basic_seconds_remaining']==500
    assert upload(env,hb,b'part-3',part=3).json()['reused'] is False
    assert upload(env,hb,b'other-lecture',source='other:lecture',part=2).json()['reused'] is False
    assert env.db.execute('SELECT COUNT(*) FROM lecture_transcript_reuse').fetchone()[0]==3


def test_lecture_cache_ttl_and_invalid_metadata(env):
    uid,headers=user(env,basic=1000)
    upload(env,headers)
    env.clock[0]+=transcript_store.DEFAULT_TTL_SECONDS
    assert upload(env,headers,b'new-encoding').json()['reused'] is False
    for part in (-1,'bad'):
        assert upload(env,headers,part=part).status_code==400
    assert upload(env,headers,source='bad/source').status_code==400
    assert upload(env,headers,mode='unknown').status_code==400


class Bucket:
    bucket_name='test-bucket'
    def __init__(self): self.objects={}
    def sign_url(self,method,key,*args,**kwargs): return 'https://test.oss-cn-beijing.aliyuncs.com/'+key
    def copy_object(self,bucket,src,dst):
        self.objects[dst]=self.objects[src]
        return SimpleNamespace(status=200)
    def head_object(self,key):
        if key not in self.objects:
            error=Exception('not found');error.status=404;raise error
        return SimpleNamespace(content_length=len(self.objects[key]))
    def get_object(self,key): return BytesIO(self.objects[key])


class DirectVendor:
    def __init__(self):
        bucket=Bucket()
        self.store=SimpleNamespace(bucket=bucket,delete=lambda key:bucket.objects.pop(key,None))
        self.calls=[]
    def transcribe_uploaded(self,key):
        self.calls.append(self.store.bucket.objects[key])
        return {'duration_seconds':90,'segments':[{'start':0,'end':90,'text':'lecture'}]}


def test_direct_upload_cache_reuse_and_completion_retry(env):
    vendor=DirectVendor();env.app.state.streaming_asr_fn=vendor
    a,ha=user(env,'a',tier='pro',basic=300,advanced=300)
    b,hb=user(env,'b',tier='pro',basic=300,advanced=300)
    def direct(headers,data,part):
        init=env.client.post('/v1/transcribe/direct/init',headers=headers,json={
            'size_bytes':len(data),'source_id':'course:direct','part_index':part,'transcribe_mode':'advanced'})
        assert init.status_code==200,init.text
        grant=init.json()
        key=env.db.execute('SELECT upload_key FROM direct_upload_grants WHERE id=?',(grant['upload_id'],)).fetchone()[0]
        vendor.store.bucket.objects[key]=data
        response=env.client.post('/v1/transcribe/direct/complete',headers=headers,json={'upload_id':grant['upload_id']})
        assert response.status_code==200,response.text
        return grant,response.json()
    _,first=direct(ha,b'encoding-a',2)
    grant,second=direct(hb,b'different-encoding-b',2)
    assert first['seconds_charged']==90 and second['seconds_charged']==0 and second['reused']
    retry=env.client.post('/v1/transcribe/direct/complete',headers=hb,json={'upload_id':grant['upload_id']})
    assert retry.status_code==200 and retry.json()==second
    assert len(vendor.calls)==1
    assert quota(env,hb)['advanced_seconds_remaining']==300
    _,third=direct(hb,b'part-three',3)
    assert not third['reused'] and len(vendor.calls)==2
    assert vendor.store.bucket.objects=={}


@pytest.mark.parametrize('operation',['notes','summary','qa'])
def test_durable_llm_jobs_are_bundled_and_idempotent(env,operation):
    uid,headers=user(env,points=0,basic=100)
    payload={'operation':operation,'system':'system','user':'lecture','model':'qwen3.5-flash',
             'idempotency_key':'a'*32}
    if operation!='notes':payload.pop('model')
    response=env.client.post('/v1/llm/jobs',headers=headers,json=payload)
    assert response.status_code==202,response.text
    job_id=response.json()['job_id']
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        result=env.client.get('/v1/llm/jobs/'+job_id,headers=headers).json()
        if result['status'] in ('succeeded','failed'):break
        time.sleep(.01)
    assert result['status']=='succeeded',result
    assert result['points_charged']==0
    assert env.client.post('/v1/llm/jobs',headers=headers,json=payload).json()['job_id']==job_id
    assert env.db.execute('SELECT COUNT(*), SUM(millipoints) FROM llm_usage').fetchone()[:]==(1,0)


def test_lecture_hits_do_not_extend_expiry(env):
    uid,headers=user(env,basic=500)
    upload(env,headers,b'initial')
    initial=env.db.execute('SELECT expires_at FROM lecture_transcript_reuse').fetchone()[0]
    env.clock[0]+=WEEK
    assert upload(env,headers,b'different').json()['reused']
    assert env.db.execute('SELECT expires_at FROM lecture_transcript_reuse').fetchone()[0]==initial
    env.clock[0]=initial
    assert not upload(env,headers,b'third').json()['reused']


def test_concurrent_weekly_refresh_does_not_double_grant(env):
    uid,headers=user(env,weekly=90000,expiry=NOW+30*86400)
    env.clock[0]+=2*WEEK
    barrier=threading.Barrier(2)
    def refresh(_):
        conn=accounts.connect(env.path)
        try:
            barrier.wait(5)
            accounts.refresh_monthly_quota(conn,uid)
        finally:conn.close()
    with ThreadPoolExecutor(2) as pool:list(pool.map(refresh,range(2)))
    assert quota(env,headers)['basic_seconds_remaining']==180000


def test_same_lecture_concurrent_different_encodings_call_vendor_once(env):
    _,ha=user(env,'a',basic=500)
    _,hb=user(env,'b',basic=500)
    entered,release=threading.Event(),threading.Event()
    original=env.app.state.asr_fn
    def asr(data):
        entered.set()
        assert release.wait(5)
        return original(data)
    env.app.state.asr_fn=asr
    with ThreadPoolExecutor(2) as pool:
        first=pool.submit(upload,env,ha,b'encoding-a')
        assert entered.wait(5)
        second=pool.submit(upload,env,hb,b'encoding-b')
        release.set()
        responses=[first.result(),second.result()]
    assert all(r.status_code==200 for r in responses)
    assert sorted(r.json()['seconds_charged'] for r in responses)==[0,90]
    assert len(env.calls)==1


def test_points_only_gift_does_not_extend_paid_membership(env):
    uid,headers=user(env,tier='max',expiry=NOW+1000)
    token=accounts.create_gift_link(env.db,llm_millipoints=10000)
    assert accounts.claim_gift_link(env.db,token,uid)
    assert quota(env,headers)['tier_expires_at']==NOW+1000
    assert quota(env,headers)['llm_points_remaining']==20


def test_expired_max_can_claim_lite_without_restoring_max(env):
    uid,headers=user(env,tier='max',expiry=NOW-1)
    token=accounts.create_gift_link(env.db,tier='lite',basic_seconds=90000,weekly_rollover_seconds=90000)
    assert accounts.claim_gift_link(env.db,token,uid)
    assert quota(env,headers)['tier']=='lite'
    assert quota(env,headers)['tier_expires_at']==NOW+30*86400


def test_new_product_account_has_no_unpurchased_monthly_refill(env):
    response=env.client.post('/v1/auth/register',json={'email':'empty@example.com','password':'password1'})
    assert response.status_code==200,response.text
    uid=env.db.execute("SELECT id FROM accounts WHERE email='empty@example.com'").fetchone()[0]
    env.clock[0]+=31*86400
    q=accounts.quota_breakdown(env.db,uid)
    assert q['transcribe_seconds_remaining']==q['llm_points_remaining']==0


def test_concurrent_gift_claims_respect_global_claim_limit(env):
    users=[user(env,'student'+str(i))[0] for i in range(2)]
    token=accounts.create_gift_link(env.db,basic_seconds=600,max_claims=1)
    barrier=threading.Barrier(2)
    def claim(uid):
        db=accounts.connect(env.path)
        try:
            barrier.wait(5)
            return accounts.claim_gift_link(db,token,uid)
        finally:db.close()
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(claim,users))
    assert sorted(results)==[False,True]
    assert env.db.execute('SELECT claimed_count FROM gift_links').fetchone()[0]==1
    assert env.db.execute('SELECT SUM(basic_seconds_remaining) FROM accounts').fetchone()[0]==600


def test_registration_confirmation_and_download_ctas(env):
    from server import auth_pages
    inviter, _ = web_account(env, 'inviter')
    code = accounts.ensure_invite_code(env.db, inviter)
    page = env.client.get('/invite/' + code).text
    assert '<option value="register" selected>' in page
    assert page.index('value="register"') < page.index('value="login"')
    assert 'name="password2"' in page
    data = {'mode': 'register', 'email': 'new@example.com', 'password': 'password1', 'password2': 'different'}
    response = env.client.post('/invite/' + code + '/claim', data=data)
    assert response.status_code == 400 and '两次输入的密码不一致' in response.text
    assert product_auth.find_by_email(env.db, 'new@example.com') is None
    response = env.client.post('/invite/' + code + '/claim', data={**data, 'password2': 'password1'})
    assert response.status_code == 200 and auth_pages.DOWNLOAD_CTA in response.text
    assert auth_pages.DOWNLOAD_CTA in auth_pages.verify_result_page(ok=True, message='Verified')
    assert auth_pages.DOWNLOAD_CTA not in auth_pages.verify_result_page(ok=False, message='Invalid')
    assert auth_pages.DOWNLOAD_CTA in auth_pages.gift_landing_page(token='gift', gift_summary='Lite', ok_message='Claimed')
    assert auth_pages.DOWNLOAD_CTA not in auth_pages.gift_landing_page(token='gift', gift_summary='Lite')


def test_pending_invite_quota_lifecycle(env):
    inviter, _ = web_account(env, 'inviter')
    invitee, headers = web_account(env, 'invitee')
    assert quota(env, headers)['invite_pending_campus_reward'] is False
    accounts.apply_invite_code(env.db, invitee, accounts.ensure_invite_code(env.db, inviter))
    pending = quota(env, headers)
    assert pending['invite_pending_campus_reward'] is True and pending['campus_bound'] is False
    response = env.client.post('/v1/auth/bind-campus', headers=headers, json={'student_id':'20261234'})
    assert response.status_code == 200
    bound = quota(env, headers)
    assert bound['campus_bound'] is True and bound['invite_pending_campus_reward'] is False


def test_gift_footer_and_membership_expiry_public_contract(env):
    from server import auth_pages
    # Account auth/me, not only the quota endpoint, exposes the 30-day grant.
    uid, headers = user(env, 'gift-expiry')
    response = env.client.post('/admin/api/gifts', headers={'Authorization':'Bearer '+ADMIN},
        json={'tier':'pro','advanced_minutes':1500,'duration_days':30})
    assert response.status_code == 200, response.text
    link = response.json()['links'][0]
    assert env.client.post('/v1/redeem', headers=headers, json={'code':link}).status_code == 200
    me = env.client.get('/v1/auth/me', headers=headers).json()
    assert me['tier_expires_at'] == NOW + 30*86400
    page = auth_pages.gift_landing_page(token='GIFT-TEST', gift_summary='Pro')
    assert page.index('<form ') < page.index('class="gift-desktop-hint"')
    assert '>登录并直接领取</button>' in page
    assert '<code id="gift-code">GIFT-TEST</code>' in page
    assert '<button type="button" id="copy-gift-code"' in page
    done = auth_pages.gift_landing_page(token='GIFT-TEST', gift_summary='Pro', ok_message='已成功绑定')
    assert '<form ' not in done and 'copy-gift-code' not in done and 'gift-desktop-hint' not in done


def test_campus_binding_conflict_is_atomic_and_legacy_mask_backfills(env):
    import hashlib
    uid, headers = user(env, 'bound-student')
    old = '2026991234'
    old_hash = hashlib.sha256(('pku-campus-v1:'+old).encode()).hexdigest()
    env.db.execute('UPDATE accounts SET campus_id_hash=? WHERE id=?', (old_hash,uid))
    env.db.commit()
    response = env.client.post('/v1/auth/bind-campus', headers=headers, json={'student_id':old})
    assert response.status_code == 200
    assert response.json()['campus_id_masked'] == '20****1234'
    response = env.client.post('/v1/auth/bind-campus', headers=headers, json={'student_id':'2026995678'})
    assert response.status_code == 409 and '20****1234' in response.json()['detail']
    assert env.db.execute('SELECT campus_id_hash FROM accounts WHERE id=?',(uid,)).fetchone()[0] == old_hash
    assert env.db.execute('SELECT COUNT(*) FROM campus_bindings WHERE account_id=?',(uid,)).fetchone()[0] == 1
