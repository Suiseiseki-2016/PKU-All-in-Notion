"""Offline fixture safety; physical NTFS success still requires the native run."""
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from product_e2e.core import ResourceBlocked
from product_e2e.disk_native_case import DiskCase, owned_seed


@pytest.mark.parametrize('fault', ['none', 'outside', 'root', 'hash'])
def test_disk_source_must_be_owned_and_match_actual_candidate(tmp_path, fault):
    root = tmp_path / 'owned'
    seed = root / 'seed'
    seed.mkdir(parents=True)
    artifact = tmp_path / 'candidate.exe'
    artifact.write_bytes(b'not-a-real-installer')
    selected = tmp_path if fault == 'outside' else root if fault == 'root' else seed
    context = {'config': {'test_root': str(root), 'instances': {'primary': {'seed_dir': str(selected), 'app_dir': str(root / 'app')}}},
               'artifact': artifact, 'artifact_sha256': 'incorrect' if fault == 'hash' else sha256(artifact.read_bytes()).hexdigest()}
    if fault == 'none':
        assert owned_seed(context) == seed
    else:
        with pytest.raises(ResourceBlocked):
            owned_seed(context)


@pytest.mark.parametrize('alive', [False, True])
def test_volume_is_never_detached_under_a_live_native_process(tmp_path, alive):
    events = []
    case = DiskCase.__new__(DiskCase)
    case.original = {'run_dir': tmp_path}
    def export(path):
        path.mkdir(parents=True)
        return []
    case.boundary = SimpleNamespace(close=lambda: events.append('detach'), export=export)
    run = SimpleNamespace(processes={'primary': SimpleNamespace(poll=lambda: None if alive else 0)})
    result = case.finish(run, {'blockers': [], 'evidence': []})
    assert events == ([] if alive else ['detach'])
    assert result['blockers'] == (['owned_disk_fixture_retained_for_active_native_process'] if alive else [])


def test_denied_hardware_setup_is_a_failed_case_without_remote_actions(tmp_path, monkeypatch):
    from product_e2e import native_driver, disk_native_case
    def denied(context):
        raise ResourceBlocked('actual_ntfs_uac_not_approved')
    monkeypatch.setattr(disk_native_case, 'DiskCase', denied)
    scenario = {'id': 'JOURNEY-06', 'assertions': ['real-assertion'], 'fault_variants': ['disk-full']}
    result = native_driver.run_case(scenario, 'fault', {
        'target': 'windows', 'config': {}, 'run_dir': tmp_path})
    assert result['blockers'] == ['actual_ntfs_uac_not_approved']
    assert not any(result['assertions'].values())
    assert all(value is None for value in result['metrics'].values())
