"""Use installed Windows Edge, macOS Chrome, or Playwright Chromium."""
from pathlib import Path

EDGE = Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
if not EDGE.is_file():
    EDGE = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')

def browser_launch_options():
    return {'executable_path':str(EDGE)} if EDGE.is_file() else {}
