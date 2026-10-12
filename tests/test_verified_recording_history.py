"""Isolated developer checks; never native/service/billing evidence."""
from copy import copy, deepcopy
from types import SimpleNamespace

import pytest
from pku_sync import catalog_snapshots as catalog, platform
from pku_sync.catalog_schema import fingerprint


def setup_history(tmp_path, monkeypatch):
    settings = SimpleNamespace(data_dir=tmp_path, platform_token='private-fixture-session',
                               cloud_transcribe_url='https://fixture.invalid/v1/transcribe')
    catalog.activate_account(settings)
    source = {'source_id': 'pku:recording:old', 'title': '原课程', 'recorded_at': '2026-09-29T10:00:00'}
    base = {'schema': 1, 'authority': 'course.pku.edu.cn', 'course_id': 'course1',
            'course': {'course_id': 'course1', 'name': '课程', 'code': '', 'term': ''},
            'sections': {key: {'complete': True, 'items': []} for key in catalog.SECTIONS}}
    base['sections']['recordings']['items'] = [source]
    current = deepcopy(base); current['sections']['recordings']['items'] = []
    envelope = {'revision': 2, 'snapshot': current}; catalog.save_cache(tmp_path, envelope)
    observations = [{'observation_id': x * 64, 'snapshot_sha256': fingerprint(current),
                     'portal_account_sha256': 'c' * 64, 'authority': 'course.pku.edu.cn',
                     'pages': [{'page_identity': 'full-page', 'raw_sha256': x * 64,
                                'stable_rows_sha256': fingerprint([]), 'row_source_ids': [],
                                'pagination_complete': True}]} for x in ['a', 'b']]
    payload = {'protocol': 1, 'account_id': 3, 'course_id': 'course1', 'base_revision': 1,
               'base_snapshot_sha256': fingerprint(base), 'fresh_snapshot_sha256': fingerprint(current),
               'snapshot': current, 'missing_source_ids': [source['source_id']], 'observations': observations,
               'backend_read_portal': False, 'approval_requirement': 'exact-independent-raw-evidence-manual-review'}
    review_id = 'd' * 64; review_sha = fingerprint(payload); evidence_sha = fingerprint(observations)
    review = {'review_id': review_id, 'review_sha256': review_sha, 'evidence_sha256': evidence_sha,
              'state': 'committed', 'expires_at': 20.0, 'backend_read_portal': False,
              'review': payload, 'approval': {'reviewer': 'operator:fixture', 'approved_at': 10.0,
               'review_sha256': review_sha, 'evidence_sha256': evidence_sha,
               'basis': 'explicit-exact-independent-raw-evidence-manual-review'},
              'result': {'revision': 2, 'snapshot': current, 'review_id': review_id,
                         'review_sha256': review_sha, 'retired_source_ids': [source['source_id']]}}
    row = {'source_id': source['source_id'], 'source': source, 'original_revision': 1,
           'original_snapshot': base, 'original_snapshot_sha256': fingerprint(base),
           'accepted_revision': 2, 'review_id': review_id, 'review_sha256': review_sha,
           'evidence_sha256': evidence_sha, 'observation_evidence': observations}
    history = {'course_id': 'course1', 'backend_read_portal': False, 'recordings': [row]}
    calls = []
    def call(settings_arg, method, cid, suffix='', body=None):
        calls.append((method, cid, suffix)); assert method == 'GET' and body is None
        return deepcopy(history if suffix == '/recording-source-history' else review if suffix.startswith('/recording-retirement-reviews/') else envelope)
    monkeypatch.setattr(catalog, '_call', call)
    class Response:
        status_code = 200
        def json(self): return {'email': 'fixture@example.invalid', 'email_verified': True}
    monkeypatch.setattr(platform, '_get', lambda *a, **k: Response())
    return settings, envelope, history, review, calls


def test_missing_sources_require_committed_history_not_current_catalog(tmp_path, monkeypatch):
    settings, envelope, history, _, calls = setup_history(tmp_path, monkeypatch)
    with platform.request_scope():
        # The old product only had a current canonical read; it cannot be
        # treated as committed history. This identical predicate goes red on
        # that missing-capability baseline rather than a collection failure.
        reader = getattr(catalog, 'read_recording_history',
                         lambda s, c, e, **kwargs: catalog.current(s, c))
        value = reader(settings, 'course1', envelope,
                       expected_previous_sources={'pku:recording:old'})
        assert value.get('recordings') == history['recordings']
        assert ('GET', 'course1', '/recording-source-history') in calls
        assert len([c for c in calls if c[2].startswith('/recording-retirement-reviews/')]) == 1


@pytest.mark.parametrize('mutation', ['pending', 'source', 'snapshot', 'review_digest', 'evidence',
                                     'base_revision', 'accepted_revision', 'unknown_field', 'duplicate',
                                     'page_coverage', 'approval', 'result', 'course'])
def test_corrupt_or_uncommitted_history_is_rejected(tmp_path, monkeypatch, mutation):
    settings, env, history, review, _ = setup_history(tmp_path, monkeypatch)
    if mutation == 'pending': review['state'] = 'pending'
    elif mutation == 'source': history['recordings'][0]['source']['title'] = '被改动'
    elif mutation == 'snapshot': history['recordings'][0]['original_snapshot_sha256'] = '0' * 64
    elif mutation == 'review_digest': review['review_sha256'] = '0' * 64
    elif mutation == 'evidence': history['recordings'][0]['observation_evidence'][0]['pages'][0]['raw_sha256'] = 'z' * 64
    elif mutation == 'base_revision': history['recordings'][0]['original_revision'] = 2
    elif mutation == 'accepted_revision': history['recordings'][0]['accepted_revision'] = 3
    elif mutation == 'unknown_field': history['recordings'][0]['approved'] = True
    elif mutation == 'duplicate': history['recordings'].append(deepcopy(history['recordings'][0]))
    elif mutation == 'page_coverage': review['review']['observations'][0]['pages'][0]['row_source_ids'] = ['unknown']
    elif mutation == 'approval': review['approval']['evidence_sha256'] = '0' * 64
    elif mutation == 'result': review['result']['retired_source_ids'] = []
    elif mutation == 'course': history['course_id'] = 'other'
    with platform.request_scope(), pytest.raises(catalog.CatalogUnavailable):
        catalog.read_recording_history(settings, 'course1', env, expected_previous_sources={'pku:recording:old'})


def test_fresh_provenance_and_cache_never_upgrade_projection(tmp_path, monkeypatch):
    settings, env, _, _, _ = setup_history(tmp_path, monkeypatch)
    from pku_sync import recording_history as rh
    with platform.request_scope():
        value = catalog.read_recording_history(settings, 'course1', env)
        rh.assert_fresh(settings, 'course1', env, value)
        for forged in [dict(value), copy(value), deepcopy(value)]:
            with pytest.raises(catalog.CatalogUnavailable): rh.assert_fresh(settings, 'course1', env, forged)
        rh.write_cache(settings, value)
        cached = rh.read_cache(tmp_path, 'course1', env)
        assert cached['display_read_only'] is True and cached['projection_fresh'] is False
        with pytest.raises(catalog.CatalogUnavailable): rh.assert_fresh(settings, 'course1', env, cached)
        value['recordings'][0]['source']['title'] = '被改动'
        with pytest.raises(catalog.CatalogUnavailable): rh.assert_fresh(settings, 'course1', env, value)
    with platform.request_scope(), pytest.raises(catalog.CatalogUnavailable):
        rh.assert_fresh(settings, 'course1', env, value)
    settings.platform_token = 'other-session'; catalog.activate_account(settings)
    assert rh.read_cache(tmp_path, 'course1', env) is None


def test_unmodified_fresh_object_cannot_cross_operations_or_account_epoch(tmp_path, monkeypatch):
    settings, env, _, _, _ = setup_history(tmp_path, monkeypatch)
    from pku_sync import recording_history as rh
    with platform.request_scope():
        value = catalog.read_recording_history(settings, 'course1', env)
        rh.assert_fresh(settings, 'course1', env, value)
        settings.platform_token = 'temporary-other'; catalog.activate_account(settings)
        settings.platform_token = 'private-fixture-session'; catalog.activate_account(settings)
        with pytest.raises(catalog.CatalogUnavailable): rh.assert_fresh(settings, 'course1', env, value)
        value = catalog.read_recording_history(settings, 'course1', env)
    with platform.request_scope(), pytest.raises(catalog.CatalogUnavailable):
        rh.assert_fresh(settings, 'course1', env, value)


def test_cache_integrity_and_current_binding_are_readonly(tmp_path, monkeypatch):
    settings, env, _, _, _ = setup_history(tmp_path, monkeypatch)
    from pku_sync import recording_history as rh
    import json
    with platform.request_scope():
        value = catalog.read_recording_history(settings, 'course1', env)
        rh.write_cache(settings, value)
    path = rh._cache_path(tmp_path, 'course1')
    original = path.read_text('utf8')
    cached = json.loads(original); cached['payload']['raw_history_sha256'] = '0' * 64
    cached['payload_sha256'] = fingerprint(cached['payload'])
    path.write_text(json.dumps(cached), 'utf8')
    assert rh.read_cache(tmp_path, 'course1', env) is None
    path.write_text(original, 'utf8')
    newer = deepcopy(env); newer['revision'] = 3
    assert rh.read_cache(tmp_path, 'course1', newer) is None
    catalog.cache_path(tmp_path, 'course1').unlink()
    assert rh.read_cache(tmp_path, 'course1', env) is None


def test_service_absent_never_falls_back_to_disk_projection(tmp_path, monkeypatch):
    settings, env, _, _, _ = setup_history(tmp_path, monkeypatch)
    from pku_sync import recording_history as rh
    with platform.request_scope():
        value = catalog.read_recording_history(settings, 'course1', env)
        rh.write_cache(settings, value)
        def fail(*args, **kwargs): raise catalog.CatalogUnavailable('fixture service unavailable')
        monkeypatch.setattr(catalog, '_call', fail)
        with pytest.raises(catalog.CatalogUnavailable): catalog.read_recording_history(settings, 'course1', env)
        assert rh.read_cache(tmp_path, 'course1', env)['projection_fresh'] is False


def test_review_page_digest_stays_bound_even_if_outer_digests_recomputed(tmp_path, monkeypatch):
    settings, env, history, review, _ = setup_history(tmp_path, monkeypatch)
    review['review']['observations'][0]['pages'][0]['stable_rows_sha256'] = '0' * 64
    review['review_sha256'] = fingerprint(review['review'])
    review['evidence_sha256'] = fingerprint(review['review']['observations'])
    review['approval']['review_sha256'] = review['result']['review_sha256'] = review['review_sha256']
    review['approval']['evidence_sha256'] = review['evidence_sha256']
    row = history['recordings'][0]
    row['review_sha256'], row['evidence_sha256'] = review['review_sha256'], review['evidence_sha256']
    row['observation_evidence'] = review['review']['observations']
    with platform.request_scope(), pytest.raises(catalog.CatalogUnavailable):
        catalog.read_recording_history(settings, 'course1', env)


def test_same_account_activation_does_not_invalidate_same_operation(tmp_path, monkeypatch):
    settings, env, _, _, _ = setup_history(tmp_path, monkeypatch)
    from pku_sync import recording_history as rh
    with platform.request_scope():
        value = catalog.read_recording_history(settings, 'course1', env)
        catalog.activate_account(settings)
        rh.assert_fresh(settings, 'course1', env, value)


def test_unknown_missing_source_or_reappeared_source_is_rejected(tmp_path, monkeypatch):
    settings, env, history, _, _ = setup_history(tmp_path, monkeypatch)
    with platform.request_scope(), pytest.raises(catalog.CatalogUnavailable):
        catalog.read_recording_history(settings, 'course1', env, expected_previous_sources={'unknown'})
    env['snapshot']['sections']['recordings']['items'] = [history['recordings'][0]['source']]
    with platform.request_scope(), pytest.raises(catalog.CatalogUnavailable):
        catalog.read_recording_history(settings, 'course1', env)
