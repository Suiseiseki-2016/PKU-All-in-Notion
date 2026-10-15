"""Keep a completed local signed NSIS candidate immutable for native runs."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_sources(root):
    package = root / 'pku_sync'
    paths = set(package.rglob('*.py'))
    paths.update(path for path in (package / 'panel/static').rglob('*') if path.is_file())
    return {path.relative_to(root).as_posix() for path in paths if '__pycache__' not in path.parts}


def verify_runtime_sources(root, packaged):
    expected = runtime_sources(root)
    if not expected or runtime_sources(packaged) != expected:
        raise RuntimeError('packaged client source set differs from workspace')
    for path in sorted(expected):
        if sha(packaged / path) != sha(root / path):
            raise RuntimeError('workspace source differs from completed packaged runtime: ' + path)
    return sorted(expected)


def verify_runtime_lock(root, runtime):
    marker = runtime / '.bundle-lock.sha256'
    if not marker.is_file() or marker.read_text('ascii').strip() != sha(root / 'uv.lock'):
        raise RuntimeError('dependency lock differs from completed packaged runtime; rebuild before freezing')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True)
    parser.add_argument('--build-log', type=Path, required=True)
    args = parser.parse_args()
    if not args.name.replace('-', '').isalnum():
        raise ValueError('invalid artifact name')
    out = ROOT / 'artifacts/windows-single-machine-20261006'
    conf = json.loads((ROOT / 'desktop/src-tauri/tauri.conf.json').read_text('utf-8'))
    source = ROOT / ('desktop/src-tauri/target/release/bundle/nsis/PKU All in Notion_' + conf['version'] + '_x64-setup.exe')
    log = args.build_log.resolve()
    if not log.is_relative_to(out) or 'Finished 1 updater signature' not in log.read_text('utf-8', errors='replace'):
        raise RuntimeError('completed signed build evidence required')
    dest = out / (args.name + '.exe')
    signature = Path(str(dest) + '.sig')
    manifest = out / (args.name + '-build.json')
    if any(p.exists() for p in (dest, signature, manifest)):
        raise RuntimeError('immutable candidate name already exists')
    paths = ['desktop/src-tauri/src/lib.rs', 'pku_sync/desktop_lifecycle.py', 'pku_sync/__main__.py',
             'pku_sync/cli.py', 'pku_sync/catalog_snapshots.py', 'pku_sync/projection_receipts.py',
             'pku_sync/projection_notion_effects.py', 'pku_sync/recordings.py',
             'pku_sync/transcription.py', 'pku_sync/pipeline.py', 'pku_sync/platform.py',
             'pku_sync/media.py', 'pku_sync/recovery_fact_readiness.py',
             'pku_sync/panel/static/app.js', 'pku_sync/panel/student_ui.py', 'pku_sync/panel/assignment_workflow.py',
             'pku_sync/notion_login.py', 'pku_sync/panel/notion_workspace.py', 'pku_sync/panel/recordings_api.py',
             'pku_sync/panel/workspace_profile.py', 'desktop/src-tauri/src/draft_save.rs',
             'desktop/src-tauri/Cargo.toml', 'desktop/src-tauri/capabilities/default.json',
             'desktop/src-tauri/build.rs', 'pku_sync/notion.py', 'pku_sync/panel/notion_home.py']
    packaged = ROOT / 'desktop/src-tauri/target/release/resources/runtime/Lib/site-packages'
    verify_runtime_lock(ROOT, packaged.parents[1])
    paths = sorted(set(paths) | set(verify_runtime_sources(ROOT, packaged)) | {
        'pyproject.toml', 'uv.lock', 'desktop/package.json', 'desktop/package-lock.json',
        'desktop/src-tauri/Cargo.lock', 'desktop/src-tauri/tauri.conf.json'})
    if not source.is_file() or not Path(str(source) + '.sig').is_file():
        raise RuntimeError('completed installer and updater signature required')
    shutil.copyfile(source, dest)
    shutil.copyfile(Path(str(source) + '.sig'), signature)
    value = {'kind': 'local-signed-candidate-not-published', 'artifact_sha256': sha(dest),
             'signature_sha256': sha(signature), 'version': conf['version'], 'build_exit_code': 0,
             'build_log_sha256': sha(log), 'source_scope': 'all-client-python-and-static-plus-build-metadata',
             'source_sha256': {p: sha(ROOT / p) for p in paths}}
    manifest.write_text(json.dumps(value, indent=2), 'utf-8')
    print(json.dumps({'candidate': str(dest), 'sha256': value['artifact_sha256']}))


if __name__ == '__main__':
    main()
