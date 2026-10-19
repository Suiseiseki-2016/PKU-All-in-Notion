"""Real keyboard, pending requests and setup layouts with isolated fake services."""
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from browser_support import EDGE, browser_launch_options
from pku_sync.panel.student_ui import PANEL_COPY, student_page

playwright = pytest.importorskip("playwright.sync_api")
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
            route.request.frame.evaluate("count => {window.__testPostsCaptured = count;}", len(posted))
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
        page.add_init_script("localStorage.setItem('pku-panel-welcome-v1', 'seen')")
        page.route("http://panel.test/**", serve)
        page.goto("http://panel.test/app")
        page.locator("#auth-email").wait_for()
        yield page, held, posted
        browser.close()


@pytest.mark.parametrize("width", [390, 800, 1100, 1280])
def test_auth_mode_tabs_switch_and_fit_viewport(ui, width):
    page, _, _ = ui
    page.set_viewport_size({"width": width, "height": 850})
    tabs = page.locator(".auth-mode-tabs")
    login = tabs.locator('[data-action="auth-mode-login"]')
    register = tabs.locator('[data-action="auth-mode-register"]')
    playwright.expect(page.locator("#auth-form")).to_have_attribute("data-auth-mode", "login")
    playwright.expect(page.locator(".card-subtitle")).to_have_text(PANEL_COPY["auth"]["login"]["subtitle"])
    assert page.locator("#auth-title").evaluate("el => el.previousElementSibling.matches('.auth-mode-tabs')")
    page.locator("#auth-email").fill("fixture@example.com")
    for mode, active, inactive in (("login", login, register), ("register", register, login), ("login", login, register)):
        if mode != page.locator("#auth-form").get_attribute("data-auth-mode"):
            active.click()
        playwright.expect(active).to_have_text(PANEL_COPY["auth"]["modes"][mode])
        playwright.expect(active).to_have_attribute("aria-pressed", "true")
        assert "active" in active.get_attribute("class").split()
        playwright.expect(inactive).to_have_attribute("aria-pressed", "false")
        assert "active" not in inactive.get_attribute("class").split()
        assert page.locator("#auth-email").input_value() == "fixture@example.com"
        playwright.expect(page.locator("#auth-password-confirm")).to_have_count(1 if mode == "register" else 0)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert tabs.evaluate("el => el.scrollWidth <= el.clientWidth")
    page.locator('.auth-switches [data-action="auth-mode-forgot"]').click()
    playwright.expect(tabs).to_have_count(0)
    page.locator('.auth-switches [data-action="auth-mode-login"]').click()
    playwright.expect(tabs).to_be_visible()


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
    for tab in page.locator(".auth-mode-tabs button").all():
        playwright.expect(tab).to_be_disabled()
    page.wait_for_function("window.__testPostsCaptured === 1")
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
    page.wait_for_function("window.__testPostsCaptured === 2")
    held.pop().abort()
    playwright.expect(page.locator("#auth-help")).to_have_text(PANEL_COPY["auth"]["network_failed"])
    playwright.expect(page.locator('[data-action="auth-submit"]')).to_be_enabled()
    page.locator('.auth-mode-tabs [data-action="auth-mode-register"]').click()
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
    page.wait_for_function("window.__testPostsCaptured === 1")
    page.evaluate("document.getElementById('recording-credentials-form').requestSubmit()")
    assert len(posted) == 1
    held.pop().fulfill(status=401, body=json.dumps({"detail": "请检查账号和密码"}), content_type="application/json")
    playwright.expect(page.locator('#recording-credentials-form [role="alert"]')).to_have_text("请检查账号和密码")
    assert page.locator("#recording-password").input_value() == "wrong"
    assert page.evaluate("window.__uiTest.state.campus.configured") is False
    page.locator("#recording-password").fill("correct")
    button.click()
    playwright.expect(button).to_be_disabled()
    page.wait_for_function("window.__testPostsCaptured === 2")
    held.pop().fulfill(body='{"configured":true,"verified":true}', content_type="application/json")
    playwright.expect(page.locator("#recording-credentials-form")).to_have_count(0)
    assert page.evaluate("window.__uiTest.state.campus.configured") is True


def test_connection_setup_uses_local_workspace_and_fits_viewport(ui):
    page, held, posted = ui
    signed_in_setup(page)
    playwright.expect(page.get_by_role("heading", name="连接设置", exact=True)).to_be_visible()
    playwright.expect(page.locator("#recording-credentials-form")).to_be_visible()
    playwright.expect(page.locator(".main")).to_contain_text("资料与笔记保存在本机")
    # 0.1.63 no longer asks students to create a Notion destination.
    assert page.locator(".home-setup, #home-parent, [data-action=campus-create-workspace]").count() == 0
    assert page.locator('.main [data-action="connect-notion"]').count() == 0
    page.locator("#recording-username").fill("fixture-student")
    for width in (1100, 800, 390):
        page.set_viewport_size({"width": width, "height": 760})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        playwright.expect(page.locator("#recording-username")).to_have_value("fixture-student")
        playwright.expect(page.locator('[data-action="recording-save-credentials"]')).to_be_visible()
    assert held == posted == []
