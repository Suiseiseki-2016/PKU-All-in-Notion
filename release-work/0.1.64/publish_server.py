"""Publish verified 0.1.64 assets, then atomically switch the live manifest."""
import hashlib
import json
import os
from pathlib import Path
import shutil

VERSION = '0.1.64'
root = Path('/opt/pku-server/deployments') / VERSION
art = root / 'artifacts'
packages = root / 'packages'
release = Path('/opt/pku-server/data/releases')
web = Path('/var/www/pku-packages')
manifest = json.loads((art / 'latest.json').read_text())
assets = set()
for platform, row in manifest.items():
    assert row['version'] == VERSION
    for field, hash_field in (('installer', 'sha256'), ('updater', 'updater_sha256')):
        name = row.get(field)
        if name:
            assert Path(name).name == name
            assert hashlib.sha256((art / name).read_bytes()).hexdigest() == row[hash_field]
            assets.add(name)
    updater = row.get('updater', row['installer'])
    assert (art / (updater + '.sig')).read_text().strip() == row['signature']
    assets.add(updater + '.sig')
wheel = f'wheels/pku_course_sync-{VERSION}-py3-none-any.whl'
digest = hashlib.sha256((packages / wheel).read_bytes()).hexdigest()
assert digest + '  ' + wheel in (packages / 'SHA256SUMS.txt').read_text()
assert Path(wheel).name + '#sha256=' + digest in (packages / 'simple/pku-course-sync/index.html').read_text()
backup = root / 'publication-backup'
backup.mkdir(mode=0o700)
for name, path in [('latest.json', release / 'latest.json'), ('SHA256SUMS.txt', web / 'SHA256SUMS.txt'), ('index.html', web / 'simple/pku-course-sync/index.html')]:
    shutil.copy2(path, backup / name)

def publish(source, dest, *, link=False):
    tmp = dest.with_name(dest.name + '.pending-' + VERSION)
    if link:
        os.link(source, tmp)
    else:
        shutil.copyfile(source, tmp)
    os.chmod(tmp, 0o644)
    os.replace(tmp, dest)

for name in sorted(assets):
    publish(art / name, release / name, link=True)
publish(packages / wheel, web / wheel)
publish(packages / 'SHA256SUMS.txt', web / 'SHA256SUMS.txt')
publish(packages / 'simple/pku-course-sync/index.html', web / 'simple/pku-course-sync/index.html')
publish(art / 'latest.json', release / f'latest-{VERSION}.json')
publish(art / 'latest.json', release / 'latest.json')
result = {'version': VERSION, 'published': True, 'wheel_sha256': digest,
          'backup': str(backup), 'assets': sorted(assets), 'server_code_changed': False}
(root / 'publication-result.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result))
