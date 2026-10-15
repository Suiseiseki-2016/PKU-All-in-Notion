"""Provision the real NTFS fault for the complete Windows native runner."""
from copy import deepcopy
from pathlib import Path
import json
import shutil
import tempfile

from dotenv import set_key

from .core import ResourceBlocked, sha256


def owned_seed(context):
    config = context['config']
    root = Path(config['test_root']).resolve()
    instance = config['instances']['primary']
    seed = Path(instance.get('seed_dir', instance['app_dir'])).resolve()
    if seed == root or not seed.is_relative_to(root) or not seed.is_dir():
        raise ResourceBlocked('disk_case_seed_not_owned')
    artifact = Path(context['artifact'])
    if not artifact.is_file() or sha256(artifact) != context['artifact_sha256']:
        raise ResourceBlocked('disk_case_candidate_hash_mismatch')
    return seed


class DiskCase:
    def __init__(self, context):
        self.original = context
        self.seed = owned_seed(context)
        self.root = Path(tempfile.mkdtemp(prefix='pku-native-disk-full-'))
        from .windows_disk_boundary import WindowsDiskBoundary
        self.boundary = WindowsDiskBoundary(self.root, lifetime_seconds=7200)
        self.context = None

    def prepare(self):
        # Start-Process RunAs is implemented by the existing bounded controller.
        # The only full volume is the owned <=64 MiB VHD, never a user drive.
        ready = self.boundary.start()
        data = self.seed / 'data'
        size = sum(path.stat().st_size for path in data.rglob('*') if path.is_file())
        if size + 4 * 1024 * 1024 > shutil.disk_usage(ready['native_data_dir']).free:
            raise ResourceBlocked('disk_case_seed_exceeds_owned_volume_capacity')
        from .windows_install import install_isolated
        binary = self.root / 'installed/pku-desktop.exe'
        receipt = install_isolated(self.original['artifact'], binary, self.root)
        if receipt['version'] != self.original['version']:
            raise ResourceBlocked('disk_case_actual_installed_version_mismatch')
        primary = self.root / 'primary'
        for source in self.seed.iterdir():
            if source.name == 'data':
                continue
            target = primary / source.name
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copyfile(source, target)
        shutil.copytree(data, ready['native_data_dir'], dirs_exist_ok=True)
        if (primary / '.env').exists():
            set_key(primary / '.env', 'DATA_DIR', str(primary / 'data'))
        context = {**self.original, 'config': deepcopy(self.original['config']),
                   'installed_path': str(binary), '_windows_disk_boundary': self.boundary}
        config = context['config']
        config['test_root'] = str(self.root)
        config['instances'] = {'primary': {'app_dir': str(primary), 'installed_path': str(binary)}}
        config['installation_receipt'] = {**receipt, 'generated_by': 'os-installer', 'installed_path': str(binary)}
        if 'external_browser' in config:
            config['external_browser']['profile_dir'] = str(self.root / 'browser')
        self.context = context
        return context

    def finish(self, run, result):
        # Never detach a volume underneath an active native writer. Failed
        # normal cleanup remains a failed case, with its bounded fixture intact.
        alive = run is not None and any(process.poll() is None for process in run.processes.values())
        if alive:
            result['blockers'].append('owned_disk_fixture_retained_for_active_native_process')
        else:
            try:
                self.boundary.close()
            except Exception as exc:
                result['blockers'].append('owned_disk_cleanup_failed_' + type(exc).__name__)
        directory = Path(self.original['run_dir']) / 'JOURNEY-06-fault/actual-ntfs-volume'
        exported = self.boundary.export(directory)
        observations = directory / 'native-physical-write-observations.json'
        observations.write_text(json.dumps(getattr(self.boundary, 'observations', []), indent=2), 'utf-8')
        exported.append({'path': observations.name, 'sha256': sha256(observations)})
        if result.get('fault_variants', {}).get('disk-full') is True and not getattr(self.boundary, 'observations', []):
            result['blockers'].append('actual_disk_failure_observation_missing')
        for row in exported:
            path = directory / row['path']
            result['evidence'].append({'role': 'runtime', 'path': path.relative_to(self.original['run_dir']).as_posix(),
                                       'sha256': row['sha256']})
        return result
