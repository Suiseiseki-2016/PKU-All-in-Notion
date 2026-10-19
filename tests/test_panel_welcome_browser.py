"""First-launch guidance and the released student navigation, without live services."""
from pathlib import Path

import pytest

from browser_support import browser_launch_options
from pku_sync.panel.student_ui import PANEL_COPY

playwright = pytest.importorskip("playwright.sync_api")
STATIC = Path(__file__).parents[1] / "pku_sync/panel/static"


@pytest.mark.parametrize("width", [390, 1280])
def test_direct_login_and_released_navigation(width, tmp_path):
    script = (STATIC / "app.js").read_text("utf-8").rstrip()
    script = script[:-5] + "window.__uiTest = {state:state, render:render};})();"
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(**browser_launch_options(), headless=True)
        try:
            page = browser.new_page(viewport={"width": width, "height": 850})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def serve(route):
                if route.request.url == "http://panel.test/":
                    route.fulfill(body='<div id="app"></div><div id="toast"></div>', content_type="text/html")
                else:
                    route.fulfill(status=404, body="{}", content_type="application/json")

            page.route("http://panel.test/**", serve)

            def load():
                page.goto("http://panel.test/")
                page.evaluate("copy => window.PANEL_COPY = copy", PANEL_COPY)
                page.add_style_tag(content=(STATIC / "app.css").read_text("utf-8"))
                page.add_script_tag(content=script)

            load()
            playwright.expect(page.locator("#auth-form")).to_be_visible()
            playwright.expect(page.locator('.auth-switches button')).to_have_count(1)
            playwright.expect(page.locator('.auth-switches')).to_contain_text('忘记密码')
            playwright.expect(page.get_by_text('使用引导', exact=True)).to_have_count(0)
            playwright.expect(page.locator('.welcome-screen')).to_have_count(0)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(tmp_path / f"login-{width}.png"), full_page=True)
            load()
            playwright.expect(page.locator("#auth-form")).to_be_visible()
            # Let initial failed reads finish before injecting a cached course.
            page.wait_for_function("window.__uiTest.state.quota === null")
            page.evaluate("""() => {
                const s = window.__uiTest.state;
                s.activated = true; s.quota = {active:true, email_verified:true};
                s.view = 'campus-course'; s.campus.configured = true;
                s.campus.courseId = 'test'; s.campus.section = 'quizzes';
                s.campus.course = {id:'test', title:'测试课程', lessons:[],
                    unassigned_materials:[], assignments:[], announcements:[],
                    announcement_tasks:[], all_materials:[]};
                window.__uiTest.render();
            }""")
            playwright.expect(page.locator('.campus-centers a')).to_have_count(3)
            playwright.expect(page.locator('[data-center="quizzes"]')).to_have_count(0)
            playwright.expect(page.locator('#course-quizzes')).to_have_count(0)
            assert '小测' not in page.locator('main').inner_text()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            if width == 390:
                page.locator('.mobile-account-menu > summary').click()
                page.locator('.mobile-account-menu [data-action="campus-account-settings"]').click()
                playwright.expect(page.get_by_role('heading', name='连接设置', exact=True)).to_be_visible()
                assert not page.locator('.mobile-account-menu').evaluate('(element) => element.open')
                assert page.locator('.course-card').count() == 0
            assert not errors
        finally:
            browser.close()
