"""Minimal page identities from actual provider HTTP responses, never app stores."""
import re
from urllib.parse import parse_qs, urlsplit

UUID = re.compile(r'^(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$', re.I)


def matches_authorization_page(url, client_id):
    parts = urlsplit(url)
    return (parts.scheme == 'https' and parts.hostname in {
        'www.notion.so', 'notion.so', 'www.notion.com', 'app.notion.com'}
        and ('oauth' in parts.path or parts.path == '/install-integration')
        and parse_qs(parts.query).get('client_id') == [client_id])


def page_identities(payload):
    """Retain page titles/parents only; discard content, users and tokens."""
    identities = {}

    def walk(value):
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            page_id, parent_id = value.get('id'), value.get('parent_id')
            if (value.get('type') == 'page' and isinstance(page_id, str) and UUID.fullmatch(page_id)
                    and isinstance(parent_id, str) and UUID.fullmatch(parent_id)
                    and value.get('parent_table') in {'space', 'block'}):
                title = value.get('properties', {}).get('title', []) if isinstance(value.get('properties', {}), dict) else None
                if isinstance(title, list) and all(isinstance(row, list) and row and isinstance(row[0], str) for row in title):
                    identities[page_id.replace('-', '').lower()] = {
                        'id':page_id.replace('-', '').lower(), 'title': ''.join(row[0] for row in title),
                        'parent_id':parent_id.replace('-', '').lower(), 'parent_table':value['parent_table']}
            for item in value.values():
                if isinstance(item, (dict, list)):
                    walk(item)

    walk(payload)
    return sorted(identities.values(), key=lambda row:row['id'])


def bind_empty_root_label(responses, independent_parent, visible_labels, expected_id):
    """Bind this provider's empty-title row to an independently read page ID."""
    assert independent_parent['id'].replace('-', '').lower() == expected_id
    assert independent_parent['parent'].get('workspace') is True
    assert not independent_parent.get('archived') and not independent_parent.get('in_trash')
    titles = [row['title'] for row in independent_parent['properties'].values() if row.get('type') == 'title']
    assert titles == [[]], 'Only an independently confirmed empty title uses this mapping'
    pages = {}
    for response in responses:
        if response.get('status') != 200 or response.get('host') != 'app.notion.com':
            continue
        for page in response.get('pages', []):
            if page['id'] in pages:
                assert pages[page['id']] == page, 'Provider identity changed during selection'
            pages[page['id']] = page
    root = pages[expected_id]
    assert root['title'] == '' and root['parent_table'] == 'space'
    collision = [page for page in pages.values() if page['parent_id'] == root['parent_id']
                 and page['parent_table'] == 'space' and page['title'] in {'', 'New page'}]
    assert collision == [root], 'An ambiguous title cannot identify the granted root'
    assert visible_labels.count('New page') == 1
    return 'New page'
