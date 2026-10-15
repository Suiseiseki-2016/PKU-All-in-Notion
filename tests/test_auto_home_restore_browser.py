"""Existing remote homes become ready automatically, even at another location."""
import json
from pathlib import Path
from urllib.parse import urlsplit
import pytest
from browser_support import browser_launch_options
from pku_sync.panel.student_ui import student_page

playwright = pytest.importorskip('playwright.sync_api')
EDGE = Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
pytestmark = pytest.mark.skipif(not EDGE.is_file(),reason='Microsoft Edge required')

@pytest.fixture
def ui():
    script = (Path(__file__).parents[1] / 'pku_sync/panel/static/app.js').read_text('utf-8')
    script = script[:script.index('  window.setInterval(refreshAccount, 8000);')] + 'window.__home={state:state,render:render,load:loadNotionParents};})();'
    reply = {'pages':[{'id':'original-parent','title':'Original space'}],
             'home':{'schema':2,'id':'home','active':True,'parent_id':'original-parent','url':'https://www.notion.so/home'},
             'workspace':{'schema':2,'id':'home','active':True,'parent_id':'original-parent','url':'https://www.notion.so/home'},
             'home_sync':{'status':'pulled'}}
    posts=[]
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(**browser_launch_options(),headless=True)
        page=browser.new_page()
        page.add_init_script("localStorage.setItem('pku-panel-welcome-v1', 'seen')")
        def serve(route):
            path=urlsplit(route.request.url).path
            if route.request.method=='POST':
                posts.append(route.request.post_data_json)
                route.fulfill(json={'schema':2,'id':'new-home','active':True,'parent_id':'todo','url':'https://www.notion.so/new-home'})
            elif path=='/app': route.fulfill(body=student_page(),content_type='text/html')
            elif path=='/app/app.js': route.fulfill(body=script,content_type='application/javascript')
            elif path=='/app/app.css': route.fulfill(body='',content_type='text/css')
            else: route.fulfill(json=reply)
        page.route('**/*',serve)
        page.goto('http://panel.test/app')
        page.wait_for_function('!!window.__home')
        page.evaluate("""() => {const s=window.__home.state;s.activated=true;s.quota={email:'fixture@example.com',email_verified:true};s.connection.connected=true;s.view='campus-account';s.campus.parentId='todo';s.campus.parents=[{id:'todo',title:'TODO LIST'}];window.__home.render();}""")
        yield page,reply,posts
        browser.close()

def test_existing_home_is_restored_without_creation_or_location_conflict(ui):
    page,reply,posts=ui
    page.evaluate('window.__home.load({selectedOnly:true})')
    playwright.expect(page.get_by_role('button',name='打开主页',exact=True)).to_have_attribute('data-url','https://www.notion.so/home')
    assert page.get_by_role('button',name='创建学习主页',exact=True).count()==0
    assert page.evaluate('window.__home.state.campus.parentId')=='original-parent'
    assert posts==[]

def test_missing_grant_explains_recovery_and_reconnect_restores_without_new_post(ui):
    page,reply,posts=ui
    reply.update(home=None,workspace=None,home_sync={'status':'notion_reconnect_required'},pages=[{'id':'todo','title':'TODO LIST'}])
    page.evaluate('window.__home.load({selectedOnly:true})')
    playwright.expect(page.locator('[data-home-recovery]')).to_contain_text('当前 Notion 授权无法访问')
    assert page.get_by_role('button',name='创建学习主页',exact=True).count()==0
    reply.update(home={'schema':2,'id':'home','active':True,'parent_id':'original-parent','url':'https://www.notion.so/home'},
                 workspace={'schema':2,'id':'home','active':True,'parent_id':'original-parent','url':'https://www.notion.so/home'},
                 home_sync={'status':'pulled'},pages=[{'id':'original-parent','title':'Original space'}])
    page.get_by_role('button',name='重试恢复主页').click()
    playwright.expect(page.get_by_role('button',name='打开主页',exact=True)).to_be_visible()
    assert posts==[]

def test_new_home_requires_confirmation_and_explicit_replacement(ui):
    page,reply,posts=ui
    reply.update(home=None,workspace=None,home_sync={'status':'notion_reconnect_required'},pages=[{'id':'todo','title':'TODO LIST'}])
    page.evaluate('window.__home.load({selectedOnly:true})')
    page.get_by_text('使用新的学习主页',exact=True).click()
    page.once('dialog',lambda dialog:dialog.dismiss())
    page.get_by_role('button',name='在所选位置新建主页').click()
    assert posts==[]
    page.once('dialog',lambda dialog:dialog.accept())
    page.get_by_role('button',name='在所选位置新建主页').click()
    playwright.expect(page.get_by_role('button',name='打开主页',exact=True)).to_have_attribute('data-url','https://www.notion.so/new-home')
    assert posts==[{'parent_page_id':'todo','replace_existing':True}]
