"""Real keyboard, pending requests and setup layouts with isolated fake services."""
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from browser_support import browser_launch_options
from pku_sync.panel.student_ui import PANEL_COPY, student_page

playwright = pytest.importorskip("playwright.sync_api")
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
pytestmark = pytest.mark.skipif(not EDGE.exists(), reason="Microsoft Edge required")


@pytest.fixture
def ui():
    script = (Path(__file__).parents[1] / "pku_sync/panel/static/app.js").read_text("utf-8")
    script = script.rstrip()[:-5] + "window.__uiTest={state:state,render:render,submitAuth:submitAuth};})();"
    css = (Path(__file__).parents[1] / "pku_sync/panel/static/app.css").read_text("utf-8")
    held, posted = [], []
    signed_in = [False]
    def serve(route):
        path = urlsplit(route.request.url).path
        if route.request.method == "POST":
            posted.append((path, route.request.post_data_json))
            held.append(route)
            return
        bodies = {"/api/platform/quota": {"active": signed_in[0], "email": "fixture@example.com", "email_verified": True},
                  "/api/campus/courses": {"courses": [], "campus_configured": False},
                  "/api/recordings/task": {"state": "idle"},
                  "/api/connection": {"connected": False, "state": "disconnected"}}
        if path == "/app":
            route.fulfill(body=student_page(), content_type="text/html")
        elif path == "/app/app.js":
            route.fulfill(body=script, content_type="application/javascript")
        elif path == "/app/app.css":
            route.fulfill(body=css, content_type="text/css")
        else:
            route.fulfill(status=200, body=json.dumps(bodies.get(path, {})), content_type="application/json")
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(**browser_launch_options(), headless=True)
        page = browser.new_page(viewport={"width": 1100, "height": 760})
        page.route("http://panel.test/**", serve)
        page.goto("http://panel.test/app")
        page.locator("#auth-email").wait_for()
        yield page, held, posted
        browser.close()


def test_enter_pending_error_and_rerender_preserve_login_inputs(ui):
    page, held, posted = ui
    page.locator("#auth-email").fill("fixture@example.com")
    page.locator("#auth-password").fill("fixture-password")
    page.locator("#auth-password").focus()
    page.locator("#auth-password").evaluate("el => el.dataset.originalField = 'yes'")
    page.evaluate("window.__uiTest.render()")
    assert page.locator("#auth-password").get_attribute("data-original-field") == "yes"
    assert page.locator("#auth-password").input_value() == "fixture-password"
    assert page.locator("#auth-password").evaluate("el => el === document.activeElement")
    page.locator("#auth-password").press("Enter")
    playwright.expect(page.locator('[data-action="auth-submit"]')).to_have_text("正在登录…")
    playwright.expect(page.locator('[data-action="auth-submit"]')).to_be_disabled()
    assert posted == [("/api/auth/login", {"email": "fixture@example.com", "password": "fixture-password"})]
    page.evaluate("document.getElementById('auth-form').requestSubmit()")
    assert len(posted) == 1
    held.pop().fulfill(status=401, body=json.dumps({"detail": "邮箱或密码不正确"}), content_type="application/json")
    playwright.expect(page.locator("#auth-help")).to_have_text("邮箱或密码不正确")
    playwright.expect(page.locator('[data-action="auth-submit"]')).to_be_enabled()
    assert page.locator("#auth-email").input_value() == "fixture@example.com"
    assert page.locator("#auth-password").input_value() == "fixture-password"
    page.locator("#auth-password").press("Enter")
    playwright.expect(page.locator('[data-action="auth-submit"]')).to_be_disabled()
    held.pop().abort()
    playwright.expect(page.locator("#auth-help")).to_have_text(PANEL_COPY["auth"]["network_failed"])
    playwright.expect(page.locator('[data-action="auth-submit"]')).to_be_enabled()
    page.locator('[data-action="auth-mode-register"]').click()
    assert page.locator("#auth-email").input_value() == "fixture@example.com"
    assert page.locator("#auth-password").input_value() == ""


def signed_in_setup(page):
    page.evaluate("""() => {let s=window.__uiTest.state;s.activated=true;
      s.quota={active:true,email:'fixture@example.com',email_verified:true};
      s.view='campus-account';s.connection={connected:true,state:'connected'};
      s.campus.parentError='';
      s.campus.parents=[{id:'parent-a',title:'我的笔记'},{id:'parent-b',title:'课程资料'}];
      s.campus.parentId='parent-a';window.__uiTest.render();}""")


def test_campus_enter_validates_before_saved_and_retains_failed_inputs(ui):
    page, held, posted = ui
    signed_in_setup(page)
    page.locator("#recording-username").fill("fixture-student")
    page.locator("#recording-password").fill("wrong")
    page.locator("#recording-password").press("Enter")
    button = page.locator('[data-action="recording-save-credentials"]')
    playwright.expect(button).to_have_text("正在验证…")
    playwright.expect(button).to_be_disabled()
    page.evaluate("document.getElementById('recording-credentials-form').requestSubmit()")
    assert len(posted) == 1
    held.pop().fulfill(status=401, body=json.dumps({"detail": "请检查账号和密码"}), content_type="application/json")
    playwright.expect(page.locator('#recording-credentials-form [role="alert"]')).to_have_text("请检查账号和密码")
    assert page.locator("#recording-password").input_value() == "wrong"
    assert page.evaluate("window.__uiTest.state.campus.configured") is False
    page.locator("#recording-password").fill("correct")
    button.click()
    playwright.expect(button).to_be_disabled()
    held.pop().fulfill(body='{"configured":true,"verified":true}', content_type="application/json")
    playwright.expect(page.locator("#recording-credentials-form")).to_have_count(0)
    assert page.evaluate("window.__uiTest.state.campus.configured") is True


def test_home_setup_destination_pending_and_ready_layout(ui, tmp_path):
    page, held, posted = ui
    signed_in_setup(page)
    page.locator("#home-parent").select_option("parent-b")
    playwright.expect(page.locator(".home-setup-footer")).to_contain_text("保存到「课程资料」下面")
    page.locator(".home-setup").screenshot(path=str(tmp_path / "home-setup.png"))
    page.locator('[data-action="campus-create-workspace"]').click()
    playwright.expect(page.locator('[data-action="campus-create-workspace"]')).to_have_text("正在创建并验证…")
    assert posted[-1] == ("/api/campus/notion/workspace", {"parent_page_id": "parent-b"})
    held.pop().fulfill(status=503, body=json.dumps({"detail": "Notion 暂时无法连接"}), content_type="application/json")
    playwright.expect(page.locator(".home-setup-error")).to_have_text("Notion 暂时无法连接")
    assert page.locator("#home-parent").input_value() == "parent-b"
    page.locator('[data-action="campus-create-workspace"]').click()
    playwright.expect(page.locator('[data-action="campus-create-workspace"]')).to_be_disabled()
    held.pop().fulfill(body=json.dumps({"id": "home", "parent_id": "parent-b", "active": True, "schema": 2, "url": "https://www.notion.so/fixture"}), content_type="application/json")
    playwright.expect(page.locator(".home-ready-badge")).to_have_text("✓ 学习主页已就绪")
    for width in (1100, 800, 390):
        page.set_viewport_size({"width": width, "height": 760})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
