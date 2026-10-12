"""Use Windows' preinstalled Edge; install Chromium on other CI platforms."""
from pathlib import Path
import subprocess
import sys

edge = Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
if sys.platform == 'win32' and edge.is_file():
    print('Using the installed Microsoft Edge Chromium engine')
else:
    subprocess.run([sys.executable,'-m','playwright','install','--with-deps','chromium'],check=True)
