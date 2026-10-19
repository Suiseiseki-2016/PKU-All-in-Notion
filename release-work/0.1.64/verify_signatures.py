"""Verify Tauri's Minisign signatures with the app's existing public key."""
import base64
import hashlib
import json
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

root = Path(__file__).resolve().parent
config = json.loads((root.parents[1] / 'desktop/src-tauri/tauri.conf.json').read_text())
key_lines = base64.b64decode(config['plugins']['updater']['pubkey']).decode().splitlines()
key_packet = base64.b64decode(key_lines[1])
assert len(key_packet) == 42
key = Ed25519PublicKey.from_public_bytes(key_packet[10:])
manifest = json.loads((root / 'latest.json').read_text())
results = {}
for platform, row in manifest.items():
    name = row.get('updater', row['installer'])
    signature = (root / (name + '.sig')).read_text().strip()
    assert signature == row['signature']
    lines = base64.b64decode(signature).decode().splitlines()
    packet = base64.b64decode(lines[1])
    assert len(packet) == 74 and packet[2:10] == key_packet[2:10]
    data = (root / name).read_bytes()
    assert packet[:2] in (b'ED', b'Ed')
    key.verify(packet[10:], hashlib.blake2b(data).digest() if packet[:2] == b'ED' else data)
    comment = lines[2].removeprefix('trusted comment: ')
    assert comment != lines[2] and 'version:0.1.64' in comment.split()
    key.verify(base64.b64decode(lines[3]), packet[10:] + comment.encode())
    assert hashlib.sha256((root / row['installer']).read_bytes()).hexdigest() == row['sha256']
    if row.get('updater_sha256'):
        assert hashlib.sha256(data).hexdigest() == row['updater_sha256']
    results[platform] = {'version': '0.1.64', 'updater': name, 'signature': 'verified',
                         'signed_version': 'verified', 'sha256': hashlib.sha256(data).hexdigest()}
print(json.dumps(results, indent=2))
