import hashlib,json,os,plistlib,subprocess,time,urllib.request
from pathlib import Path
base=Path('/Users/wudi/Desktop/remote_project/pku-course-sync/release-work/0.1.64')
app=Path('/private/tmp/pku-release-0.1.64-installed/PKU All in One.app')
runtime=app/'Contents/Resources/resources/runtime'
site=runtime/'lib/python3.11/site-packages/pku_sync'
source=Path('/Users/wudi/Desktop/remote_project/pku-course-sync/pku_sync')
checks=[]
for p in source.rglob('*'):
    if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc' and p.name!='.DS_Store':
        target=site/p.relative_to(source)
        assert target.is_file(),str(target)
        assert target.read_bytes()==p.read_bytes(),str(p)
        checks.append(str(p.relative_to(source)))
info=plistlib.loads((app/'Contents/Info.plist').read_bytes())
assert info['CFBundleShortVersionString']=='0.1.64'

def digest():
    return {str(p.relative_to(app)):hashlib.sha256(p.read_bytes()).hexdigest() for p in app.rglob('*') if p.is_file() and not p.is_symlink()}
prior=digest()
results=[]
active = subprocess.run(["pgrep", "-x", "pku-desktop"], capture_output=True).returncode == 0
for n in (() if active else (1,2)):
    profile=Path('/private/tmp/pku-release-0.1.64-profile')
    profile.mkdir(exist_ok=True)
    env=dict(os.environ,PKU_DESKTOP_DATA_DIR=str(profile),PYTHONDONTWRITEBYTECODE='1')
    t=time.monotonic()
    p=subprocess.Popen([str(app/'Contents/MacOS/pku-desktop')],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    ready=None
    try:
        for _ in range(150):
            assert p.poll() is None, 'native desktop exited during startup'
            log=profile/'panel.log'
            if log.exists() and '健康检查通过' in log.read_text(errors='replace'):
                for port in (8791,8792,8793):
                    try:
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz',timeout=.3) as r: data=json.load(r)
                        if str(data.get('instance_id','')).startswith(f'{p.pid}-'):
                            ready=port
                            break
                    except Exception: pass
            if ready: break
            time.sleep(.2)
        assert ready,'native panel did not become ready'
        startup=round(time.monotonic()-t,3)
        with urllib.request.urlopen(f'http://127.0.0.1:{ready}/app',timeout=5) as r:
            html=r.read().decode()
            assert r.status==200 and 'PKU' in html
        # Deliver the native macOS quit event to this isolated installed app.
        quit_result=subprocess.run(['osascript','-e',f'tell application "{app}" to quit'],capture_output=True,text=True,timeout=15)
        assert quit_result.returncode==0,quit_result.stderr
        p.wait(timeout=15)
        for _ in range(50):
            try: urllib.request.urlopen(f'http://127.0.0.1:{ready}/healthz',timeout=.2)
            except Exception: break
            time.sleep(.1)
        else: raise AssertionError('panel still listening after native quit')
        assert digest()==prior,'installed bundle changed after launch'
        results.append({'round':n,'startup_seconds':startup,'port':ready,'native_quit':True,'port_released':True,'bundle_unchanged':True})
    finally:
        if p.poll() is None: p.terminate(); p.wait(timeout=15)
report={'version':'0.1.64','source_files_equal':len(checks),'bundle_files_checked':len(prior),'launches':results,'signature':'ad-hoc, deep strict verification passed','notarized':False,'native_smoke':'skipped_existing_desktop_instance_left_running' if active else 'passed','full_product_e2e_passed':False}
(base/'macos-verification.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report,indent=2))
