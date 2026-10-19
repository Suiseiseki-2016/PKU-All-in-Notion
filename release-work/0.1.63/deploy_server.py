"""Deploy the tested 0.1.63 server source with code rollback and a DB backup.

Run on server-a only after the desktop publication gate is accepted.
The default command verifies staged files; --apply performs the deployment.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import urllib.request

ROOT = Path('/opt/pku-server/deployments/0.1.63')
CANDIDATE = ROOT / 'candidate/server'
RELAY = 'pku-relay'
SITE = 'pku-release-site-20261016'


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def docker(*args):
    return run('docker', *args)


def no_active_work():
    with sqlite3.connect('file:/opt/pku-server/data/accounts.db?mode=ro', uri=True) as db:
        assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        jobs = db.execute("SELECT count(*) FROM llm_jobs WHERE status IN ('queued','running')").fetchone()[0]
        uploads = db.execute("SELECT count(*) FROM direct_upload_grants WHERE state='processing'").fetchone()[0]
        assert jobs == uploads == 0, 'Active jobs: deployment deferred'


def healthy(port):
    for _ in range(30):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=2) as response:
                if response.status == 200 and json.load(response)['ok']:
                    return True
        except (OSError, ValueError):
            pass
        time.sleep(1)
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    expected = json.loads((ROOT / 'server-source-sha256.json').read_text())
    for relative, digest in expected.items():
        path = CANDIDATE / relative
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, relative
        if path.suffix == '.py':
            compile(path.read_bytes(), str(path), 'exec')
    assert healthy(8000) and healthy(8015)
    if not args.apply:
        print(json.dumps({'status': 'staged', 'source_files': len(expected), 'production_changed': False}))
        return
    no_active_work()
    rollback = ROOT / 'rollback'
    rollback.mkdir(mode=0o700)
    docker('cp', RELAY + ':/app/server', str(rollback / 'relay-server'))
    docker('cp', SITE + ':/app/server', str(rollback / 'site-server'))
    shutil.copytree('/opt/pku-server/server', rollback / 'host-server')
    backup = ROOT / 'accounts-before.db'
    with sqlite3.connect('file:/opt/pku-server/data/accounts.db?mode=ro', uri=True) as source:
        with sqlite3.connect(backup) as target:
            source.backup(target)
    os.chmod(backup, 0o600)
    no_active_work()
    try:
        docker('cp', str(CANDIDATE) + '/.', RELAY + ':/app/server/')
        docker('exec', RELAY, 'python', '-c',
               "import shutil; shutil.rmtree('/app/server/__pycache__', ignore_errors=True)")
        docker('restart', '-t', '30', RELAY)
        assert healthy(8000), 'Relay did not recover'
        # The public site is a separate container, with the same release volume.
        docker('cp', str(CANDIDATE / 'landing.py'), SITE + ':/app/server/landing.py')
        docker('cp', str(CANDIDATE / 'site_assets') + '/.', SITE + ':/app/server/site_assets/')
        docker('exec', SITE, 'python', '-c',
               "import shutil; shutil.rmtree('/app/server/__pycache__', ignore_errors=True)")
        docker('restart', '-t', '30', SITE)
        assert healthy(8015), 'Public site did not recover'
        for relative in expected:
            source = CANDIDATE / relative
            dest = Path('/opt/pku-server/server') / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
        live_hashes = json.loads(docker('exec', RELAY, 'python', '-c',
            "import pathlib,hashlib,json;root=pathlib.Path('/app/server');"
            "print(json.dumps({str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()"
            " for p in root.rglob('*') if p.is_file() and p.suffix in ('.py','.js','.css','.html','.png')}))"))
        assert all(live_hashes.get(p) == h for p, h in expected.items())
        result = {'status': 'deployed', 'version': '0.1.63', 'source_files': len(expected),
                  'relay_healthz': 200, 'site_healthz': 200, 'backup': str(backup),
                  'vendor_calls': 0}
        (ROOT / 'deployment-result.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result))
    except BaseException:
        for container, directory in ((RELAY, 'relay-server'), (SITE, 'site-server')):
            docker('cp', str(rollback / directory) + '/.', container + ':/app/server/')
            docker('exec', container, 'python', '-c',
                   "import shutil; shutil.rmtree('/app/server/__pycache__', ignore_errors=True)")
            docker('restart', '-t', '30', container)
        # Schema changes are additive. Never restore a DB over new live writes.
        print(json.dumps({'status': 'code-rolled-back', 'relay_healthy': healthy(8000),
                          'site_healthy': healthy(8015)}))
        raise


if __name__ == '__main__':
    main()
