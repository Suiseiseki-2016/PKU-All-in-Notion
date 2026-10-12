"""Non-expiring account/course permits around native learning-record writes."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path

from .. import catalog_snapshots as catalog
from .exercise_organizer import OrganizeBlocked
from .notion_home import _document_signature
from .notion_workspace import _save

_active = ContextVar('native_course_writer', default=None)


def _signatures(rows):
    return [_document_signature(row) for row in rows]


class _Writer:
    def __init__(self, path, state, factory):
        self.path, self.state, self.factory = path, state, factory
        self.parents = {}

    def save(self):
        _save(self.path, self.state)

    def reconcile(self):
        request = self.state.get('pending')
        if not request:
            return True
        with self.factory() as client:
            if request['method'] == 'create_page':
                matches = []
                for page in client.list_child_pages(request['target']):
                    if page['id'] in request['before_ids'] or page.get('title') != request['title']:
                        continue
                    remote = client.get_page(page['id'])
                    rows = client.list_children(page['id'])
                    if (not remote.get('archived') and not remote.get('in_trash')
                            and remote.get('parent', {}).get('page_id') == request['target']
                            and _signatures(rows[:len(request['signatures'])]) == request['signatures']):
                        matches.append(page)
                confirmed = len(matches) == 1
            elif request['method'] == 'append_blocks':
                rows = _signatures(client.list_children(request['target']))
                expected = request['signatures']
                confirmed = bool(expected) and any(rows[i:i + len(expected)] == expected
                    for i in range(request['before_count'], len(rows)))
            elif request['method'] == 'archive_block':
                if hasattr(client, 'get_block'):
                    block = client.get_block(request['target'])
                    confirmed = block.get('archived') is True or block.get('in_trash') is True
                else:
                    confirmed = not any(row.get('id') == request['target'] for row in
                                        client.list_children(request['parent']))
            else:
                confirmed = False
        if confirmed:
            self.state.pop('pending', None)
            self.save()
        return confirmed

    def mutate(self, client, method, target, *args, **kwargs):
        if not self.reconcile():
            raise OrganizeBlocked(409, '上次 Notion 写入尚未确认，禁止重复写入；请稍后只读核对。')
        request = {'method': method, 'target': target}
        if method == 'create_page':
            request.update(title=args[0], signatures=_signatures(kwargs.get('children') or []))
            request['before_ids'] = [page['id'] for page in client.list_child_pages(target)]
            if not request['signatures']:
                raise OrganizeBlocked(409, '共享页面必须包含可验证的身份与正文。')
        elif method == 'append_blocks':
            request['signatures'] = _signatures(args[0])
            request['before_count'] = len(client.list_children(target))
        elif method == 'archive_block':
            request['parent'] = self.parents.get(target)
            if not request['parent']:
                raise OrganizeBlocked(409, '尚未读取待归档块的父页面，禁止写入。')
        else:
            raise OrganizeBlocked(409, '未授权的共享 Notion 变更。')
        self.state['pending'] = request
        self.save()  # Intent is durable before the first potentially committing request.
        kwargs['retry'] = False
        result = getattr(client, method)(target, *args, **kwargs)
        if not self.reconcile():
            raise OrganizeBlocked(502, 'Notion 返回后仍未读取到完整写入结果，共享锁已保留。')
        return result


class _Client:
    def __init__(self, client, writer):
        self.client, self.writer = client, writer

    def list_children(self, parent):
        rows = self.client.list_children(parent)
        self.writer.parents.update({row['id']: parent for row in rows if row.get('id')})
        return rows

    def __getattr__(self, name):
        if name in {'create_page', 'append_blocks', 'archive_block'}:
            return lambda target, *args, **kwargs: self.writer.mutate(self.client, name, target, *args, **kwargs)
        if name in {'get_page', 'get_block', 'list_child_pages', 'get_database', 'query_database', 'search', 'whoami'}:
            return getattr(self.client, name)
        raise OrganizeBlocked(409, '该 Notion 操作不在课程共享写入范围内。')


@contextmanager
def guarded_client(factory):
    with factory() as client:
        writer = _active.get()
        yield _Client(client, writer) if writer else client


@contextmanager
def course_writer(settings, course_id, factory):
    from .campus_quiz import _journal_lock
    catalog.activate_account(settings)
    path = catalog.cache_path(Path(settings.data_dir), course_id).with_suffix('.writer.json')
    with _journal_lock(path):
        try:
            try:
                state = json.loads(path.read_text('utf-8'))
            except FileNotFoundError:
                state = {}
            if not isinstance(state, dict):
                raise ValueError('invalid writer journal')
            expected = catalog.current(settings, course_id)
            if state:
                verified = catalog._call(settings, 'POST', course_id, '/projection/verify', {
                    'revision': state['revision'], 'permit_id': state['permit_id']})
                if verified != {'verified': True, 'revision': expected['revision']}:
                    raise catalog.CatalogUnavailable('共享写入许可未能验证。')
            else:
                permit = catalog._call(settings, 'POST', course_id, '/projection', {'revision': expected['revision']})
                if not isinstance(permit.get('permit_id'), str) or not permit['permit_id']:
                    raise catalog.CatalogUnavailable('服务端未授权共享写入。')
                state = {'revision': expected['revision'], 'permit_id': permit['permit_id']}
                _save(path, state)
            writer = _Writer(path, state, factory)
            if not writer.reconcile():
                raise OrganizeBlocked(409, '上次 Notion 写入仍未确认，共享锁已保留，禁止重复写入。')
            token = _active.set(writer)
            try:
                yield
            finally:
                _active.reset(token)
                # A timeout never expires this permit. Only readback proof or
                # a successful transport response can clear pending mutation.
                if writer.reconcile():
                    released = catalog._call(settings, 'POST', course_id, '/projection/release',
                                             {'permit_id': state['permit_id']})
                    if released != {'released': True}:
                        raise catalog.CatalogUnavailable('共享许可释放未确认。')
                    path.unlink()
        except OrganizeBlocked:
            raise
        except TimeoutError:
            raise
        except (catalog.CatalogUnavailable, OSError, ValueError, KeyError) as exc:
            raise OrganizeBlocked(409, '共享课程写入权限未确认，已停止写入；请核对账号服务与恢复记录。') from exc
