"""Edge stress coverage using the production JS/CSS and deterministic API responses."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from browser_support import EDGE
from pku_sync.panel.student_ui import PANEL_COPY

pw = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.skipif(not EDGE.is_file(), reason="Microsoft Edge is required")
STATIC = Path(__file__).parents[1] / "pku_sync/panel/static"


@pytest.fixture
def panel():
    courses = []
    for i, title in enumerate(("CNS解剖", "中苏关系及其对中国社会发展的影响", "信息安全引论")):
        materials = [dict(index=n, title=f"待关联资料 {n}", category="material",
                          files=[f"lecture_{n}_" + "long_filename_" * 5 + ".pdf"])
                     for n in range(33)]
        courses.append(dict(
            id=f"course-{i}", title=title + "(26-27学年第1学期)", code=f"TEST-{i}",
            recordings=8, assignments=[dict(title=f"作业 {n}", content_id=f"assignment-{n}",
                                           source="assignment", instructions="请阅读教材并提交报告", files=[])
                                          for n in range(4)],
            announcements=[dict(title=f"通知 {n}", body_text="课程安排已更新") for n in range(4)],
            announcement_tasks=[], unassigned_materials=materials, all_materials=materials,
            lessons=[dict(id=f"lesson-{i}-{n}", title="2026-09-10第7-8节", date="2026-09-10",
                          materials=[], transcript_available=True, notes_available=True,
                          video_available=True) for n in range(8)], summary={}))
    home = dict(id="home", schema=2, url="https://www.notion.so/home")
    mutations = []
    errors = []

    def serve(route):
        path = urlsplit(route.request.url).path
        if path == "/":
            route.fulfill(content_type="text/html; charset=utf-8", body='<!doctype html><html><head><meta charset="utf-8">'
                          '<meta name="viewport" content="width=device-width, initial-scale=1">'
                          '<link rel="stylesheet" href="/app.css"></head><body>'
                          '<div id="app"></div><div id="toast"></div>'
                          '<script>window.PANEL_COPY=' + json.dumps(PANEL_COPY) + ';</script>'
                          '<script src="/app.js"></script></body></html>')
            return
        if path in ("/app.css", "/app.js"):
            route.fulfill(path=str(STATIC / path[1:]), content_type=(
                "text/css" if path.endswith("css") else "application/javascript"))
            return
        body = {}
        if path == "/api/platform/quota":
            body = dict(active=True, available=True, email="stress@example.com", email_verified=True)
        elif path == "/api/auth/me":
            body = dict(available=True, email="stress@example.com", email_verified=True)
        elif path == "/api/update":
            body = dict(version="0.1.17", status="desktop_managed")
        elif path == "/api/connection":
            body = dict(connected=True, state="connected")
        elif path == "/api/recordings/task":
            body = dict(state="idle")
        elif path == "/api/campus/notion/parents":
            body = dict(pages=[], home=home)
        elif path == "/api/campus/courses":
            body = dict(campus_configured=True, notion_home=home, courses=[
                dict(c, assignments=len(c["assignments"]), announcements=len(c["announcements"]))
                for c in courses])
        elif path.endswith("/match"):
            mutations.append(json.loads(route.request.post_data))
            course = next(c for c in courses if c["id"] == path.split("/")[4])
            index = int(path.split("/")[-2])
            course["unassigned_materials"][index]["match_state"] = "ignored"
            body = course
        elif path.startswith("/api/campus/courses/"):
            body = next((c for c in courses if c["id"] == path.split("/")[4]), {})
        route.fulfill(json=body)

    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=str(EDGE), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            page.add_init_script("localStorage.setItem('pku-panel-welcome-v1', 'seen')")
            page.route("http://panel.test/**", serve)
            page.goto("http://panel.test/")
            pw.expect(page.locator(".course-card")).to_have_count(3)
            yield page, errors, mutations
            assert errors == []
        finally:
            browser.close()


def action(page, name):
    page.locator(f'.main [data-action="{name}"]').first.click()


def center(page, name):
    # Centers are reached from the course home; the settings button also stays on materials.
    action(page, "campus-course-home")
    page.locator(f'.main [data-center="{name}"]').click()


def test_rapid_navigation_and_card_click_stability_in_edge(panel):
    page, errors, mutations = panel
    for i in range(12):
        card = page.locator(f'[data-course-card="course-{i % 3}"]')
        # Disclosure and its text never navigate the enclosing card.
        card.locator("summary").click()
        pw.expect(page.locator(".course-card")).to_have_count(3)
        card.locator("h3").click()
        pw.expect(page.locator(".campus-lesson-page")).to_have_count(8)
        page.locator(".campus-lesson-page h3").first.click()
        pw.expect(page.locator(".recording-row")).to_have_count(1)
        for name in ("assignments", "notices", "materials"):
            center(page, name)
            pw.expect(page.locator(".main h1")).to_have_count(1)
        disclosures = page.locator(".material-match-disclosure")
        pw.expect(disclosures).to_have_count(33)
        assert disclosures.locator("[data-action=campus-material-ignore]:visible").count() == 0
        disclosures.first.locator("summary").click()
        pw.expect(disclosures.first.locator("select")).to_be_visible()
        disclosures.first.locator("select").select_option(f"lesson-{i % 3}-0")
        if i == 11:
            disclosures.first.locator("[data-action=campus-material-ignore]").click()
            pw.expect(page.locator("[data-action=campus-material-restore]")).to_have_count(1)
            assert mutations == [dict(lecture_id="", ignore=True)]
        assert page.get_by_role("button", name="课程设置", exact=True).count() == 0
        assert 'Notion' not in page.locator('body').inner_text()
        action(page, "open-dashboard")
        pw.expect(page.locator(".course-card")).to_have_count(3)
        assert errors == []


def test_multi_viewport_zero_horizontal_overflow_and_button_hierarchy(panel, tmp_path):
    page, errors, _ = panel

    def no_overflow():
        dimensions = page.evaluate("""() => ({width: innerWidth, scroll: document.documentElement.scrollWidth,
            offenders: [...document.querySelectorAll('.main *')].filter(el => el.getClientRects().length &&
                el.getBoundingClientRect().right > innerWidth + 1).map(el => el.className).slice(0, 8)})""")
        assert dimensions["scroll"] <= dimensions["width"], dimensions
        assert page.locator(".main").evaluate("el => el.scrollWidth <= el.clientWidth")

    for width, height in ((1440, 900), (1024, 768), (768, 1024), (390, 844), (320, 568)):
        page.set_viewport_size(dict(width=width, height=height))
        no_overflow()
        card = page.locator(".course-card").first
        pw.expect(card.get_by_role("button", name="查看课程")).to_be_visible()
        assert card.locator(".course-card-footer button").count() == 1
        assert card.locator("h3").text_content().endswith("(26-27学年第1学期)")
        if width in (1440, 320):
            page.screenshot(path=str(tmp_path / f"dashboard-{width}.png"), full_page=True)
        card.locator("h3").click()
        pw.expect(page.locator(".campus-lesson-page")).to_have_count(8)
        no_overflow()
        page.locator(".campus-lesson-page h3").first.click()
        pw.expect(page.locator(".recording-row")).to_have_count(1)
        no_overflow()
        pw.expect(page.locator('[data-action="campus-preview-lecture-note"]')).to_have_text("收起本讲总结")
        assert page.locator(".recording-primary-bar .btn-primary").count() == 0
        if width in (1440, 320):
            page.screenshot(path=str(tmp_path / f"lecture-{width}.png"), full_page=True)
        pw.expect(page.locator(".recording-secondary-bar [data-action=campus-estimate-duration]")).to_be_visible()
        for name in ("assignments", "materials"):
            center(page, name)
            no_overflow()
            if name == "assignments":
                pw.expect(page.locator("[data-action=campus-prepare-assignment]").first).to_be_visible()
                pw.expect(page.locator("[data-action=campus-assignment-status]").first).to_be_visible()
            else:
                page.locator(".material-match-disclosure summary").first.click()
                no_overflow()
        action(page, "open-dashboard")
        pw.expect(page.locator(".course-card")).to_have_count(3)
        assert errors == []
