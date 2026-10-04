"""Windows Edge smoke tests for the real student HTML and JavaScript.

Run with: python -m pytest tests/test_panel_ui_browser.py
The browser dependency is optional for ordinary unit-test environments.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from pku_sync.panel.connection import ConnectionService, CONNECTED_COPY, CONNECT_FAILED_COPY
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.platform_bridge import FakePlatformBridge
from pku_sync.platform import PlatformError
from pku_sync.panel.webapi import create_app
from pku_sync.panel.student_ui import PANEL_COPY

playwright = pytest.importorskip("playwright.sync_api")
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")


class _NoopRunner:
    def submit(self, kind):
        return None

    def current(self):
        return None

    def last(self):
        return None


class _RetryConnection:
    instant_connect = True

    def __init__(self):
        self.attempts = 0
        self.is_connected = False

    def connected(self):
        return self.is_connected

    def disconnect(self):
        self.is_connected = False

    def connect(self):
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("private upstream response must not reach the UI")
        self.is_connected = True


@pytest.mark.skipif(not EDGE.exists(), reason="Microsoft Edge is required")
def test_registration_verification_and_notion_connection_in_edge():
    platform = FakePlatformBridge(activated=False)
    provider = _RetryConnection()
    app = create_app(
        settings=SimpleNamespace(data_dir="data", platform_token=""),
        runner=_NoopRunner(),
        directory_service=build_fake_directory(),
        connection_service=ConnectionService(provider),
        platform_service=platform,
    )
    client = TestClient(app)

    def serve(route):
        request = route.request
        url = urlsplit(request.url)
        path = url.path + ("?" + url.query if url.query else "")
        if path == "/api/update":
            route.fulfill(status=404, body="{}", content_type="application/json")
            return
        response = client.request(
            request.method,
            path,
            content=request.post_data.encode() if request.post_data else None,
            headers={"content-type": request.headers.get("content-type", "application/json")},
        )
        route.fulfill(
            status=response.status_code,
            body=response.content,
            content_type=response.headers.get("content-type", "text/plain"),
        )

    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=str(EDGE), headless=True)
        try:
            page = browser.new_page()
            page.route("http://panel.test/**", serve)
            page.goto("http://panel.test/app")
            page.set_viewport_size({"width": 800, "height": 560})
            for selector in ("#auth-email", "#auth-form [data-action=auth-submit]"):
                playwright.expect(page.locator(selector)).to_be_in_viewport(ratio=1)
            page.set_viewport_size({"width": 1280, "height": 720})
            page.get_by_role("button", name=PANEL_COPY["auth"]["login"]["switch_register"]).click()
            assert page.locator("#auth-password-confirm").is_visible()
            page.locator("#auth-email").fill("new@example.com")
            page.locator("#auth-password").fill("password1")
            page.locator("#auth-password-confirm").fill("different1")
            page.locator("#auth-form [data-action=auth-submit]").click()
            assert page.locator("#auth-help").inner_text() == PANEL_COPY["auth"]["password_mismatch"]
            assert not platform.auth_calls
            assert page.locator("#auth-password").input_value() == "password1"

            page.locator("#auth-password-confirm").fill("password1")
            page.locator("#auth-form [data-action=auth-submit]").click()
            page.locator(".sidebar").get_by_text(PANEL_COPY["account"]["unverified"], exact=True).wait_for()
            assert {"op": "register", "email": "new@example.com"} in platform.auth_calls
            assert page.locator(".sidebar").get_by_role("button", name=PANEL_COPY["account"]["refresh_verification"]).is_visible()

            platform.mark_verified()
            page.locator(".sidebar").get_by_role("button", name=PANEL_COPY["account"]["refresh_verification"]).click()
            page.locator(".sidebar").get_by_text(PANEL_COPY["account"]["verified"], exact=True).wait_for()
            assert not page.locator(".sidebar").get_by_role("button", name=PANEL_COPY["account"]["refresh_verification"]).count()

            assert page.get_by_role("heading", name="我的课程").is_visible()
            page.locator('.sidebar [data-block="connection"] [data-action="connect-notion"]').click()
            error = page.locator('.sidebar [data-block="connection"] [role="alert"]')
            error.wait_for()
            assert CONNECT_FAILED_COPY in error.inner_text()
            assert "private upstream" not in page.locator("body").inner_text()

            page.locator('.sidebar [data-block="connection"] [data-action="connect-notion"]').click()
            page.locator('.sidebar [data-block="connection"] [data-field="connection-status"]').filter(has_text=CONNECTED_COPY).wait_for()
            assert provider.attempts == 2
            for width, height in ((800, 560), (900, 800)):
                page.set_viewport_size({"width": width, "height": height})
                menu = page.locator(".mobile-account-menu")
                assert menu.is_visible()
                menu.locator("summary").click()
                content = menu.locator(".mobile-account-content")
                bounds = content.bounding_box()
                assert bounds and bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= height
                assert content.evaluate("(el) => getComputedStyle(el).overflowY") == "auto"
                content.evaluate("(el) => el.scrollTop = el.scrollHeight")
                logout = menu.get_by_role("button", name=PANEL_COPY["account"]["logout"])
                assert logout.is_visible()
                logout_bounds = logout.bounding_box()
                assert logout_bounds and logout_bounds["y"] + logout_bounds["height"] <= height
                assert menu.locator("[data-redeem-form]").is_visible()
                menu.locator("summary").click()
            page.set_viewport_size({"width": 950, "height": 800})
            assert page.locator(".sidebar [data-block='quota']").is_visible()
        finally:
            browser.close()


@pytest.mark.skipif(not EDGE.exists(), reason="Microsoft Edge is required")
def test_recording_progress_keeps_unsaved_assignment_form_and_files():
    """A background catalog update must not erase homework the student is preparing."""
    import json

    script = (Path(__file__).parents[1] / "pku_sync" / "panel" / "static" / "app.js").read_text("utf-8")
    assert script.rstrip().endswith("})();")
    script = script.rstrip()[:-5] + (
        "window.__candidateTest = { state: state, render: render, poll: pollRecordingTask };})();"
    )
    posted = []

    def serve(route):
        url = route.request.url
        if url.endswith("/api/recordings/task"):
            route.fulfill(status=200, body=json.dumps({
                "state": "running", "kind": "catalog", "stage": "读取目录",
                "progress_done": 1, "progress_total": 10, "progress_unit": "items",
            }), content_type="application/json")
        elif url.endswith("/prepare"):
            posted.append(route.request.post_data_buffer)
            route.fulfill(status=400, body="{}", content_type="application/json")
        elif url.rstrip("/") == "http://panel.test":
            route.fulfill(status=200, body='<div id="app"></div><div id="toast"></div>', content_type="text/html")
        else:
            route.fulfill(status=404, body="{}", content_type="application/json")

    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=str(EDGE), headless=True)
        try:
            page = browser.new_page()
            page.route("http://panel.test/**", serve)
            page.goto("http://panel.test/")
            page.evaluate("(copy) => { window.PANEL_COPY = copy; }", PANEL_COPY)
            page.add_script_tag(content=script)
            page.wait_for_function("Boolean(window.__candidateTest)")
            page.evaluate("""() => {
                const s = window.__candidateTest.state;
                s.activated = true; s.quota = {active:true}; s.view = 'campus-course';
                s.update = {version:'0.1.17', status:'desktop_managed'};
                s.campus.courseId = 'course-one'; s.campus.configured = true;
                s.campus.course = {
                    id:'course-one', title:'测试课程', code:'TEST',
                    lessons:[{id:'recording-one', title:'第一讲', materials:[], duration_seconds:3600}],
                    unassigned_materials:[], announcements:[], announcement_tasks:[],
                    assignments:[{title:'第一次作业', content_id:'assignment-one', source:'assignment', files:[]}]
                };
                s.campus.submission.selectedId = 'assignment-one';
                s.recordings.task = {
                    state:'running', kind:'catalog', stage:'读取目录',
                    progress_done:0, progress_total:10, progress_unit:'items'
                };
                window.__candidateTest.render();
            }""")
            form = page.locator("#assignment-prepare-form")
            assert "启动后会检查更新" in page.locator('.sidebar [data-block="update"]').inner_text()
            assert page.locator('[data-action="request-update"]').count() == 0
            form.locator('[name="notion_url"]').fill("https://www.notion.so/example")
            form.locator('[name="attachments"]').set_input_files({
                "name": "answer.txt", "mimeType": "text/plain", "buffer": b"unique homework content"
            })
            form.locator('[name="archive_name"]').fill("student-homework.zip")
            page.evaluate("window.__candidateForm = document.querySelector('#assignment-prepare-form')")
            page.evaluate("window.__candidateTest.poll()")
            page.wait_for_function("window.__candidateTest.state.recordings.task.progress_done === 1")
            assert page.evaluate("window.__candidateForm === document.querySelector('#assignment-prepare-form')")
            assert "10%" in page.locator(".recording-task").inner_text()
            assert form.locator('[name="notion_url"]').input_value() == "https://www.notion.so/example"
            assert form.locator('[name="archive_name"]').input_value() == "student-homework.zip"
            assert page.evaluate("document.activeElement.name") == "archive_name"
            assert "answer.txt" in form.locator("[data-assignment-attachments]").inner_text()
            form.get_by_role("button", name="生成本地草稿并预检").click()
            page.wait_for_function("window.__candidateTest.state.campus.submission.error !== ''")
            assert len(posted) == 1
            assert b'unique homework content' in posted[0]
            form.locator('[name="submission_mode"]').select_option("single_file")
            form.locator('[name="attachments"]').set_input_files({
                "name": "answer.pdf", "mimeType": "application/pdf", "buffer": b"original single-file answer"
            })
            page.evaluate("window.__candidateTest.poll()")
            page.wait_for_function("window.__candidateTest.state.recordings.task.progress_done === 1")
            assert "answer.pdf" in form.locator("[data-assignment-attachments]").inner_text()
            form.get_by_role("button", name="生成本地草稿并预检").click()
            page.wait_for_function("window.__candidateTest.state.campus.submission.error !== ''")
            assert len(posted) == 2
            assert b'original single-file answer' in posted[1]
            assert b'single_file' in posted[1]
            page.evaluate("""() => {
                const s = window.__candidateTest.state;
                s.recordings.task = {state:'running', kind:'process', stage:'转写并生成笔记',
                    progress_done:480000, progress_total:1920000, progress_unit:'audio_extract'};
                window.__candidateTest.render();
            }""")
            assert "正在提取音轨" in page.locator(".recording-task").inner_text()
            assert "已处理 0:30 / 2:00 音频" in page.locator(".recording-task").inner_text()
            assert "剩余 1:30 音频" in page.locator(".recording-task").inner_text()
        finally:
            browser.close()


@pytest.mark.skipif(not EDGE.exists(), reason="Microsoft Edge is required")
def test_notion_preview_opens_browser_and_returns_to_dashboard():
    from pku_sync.panel.connection import build_fake_connection_service

    opened = []
    directory = build_fake_directory()
    app = create_app(
        settings=SimpleNamespace(data_dir="data", platform_token=""),
        runner=_NoopRunner(),
        directory_service=directory,
        connection_service=build_fake_connection_service(directory_service=directory),
        platform_service=FakePlatformBridge(activated=True),
        notion_opener=lambda url: opened.append(url) or True,
    )
    client = TestClient(app)

    def serve(route):
        request = route.request
        url = urlsplit(request.url)
        path = url.path + ("?" + url.query if url.query else "")
        if path == "/api/update":
            route.fulfill(status=404, body="{}", content_type="application/json")
            return
        response = client.request(
            request.method,
            path,
            content=request.post_data.encode() if request.post_data else None,
            headers={"content-type": request.headers.get("content-type", "application/json")},
        )
        route.fulfill(
            status=response.status_code,
            body=response.content,
            content_type=response.headers.get("content-type", "text/plain"),
        )

    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=str(EDGE), headless=True)
        try:
            page = browser.new_page()
            page.route("http://panel.test/**", serve)
            page.goto("http://panel.test/app")
            page.locator('[data-action="open-course"]').first.click()
            page.locator('[data-action="select-lecture"]').first.click()
            page.locator('[data-action="launch"][data-kind="lecture"]').first.click()
            page.get_by_text(PANEL_COPY["launch"]["opened_external"]).wait_for()
            assert len(opened) == 1
            assert opened[0].startswith("https://www.notion.so/")
            assert page.url == "http://panel.test/app"
            assert page.locator('[data-nav="dashboard"][aria-current="page"]').count() >= 1
            assert page.locator('[data-action="open-course"]').count() >= 1
        finally:
            browser.close()


@pytest.mark.skipif(not EDGE.exists(), reason="Microsoft Edge is required")
def test_sidebar_stays_put_while_right_content_scrolls():
    css = (Path(__file__).resolve().parents[1] / "pku_sync/panel/static/app.css").read_text(encoding="utf-8")
    markup = (
        "<style>" + css + "</style>"
        "<div class='app-shell'>"
        "<aside class='sidebar'><div class='sidebar-brand'>PKU</div><div class='sidebar-spacer'></div><div class='account-card'>Account</div></aside>"
        "<main class='main'><div class='main-inner' style='height: 2000px'>Course content</div></main>"
        "</div>"
    )
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=str(EDGE), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.set_content(markup)
            before = page.locator(".sidebar").bounding_box()
            page.mouse.move(800, 350)
            page.mouse.wheel(0, 500)
            page.wait_for_function("document.querySelector('.main').scrollTop > 0")
            after = page.locator(".sidebar").bounding_box()
            assert before["y"] == after["y"] == 0
            assert page.evaluate("document.scrollingElement.scrollTop") == 0
            assert page.locator(".sidebar").evaluate("(element) => element.scrollTop") == 0

            page.set_viewport_size({"width": 800, "height": 720})
            page.evaluate("window.scrollTo(0, 400)")
            assert page.evaluate("document.scrollingElement.scrollTop") > 0
            assert not page.locator(".sidebar").is_visible()
        finally:
            browser.close()


class _RedeemPlatform(FakePlatformBridge):
    def __init__(self):
        super().__init__(activated=True, email="student@example.com", email_verified=False, llm_points=0)
        self.seconds = 0
        self.used = False
        self.redeem_calls = 0

    def quota(self):
        result = super().quota()
        result["transcribe_seconds_remaining"] = self.seconds
        return result

    def redeem(self, code):
        self.redeem_calls += 1
        self._require_verified_for_metered()
        if code != "TEST1234" or self.used:
            error = PlatformError("兑换码无效或已使用")
            error.status_code = 400
            raise error
        self.used = True
        self.seconds += 120
        self._llm_points += 2.5
        return {"transcribe_seconds_remaining": self.seconds}


@pytest.mark.skipif(not EDGE.exists(), reason="Microsoft Edge is required")
def test_student_redeem_updates_both_balances_and_rejects_duplicate():
    from pku_sync.panel.connection import build_fake_connection_service

    platform = _RedeemPlatform()
    directory = build_fake_directory()
    app = create_app(
        settings=SimpleNamespace(data_dir="data", platform_token=""),
        runner=_NoopRunner(),
        directory_service=directory,
        connection_service=build_fake_connection_service(directory_service=directory),
        platform_service=platform,
    )
    client = TestClient(app)
    assert client.post("/api/platform/redeem", json={"code": "TEST1234"}).status_code == 403

    def serve(route):
        request = route.request
        url = urlsplit(request.url)
        path = url.path + ("?" + url.query if url.query else "")
        if path == "/api/update":
            route.fulfill(status=404, body="{}", content_type="application/json")
            return
        response = client.request(
            request.method,
            path,
            content=request.post_data.encode() if request.post_data else None,
            headers={"content-type": request.headers.get("content-type", "application/json")},
        )
        route.fulfill(
            status=response.status_code,
            body=response.content,
            content_type=response.headers.get("content-type", "text/plain"),
        )

    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=str(EDGE), headless=True)
        try:
            page = browser.new_page()
            page.route("http://panel.test/**", serve)
            page.goto("http://panel.test/app")
            page.locator(".sidebar").get_by_text(PANEL_COPY["account"]["unverified"], exact=True).wait_for()
            assert page.locator("#redeem-form").count() == 0
            platform.mark_verified()
            # The panel polls verification automatically; a manual refresh button can
            # disappear during Playwright's click action once the desired state arrives.
            page.locator("#redeem-form").wait_for(timeout=15000)
            page.locator("#code").fill("NOPE0000")
            page.locator('#redeem-form [data-action="redeem"]').click()
            page.get_by_text("兑换码无效或已使用", exact=True).wait_for()
            assert platform.seconds == 0

            page.locator("#code").fill("TEST1234")
            page.evaluate("""() => {
                const button = document.querySelector('#redeem-form [data-action="redeem"]');
                button.click();
                button.click();
            }""")
            page.get_by_text(PANEL_COPY["account"]["redeemed_toast"], exact=True).wait_for()
            assert platform.redeem_calls == 3
            quota = page.locator('.sidebar [data-block="quota"]')
            assert "2" in quota.inner_text()
            assert "2.5" in quota.inner_text()
            assert client.get("/api/platform/quota").json()["transcribe_seconds_remaining"] == 120

            page.locator("#code").fill("TEST1234")
            page.locator('#redeem-form [data-action="redeem"]').click()
            page.get_by_text("兑换码无效或已使用", exact=True).wait_for()
            assert platform.seconds == 120
        finally:
            browser.close()

@pytest.mark.skipif(not EDGE.exists(), reason="Microsoft Edge is required")
def test_course_recording_stays_metadata_only_until_user_confirms(tmp_path, monkeypatch):
    import json
    from pku_sync.config import Settings
    from pku_sync.models import Recording
    from pku_sync.panel.connection import build_fake_connection_service
    from pku_sync.panel import notion_home

    monkeypatch.setattr(notion_home, "available_parents", lambda _token: [{
        "id": "oauth-template", "title": "已授权父页面", "url": "https://www.notion.so/oauth-template"
    }])

    directory = build_fake_directory()
    notion_course = directory.load()["courses"][0]
    settings = Settings(_env_file=None, data_dir=tmp_path, pku_username="student",
                        pku_password="secret", platform_token="session", notion_token="notion")
    opened = []
    app = create_app(
        settings=settings, runner=_NoopRunner(), directory_service=directory,
        notion_opener=lambda url: (opened.append(url) or True),
        connection_service=build_fake_connection_service(),
        platform_service=FakePlatformBridge(activated=True),
    )
    client = TestClient(app)
    campus = tmp_path / "campus-course"
    (campus / "recordings").mkdir(parents=True)
    (campus / "course.json").write_text(
        json.dumps({"course_id": "campus-one", "name": notion_course["title"]}), "utf-8"
    )
    recording = Recording(course_id="campus-one", title="第1讲录像",
                          recorded_at="2026-09-24 09:00:00")
    (campus / "recordings" / "index.json").write_text(
        json.dumps([recording.model_dump()]), "utf-8"
    )
    (campus / "materials").mkdir()
    (campus / "materials" / "index.json").write_text(json.dumps([
        {"title": "第1讲课件", "kind": "文件", "path": "教学内容/第1讲课件",
         "files": ["slides.pdf"]},
        {"title": "补充阅读", "kind": "文件", "path": "资料/补充阅读", "files": []},
    ]), "utf-8")
    (tmp_path / "notion-learning-home.json").write_text(json.dumps({
        "id": "home", "parent_id": "oauth-template", "url": "https://notion.so/home"
    }), "utf-8")
    started = []
    catalog_calls = []
    app.state.recording_manager.start_catalog_sync = lambda: (catalog_calls.append("startup") or {
        "state": "done", "kind": "catalog", "stage": "Notion 目录已建立"
    })
    app.state.recording_manager.start_campus_process = lambda *args, **kwargs: (
        started.append((args, kwargs)) or {"state": "running", "kind": "process", "stage": "下载录像",
                                 "progress_done": 5242880, "progress_total": 10485760,
                                 "progress_unit": "bytes", "bytes_done": 5242880, "speed_bps": 10240}
    )

    def serve(route):
        request = route.request
        url = urlsplit(request.url)
        path = url.path + ("?" + url.query if url.query else "")
        if path == "/api/update":
            route.fulfill(status=404, body="{}", content_type="application/json")
            return
        response = client.request(
            request.method, path,
            content=request.post_data.encode() if request.post_data else None,
            headers={"content-type": request.headers.get("content-type", "application/json")},
        )
        route.fulfill(status=response.status_code, body=response.content,
                      content_type=response.headers.get("content-type", "text/plain"))

    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=str(EDGE), headless=True)
        try:
            page = browser.new_page()
            page.route("http://panel.test/**", serve)
            page.goto("http://panel.test/app")
            for _ in range(20):
                if catalog_calls: break
                page.wait_for_timeout(100)
            assert catalog_calls == ["startup"]
            page.locator('[data-action="open-campus-course"][data-course="campus-one"]').click()
            page.get_by_role("heading", name="第1讲录像").wait_for()
            assert page.locator(".material-file-link").count() == 1
            assert "slides.pdf" in page.locator(".material-file-link").inner_text()
            page.locator('[data-material-select="1"]').select_option(index=1)
            page.locator('[data-action="campus-material-assign"][data-material="1"]').click()
            page.locator(".recording-row").get_by_text("补充阅读").wait_for()
            assert started == []
            assert not list(tmp_path.rglob("video.mp4"))
            consent = page.locator('[data-recording-direct-oss]')
            assert consent.count() == 1 and not consent.is_checked()
            button = page.locator('[data-action="campus-process"]')
            button.wait_for()
            page.wait_for_function('!document.querySelector("[data-action=campus-process]").disabled')
            page.once("dialog", lambda dialog: dialog.dismiss())
            button.click()
            assert started == []
            consent.check()
            page.once("dialog", lambda dialog: dialog.accept())
            button.click()
            assert len(started) == 1
            assert started[0][0][1] == "campus-one"
            assert started[0][1]["direct_oss"] is True
            page.wait_for_function('!document.querySelector("[data-recording-direct-oss]").checked')
            assert not list(tmp_path.rglob("video.mp4"))
            page.locator(".recording-task progress[value='5242880']").wait_for()
            assert "50%" in page.locator(".recording-task").inner_text()
            assert "10.0 KB/秒" in page.locator(".recording-task").inner_text()

            from pku_sync.panel.recordings_api import collect_jobs, recording_id
            job = collect_jobs(tmp_path)[0]
            job.directory.mkdir(exist_ok=True)
            (job.directory / "transcript.json").write_text(json.dumps({
                "segments": [{"start": 0, "end": 10, "text": "课堂内容。"}]
            }, ensure_ascii=False), "utf-8")
            (job.directory / "notes.md").write_text("# 已有课堂笔记", "utf-8")
            (job.directory / "keyframes").mkdir()
            (job.directory / "keyframes" / "index.json").write_text(
                json.dumps({"keyframes": []}), "utf-8")
            page.reload()
            page.locator('[data-action="open-campus-course"][data-course="campus-one"]').click()
            publish = page.get_by_role("button", name="发布已有笔记")
            publish.wait_for()
            page.once("dialog", lambda dialog: dialog.accept())
            publish.click()
            assert len(started) == 2
            assert started[1][1]["reuse_existing"] is True
            assert started[1][1]["direct_oss"] is False
            assert not job.video.exists()

            (job.directory / "video.mp4").write_bytes(b"cached-video-for-review")
            (job.directory / "transcript.json").write_text(json.dumps({
                "segments": [{"start": 2425.0, "end": 2437.0,
                              "text": "这里介绍RAC算法和公钥。"}]
            }, ensure_ascii=False), "utf-8")
            (job.directory / "notes.md").write_text("# 待重整的旧笔记", "utf-8")
            (job.directory / "notion-publication.json").write_text(json.dumps({
                "recording_id": recording_id(job), "home_id": "home",
                "lecture_url": "https://www.notion.so/" + "a" * 32,
                "note_url": "https://app.notion.com/" + "b" * 32
            }), "utf-8")
            page.reload()
            page.locator('[data-action="open-campus-course"][data-course="campus-one"]').click()
            jump = page.locator('[data-action="campus-open-lecture"]')
            jump.wait_for()
            assert page.locator('[data-action="campus-process"]').count() == 0
            page.locator('[data-action="campus-review-term"]').click()
            panel = page.locator(".term-review-panel")
            panel.get_by_text("这里介绍RAC算法和公钥。").wait_for()
            assert panel.locator("video").get_attribute("src").endswith("/preview")
            assert panel.get_by_role("button", name="确认修正为 RSA").is_disabled()
            panel.locator("[data-term-review-confirm]").check()
            panel.get_by_role("button", name="确认修正为 RSA").click()
            page.get_by_text("转写已修正，请重新整理笔记。").wait_for()
            assert "RSA算法" in (job.directory / "transcript.json").read_text("utf-8")
            assert "RAC算法" in next(job.directory.glob("transcript.before-review-*.json")).read_text("utf-8")
            assert (job.directory / "notes.md").read_text("utf-8") == "# 待重整的旧笔记"
            jump.click()
            assert opened == ["https://app.notion.com/" + "b" * 32]
            page.get_by_role("heading", name="我的课程").wait_for()
        finally:
            browser.close()


@pytest.mark.skipif(not EDGE.exists(), reason="Microsoft Edge is required")
def test_materials_only_course_and_demo_notice_in_edge(tmp_path):
    import json
    from pku_sync.config import Settings
    from pku_sync.panel.connection import build_fake_connection_service

    folder = tmp_path / "materials-only"
    (folder / "recordings").mkdir(parents=True)
    (folder / "materials").mkdir()
    (folder / "recordings" / "index.json").write_text("[]", "utf-8")
    (folder / "course.json").write_text(json.dumps({
        "course_id": "_101540_1", "code": "LONG-COURSE-CODE-2026",
        "name": "只有课件的课程", "term": "2026 秋",
    }), "utf-8")
    (folder / "materials" / "index.json").write_text(json.dumps([{
        "title": "第一讲课件", "kind": "文件", "path": "资料/第一讲课件", "files": ["slides.pdf"]
    }]), "utf-8")
    (folder / "assignments").mkdir()
    (folder / "assignments" / "index.json").write_text(json.dumps([{
        "title": "第一次作业", "content_id": "_1714169_1",
        "due_at": "2026-10-01 23:30", "source": "content-tree",
        "instructions": "答案与代码打包提交", "files": []
    }]), "utf-8")
    directory = build_fake_directory()
    app = create_app(
        settings=Settings(_env_file=None, data_dir=tmp_path, pku_username="student",
                          pku_password="secret", platform_token="session"),
        runner=_NoopRunner(), directory_service=directory,
        connection_service=build_fake_connection_service(),
        platform_service=FakePlatformBridge(activated=True),
    )
    client = TestClient(app)
    def serve(route):
        request = route.request
        url = urlsplit(request.url)
        path = url.path + ("?" + url.query if url.query else "")
        if path == "/api/update":
            route.fulfill(status=404, body="{}", content_type="application/json")
            return
        response = client.request(
            request.method, path,
            content=request.post_data.encode() if request.post_data else None,
            headers={"content-type": request.headers.get("content-type", "application/json")},
        )
        route.fulfill(status=response.status_code, body=response.content,
                      content_type=response.headers.get("content-type", "text/plain"))

    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=str(EDGE), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1100, "height": 760})
            page.route("http://panel.test/**", serve)
            page.goto("http://panel.test/app")
            card = page.locator(".course-card")
            card.wait_for()
            assert "…1540_1" in card.inner_text()
            assert "_101540_1" not in card.inner_text()
            assert "LONG-COURSE-CODE-2026" not in card.inner_text()
            assert page.locator(".campus-setup .setup-steps li").count() == 4
            assert page.get_by_text("Notion 演示连接").count() == 0
            assert page.locator(".campus-setup").get_by_text("演示模式", exact=False).count() == 1
            card.get_by_role("button", name="查看课程").click()
            page.get_by_role("heading", name="课程内容").wait_for()
            assert page.get_by_text("这门课程暂时没有录像").is_visible()
            assert page.locator(".material-file-link").count() == 1
            assert page.locator('[data-action="campus-material-ignore"]').count() == 0
            assert page.locator("[data-recording-direct-oss]").count() == 0
            assert page.get_by_text("音频经 15 分钟").count() == 0
            assert "_101540_1" in page.locator(".course-hero").inner_text()
            page.get_by_role("button", name="准备作业文件").click()
            form = page.locator("#assignment-prepare-form")
            form.wait_for()
            form.locator('[name="archive_name"]').fill("学号姓名第1次作业.zip")
            form.locator('[name="attachments"]').set_input_files({
                "name": "答案.pdf", "mimeType": "application/pdf", "buffer": b"example answer"
            })
            page.get_by_role("button", name="生成本地草稿并预检").click()
            page.get_by_text("本地草稿：学号姓名第1次作业.zip").wait_for()
            assert page.get_by_text("答案.pdf", exact=False).count() >= 1
            page.get_by_role("button", name="确认提交到教学网").click()
            page.get_by_text("请先下载检查草稿文件，并勾选确认。").wait_for()
            page.get_by_role("button", name="准备作业文件").click()
            form = page.locator("#assignment-prepare-form")
            form.locator('[name="submission_mode"]').select_option("single_file")
            assert form.locator('[name="archive_name"]').count() == 0
            assert "不会转换页面格式" in form.inner_text()
            form.locator('[name="attachments"]').set_input_files({
                "name": "学号姓名第一次作业.pdf", "mimeType": "application/pdf", "buffer": b"original pdf bytes"
            })
            page.get_by_role("button", name="生成本地草稿并预检").click()
            page.get_by_text("本地草稿：学号姓名第一次作业.pdf").wait_for()
            assert page.get_by_role("link", name="下载并检查文件").is_visible()
        finally:
            browser.close()


@pytest.mark.skipif(not EDGE.exists(), reason="Microsoft Edge is required")
def test_handbook_requirements_visible_when_formal_assignment_index_is_empty(tmp_path, monkeypatch):
    import json
    from pku_sync.config import Settings
    from pku_sync.panel.connection import build_fake_connection_service
    from pku_sync.panel import handbook_requirements

    folder = tmp_path / "handbook-course"
    (folder / "materials" / "教学大纲").mkdir(parents=True)
    (folder / "assignments").mkdir()
    (folder / "course.json").write_text(json.dumps({
        "course_id": "campus-handbook", "name": "发展心理学", "assignments_status": "complete",
        "synced_at": "2026-09-28T10:00:00+08:00",
    }), "utf-8")
    (folder / "materials" / "index.json").write_text(json.dumps([{
        "title": "课程手册", "path": "教学大纲/课程手册", "files": ["学生手册.pdf"],
    }]), "utf-8")
    (folder / "materials" / "教学大纲" / "学生手册.pdf").write_bytes(b"synthetic")
    (folder / "assignments" / "index.json").write_text("[]", "utf-8")
    monkeypatch.setattr(handbook_requirements, "_pdf_pages", lambda path: [
        (6, "课程考核方式。小组作业需要文字报告与课堂展示，出勤须本人参加。" * 10 +
         "\n课堂出勤（3%）：包括集体授课和小班研讨的出勤。\n"
         "小测（4%×3）：集体授课前进行。\n课堂展示：各组需围绕文献研读。"),
        (7, "所有组最晚于 10 月 31 日 24 点前，将所选文献发至本班助教邮箱。\n" * 8),
        (8, "文字报告电子版、科普视频成片请于 12 月 28 日 24 点前发至小班助教邮箱。\n" * 8 +
         "考试形式：闭卷考试"),
    ])
    app = create_app(
        settings=Settings(_env_file=None, data_dir=tmp_path, pku_username="student",
                          pku_password="secret", platform_token="session"),
        runner=_NoopRunner(), directory_service=build_fake_directory(),
        connection_service=build_fake_connection_service(),
        platform_service=FakePlatformBridge(activated=True),
    )
    client = TestClient(app)

    def serve(route):
        request = route.request
        url = urlsplit(request.url)
        path = url.path + ("?" + url.query if url.query else "")
        if path == "/api/update":
            route.fulfill(status=404, body="{}", content_type="application/json")
            return
        response = client.request(request.method, path,
                                  content=request.post_data.encode() if request.post_data else None,
                                  headers={"content-type": request.headers.get("content-type", "application/json")})
        route.fulfill(status=response.status_code, body=response.content,
                      content_type=response.headers.get("content-type", "text/plain"))

    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=str(EDGE), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1100, "height": 1700})
            page.route("http://panel.test/**", serve)
            page.goto("http://panel.test/app")
            card = page.locator(".course-card")
            card.wait_for()
            assert "0 项教学网正式作业索引" in card.inner_text()
            assert "1 份课程手册待核对" in card.inner_text()
            card.get_by_role("button", name="查看课程").click()
            section = page.locator("#course-assignments")
            assert "教学网正式作业索引当前为 0 项" in section.inner_text()
            assert "课程手册中的待核对要求" in section.inner_text()
            assert "10 月 31 日" in section.inner_text()
            assert "12 月 28 日" in section.inner_text()
            assert "笔记无法代替的参与事项" in section.inner_text()
            assert "出勤 · PDF 第 6 页" in section.inner_text()
            assert "小测 · PDF 第 6 页" in section.inner_text()
            assert "课堂展示 · PDF 第 6 页" in section.inner_text()
            assert "考试 · PDF 第 8 页" in section.inner_text()
            assert section.locator("details[open]").count() == 0
            import os
            screenshot = os.environ.get("PKU_HANDBOOK_SCREENSHOT")
            if screenshot:
                section.screenshot(path=screenshot)
            section.locator("details summary").filter(has_text="PDF 第 6 页").click()
            assert "文字报告与课堂展示" in section.inner_text()
            assert section.get_by_role("button", name="准备作业文件").count() == 0
            assert "/materials/0/files/0" in section.get_by_role(
                "link", name="下载课程手册 PDF（核对原页）").get_attribute("href")
        finally:
            browser.close()


@pytest.mark.skipif(not EDGE.exists(), reason="Microsoft Edge is required")
def test_catalog_progress_and_course_summary_actions_in_edge():
    """Progress shows the real stage and remaining work; summaries are one click away."""
    import json

    script = (Path(__file__).parents[1] / "pku_sync" / "panel" / "static" / "app.js").read_text("utf-8")
    assert script.rstrip().endswith("})();")
    script = script.rstrip()[:-5] + (
        "window.__candidateTest = { state: state, render: render, poll: pollRecordingTask };})();"
    )
    posted = []

    def serve(route):
        url = route.request.url
        if url.endswith("/api/campus/courses/course-one/summary"):
            posted.append(route.request.method)
            route.fulfill(status=200, body=json.dumps({
                "state": "running", "kind": "summary", "stage": "生成课程总结：共 1 门课程",
                "stage_index": 1, "stage_total": 2,
                "progress_done": 0, "progress_total": 1, "progress_unit": "courses",
                "progress_remaining": 1, "progress_percent": 0, "current_item": "测试课程",
            }), content_type="application/json")
        elif url.rstrip("/") == "http://panel.test":
            route.fulfill(status=200, body='<div id="app"></div><div id="toast"></div>',
                          content_type="text/html")
        else:
            route.fulfill(status=404, body="{}", content_type="application/json")

    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=str(EDGE), headless=True)
        try:
            page = browser.new_page()
            page.route("http://panel.test/**", serve)
            page.on("dialog", lambda dialog: dialog.accept())
            page.goto("http://panel.test/")
            page.evaluate("(copy) => { window.PANEL_COPY = copy; }", PANEL_COPY)
            page.add_script_tag(content=script)
            page.wait_for_function("Boolean(window.__candidateTest)")
            page.evaluate("""() => {
                const s = window.__candidateTest.state;
                s.activated = true; s.quota = {active:true}; s.view = 'campus-course';
                s.connection.connected = true;
                s.campus.courseId = 'course-one'; s.campus.configured = true;
                s.campus.home = {id:'home', url:'https://www.notion.so/home'};
                s.campus.course = {
                    id:'course-one', title:'测试课程', code:'TEST',
                    lessons:[{id:'recording-one', title:'第一讲', materials:[], duration_seconds:3600}],
                    unassigned_materials:[], announcements:[], announcement_tasks:[],
                    assignments:[], all_materials:[], summary:{}
                };
                s.recordings.task = {
                    state:'running', kind:'catalog', stage:'建立 Notion 课程与讲次页',
                    stage_index:1, stage_total:2,
                    progress_done:41, progress_total:110, progress_unit:'items',
                    progress_remaining:69, progress_percent:37,
                    current_item:'计算机网络 · 作业：第一次书面作业'
                };
                window.__candidateTest.render();
            }""")
            box = page.locator(".recording-task")
            text = box.inner_text()
            assert "第 1/2 步" in text
            assert "37% · 已完成 41 / 110 项 · 剩余 69 项 · 计算机网络 · 作业：第一次书面作业" in text
            assert box.locator("progress").get_attribute("value") == "41"
            assert box.locator("progress").get_attribute("max") == "110"

            # A finished summary task offers the snapshot itself, not the lecture note.
            page.evaluate("""() => {
                const s = window.__candidateTest.state;
                s.recordings.task = {
                    state:'done', kind:'summary',
                    stage:'课程总结已生成：新建 1 页，当日已有 0 页未重复写入',
                    stage_index:2, stage_total:2,
                    result_url:'https://www.notion.so/summary-page',
                    result_urls:['https://www.notion.so/summary-page'],
                    warnings:['《计算机网络》：Notion 拒绝写入'],
                    progress_done:1, progress_total:1, progress_unit:'courses',
                    progress_remaining:0, progress_percent:100, current_item:'计算机网络'
                };
                window.__candidateTest.render();
            }""")
            assert "打开课程总结" in box.inner_text()
            assert "Notion 拒绝写入" in box.inner_text()
            assert box.locator(
                '[data-action="recording-open-result"][data-url="https://www.notion.so/summary-page"]'
            ).count() == 1

            # Unexpected failures expose a support reference without leaking internals.
            page.evaluate("""() => {
                const s = window.__candidateTest.state;
                s.recordings.task = {
                    state:'failed', kind:'process', stage:'将课堂画面和笔记发布到 Notion',
                    error:'写入 Notion 时中断；本机已有转写和笔记会保留，请检查 Notion 连接后重试。',
                    failure_id:'A1B2C3D4'
                };
                window.__candidateTest.render();
            }""")
            assert "写入 Notion 时中断" in box.inner_text()
            assert "故障编号：A1B2C3D4" in box.inner_text()

            # The course page keeps a permanent way to generate or reopen it.
            assert page.locator('[data-action="campus-course-summary"]').count() == 1
            assert page.get_by_role("button", name="生成全部课程总结").count() == 1
            page.evaluate("""() => {
                const s = window.__candidateTest.state;
                s.campus.course.summary = {url:'https://www.notion.so/summary-page',
                                           title:'测试课程｜课程总结（截至 2026-09-30）'};
                window.__candidateTest.render();
            }""")
            assert page.locator('[data-action="campus-open-summary"]').count() == 1
            assert page.get_by_role("button", name="重新生成课程总结").count() == 1

            page.evaluate("""() => {
                const s = window.__candidateTest.state;
                s.campus.course.summary = {};
                s.recordings.task = {state:'idle'};
                window.__candidateTest.render();
            }""")
            page.get_by_role("button", name="生成课程总结").click()
            page.wait_for_function("window.__candidateTest.state.recordings.task.kind === 'summary'")
            assert posted == ["POST"]
            assert "第 1/2 步" in page.locator(".recording-task").inner_text()

            # A background course refresh must not replace the long course
            # page with the short loading placeholder and clamp scroll to 0.
            page.evaluate("""() => {
                const s = window.__candidateTest.state;
                s.recordings.task = {state:'idle'};
                s.campus.loading = false;
                const style = document.createElement('style');
                style.textContent = '.app-shell{height:400px;display:block}.main{height:120px;overflow-y:auto}';
                document.head.appendChild(style);
                window.__candidateTest.render();
                document.querySelector('.main').scrollTop = 320;
                s.campus.loading = true;
                window.__candidateTest.render();
            }""")
            assert page.locator(".course-hero").count() == 1
            assert page.locator(".main").evaluate("(el) => el.scrollTop") == 320

            # The narrow layout scrolls the document rather than .main.
            page.set_viewport_size({"width": 600, "height": 700})
            page.evaluate("""() => {
                const s = window.__candidateTest.state;
                document.body.style.minHeight = '1600px';
                window.scrollTo(0, 280);
                s.campus.loading = true;
                window.__candidateTest.render();
            }""")
            assert page.evaluate("window.scrollY") == 280
        finally:
            browser.close()
