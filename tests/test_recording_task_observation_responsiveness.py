"""Developer regression: task observations survive unrelated slow HTTP reads.

This is an in-process ASGI check, never evidence of native E2E acceptance.
"""
import asyncio
import threading
from types import SimpleNamespace

import anyio
import httpx
import pytest
from fastapi import FastAPI

from pku_sync.panel.recordings_api import RecordingWorkManager, Work, add_recording_routes


@pytest.mark.parametrize('state', ['running', 'failed', 'done'])
def test_task_state_remains_observable_when_sync_http_workers_are_busy(tmp_path, state):
    async def exercise():
        app = FastAPI()
        manager = RecordingWorkManager(SimpleNamespace(data_dir=str(tmp_path)), None)
        manager._work = Work(kind='sync', state=state, stage='同步并验证新版 Notion 目录')
        add_recording_routes(app, manager)
        started, release = threading.Event(), threading.Event()
        @app.get('/held-unrelated-read')
        def held_read():
            started.set()
            assert release.wait(5), 'The regression must not leak a blocked worker'
            return {'read_finished': True}
        limiter = anyio.to_thread.current_default_thread_limiter()
        original_limit = limiter.total_tokens
        limiter.total_tokens = 1
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            held = asyncio.create_task(client.get('/held-unrelated-read'))
            try:
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.01)
                assert started.is_set(), 'The slow read must actually own the HTTP worker'
                response = await asyncio.wait_for(client.get('/api/recordings/task'), timeout=0.75)
                assert response.status_code == 200
                assert response.json()['kind'] == 'sync' and response.json()['state'] == state
                assert response.json()['stage'] == manager._work.stage
                assert not release.is_set(), 'Task observation must not wait for the unrelated read'
            finally:
                release.set()
                assert (await held).status_code == 200
                limiter.total_tokens = original_limit
    asyncio.run(exercise())
