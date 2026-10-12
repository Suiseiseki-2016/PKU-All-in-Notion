"""Use installed Windows Edge or Playwright's platform Chromium."""
from pathlib import Path

EDGE = Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')

def browser_launch_options():
    return {'executable_path':str(EDGE)} if EDGE.is_file() else {}
