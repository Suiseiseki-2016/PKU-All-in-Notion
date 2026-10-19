"""Add download-page aliases without changing API or asset routing."""
from pathlib import Path
import hashlib
import json
import subprocess

root = Path('/opt/pku-server/deployments/0.1.64')
config = Path('/etc/nginx/conf/conf.d/pku.conf')
original = config.read_bytes()
backup = root / 'pku.conf.before-download-alias'
assert not backup.exists()
backup.write_bytes(original)
marker = '    location ^~ /download/ {\n'
addition = '''    # v0.1.64: the download page shares the public landing page.
    location = /download {
        proxy_pass http://127.0.0.1:8015/;
        proxy_set_header Host $host;
    }
    location = /download/ {
        proxy_pass http://127.0.0.1:8015/;
        proxy_set_header Host $host;
    }
'''
text = original.decode()
assert text.count(marker) == 2
updated = text.replace(marker, addition + marker).encode()
nginx = ['/etc/nginx/sbin/nginx', '-c', '/etc/nginx/conf/nginx.conf']
try:
    config.write_bytes(updated)
    subprocess.run(nginx + ['-t'], check=True)
    subprocess.run(nginx + ['-s', 'reload'], check=True)
except BaseException:
    config.write_bytes(original)
    subprocess.run(nginx + ['-t'], check=True)
    subprocess.run(nginx + ['-s', 'reload'], check=True)
    raise
result = {'version': '0.1.64', 'aliases': ['/download', '/download/'],
          'nginx_config_test': 'passed', 'reload': 'graceful',
          'before_sha256': hashlib.sha256(original).hexdigest(),
          'after_sha256': hashlib.sha256(updated).hexdigest(), 'backup': str(backup)}
(root / 'download-alias-result.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result))
