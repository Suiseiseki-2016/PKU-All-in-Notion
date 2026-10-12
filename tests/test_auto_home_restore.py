"""Existing account homes recover automatically, without creating Notion pages."""
from copy import deepcopy
from types import SimpleNamespace
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from pku_sync import platform, catalog_snapshots
from pku_sync.notion import NotionError
from pku_sync.panel import notion_home, notion_workspace, recordings_api, workspace_profile
from test_catalog_authority_e2e import account_service

HOME = 'a' * 32
PARENT = 'b' * 32
TODO = 'c' * 32

@pytest.fixture
def setup(tmp_path, monkeypatch):
    settings = SimpleNamespace(data_dir=tmp_path, platform_token='account', notion_token='notion')
    identity = {'id': HOME, 'parent_id': PARENT, 'url': 'https://www.notion.so/' + HOME}
    state = {'profile': {'status':'ready', 'revision':7, 'profile':{'notion_workspace_v2': identity}},
             'fault':'', 'reads':[], 'writes':[]}
    monkeypatch.setattr(platform, '_fetch_account_profile', lambda _: deepcopy(state['profile']))
    class Notion:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get_page(self, identifier):
            state['reads'].append(identifier)
            if state['fault'] == '403' or state['fault'] == '404':
                raise NotionError('fixture inaccessible', status=int(state['fault']))
            if state['fault'] == 'offline': raise httpx.ConnectError('fixture network')
            if state['fault'] == '5xx': raise NotionError('fixture unavailable', status=503)
            if state['fault'] == 'changed-account': settings.platform_token = 'different-account'
            if state['fault'] == 'changed-profile': state['profile']['revision'] = 8
            return {'id': identifier, 'parent':{'type':'page_id', 'page_id':TODO if state['fault'] == 'moved' else PARENT},
                    'archived':state['fault']=='archived', 'in_trash':state['fault']=='trash',
                    'properties':{'title':{'type':'title','title':[{'plain_text':'Original space'}]}}}
    monkeypatch.setattr(notion_workspace, 'NotionClient', lambda _: Notion())
    from pku_sync import notion
    monkeypatch.setattr(notion, 'NotionClient', lambda _: Notion())
    monkeypatch.setattr(notion_home, 'available_parents', lambda _: [{'id':TODO,'title':'TODO LIST'}])
    monkeypatch.setattr(notion_workspace, 'read_projection', lambda *a: pytest.fail('Startup must not scan all courses'))
    app = FastAPI()
    recordings_api.add_recording_routes(app, recordings_api.RecordingWorkManager(settings, None))
    return settings, state, TestClient(app)

def test_startup_restores_existing_home_and_selects_its_actual_parent(setup):
    settings, state, client = setup
    response = client.get('/api/campus/notion/parents?selected_only=true')
    assert response.status_code == 200
    body = response.json()
    assert body['home_sync']['status'] == 'pulled'
    assert body['home']['active'] and body['home']['id'] == HOME
    assert body['pages'][0]['id'] == PARENT
    assert notion_home.saved_home(settings.data_dir)['parent_id'] == PARENT
    assert set(state['reads']) == {HOME, PARENT}
    assert state['writes'] == []

@pytest.mark.parametrize('fault,status', [('403','notion_reconnect_required'),('404','notion_reconnect_required'),
    ('offline','notion_unavailable'),('5xx','notion_unavailable'),('archived','notion_home_archived'),
    ('trash','notion_home_archived'),('moved','notion_home_changed'),
    ('changed-account','account_unavailable'),('changed-profile','account_unavailable')])
def test_failed_verification_preserves_old_manifest_and_explains_recovery(setup, fault, status):
    settings, state, client = setup
    catalog_snapshots.activate_account(settings)
    path = notion_workspace.manifest_path(settings.data_dir)
    path.write_text(json.dumps({'schema':2,'id':'d'*32,'active':False,'student':'retain'}),encoding='utf-8')
    before = path.read_bytes()
    state['fault'] = fault
    body = client.get('/api/campus/notion/parents?selected_only=true').json()
    assert body['home_sync']['status'] == status
    assert body['home'] is None and body['workspace'] is None
    assert path.read_bytes() == before and state['writes'] == []

def test_reconnect_retries_restore_in_same_login_session(setup):
    settings, state, client = setup
    state['fault'] = '404'
    assert client.get('/api/campus/notion/parents?selected_only=true').json()['home_sync']['status'] == 'notion_reconnect_required'
    state['fault'] = ''
    settings.notion_token = 'reconnected-notion'
    body = client.get('/api/campus/notion/parents?selected_only=true').json()
    assert body['home']['id'] == HOME and body['home_sync']['status'] == 'pulled'

def test_inaccessible_legacy_home_does_not_block_verified_modern_home(setup, monkeypatch):
    settings, state, _ = setup
    state['profile']['profile']['notion_home'] = {'id':'d'*32,'parent_id':'e'*32,'url':'https://www.notion.so/legacy'}
    monkeypatch.setattr(platform, '_notion_home_accessible', lambda *a: pytest.fail('Modern home must not depend on legacy grant'))
    assert platform.sync_account_profile(settings)['status'] == 'pulled'
    assert notion_home.saved_home(settings.data_dir)['id'] == HOME

def test_explicit_replacement_keeps_other_profile_fields_and_revision_guard(setup, monkeypatch):
    settings, state, _ = setup
    previous = deepcopy(state['profile'])
    previous['profile']['notion_home'] = {'id':'d'*32,'parent_id':'e'*32,'url':'https://www.notion.so/legacy'}
    new = {'id':'f'*32,'parent_id':TODO,'url':'https://www.notion.so/new'}
    with pytest.raises(catalog_snapshots.CatalogUnavailable): workspace_profile.publish(settings,new,previous)
    def put(config, revision, profile, **options):
        assert revision == 7
        assert options == {'replace_workspace_id': previous['profile']['notion_workspace_v2']['id']}
        state['writes'].append(deepcopy(profile))
        state['profile'] = {'status':'ready','revision':8,'profile':deepcopy(profile)}
        return {'status':'pushed','revision':8}
    monkeypatch.setattr(platform, '_put_account_profile', put)
    assert workspace_profile.publish(settings,new,previous,replace_existing=True) == 8
    assert state['writes'][0]['notion_home'] == previous['profile']['notion_home']
    assert state['writes'][0]['notion_workspace_v2'] == new

def test_replace_requires_explicit_request_flag(setup, monkeypatch):
    _, _, client = setup
    calls=[]
    monkeypatch.setattr(notion_workspace,'sync_workspace',lambda config,parent,**kw: calls.append((parent,kw)) or {'id':HOME})
    assert client.post('/api/campus/notion/workspace',json={'parent_page_id':TODO}).status_code == 200
    assert client.post('/api/campus/notion/workspace',json={'parent_page_id':TODO,'replace_existing':True}).status_code == 200
    assert calls == [(TODO,{}),(TODO,{'replace_existing':True})]

def test_new_workspace_replaces_cloud_identity_after_verification_and_preserves_old_tree(tmp_path, monkeypatch, account_service):
    from test_catalog_authority_e2e import Notion, fake_portal, settings as config
    from server import account_profiles
    from backend_changes import account_profiles as candidate
    monkeypatch.setattr(account_profiles, 'put', candidate.put)
    fake_portal(monkeypatch)
    settings = config(tmp_path)
    settings.notion_token = 'fixture-notion'
    recordings_api.sync_recording_index(settings, lambda _: None)
    store = Notion()
    store.pages[TODO] = {'id':TODO,'title':'TODO LIST','parent':None}
    store.blocks[TODO] = []
    old = {'id':HOME,'parent_id':PARENT,'url':'https://www.notion.so/' + HOME}
    store.pages[HOME] = {'id':HOME,'title':'Old learning home','parent':PARENT}
    store.blocks[HOME] = [{'type':'paragraph','paragraph':{'rich_text':[{'text':{'content':'Student content'}}]}}]
    before = deepcopy((store.pages[HOME],store.blocks[HOME]))
    result = platform._put_account_profile(settings,0,{'notion_workspace_v2':old})
    assert result['status'] == 'pushed'
    monkeypatch.setattr(notion_workspace,'NotionClient',lambda _:store)
    monkeypatch.setattr(notion_home,'available_parents',lambda _: [{'id':TODO,'title':'TODO LIST'}])
    new = notion_workspace.sync_workspace(settings,TODO,replace_existing=True)
    assert new['active'] and new['id'] != HOME and new['parent_id'] == TODO
    assert (store.pages[HOME],store.blocks[HOME]) == before
    assert platform._fetch_account_profile(settings)['profile']['notion_workspace_v2']['id'] == new['id']
    assert notion_home.saved_home(tmp_path)['id'] == new['id']
