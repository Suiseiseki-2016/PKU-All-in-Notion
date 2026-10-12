"""Developer guards for the native-proven moved-home bug, not E2E evidence."""
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

import pytest

from pku_sync import catalog_snapshots
from pku_sync.panel import notion_workspace

HOME, PARENT = '1' * 32, 'a' * 32


@pytest.mark.parametrize('entry', ['read', 'sync'])
@pytest.mark.parametrize('fault', ['moved', 'trash', 'archived', 'wrong-id',
                                  'missing-id', 'missing-parent', 'database-parent'])
def test_invalid_home_stops_before_traversal_or_writer_and_preserves_cache(
    tmp_path, monkeypatch, entry, fault
):
    config = SimpleNamespace(data_dir=tmp_path, notion_token='developer-only')
    workspace = {'schema': 2, 'id': HOME, 'parent_id': PARENT, 'active': True,
                 'courses': {'student-saved-course': {'student-text': '保留原记录'}}}
    notion_workspace._save(notion_workspace.manifest_path(tmp_path), workspace)
    before = notion_workspace.manifest_path(tmp_path).read_bytes()
    remote = {'object': 'page', 'id': str(UUID(HOME)), 'archived': False,
              'in_trash': False, 'parent': {'type': 'page_id', 'page_id': str(UUID(PARENT))}}
    if fault == 'moved':
        remote['parent']['page_id'] = 'f' * 32
    elif fault == 'trash':
        remote['in_trash'] = True
    elif fault == 'archived':
        remote['archived'] = True
    elif fault == 'wrong-id':
        remote['id'] = '2' * 32
    elif fault == 'missing-id':
        remote.pop('id')
    elif fault == 'missing-parent':
        remote.pop('parent')
    else:
        remote['parent']['type'] = 'database_id'
    reads = []

    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get_page(self, identifier):
            reads.append(identifier)
            return deepcopy(remote)
        def list_child_pages(self, *args):
            pytest.fail('Invalid home must stop before traversing any descendant')

    monkeypatch.setattr(catalog_snapshots, 'activate_account', lambda _: None)
    monkeypatch.setattr(notion_workspace, 'NotionClient', lambda _: Client())
    monkeypatch.setattr(notion_workspace, 'campus_courses', lambda _: {'courses': []})
    monkeypatch.setattr(notion_workspace.legacy, '_catalog_authority',
                        lambda *a, **k: pytest.fail('Invalid home must not acquire a writer'))
    with pytest.raises(ValueError, match='主页.*请'):
        if entry == 'read':
            notion_workspace.read_projection(config)
        else:
            notion_workspace.sync_workspace(config)
    assert reads == [HOME]
    assert notion_workspace.manifest_path(tmp_path).read_bytes() == before


def test_active_home_accepts_compact_and_dashed_ids_without_remote_writes(tmp_path, monkeypatch):
    config = SimpleNamespace(data_dir=tmp_path, notion_token='developer-only')
    workspace = {'schema': 2, 'id': HOME, 'parent_id': PARENT, 'active': True, 'courses': {}}
    notion_workspace._save(notion_workspace.manifest_path(tmp_path), workspace)
    reads = []
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get_page(self, identifier):
            reads.append(identifier)
            return {'id': str(UUID(HOME)), 'parent': {'type': 'page_id', 'page_id': str(UUID(PARENT))}}
        def list_child_pages(self, identifier): return []
    monkeypatch.setattr(catalog_snapshots, 'activate_account', lambda _: None)
    monkeypatch.setattr(notion_workspace, 'NotionClient', lambda _: Client())
    monkeypatch.setattr(notion_workspace, 'campus_courses', lambda _: {'courses': []})
    assert notion_workspace.read_projection(config) == workspace
    assert reads == [HOME]
