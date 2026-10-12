"""Developer renderer checks only; actual installed evidence is separate."""
import json
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from pku_sync.panel.student_ui import PANEL_COPY

ROOT = Path(__file__).resolve().parents[1]
EDGE = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")

@pytest.fixture
def renderer():
    script = (ROOT/"pku_sync/panel/static/app.js").read_text("utf-8")
    script = script[:script.index("  window.setInterval(refreshAccount, 8000);")] + (
        "window.__quotaTest={state:state,render:render,poll:pollRecordingTask,logout:logoutAccount,redeem:redeem};})();")
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=str(EDGE),headless=True)
        page = browser.new_page()
        controls = {"terminal":"failed","quota_mode":"success","held":None,
                    "redeem_mode":"success","held_redeem":None,"redeem_posts":[]}
        requests = []
        def serve(route):
            path = route.request.url.split("quota.test")[-1]
            requests.append(path)
            if path == "/":
                route.fulfill(body='<div id="app"></div><div id="toast"></div>',content_type="text/html")
                return
            body = {}
            status = 200
            if path == "/api/platform/redeem":
                controls["redeem_posts"].append(route.request.post_data_json)
                if controls["redeem_mode"] == "hold":
                    controls["held_redeem"] = route
                    return
                status = 400 if controls["redeem_mode"] == "failure" else 200
                body = {"detail":"本地测试码无效"} if status == 400 else {"ok":True}
            elif path == "/api/recordings/task":
                body = {"state":controls["terminal"],"kind":"process","failure_id":"TEST"}
            elif path == "/api/platform/quota":
                if controls["quota_mode"] == "hold":
                    controls["held"] = route
                    return
                if controls["quota_mode"] == "failure":
                    status = 502
                else:
                    body = {"active":True,"email":"first@example.test","email_verified":True,
                        "llm_points_remaining":9.75,"transcribe_seconds_remaining":600}
            elif path == "/api/campus/courses":
                body = {"courses":[],"campus_configured":True}
            route.fulfill(status=status,body=json.dumps(body),content_type="application/json")
        page.route("**/*",serve)
        page.goto("http://quota.test/")
        page.evaluate("copy=>window.PANEL_COPY=copy",PANEL_COPY)
        page.add_style_tag(content=(ROOT/"pku_sync/panel/static/app.css").read_text("utf-8"))
        page.add_script_tag(content=script)
        page.evaluate("""() => {const s=window.__quotaTest.state;s.activated=true;
            s.quota={active:true,email:'first@example.test',email_verified:true,
                llm_points_remaining:10,transcribe_seconds_remaining:660};
            s.recordings.task={state:'running',kind:'process'};window.__quotaTest.render();}""")
        yield page,controls,requests
        browser.close()

@pytest.mark.parametrize("terminal",["done","failed"])
def test_terminal_processing_refreshes_real_quota_endpoint_and_visible_balance(renderer,terminal):
    page,controls,requests = renderer
    controls["terminal"]=terminal
    page.evaluate("window.__quotaTest.poll()")
    assert "/api/platform/quota" in requests
    assert page.evaluate("window.__quotaTest.state.quota.llm_points_remaining")==9.75
    assert "AI 点 9.75" in page.locator('.sidebar [data-block="quota"]').inner_text()

def test_quota_read_failure_preserves_authenticated_cached_data_and_marks_stale(renderer):
    page,controls,_ = renderer
    controls["quota_mode"]="failure"
    page.evaluate("window.__quotaTest.poll()")
    assert page.evaluate("window.__quotaTest.state.activated") is True
    assert page.evaluate("window.__quotaTest.state.quota.llm_points_remaining")==10
    assert "额度更新失败" in page.locator('.sidebar [data-block="quota"]').inner_text()

@pytest.mark.parametrize("new_account", [False, True])
def test_late_quota_result_cannot_refill_account_after_logout(renderer, new_account):
    page,controls,requests = renderer
    controls["quota_mode"]="hold"
    page.evaluate("() => {window.__terminalPoll=window.__quotaTest.poll();}")
    page.wait_for_timeout(100)
    assert controls["held"] is not None, "Terminal task did not request quota"
    page.evaluate("window.__quotaTest.logout()")
    if new_account:
        page.evaluate("""() => {const s=window.__quotaTest.state;s.activated=true;
            s.quota={active:true,email:'second@example.test',email_verified:true,
                llm_points_remaining:88,transcribe_seconds_remaining:1200};}""")
    controls["held"].fulfill(body=json.dumps({"active":True,"email":"first@example.test",
        "email_verified":True,"llm_points_remaining":9.75,"transcribe_seconds_remaining":600}),
        content_type="application/json")
    page.evaluate("window.__terminalPoll")
    assert page.evaluate("window.__quotaTest.state.activated") is new_account
    if new_account:
        assert page.evaluate("window.__quotaTest.state.quota.email") == "second@example.test"
        assert page.evaluate("window.__quotaTest.state.quota.llm_points_remaining") == 88
    else:
        assert page.evaluate("window.__quotaTest.state.quota") is None


def open_code_field(page, mobile=False):
    page.set_viewport_size({"width":390 if mobile else 1100,"height":844 if mobile else 760})
    menu = page.locator('[data-persistent-disclosure="%s"]' % ("mobile-account" if mobile else "desktop-account"))
    menu.locator(':scope > summary').click()
    return page.locator("#code-mobile" if mobile else "#code"), menu


@pytest.mark.parametrize("mobile", [False, True])
@pytest.mark.parametrize("terminal", ["done", "failed"])
def test_terminal_poll_preserves_in_progress_code_and_selection(renderer, mobile, terminal):
    page, controls, _ = renderer
    controls["terminal"] = terminal
    field, menu = open_code_field(page, mobile)
    field.fill("LOCAL-DEMO-CODE")
    field.evaluate("element => element.setSelectionRange(2, 7)")
    page.evaluate("window.__quotaTest.poll()")
    assert field.input_value() == "LOCAL-DEMO-CODE"
    assert field.evaluate("element => document.activeElement === element") is True
    assert field.evaluate("element => [element.selectionStart, element.selectionEnd]") == [2, 7]
    assert menu.evaluate("element => element.open") is True
    assert controls["redeem_posts"] == []


@pytest.mark.parametrize("terminal", ["done", "failed"])
def test_terminal_poll_keeps_scrolled_account_input_visible(renderer, terminal):
    page, controls, _ = renderer
    controls["terminal"] = terminal
    page.evaluate("""() => {window.__quotaTest.state.update={status:'desktop_managed',version:'0.1.52'};
        window.__quotaTest.render();}""")
    field, menu = open_code_field(page)
    field.fill("LOCAL-DEMO-CODE")
    sidebar = page.locator('.sidebar')
    field.scroll_into_view_if_needed()
    before = sidebar.evaluate('element => element.scrollTop')
    assert before > 20, 'Exercise the actual overflowing desktop sidebar'
    page.evaluate('window.__quotaTest.poll()')
    assert sidebar.evaluate('element => element.scrollTop') == pytest.approx(before, abs=1)
    assert field.input_value() == 'LOCAL-DEMO-CODE'
    assert menu.evaluate('element => element.open') is True
    assert field.evaluate('element => {const r=element.getBoundingClientRect();return r.top>=0 && r.bottom<=innerHeight;}')
    assert controls['redeem_posts'] == []


@pytest.mark.parametrize("new_account", [False, True])
def test_code_draft_cannot_cross_logout_or_account_switch(renderer, new_account):
    page, _, _ = renderer
    field, _ = open_code_field(page)
    field.fill("LOCAL-DEMO-CODE")
    page.evaluate("window.__quotaTest.logout()")
    if new_account:
        page.evaluate("""() => {const s=window.__quotaTest.state;s.activated=true;
            s.quota={active:true,email:'second@example.test',email_verified:true};window.__quotaTest.render();}""")
        assert page.locator("#code").input_value() == ""
    else:
        assert page.locator("[data-redeem-form]").count() == 0


@pytest.mark.parametrize("success", [False, True])
def test_redeem_clears_only_successfully_submitted_code(renderer, success):
    page, controls, _ = renderer
    controls["redeem_mode"] = "success" if success else "failure"
    field, _ = open_code_field(page)
    field.fill("LOCAL-DEMO-CODE")
    page.evaluate("window.__quotaTest.redeem()")
    assert field.input_value() == ("" if success else "LOCAL-DEMO-CODE")
    assert controls["redeem_posts"] == [{"code":"LOCAL-DEMO-CODE"}]


def test_confirmed_redemption_preserves_next_code_typed_while_waiting(renderer):
    page, controls, _ = renderer
    controls["redeem_mode"] = "hold"
    field, _ = open_code_field(page)
    field.fill("LOCAL-DEMO-CODE")
    page.evaluate("() => {window.__redeemPromise=window.__quotaTest.redeem();}")
    page.wait_for_function("document.querySelector('[data-action=redeem]').disabled")
    assert controls["held_redeem"] is not None
    field.fill("LOCAL-NEXT-CODE")
    controls["held_redeem"].fulfill(body='{"ok":true}',content_type="application/json")
    page.evaluate("window.__redeemPromise")
    assert field.input_value() == "LOCAL-NEXT-CODE"
    assert controls["redeem_posts"] == [{"code":"LOCAL-DEMO-CODE"}]


@pytest.mark.parametrize("new_account", [False, True])
def test_late_redemption_cannot_restore_logged_out_account(renderer, new_account):
    page, controls, requests = renderer
    controls["redeem_mode"] = "hold"
    field, _ = open_code_field(page)
    field.fill("LOCAL-DEMO-CODE")
    page.evaluate("() => {window.__redeemPromise=window.__quotaTest.redeem();}")
    page.wait_for_function("document.querySelector('[data-action=redeem]').disabled")
    assert controls["held_redeem"] is not None
    page.evaluate("window.__quotaTest.logout()")
    if new_account:
        page.evaluate("""() => {const s=window.__quotaTest.state;s.activated=true;
            s.quota={active:true,email:'second@example.test',email_verified:true,
                llm_points_remaining:88,transcribe_seconds_remaining:1200};window.__quotaTest.render();}""")
    quota_reads = requests.count("/api/platform/quota")
    controls["held_redeem"].fulfill(body='{"ok":true}',content_type="application/json")
    page.evaluate("window.__redeemPromise")
    assert requests.count("/api/platform/quota") == quota_reads
    assert page.evaluate("window.__quotaTest.state.activated") is new_account
    if new_account:
        assert page.evaluate("window.__quotaTest.state.quota.email") == "second@example.test"
        assert page.evaluate("window.__quotaTest.state.quota.llm_points_remaining") == 88
        assert page.locator("#code").input_value() == ""
        assert page.locator('#redeem-form [data-action="redeem"]').is_enabled()
    else:
        assert page.evaluate("window.__quotaTest.state.quota") is None


@pytest.mark.parametrize("new_account", [False, True])
def test_pending_redemption_quota_read_is_ignored_after_account_exit(renderer, new_account):
    page, controls, _ = renderer
    controls["quota_mode"] = "hold"
    field, _ = open_code_field(page)
    field.fill("LOCAL-DEMO-CODE")
    page.evaluate("() => {window.__redeemPromise=window.__quotaTest.redeem();}")
    page.wait_for_function("document.querySelector('[data-action=redeem]').disabled")
    # Pump the actual second HTTP request, not merely the synchronous button
    # disable that occurs before the first response has been received.
    for _ in range(100):
        if controls["held"] is not None:
            break
        page.wait_for_timeout(20)
    assert controls["held"] is not None
    page.evaluate("window.__quotaTest.logout()")
    if new_account:
        page.evaluate("""() => {const s=window.__quotaTest.state;s.activated=true;
            s.quota={active:true,email:'second@example.test',email_verified:true,
                llm_points_remaining:88,transcribe_seconds_remaining:1200};window.__quotaTest.render();}""")
    controls["held"].fulfill(body=json.dumps({"active":True,"email":"first@example.test",
        "email_verified":True,"llm_points_remaining":9.75,"transcribe_seconds_remaining":600}),
        content_type="application/json")
    page.evaluate("window.__redeemPromise")
    assert page.evaluate("window.__quotaTest.state.activated") is new_account
    if new_account:
        assert page.evaluate("window.__quotaTest.state.quota.email") == "second@example.test"
        assert page.evaluate("window.__quotaTest.state.quota.llm_points_remaining") == 88
        assert page.locator('#redeem-form [data-action="redeem"]').is_enabled()
    else:
        assert page.evaluate("window.__quotaTest.state.quota") is None
