import httpx
import pytest

from pku_sync import materials


def empty_page(course='_101545_1', folder='_1638942_1'):
    return f'''<div id="content"><form name="contentForm" method="POST"
    action="listContent.jsp?course_id={course}&amp;content_id={folder}"></form>
    <div id="containerdiv"><div class="noItems container-empty">
    <div class="main-message">没有要显示的内容。</div></div></div></div>'''


def walk(html):
    def respond(request):
        if request.url.path.endswith('/contents'):
            return httpx.Response(200, json={'results': [{'id': '_1638942_1', 'title': '选课指导'}]})
        return httpx.Response(200, text=html)
    with httpx.Client(base_url='https://course.pku.edu.cn', transport=httpx.MockTransport(respond)) as client:
        return materials.walk_materials(client, '_101545_1')


def test_official_identity_bound_empty_folder_is_complete():
    assert walk(empty_page()) == ([], [])


@pytest.mark.parametrize('html', [
    empty_page(course='_other_1'), empty_page(folder='_other_1'),
    empty_page().replace('name="contentForm"', 'name="loginForm"'),
    empty_page().replace('content_id=_1638942_1', 'content_id=_1638942_1&amp;content_id=_other_1'),
    empty_page().replace('listContent.jsp?', 'receiptNotAvailable.jsp?'),
    '<div id="content"><div id="containerdiv">该项目当前不可用。</div></div>',
    '<script>var marker = "content_listContainer";</script>',
    empty_page().replace('没有要显示的内容。', '读取失败'),
    empty_page().replace('id="content"', 'id="other"'),
])
def test_unproved_empty_or_unavailable_page_remains_incomplete(html):
    with pytest.raises(ValueError, match='完整页面'):
        walk(html)


def test_explicit_api_unavailable_area_is_not_requested():
    seen = []
    def respond(request):
        if request.url.path.endswith('/contents'):
            return httpx.Response(200, json={'results': [
                {'id': '_1638942_1', 'title': '指导', 'availability': {'available': 'Yes'}},
                {'id': '_hidden_1', 'title': '成果', 'availability': {'available': 'No'}},
            ]})
        seen.append(request.url.params['content_id'])
        return httpx.Response(200, text=empty_page())
    with httpx.Client(base_url='https://course.pku.edu.cn', transport=httpx.MockTransport(respond)) as client:
        assert materials.walk_materials(client, '_101545_1') == ([], [])
    assert seen == ['_1638942_1']
