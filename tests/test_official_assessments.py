"""Official assessment facts are retained without assignment upload mapping."""
import json
from types import SimpleNamespace

import pytest

from pku_sync import materials
from pku_sync.models import Assignment
from pku_sync.panel.campus_catalog import campus_course, campus_courses
from pku_sync.panel.assignment_workflow import _assignment, PreparationError
from pku_sync.panel import notion_home
from tests.test_panel_assignment_workflow import _client
from datetime import datetime, timezone


def test_calendar_projection_does_not_claim_formal_assignment_or_deadline():
    item = {'source': 'calendar', 'source_id': 'calendar:one',
            'title': '每周作业', 'due_at': '2026-10-05T23:00:00Z'}
    summary = notion_home._summary_line('作业', item)
    assert '教学网日历事项' in summary
    assert '日历时间' in summary and '截止：' not in summary
    blocks = json.dumps(notion_home._item_blocks('_1_1', '作业', item, ''), ensure_ascii=False)
    assert '尚未关联到正式作业' in blocks
    assert 'uploadAssignment' not in blocks


def test_calendar_time_does_not_become_next_formal_assignment_deadline():
    course = {'assignments': [{'source': 'calendar', 'title': '每周作业',
                               'due_at': '2026-10-05T23:00:00Z'}]}
    assert notion_home._next_formal_assignment_due(
        course, datetime(2026, 10, 4, tzinfo=timezone.utc)) == ''


def test_course_summary_counts_calendar_separately(tmp_path, monkeypatch):
    _client(tmp_path, monkeypatch)
    path = tmp_path / 'course/assignments/index.json'
    rows = json.loads(path.read_text('utf-8'))
    rows.append({'source': 'calendar', 'source_id': 'calendar:one',
                 'title': '待核对日历事项', 'due_at': '2026-10-05T23:00:00Z'})
    path.write_text(json.dumps(rows), 'utf-8')
    course = campus_courses(tmp_path)['courses'][0]
    assert course['assignments'] == 1
    assert course['calendar_items'] == 1


def test_content_tests_are_not_formal_attachment_assignments(monkeypatch):
    html = '''<ul id="content_listContainer">
      <li class="liItem" id="contentListItem:_2_1"><img alt="测试"><h3>第一章测验</h3></li>
      <li class="liItem" id="contentListItem:_3_1"><img alt="考试"><h3>期中考试</h3></li>
      <li class="liItem" id="contentListItem:_4_1"><img alt="作业"><h3>书面作业</h3></li>
      </ul>'''
    monkeypatch.setattr(materials, '_top_level_areas', lambda *a: [{'id': '_10_1', 'title': '教学内容'}])
    response = SimpleNamespace(text=html, raise_for_status=lambda: None)
    items, assignments = materials.walk_materials(SimpleNamespace(get=lambda *a, **kw: response), '_1_1')
    assert [item.content_id for item in assignments] == ['_4_1']
    assert len(items) == 3  # Official tests remain in the complete content tree.


@pytest.mark.parametrize('ambiguous', ['assignments', 'calendar', 'title-only'])
def test_same_title_does_not_attach_ambiguous_deadline_to_formal_target(ambiguous):
    assignments = [Assignment(course_id='_1_1', content_id='_2_1', title='每周作业', source='content-tree')]
    deadlines = [Assignment(course_id='_1_1', source_id='calendar:one', title='每周作业', due_at='2026-10-05T23:00:00Z', source='calendar')]
    if ambiguous == 'assignments':
        assignments.append(Assignment(course_id='_1_1', content_id='_3_1', title='每周作业', source='content-tree'))
    else:
        deadlines.append(Assignment(course_id='_1_1', source_id='calendar:two', title='每周作业', due_at='2026-10-06T23:00:00Z', source='calendar'))
    result = materials.merge_deadlines(assignments, deadlines)
    assert not any(item.due_at for item in assignments)
    assert len(result) == len(assignments) + len(deadlines)


def test_deadline_can_map_by_unique_same_course_content_identity():
    assignment = Assignment(course_id='_1_1', content_id='_2_1', title='正式作业', source='content-tree')
    deadline = Assignment(course_id='_1_1', content_id='_2_1', source_id='calendar:one',
                          title='不同展示标题', due_at='2026-10-05T23:00:00Z', source='calendar')
    assert materials.merge_deadlines([assignment], [deadline]) == [assignment]
    assert assignment.due_at == deadline.due_at


def test_old_test_assignment_cache_is_filtered_and_official_test_has_readonly_source(tmp_path, monkeypatch):
    _client(tmp_path, monkeypatch)
    folder = tmp_path / 'course'
    test = {'source_id': 'pku:content:_8_1', 'content_id': '_8_1', 'parent_content_id': '_10_1',
            'title': '第一次正式测验', 'kind': '测试', 'body_text': '在教学网完成题目', 'path': '教学内容/测试', 'files': [],
            'private_url': 'https://private.example/?token=secret'}
    (folder / 'materials/index.json').write_text(json.dumps([test]), 'utf-8')
    path = folder / 'assignments/index.json'
    rows = json.loads(path.read_text('utf-8'))
    rows.append({'content_id': '_8_1', 'title': test['title'], 'source': 'content-tree', 'instructions': 'old client misclassification'})
    path.write_text(json.dumps(rows), 'utf-8')
    course = campus_course(tmp_path, '_103987_1')
    assert [item['content_id'] for item in course['assignments']] == ['_1714169_1']
    assert campus_courses(tmp_path)['courses'][0]['assignments'] == 1
    assessment = course['official_assessments'][0]
    assert assessment['source_id'] == test['source_id']
    assert 'listContent.jsp' in assessment['source_url'] and 'uploadAssignment' not in assessment['source_url']
    assert assessment['submission_state'] == 'unknown'
    assert 'secret' not in json.dumps(assessment)
    with pytest.raises(PreparationError):
        _assignment(tmp_path, '_103987_1', '_8_1')
    blocks = notion_home._item_blocks('_103987_1', '测验', assessment, '')
    encoded = json.dumps(blocks, ensure_ascii=False)
    assert '教学网正式测验' in encoded and 'uploadAssignment' not in encoded
