"""Check source binding and real clipping with the bundled interpreter."""
import ast
import importlib.metadata
import json
from pathlib import Path
import ssl
import sys
import tempfile

root = Path(__file__).resolve().parents[2]
runtime = Path(sys.argv[1]).resolve()
version = json.loads((root/'desktop/package.json').read_text('utf-8'))['version']
assert importlib.metadata.version('pku-course-sync') == version
import pku_sync
from pku_sync.panel import note_videos
package = Path(pku_sync.__file__).resolve().parent
assert package.is_relative_to(runtime)
count = 0
for file in (root/'pku_sync').rglob('*'):
    if file.is_file() and '__pycache__' not in file.parts and file.suffix != '.pyc':
        bundled = package/file.relative_to(root/'pku_sync')
        # Git normalizes line endings while Windows source files may use CRLF.
        assert bundled.read_bytes().replace(b'\r\n',b'\n') == file.read_bytes().replace(b'\r\n',b'\n'), str(file)
        count += 1
tree = ast.parse((root/'tests/test_panel_note_videos.py').read_text('utf-8'))
names = {'_write_sample_recording',
         'test_extract_clip_produces_keyframe_aligned_playable_clip',
         'test_extract_clip_rejects_markers_outside_the_recording',
         'test_extract_clip_requires_a_video_track'}
namespace = {'note_videos':note_videos}
for node in tree.body:
    if isinstance(node,ast.FunctionDef) and node.name in names:
        exec(compile(ast.Module(body=[node],type_ignores=[]),'<bundled-regression>','exec'),namespace)
for name in sorted(names-{'_write_sample_recording'}):
    with tempfile.TemporaryDirectory(prefix='pku-bundled-clip-') as directory:
        namespace[name](Path(directory))
assert ssl.create_default_context().cert_store_stats()['x509_ca'] > 0
print(json.dumps({'version':version,'source_files':count,'real_clip_checks':3,'tls':True}))
