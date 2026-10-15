"""Real local endpoint must explain missing courses before any Notion write."""
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from pku_sync import catalog_snapshots as catalog
from pku_sync.config import Settings
from pku_sync.panel import notion_workspace, recordings_api


@pytest.mark.parametrize('unreadable_course', [False, True])
def test_new_workspace_requires_readable_course_without_changing_connections(tmp_path, monkeypatch, unreadable_course):
    settings = Settings(_env_file=None, data_dir=tmp_path, platform_token='fixture-account',
                        notion_token='fixture-notion', pku_username='fixture-student', pku_password='fixture-password')
    monkeypatch.setattr(catalog, 'activate_account', lambda _: None)
    monkeypatch.setattr(notion_workspace, 'campus_courses', lambda _: {'courses':
        [{'id': '_fixture_1', 'synced_at': '2026-10-13'}] if unreadable_course else []})
    if unreadable_course:
        (tmp_path / 'portal-read-status.json').write_text(json.dumps({'courses': {'_fixture_1': {}}}))
        def unreadable(*args):
            raise catalog.CatalogUnavailable('fixture course read failed')
        monkeypatch.setattr(catalog, 'assert_portal_readable', unreadable)
    monkeypatch.setattr(notion_workspace, 'NotionClient', lambda *args: pytest.fail('No Notion writes before courses'))
    monkeypatch.setattr(notion_workspace.legacy, '_catalog_authority',
                        lambda *args, **kwargs: pytest.fail('No course write permit before courses'))
    app = FastAPI()
    recordings_api.add_recording_routes(app, recordings_api.RecordingWorkManager(settings))
    with TestClient(app) as client:
        response = client.post('/api/campus/notion/workspace', json={'parent_page_id': 'a' * 32})
    assert response.status_code == 409
    assert '请先更新课程目录' in response.json()['detail']
    assert '连接已保留' in response.json()['detail']
    assert settings.platform_token == 'fixture-account' and settings.notion_token == 'fixture-notion'
    assert settings.pku_username == 'fixture-student' and settings.pku_password == 'fixture-password'
    assert not notion_workspace.manifest_path(tmp_path).exists()
