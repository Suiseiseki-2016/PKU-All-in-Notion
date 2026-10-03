from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pku_sync.cli as cli


def _job(tmp_path: Path, name: str):
    directory = tmp_path / name
    directory.mkdir()
    (directory / "video.mp4").write_bytes(b"video")
    return SimpleNamespace(directory=directory, video=directory / "video.mp4",
                           label=name)


def test_download_workers_run_bounded_concurrently(tmp_path, monkeypatch):
    jobs = [_job(tmp_path, f"job-{index}") for index in range(3)]
    state = {"active": 0, "maximum": 0}
    lock = threading.Lock()

    monkeypatch.setattr("pku_sync.config.settings",
                        SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr("pku_sync.pipeline.collect_jobs", lambda root, course: jobs)
    monkeypatch.setattr("pku_sync.auth.get_session", lambda: SimpleNamespace(close=lambda: None))

    def fake_download(client, job):
        with lock:
            state["active"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
        time.sleep(0.03)
        with lock:
            state["active"] -= 1
        return SimpleNamespace(skipped=False, resumed=False, path=job.video, error="")

    monkeypatch.setattr("pku_sync.pipeline.download_job", fake_download)
    cli.download_cmd(workers=2)

    assert 2 <= state["maximum"] <= 2


def test_process_workers_are_forced_serial_for_local_whisper(tmp_path, monkeypatch):
    jobs = [_job(tmp_path, f"job-{index}") for index in range(2)]
    state = {"calls": 0}
    monkeypatch.setattr(
        "pku_sync.config.settings",
        SimpleNamespace(data_dir=tmp_path, transcription_backend="local"),
    )
    monkeypatch.setattr("pku_sync.pipeline.collect_jobs", lambda root, course: jobs)

    def fake_process(job, settings):
        state["calls"] += 1
        return SimpleNamespace(transcribed=True, keyframes=0, notes=True,
                               removed_video=False, errors=[])

    monkeypatch.setattr("pku_sync.pipeline.process_job", fake_process)
    cli.process_cmd(workers=2)

    assert state["calls"] == 2
