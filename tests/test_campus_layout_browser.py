"""Portal-first layout in Windows Edge; every request is intercepted locally."""
from pathlib import Path
import json

import pytest

from pku_sync.panel.student_ui import PANEL_COPY

playwright = pytest.importorskip("playwright.sync_api")
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
STATIC = Path(__file__).parents[1] / "pku_sync/panel/static"


@pytest.mark.skipif(not EDGE.exists(), reason="Microsoft Edge is required")
def test_account_controls_survive_refresh_and_quiz_offers_a_next_step():
    """Check keyboard settings access and recovery from a disabled quiz action."""
    script = (STATIC / 'app.js').read_text('utf-8')
    script = script[:script.index('  window.setInterval(refreshAccount, 8000);')] + (
        'window.__layout = {state: state, render: render};})();'
    )
    writes = []
    def serve(route):
        if route.request.method != 'GET':
            writes.append(route.request.url)
        if route.request.url.rstrip('/') == 'http://panel.test':
            route.fulfill(body='<div id="app"></div><div id="toast"></div>', content_type='text/html')
        else:
            route.fulfill(status=404, body='{}', content_type='application/json')
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=str(EDGE), headless=True)
        try:
            page = browser.new_page(viewport={'width': 1100, 'height': 760})
            page.route('**/*', serve)
            page.goto('http://panel.test')
            page.evaluate('copy => window.PANEL_COPY = copy', PANEL_COPY)
            page.add_style_tag(content=(STATIC / 'app.css').read_text('utf-8'))
            page.add_script_tag(content=script)
            page.evaluate('''() => {
                const s=window.__layout.state;
                s.activated=true; s.quota={active:true,email:'student@example.test',email_verified:true,
                    available:true,llm_points_remaining:12,transcribe_seconds_remaining:3600};
                s.connection.connected=true; s.campus.configured=true; s.campus.home={id:'home'};
                s.campus.course={id:'course',title:'课程',lessons:[],unassigned_materials:[],all_materials:[],
                    assignments:[],announcements:[],announcement_tasks:[]};
                s.view='campus-course';s.campus.section='quizzes';s.campus.courseId='course';
                s.campusQuiz.ready=true; window.__layout.render();
            }''')
            account = page.locator('.sidebar [data-persistent-disclosure="desktop-account"]')
            assert not account.evaluate('el => el.open')
            assert not page.locator('#redeem-form').is_visible()
            assert 'AI 点 12' in page.locator('.sidebar [data-block="quota"]').inner_text()
            account.locator('summary').focus()
            page.keyboard.press('Enter')
            assert page.locator('#redeem-form').is_visible()
            page.evaluate('window.__layout.render()')
            assert account.evaluate('el => el.open')
            account.locator('summary').click()
            page.evaluate('window.__layout.render()')
            assert not account.evaluate('el => el.open')
            assert page.get_by_role('button', name='生成学习小测', exact=True).is_disabled()
            page.get_by_role('button', name='查看本课讲次', exact=True).click()
            assert page.get_by_role('heading', name='每节课', exact=True).is_visible()
            page.evaluate('''() => { const s=window.__layout.state;
                s.campus.section='quizzes'; s.campus.home=null; window.__layout.render(); }''')
            page.get_by_role('button', name='打开课程设置', exact=True).click()
            assert page.locator('.campus-settings').evaluate('el => el.open')
            page.set_viewport_size({'width':390,'height':844})
            mobile = page.locator('.mobile-account-menu')
            mobile.locator(':scope > summary').click()
            page.evaluate('window.__layout.render()')
            assert mobile.evaluate('el => el.open')
            assert writes == []
        finally:
            browser.close()


@pytest.mark.skipif(not EDGE.exists(), reason="Microsoft Edge is required")
def test_portal_first_layout_and_account_settings():
    script = (STATIC / "app.js").read_text("utf-8").rstrip()
    assert script.endswith("})();")
    # Exercise the real renderer and events without async bootstrap replacing
    # our fixture state; the existing HTTP browser suite covers bootstrap.
    script = script[:script.index("  window.setInterval(refreshAccount, 8000);")] + (
        "window.__layout = {state: state, render: render};})();"
    )
    writes = []
    generated = []
    graded = []
    reviewed = []
    history_reviewed = []
    receipt_recovered = []

    def serve(route):
        if route.request.method != "GET":
            writes.append(route.request.url)
        if route.request.url.endswith('/api/campus/submission-drafts/pending-receipt/recover'):
            assert route.request.method == 'POST'
            receipt_recovered.append('recovered')
            route.fulfill(body=json.dumps({'state': 'submitted', 'message': '已恢复回执保存，未重新发送作业。'}), content_type='application/json')
            return
        if route.request.url.endswith('/submission-receipts'):
            route.fulfill(body=json.dumps({'items': [{'draft_id': 'pending-receipt', 'assignment_title': '第二次正式作业',
                'archive_name': 'answer.pdf', 'verified_at': '2026-10-04T08:00:00Z',
                'state': 'submitted' if receipt_recovered else 'receipt_verified',
                'recovery_url': '/api/campus/submission-drafts/pending-receipt/recover',
                'download_url': '/api/campus/submission-drafts/pending-receipt/receipt' if receipt_recovered else ''}], 'pending': []}), content_type='application/json')
            return
        if route.request.url.endswith('/assignments/formal-assignment/status'):
            assert route.request.method == 'GET'
            route.fulfill(body=json.dumps({'state':'review_history','verified':True,
                'label':'教学网显示提交历史，请核对具体尝试与成绩','checked_at':'2026-10-04T08:00:00Z'}),
                content_type='application/json')
            return
        if route.request.url.endswith('/quizzes/quiz-page/grade'):
            graded.append(json.loads(route.request.post_data))
            route.fulfill(body=json.dumps({'status': 'completed', 'score': 70, 'points_charged': 2}), content_type='application/json')
            return
        if route.request.url.endswith('/quiz-reviews/review-page'):
            reviewed.append(json.loads(route.request.post_data))
            route.fulfill(body=json.dumps({'id': 'review-page', 'reviewed': True}), content_type='application/json')
            return
        if route.request.url.endswith('/quiz-reviews/grade-history'):
            history_reviewed.append(json.loads(route.request.post_data))
            route.fulfill(body=json.dumps({'id': 'grade-history', 'reviewed': True}), content_type='application/json')
            return
        if route.request.url.endswith('/quiz-reviews'):
            route.fulfill(body=json.dumps({'items': [{'id': 'review-page', 'title': '复习 · 第2题',
                'url': 'https://www.notion.so/review-page', 'reviewed': bool(reviewed)}] if graded else [],
                'histories': [{'id': 'grade-history', 'title': '批改记录', 'score': 70,
                    'url': 'https://www.notion.so/grade-history', 'reviewed': bool(history_reviewed)}] if graded else []}), content_type='application/json')
            return
        if route.request.url.endswith('/api/campus/courses/official-course/quizzes'):
            if route.request.method == 'POST':
                generated.append(json.loads(route.request.post_data))
                route.fulfill(body=json.dumps({'id': 'quiz-page', 'title': '第五讲学习小测'}), content_type='application/json')
            else:
                route.fulfill(body=json.dumps({'items': [{'id': 'quiz-page', 'title': '第五讲学习小测',
                    'url': 'https://www.notion.so/quiz-page'}] if generated else [],
                    'lectures': [{'id': 'pku:recording:stable5', 'title': '第五讲', 'date': '2026-09-29', 'accepted': True},
                                 {'id': 'pku:recording:stable4', 'title': '第四讲', 'accepted': False}]}), content_type='application/json')
            return
        if route.request.url.endswith("/api/campus/notion/workspace"):
            assert json.loads(route.request.post_data) == {"parent_page_id": "parent"}
            route.fulfill(body=json.dumps({"schema": 2, "id": "new-home", "parent_id": "parent", "active": True,
                                           "url": "https://www.notion.so/new-home"}),
                          content_type="application/json")
            return
        if route.request.url.rstrip("/") == "http://panel.test":
            route.fulfill(body='<div id="app"></div><div id="toast"></div>',
                          content_type="text/html")
        else:
            route.fulfill(status=404, body="{}", content_type="application/json")

    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=str(EDGE), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.route("**/*", serve)
            page.goto("http://panel.test")
            page.evaluate("copy => { window.PANEL_COPY = copy; }", PANEL_COPY)
            page.add_style_tag(content=(STATIC / "app.css").read_text("utf-8"))
            page.add_script_tag(content=script)
            page.evaluate("""() => {
                const s = window.__layout.state;
                s.activated = true; s.quota = {active:true};
                s.connection.connected = true; s.campus.configured = true;
                s.campus.courses = [];
                s.directory = {courses:[{id:'old-course',title:'旧缓存课程',scope:'历史资料',
                    counts:{lectures:4,materials:0},lectures:[]}],
                    stats:{lectures:4,materials:0,courses:1},sync:{last_sync_at:null},activity:[]};
                s.view = 'dashboard'; window.__layout.render();
            }""")
            assert page.get_by_role("heading", name="我的课程").is_visible()
            assert page.locator('[data-action="open-course"]').count() == 0
            assert "旧缓存课程" not in page.locator(".main").inner_text()
            page.locator('.sidebar [data-action="open-library"]').click()
            assert page.get_by_role("heading", name="旧学习资料").is_visible()
            assert "旧缓存课程" in page.locator(".main").inner_text()
            page.locator('.sidebar [data-action="open-dashboard"]').click()
            assert page.locator('[data-action="open-course"]').count() == 0
            page.evaluate("""() => {
                const s = window.__layout.state;
                s.activated = true; s.quota = {active:true};
                s.connection.connected = true; s.campus.configured = true;
                s.campus.courses = [{id:'official-course', title:'发展心理学',
                    recordings:5, materials:9, assignments:1, announcements:2,
                    synced_at:'2026-10-04', code:'PSY', term:'2026 秋季'}];
                s.view = 'dashboard'; window.__layout.render();
            }""")
            assert page.get_by_role("heading", name="我的课程").is_visible()
            assert "欢迎使用" in page.locator(".main").inner_text()
            assert "5 节课 · 1 项正式作业 · 2 则通知" in page.locator(".course-card").inner_text()
            assert not page.locator(".campus-settings").evaluate("el => el.open")
            page.locator('.sidebar [data-action="campus-account-settings"]').click()
            assert page.locator(".campus-settings").evaluate("el => el.open")
            assert page.get_by_role("button", name="更换账号").is_visible()
            page.evaluate("""() => {
                const s = window.__layout.state;
                s.view = 'campus-course'; s.campus.courseId = 'official-course';
                s.campus.course = {id:'official-course', title:'发展心理学', code:'PSY',
                    lessons:[{id:'sep29',title:'第五讲',date:'2026-09-29',materials:[{index:0,title:'第五讲课件',files:['第五讲.pdf'],category:'material'}],
                        notion_note_url:'https://www.notion.so/accepted-note',
                        notion_lecture_url:'https://www.notion.so/lecture',video_available:true}],
                    unassigned_materials:[],all_materials:[],assignments:[{source:'calendar',
                        source_id:'calendar:one',title:'待核对日历事项',due_at_label:'2026-10-05 20:00 北京时间'}],
                    announcements:[],announcement_tasks:[],summary:{}};
                window.__layout.render();
            }""")
            assert page.locator('.campus-centers a').count() == 4
            assert page.locator('#course-assignments, #course-notices, #course-quizzes, .recording-row').count() == 0
            assert page.locator(".campus-centers").evaluate(
                "el => !!(el.compareDocumentPosition(document.querySelector('#course-lessons')) & Node.DOCUMENT_POSITION_FOLLOWING)")
            page.locator('[data-action="campus-open-lesson"][data-recording="sep29"]').click()
            assert page.get_by_role("button", name="查看总结").is_visible()
            assert page.get_by_role("link", name="下载原始录像").get_attribute("href") == "/api/recordings/sep29/video"
            assert page.locator('.recording-row .course-meta').first.inner_text() == '第 1 节课'
            page.locator('[data-action="campus-course-home"]').click()
            page.locator('[data-center="materials"]').click()
            assert page.get_by_role('link', name='下载 第五讲.pdf').get_attribute('href') == '/api/campus/courses/official-course/materials/0/files/0'
            page.locator('[data-action="campus-course-home"]').click()
            page.locator('[data-center="assignments"]').click()
            assert page.get_by_role("heading", name="作业中心").is_visible()
            assert '日历时间：2026-10-05 20:00 北京时间' in page.locator('#course-assignments').inner_text()
            assert '尚未关联到正式作业' in page.locator('#course-assignments').inner_text()
            assert page.get_by_role('button', name='准备作业文件', exact=True).count() == 0
            assert page.get_by_role('button', name='读取教学网提交状态', exact=True).count() == 0
            page.locator('[data-action="campus-course-home"]').click()
            page.locator('[data-center="notices"]').click()
            assert page.get_by_role("heading", name="通知中心").is_visible()
            page.locator('[data-action="campus-course-home"]').click()
            page.locator('[data-center="quizzes"]').click()
            assert page.get_by_role("heading", name="小测中心", exact=True).is_visible()
            assert page.locator(".campus-settings").count() == 0
            assert writes == []
            page.evaluate("""() => {
                const s = window.__layout.state;
                s.campus.home = {id:'old-home',parent_id:'parent',url:'https://www.notion.so/old-home'};
                s.campus.parentId = 'parent'; window.__layout.render();
            }""")
            page.get_by_role('button', name='课程设置', exact=True).click()
            assert page.locator('.campus-settings').evaluate('el => el.open')
            page.locator('.home-setup-advanced > summary').click()
            page.get_by_role("button", name="创建附加新版主页").click()
            playwright.expect(page.get_by_role("button", name="打开主页", exact=True)).to_have_attribute(
                "data-url", "https://www.notion.so/new-home")
            assert page.evaluate("window.__layout.state.campus.home.id") == "new-home"
            assert page.evaluate("window.__layout.state.campus.legacyHome.id") == "old-home"
            assert page.get_by_role("button", name="打开旧主页（回滚）").get_attribute("data-url") == "https://www.notion.so/old-home"
            assert writes == ["http://panel.test/api/campus/notion/workspace"]
            page.locator('[data-action="campus-course-home"]').click()
            page.locator('[data-center="quizzes"]').click()
            page.get_by_role('button', name='刷新小测中心').click()
            page.locator('[data-campus-quiz-source="pku:recording:stable5"]').wait_for()
            assert page.locator('[data-campus-quiz-source="pku:recording:stable4"]').is_disabled()
            page.locator('[data-campus-quiz-source="pku:recording:stable5"]').check()
            page.get_by_text('参考往年题（可选）', exact=True).click()
            page.locator('[data-campus-quiz-paper-file]').set_input_files({'name': 'past-paper.txt',
                'mimeType': 'text/plain', 'buffer': '学生上传参考题'.encode('utf-8')})
            playwright.expect(page.locator('[data-campus-quiz-paper]')).to_have_value('学生上传参考题')
            page.once('dialog', lambda dialog: dialog.dismiss())
            page.get_by_role('button', name='生成学习小测', exact=True).click()
            assert generated == []
            page.once('dialog', lambda dialog: dialog.accept())
            page.get_by_role('button', name='生成学习小测', exact=True).click()
            page.get_by_role('heading', name='第五讲学习小测').wait_for()
            assert generated == [{'lecture_ids': ['pku:recording:stable5'], 'prior_paper': '学生上传参考题', 'confirmed': True}]
            assert page.get_by_role('button', name='打开小测并作答').get_attribute('data-url') == 'https://www.notion.so/quiz-page'
            # Exercise native confirmation semantics as well as the ordinary
            # browser dialogs above. A pending native choice cannot POST.
            page.evaluate('window.__TAURI_INTERNALS__ = {}')
            page.get_by_role('button', name='批改小测', exact=True).click()
            page.locator('[data-product-confirmation]').wait_for()
            page.wait_for_timeout(100)
            assert graded == []
            assert '1–5 AI 点' in page.locator('#product-confirmation-message').inner_text()
            page.locator('[data-confirmation-decision="cancel"]').click()
            assert graded == []
            page.get_by_role('button', name='批改小测', exact=True).click()
            page.locator('[data-product-confirmation]').wait_for()
            assert graded == []
            page.locator('[data-confirmation-decision="confirm"]').click()
            page.evaluate('delete window.__TAURI_INTERNALS__')
            playwright.expect(page.locator('#course-quizzes')).to_contain_text('学习评分：70/100 · 本次 2 AI 点')
            assert graded == [{'confirmed': True, 'force': False}]
            page.get_by_role('button', name='查看作答与批改历史').wait_for()
            assert page.get_by_role('button', name='查看作答与批改历史').get_attribute('data-url') == 'https://www.notion.so/grade-history'
            page.get_by_role('button', name='打开复习记录').wait_for()
            assert page.get_by_role('button', name='打开复习记录').get_attribute('data-url') == 'https://www.notion.so/review-page'
            page.once('dialog', lambda dialog: dialog.accept('已重新复习并理解概念'))
            page.locator('[data-review="review-page"]').click()
            page.get_by_text('已有复习记录', exact=True).wait_for()
            assert reviewed == [{'confirmed': True, 'note': '已重新复习并理解概念'}]
            page.once('dialog', lambda dialog: dialog.accept('整份小测已复盘'))
            page.locator('[data-review="grade-history"]').click()
            page.get_by_text('已记录复习', exact=True).wait_for()
            assert history_reviewed == [{'confirmed': True, 'note': '整份小测已复盘'}]
            page.evaluate("""() => {
                window.__layout.state.campusQuiz.grades['quiz-page'].recovered_from_relay = true;
                window.__layout.render();
            }""")
            assert '原任务已结算 2 AI 点，恢复未新增扣费' in page.locator('#course-quizzes').inner_text()
            page.evaluate("""() => {
                window.__layout.state.campus.receipts = [{assignment_title:'第一次正式作业',
                    archive_name:'answer.pdf',verified_at:'2026-10-04T08:00:00Z',
                    download_url:'/api/campus/submission-drafts/receipt-id/receipt'}];
                window.__layout.render();
            }""")
            page.locator('[data-action="campus-course-home"]').click()
            page.locator('[data-center="assignments"]').click()
            page.get_by_text('本机教学网回执记录', exact=True).click()
            assert page.get_by_role('link', name='下载教学网正式回执').get_attribute('href') == '/api/campus/submission-drafts/receipt-id/receipt'
            page.evaluate("""() => {
                window.__layout.state.campus.receipts = [{draft_id:'pending-receipt',assignment_title:'第二次正式作业',
                    archive_name:'answer.pdf',verified_at:'2026-10-04T08:00:00Z',state:'receipt_verified',
                    recovery_url:'/api/campus/submission-drafts/pending-receipt/recover'}];
                window.__layout.render();
            }""")
            page.get_by_text('本机教学网回执记录', exact=True).click()
            page.get_by_role('button', name='恢复回执保存', exact=True).click()
            playwright.expect(page.get_by_role('button', name='恢复回执保存', exact=True)).to_have_count(0)
            page.get_by_text('本机教学网回执记录', exact=True).click()
            assert page.get_by_role('link', name='下载教学网正式回执').get_attribute('href') == '/api/campus/submission-drafts/pending-receipt/receipt'
            assert receipt_recovered == ['recovered']
            assert not any(url.endswith('/submit') for url in writes)
            page.evaluate("""() => {
                window.__layout.state.campus.course.official_assessments = [{title:'教学网期中测验',body_text:'在教学网完成正式题目',
                    status_label:'请在教学网核对测验截止时间、作答与提交状态。',
                    source_url:'https://course.pku.edu.cn/webapps/blackboard/content/listContent.jsp?course_id=_1_1&content_id=_10_1'}];
                window.__layout.render();
            }""")
            page.locator('[data-action="campus-course-home"]').click()
            page.locator('[data-center="quizzes"]').click()
            assert page.get_by_role('heading', name='教学网正式测验', exact=True).count() == 1
            assert page.get_by_role('heading', name='学习小测', exact=True).count() == 1
            assert page.get_by_role('heading', name='教学网期中测验', exact=True).count() == 1
            assert 'listContent.jsp' in page.get_by_role('link', name='查看教学网测验原目录').get_attribute('href')
            page.evaluate("""() => {
                window.__layout.state.campus.course.assignments = [{content_id:'formal-assignment',
                    title:'第一次正式作业',source:'content-tree',instructions:'按原要求提交'}];
                window.__layout.render();
            }""")
            page.locator('[data-action="campus-course-home"]').click()
            page.locator('[data-center="assignments"]').click()
            page.get_by_role('button', name='读取教学网提交状态').click()
            page.get_by_text('教学网显示提交历史，请核对具体尝试与成绩', exact=False).wait_for()
        finally:
            browser.close()


UI_GUIDANCE_STATIC = Path(__file__).resolve().parents[1] / 'pku_sync/panel/static'
UI_GUIDANCE_CASES = [
    ('new', {}, False, True),
    ('saved-transcript', {'transcript_available': True}, False, False),
    ('regenerate', {'transcript_available': True, 'notes_available': True,
        'notion_lecture_url': 'https://www.notion.so/local-fixture'}, True, False),
    ('publish', {'transcript_available': True, 'notes_available': True,
        'publish_ready': True, 'video_available': False}, False, False),
    ('regenerate-no-video', {'transcript_available': True, 'notes_available': True,
        'publish_ready': True, 'video_available': False,
        'notion_lecture_url': 'https://www.notion.so/local-fixture'}, True, False),
]

@pytest.fixture
def usage_guidance_renderer():
    from playwright.sync_api import sync_playwright
    from urllib.parse import urlsplit
    script=(UI_GUIDANCE_STATIC/'app.js').read_text('utf-8')
    script=script[:script.index('  window.setInterval(refreshAccount, 8000);')]+(
        'window.__usageGuidance={state:state,render:render};})();')
    with sync_playwright() as driver:
        browser=driver.chromium.launch(executable_path='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless=True)
        try:
            page=browser.new_page(viewport={'width':1100,'height':760})
            posts=[]
            def serve(route):
                path=urlsplit(route.request.url).path
                if route.request.method=='POST':
                    posts.append({'path':path,'body':route.request.post_data_json})
                    route.fulfill(body='{"id":"local-task","state":"running","kind":"process"}',content_type='application/json')
                elif path=='/':route.fulfill(body='<div id="app"></div><div id="toast"></div>',content_type='text/html')
                else:route.fulfill(status=404,body='{}',content_type='application/json')
            page.route('**/*',serve);page.goto('http://guide.test/')
            page.evaluate('copy=>window.PANEL_COPY=copy',PANEL_COPY)
            page.add_style_tag(content=(UI_GUIDANCE_STATIC/'app.css').read_text('utf-8'))
            page.add_script_tag(content=script)
            yield page,posts
        finally:browser.close()


def setup_usage_guidance(page, extra, approve):
    lesson={'id':'local-lesson','title':'本地用量提示检查','date':'2026-09-30',
        'duration_seconds':7200,'materials':[],'video_available':True,
        'notes_available':False,'transcript_available':False,**extra}
    page.evaluate('''data=>{const s=window.__usageGuidance.state;s.activated=true;
        s.quota={active:true,email:'fixture@example.test',email_verified:true};s.connection.connected=true;
        s.campus.configured=true;s.campus.home={id:'local-home'};s.campus.courseId='local-course';
        s.campus.course={id:'local-course',title:'本地课程',lessons:[data.lesson],unassigned_materials:[],
            all_materials:[],assignments:[],announcements:[],announcement_tasks:[],summary:{}};
        s.campus.section='lecture';s.campus.lessonId=data.lesson.id;s.view='campus-course';
        s.recordings.directOss=true;window.__usageGuidance.render();
        window.__guideConfirm=[];window.confirm=text=>{window.__guideConfirm.push(text);return data.approve;};
    }''',{'lesson':lesson,'approve':approve})


@pytest.mark.parametrize('kind,extra,regenerate,new_audio',UI_GUIDANCE_CASES,ids=[c[0] for c in UI_GUIDANCE_CASES])
def test_lecture_usage_guidance_and_cancel_match_actual_reuse(usage_guidance_renderer,kind,extra,regenerate,new_audio):
    page,posts=usage_guidance_renderer
    setup_usage_guidance(page,extra,False)
    text=page.locator('.recording-row').inner_text()
    assert ('预计转写用量约 120 分钟' in text) is new_audio
    assert page.locator('[data-recording-direct-oss]').count()==int(new_audio)
    if kind=='publish':
        assert '不重复消耗转写或 AI 额度' in text
    elif not new_audio:
        assert '复用已有转写' in text and 'AI' in text
    action='campus-regenerate' if regenerate else 'campus-process'
    page.locator('[data-action="'+action+'"]').click()
    page.wait_for_function('window.__guideConfirm.length===1')
    confirmation=page.evaluate('window.__guideConfirm[0]')
    assert ('转写与 AI 笔记会消耗额度' in confirmation) is new_audio
    assert ('短时授权直传阿里云 OSS' in confirmation) is new_audio
    if kind=='saved-transcript' or kind.startswith('regenerate'):
        assert '已有转写' in confirmation and 'AI' in confirmation
    if kind=='publish':assert '不重复消耗转写或 AI 笔记额度' in confirmation
    assert posts==[]


@pytest.mark.parametrize('kind,extra,regenerate,new_audio',UI_GUIDANCE_CASES,ids=[c[0] for c in UI_GUIDANCE_CASES])
def test_existing_transcript_does_not_forward_stale_audio_upload_selection(usage_guidance_renderer,kind,extra,regenerate,new_audio):
    page,posts=usage_guidance_renderer
    setup_usage_guidance(page,extra,True)
    action='campus-regenerate' if regenerate else 'campus-process'
    page.locator('[data-action="'+action+'"]').click()
    page.wait_for_function("window.__usageGuidance.state.recordings.task.id==='local-task'")
    assert len(posts)==1
    assert posts[0]['path']=='/api/campus/courses/local-course/recordings/local-lesson/process'
    assert posts[0]['body']['direct_oss'] is new_audio
    assert posts[0]['body']['regenerate'] is regenerate
    assert posts[0]['body']['reuse_existing'] is (kind=='publish')
