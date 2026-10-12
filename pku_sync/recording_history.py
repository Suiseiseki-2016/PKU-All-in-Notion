"""Verified retired sources: fresh operation proof versus read-only disk display.

History never changes canonical authority, processing eligibility or metering.
Only authenticated, independently read committed receipts may preserve old
source identities. A cached history is never projection authorization.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import re
import secrets
import time
from urllib.parse import quote
from weakref import WeakValueDictionary

from . import catalog_snapshots as catalog, platform
from .catalog_schema import fingerprint, validate_snapshot

_STAMP = object()
_ISSUED = WeakValueDictionary()
_ROW_FIELDS = {'source_id', 'source', 'original_revision', 'original_snapshot',
               'original_snapshot_sha256', 'accepted_revision', 'review_id',
               'review_sha256', 'evidence_sha256', 'observation_evidence'}


class _FreshHistory(dict):
    def __deepcopy__(self, memo):
        return deepcopy(dict(self), memo)  # Workers/caches get data, never authority.


def _reject():
    raise catalog.CatalogUnavailable('历史录像的账号、课程版本或已提交证据未核验；已有内容保留，请重新读取课程目录。')


def _require(condition):
    if not condition:
        _reject()


def _fields(value, fields):
    _require(isinstance(value, dict) and set(value) == fields)


def _hex(value):
    _require(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None)


def _positive(value):
    _require(type(value) is int and value > 0)


def _number(value):
    _require(type(value) in {int, float})
    try:
        _require(math.isfinite(value) and value > 0)
    except OverflowError:
        _reject()


def _namespace(root, course_id):
    ns = catalog.cache_path(Path(root), course_id).parent.name
    _hex(ns)
    return ns


def _binding(settings, course_id):
    binding = catalog._read_binding(settings)
    _require(platform._request_client.get() is not None)
    _require(_namespace(settings.data_dir, course_id) == binding[1])
    active = Path(settings.data_dir) / '.account-catalog/active-account'
    return binding + (active.stat().st_mtime_ns,)


def _owner(settings):
    try:
        catalog._check_read_worker()
        response = platform._get(platform._endpoint(settings.cloud_transcribe_url, '/v1/auth/me'),
                                 headers=platform._profile_headers(settings), timeout=20)
        _require(response.status_code == 200)
        value = response.json()
        _require(isinstance(value, dict) and value.get('email_verified') is True
                 and isinstance(value.get('email'), str) and bool(value['email'].strip()))
        return fingerprint({'email': value['email'].strip().casefold(),
                            'catalog_endpoint': platform._endpoint(settings.cloud_transcribe_url, '/v1/catalog')})
    except (platform.httpx.HTTPError, ValueError, KeyError, TypeError):
        _reject()


def _observations(observations, snapshot):
    _require(isinstance(observations, list) and len(observations) == 2)
    expected = {row['source_id']: row for row in snapshot['sections']['recordings']['items']}
    identities, accounts = set(), set()
    for obs in observations:
        _fields(obs, {'observation_id', 'snapshot_sha256', 'portal_account_sha256', 'authority', 'pages'})
        for key in ('observation_id', 'snapshot_sha256', 'portal_account_sha256'): _hex(obs[key])
        _require(obs['snapshot_sha256'] == fingerprint(snapshot) and obs['authority'] == 'course.pku.edu.cn')
        _require(obs['observation_id'] not in identities)
        identities.add(obs['observation_id']); accounts.add(obs['portal_account_sha256'])
        _require(isinstance(obs['pages'], list) and 0 < len(obs['pages']) <= 100)
        page_ids, sources = set(), set()
        for page in obs['pages']:
            _fields(page, {'page_identity', 'raw_sha256', 'stable_rows_sha256', 'row_source_ids', 'pagination_complete'})
            identity = page['page_identity']
            _require(isinstance(identity, str) and re.fullmatch('[A-Za-z0-9:_-]{1,150}', identity) is not None
                     and identity not in page_ids and page['pagination_complete'] is True)
            page_ids.add(identity)
            _hex(page['raw_sha256']); _hex(page['stable_rows_sha256'])
            ids = page['row_source_ids']
            _require(isinstance(ids, list) and all(isinstance(s, str) for s in ids))
            _require(len(set(ids)) == len(ids) and not sources.intersection(ids) and set(ids) <= set(expected))
            _require(page['stable_rows_sha256'] == fingerprint([expected[s] for s in ids]))
            sources.update(ids)
        _require(sources == set(expected))
    _require(len(accounts) == 1)


def _validate(raw, reviews, course_id, current, expected_previous_sources=None):
    current = catalog._envelope(current, course_id)
    _fields(raw, {'course_id', 'backend_read_portal', 'recordings'})
    _require(raw['course_id'] == course_id and raw['backend_read_portal'] is False
             and isinstance(raw['recordings'], list) and isinstance(reviews, list))
    review_map = {}
    for receipt in reviews:
        _fields(receipt, {'review_id', 'review_sha256', 'evidence_sha256', 'state', 'expires_at',
                          'backend_read_portal', 'review', 'approval', 'result'})
        for key in ('review_id', 'review_sha256', 'evidence_sha256'): _hex(receipt[key])
        _require(receipt['review_id'] not in review_map and receipt['state'] == 'committed'
                 and receipt['backend_read_portal'] is False)
        _number(receipt['expires_at'])  # Committed evidence remains valid after ticket expiry.
        payload, approval, result = receipt['review'], receipt['approval'], receipt['result']
        _fields(payload, {'protocol', 'account_id', 'course_id', 'base_revision', 'base_snapshot_sha256',
                          'fresh_snapshot_sha256', 'snapshot', 'missing_source_ids', 'observations',
                          'backend_read_portal', 'approval_requirement'})
        _require(type(payload['protocol']) is int and payload['protocol'] == 1)
        _positive(payload['account_id']); _positive(payload['base_revision'])
        _require(payload['course_id'] == course_id and payload['backend_read_portal'] is False
                 and payload['approval_requirement'] == 'exact-independent-raw-evidence-manual-review')
        snapshot = validate_snapshot(payload['snapshot'])
        _require(snapshot['course_id'] == course_id and fingerprint(snapshot) == payload['fresh_snapshot_sha256'])
        _hex(payload['base_snapshot_sha256']); _hex(payload['fresh_snapshot_sha256'])
        missing = payload['missing_source_ids']
        _require(isinstance(missing, list) and bool(missing) and all(isinstance(s, str) and s for s in missing)
                 and missing == sorted(set(missing)))
        _observations(payload['observations'], snapshot)
        _require(fingerprint(payload) == receipt['review_sha256']
                 and fingerprint(payload['observations']) == receipt['evidence_sha256'])
        _fields(approval, {'reviewer', 'approved_at', 'review_sha256', 'evidence_sha256', 'basis'})
        _require(isinstance(approval['reviewer'], str) and re.fullmatch('[A-Za-z0-9:_-]{1,120}', approval['reviewer']) is not None)
        _number(approval['approved_at'])
        _require(approval['approved_at'] <= receipt['expires_at']
                 and approval['review_sha256'] == receipt['review_sha256']
                 and approval['evidence_sha256'] == receipt['evidence_sha256']
                 and approval['basis'] == 'explicit-exact-independent-raw-evidence-manual-review')
        _fields(result, {'revision', 'snapshot', 'review_id', 'review_sha256', 'retired_source_ids'})
        _positive(result['revision'])
        _require(result['revision'] == payload['base_revision'] + 1 and result['revision'] <= current['revision']
                 and result['snapshot'] == snapshot and result['review_id'] == receipt['review_id']
                 and result['review_sha256'] == receipt['review_sha256'] and result['retired_source_ids'] == missing)
        if result['revision'] == current['revision']: _require(snapshot == current['snapshot'])
        review_map[receipt['review_id']] = receipt
    _require(len({r['review']['account_id'] for r in reviews}) <= 1)
    current_ids = {r['source_id'] for r in current['snapshot']['sections']['recordings']['items']}
    source_ids, used_reviews = set(), set()
    for row in raw['recordings']:
        _fields(row, _ROW_FIELDS)
        _require(isinstance(row['source_id'], str) and bool(row['source_id'])
                 and row['source_id'] not in source_ids and row['source_id'] not in current_ids)
        source_ids.add(row['source_id']); _positive(row['original_revision']); _positive(row['accepted_revision'])
        for key in ('original_snapshot_sha256', 'review_id', 'review_sha256', 'evidence_sha256'): _hex(row[key])
        original = validate_snapshot(row['original_snapshot'])
        _require(original['course_id'] == course_id and fingerprint(original) == row['original_snapshot_sha256'])
        _require([r for r in original['sections']['recordings']['items'] if r['source_id'] == row['source_id']] == [row['source']])
        _require(row['review_id'] in review_map); receipt = review_map[row['review_id']]; payload = receipt['review']
        _require(row['original_revision'] == payload['base_revision']
                 and row['original_snapshot_sha256'] == payload['base_snapshot_sha256']
                 and row['accepted_revision'] == receipt['result']['revision']
                 and row['review_sha256'] == receipt['review_sha256']
                 and row['evidence_sha256'] == receipt['evidence_sha256']
                 and row['observation_evidence'] == payload['observations'])
        for section in catalog.SECTIONS:
            old_ids = {r['source_id'] for r in original['sections'][section]['items']}
            new_ids = {r['source_id'] for r in payload['snapshot']['sections'][section]['items']}
            _require(old_ids - new_ids == set(payload['missing_source_ids']) if section == 'recordings' else not old_ids - new_ids)
        used_reviews.add(row['review_id'])
    _require(used_reviews == set(review_map))
    for receipt in reviews:
        _require({r['source_id'] for r in raw['recordings'] if r['review_id'] == receipt['review_id']} == set(receipt['review']['missing_source_ids']))
    if expected_previous_sources is not None:
        _require(isinstance(expected_previous_sources, (dict, set, list, tuple)))
        expected = set(expected_previous_sources)
        _require(all(isinstance(s, str) and s for s in expected) and expected <= source_ids)
        if isinstance(expected_previous_sources, dict):
            _require(all(r['source'] == expected_previous_sources[r['source_id']] for r in raw['recordings'] if r['source_id'] in expected))
    return deepcopy(raw['recordings'])


def verified_history(settings, course_id, current_envelope, expected_previous_sources=None):
    try:
        binding = _binding(settings, course_id)
        current = catalog._envelope(current_envelope, course_id)
        _require(catalog.current(settings, course_id) == current)
        owner = _owner(settings)
        raw = catalog._call(settings, 'GET', course_id, '/recording-source-history')
        _require(isinstance(raw.get('recordings'), list))
        ids = {r.get('review_id') for r in raw['recordings'] if isinstance(r, dict)}
        _require(all(isinstance(i, str) and re.fullmatch('[0-9a-f]{64}', i) for i in ids))
        reviews = [catalog._call(settings, 'GET', course_id, '/recording-retirement-reviews/' + quote(i, safe='')) for i in sorted(ids)]
        recordings = _validate(raw, reviews, course_id, current, expected_previous_sources)
        _require(_binding(settings, course_id) == binding and catalog.current(settings, course_id) == current)
        _require(_owner(settings) == owner)
        value = _FreshHistory(schema=1, account_namespace=binding[1], account_owner_sha256=owner,
            course_id=course_id, current_revision=current['revision'], current_snapshot_sha256=fingerprint(current['snapshot']),
            raw_history=raw, raw_history_sha256=fingerprint(raw), reviews=reviews, reviews_sha256=fingerprint(reviews),
            recordings=recordings, verified_at=time.time())
        value._stamp, value._binding, value._digest = _STAMP, binding, fingerprint(value)
        value._client = platform._request_client.get()
        _ISSUED[id(value)] = value
        return value
    except (KeyError, TypeError, ValueError, OSError):
        _reject()


def assert_fresh(settings, course_id, current_envelope, value):
    try:
        _require(type(value) is _FreshHistory and _ISSUED.get(id(value)) is value and value._stamp is _STAMP
                 and value._client is platform._request_client.get()
                 and value._binding == _binding(settings, course_id) and value._digest == fingerprint(value))
        current = catalog._envelope(current_envelope, course_id)
        _require(value['course_id'] == course_id and value['current_revision'] == current['revision']
                 and value['current_snapshot_sha256'] == fingerprint(current['snapshot']))
    except (AttributeError, KeyError, TypeError, ValueError, OSError): _reject()


def _cache_path(root, course_id):
    return Path(root) / '.recording-history' / _namespace(root, course_id) / (fingerprint({'course': course_id}) + '.json')


def write_cache(settings, value):
    current = catalog.cached(settings.data_dir, value['course_id'])
    assert_fresh(settings, value['course_id'], current, value)
    path = _cache_path(settings.data_dir, value['course_id']); path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    try:
        temp.write_text(json.dumps({'payload': value, 'payload_sha256': fingerprint(value)}, ensure_ascii=False), 'utf8')
        temp.replace(path)
    finally: temp.unlink(missing_ok=True)


def read_cache(data_dir, course_id, current_envelope):
    try:
        current = catalog._envelope(current_envelope, course_id)
        _require(catalog.cached(data_dir, course_id) == current)
        cached = json.loads(_cache_path(data_dir, course_id).read_text('utf8'))
        _fields(cached, {'payload', 'payload_sha256'}); value = cached['payload']
        _fields(value, {'schema', 'account_namespace', 'account_owner_sha256', 'course_id', 'current_revision',
                       'current_snapshot_sha256', 'raw_history', 'raw_history_sha256', 'reviews', 'reviews_sha256', 'recordings', 'verified_at'})
        _require(type(value['schema']) is int and value['schema'] == 1 and fingerprint(value) == cached['payload_sha256'])
        _hex(value['account_owner_sha256']); _number(value['verified_at'])
        _positive(value['current_revision'])
        _require(value['account_namespace'] == _namespace(data_dir, course_id) and value['course_id'] == course_id
                 and value['current_revision'] == current['revision'] and value['current_snapshot_sha256'] == fingerprint(current['snapshot']))
        _require(value['raw_history_sha256'] == fingerprint(value['raw_history']) and value['reviews_sha256'] == fingerprint(value['reviews']))
        _require(value['recordings'] == _validate(value['raw_history'], value['reviews'], course_id, current))
        return {**value, 'projection_fresh': False, 'display_read_only': True}
    except (KeyError, TypeError, ValueError, OSError): return None
