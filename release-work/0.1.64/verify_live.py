"""Read-only live checks after the release manifest switch."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import urllib.request

root = Path(__file__).resolve().parent
base = 'https://pku.aeoluswu.info'
manifest = json.loads((root / 'latest.json').read_text())

def read(path, limit=None):
    request = urllib.request.Request(base + path, headers={'User-Agent': 'PKU-release-verification/0.1.64', 'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read() if limit is None else response.read(limit), response.url

result = {'version': '0.1.64', 'checked_at': datetime.now(timezone.utc).isoformat()}
status, _, body, _ = read('/download')
assert status == 200 and b'0.1.64' in body
result['download_page'] = {'status': status, 'version': '0.1.64'}
status, _, body, _ = read('/healthz')
assert status == 200 and json.loads(body)['ok']
result['healthz'] = status
result['platforms'] = {}
for platform, row in manifest.items():
    target, arch = platform.split('-', 1)
    status, _, body, _ = read(f'/updates/{target}/{arch}/0.1.63')
    feed = json.loads(body)
    updater = row.get('updater', row['installer'])
    assert status == 200 and feed['version'] == '0.1.64'
    assert feed['signature'] == row['signature']
    assert feed['url'] == base + '/download/release/' + updater
    checks = {'update_feed': {'status': status, 'version': feed['version'], 'signature_matches': True}, 'assets': {}}
    endpoint = '/download/windows' if target == 'windows' else '/download/mac/arm'
    for path, name in [(endpoint, row['installer']), ('/download/release/' + updater, updater), (f'/download/update/{target}/{arch}/0.1.64', updater)]:
        status, headers, body, url = read(path, 1024)
        local = root / name
        assert status == 200 and body == local.read_bytes()[:1024]
        if headers.get('content-length'):
            assert int(headers['content-length']) == local.stat().st_size
        checks['assets'][path] = {'status': status, 'url': url, 'length': local.stat().st_size, 'first_1024_bytes_match': True}
    status, _, _, _ = read(f'/updates/{target}/{arch}/0.1.64')
    assert status == 204
    checks['already_current_status'] = status
    result['platforms'][platform] = checks
status, _, body, _ = read('/packages/simple/pku-course-sync/')
wheel = 'pku_course_sync-0.1.64-py3-none-any.whl'
assert status == 200 and wheel.encode() in body
status, _, body, _ = read('/packages/wheels/' + wheel)
local_wheel = root.parents[1] / 'installer/release/site/wheels' / wheel
assert status == 200 and hashlib.sha256(body).digest() == hashlib.sha256(local_wheel.read_bytes()).digest()
result['python_package'] = {'status': status, 'version': '0.1.64', 'sha256': hashlib.sha256(body).hexdigest()}
(root / 'post-deploy-live-verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(result, ensure_ascii=False, indent=2))
