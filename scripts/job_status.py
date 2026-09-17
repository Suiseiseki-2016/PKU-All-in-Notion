"""Report the processing state of every indexed recording.

Used to verify a daily run's outcome. Reads the data dir from the project
`.env` via pku_sync.settings, so it reports wherever DATA_DIR points
(E:/pku-course-data on the Windows host).

Usage: python scripts/job_status.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# pku_sync.config reads `.env` relative to the process CWD; make the script
# independent of where it is launched (Task Scheduler, SSH home dir, …).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)

sys.path.insert(0, str(PROJECT_ROOT))

for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")

from pku_sync.config import settings
from pku_sync.pipeline import collect_jobs


def main() -> int:
    root = settings.data_dir
    jobs = collect_jobs(root)
    out = []
    for job in jobs:
        directory = job.directory
        keyframe_dir = directory / "keyframes"
        keyframes = len(list(keyframe_dir.glob("*.jpg"))) if keyframe_dir.is_dir() else 0
        out.append(
            {
                "course": job.course_name,
                "title": job.recording.title,
                "date": job.recording.date,
                "slug": job.recording.slug,
                "video_mb": round(job.video.stat().st_size / 1e6, 1) if job.video.exists() else None,
                "transcript": (directory / "transcript.json").exists(),
                "notes": (directory / "notes.md").exists(),
                "keyframes": keyframes,
            }
        )
    out.sort(key=lambda r: (r["course"], r["date"] or ""))
    print(json.dumps(out, ensure_ascii=False, indent=1), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
