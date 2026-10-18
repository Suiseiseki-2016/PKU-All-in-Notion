"""Local MCP knowledge search includes course summaries and material metadata."""
from __future__ import annotations

import json

import pytest

from pku_sync.mcp_tools import LocalCourseTools


@pytest.fixture
def local_knowledge(tmp_path):
    course = tmp_path / 'Networks'
    course.mkdir()
    (course / 'course.json').write_text(json.dumps({
        'course_id': 'networks', 'name': '计算机网络'}), 'utf8')
    (course / 'course-summary.md').write_text('# 课程总结\n复习滑动窗口与可靠传输。', 'utf8')
    (course / 'materials').mkdir()
    (course / 'materials/index.json').write_text(json.dumps([{
        'title': '第三讲课件', 'body_text': '发送窗口与确认机制', 'category': '课堂课件',
        'files': ['Lec03_SlidingWindow.pdf'],
        'source_url': 'https://private.example/secret-material-url',
        'token': 'private-material-token',
    }], ensure_ascii=False), 'utf8')
    return LocalCourseTools(tmp_path)


def test_search_local_course_summary(local_knowledge):
    hits = local_knowledge.search_course_knowledge('滑动窗口', 'networks')
    assert len(hits) == 1
    assert hits[0]['source_type'] == 'summary'
    assert hits[0]['title'] == '计算机网络 课程总结'
    assert '复习滑动窗口与可靠传输' in hits[0]['snippet']
    assert local_knowledge.search_course_knowledge('滑动窗口', 'other') == []


def test_course_summary_includes_local_assignment_deadlines(local_knowledge):
    folder = local_knowledge.data_dir / 'Networks' / 'assignments'
    folder.mkdir()
    (folder / 'index.json').write_text(json.dumps([
        {'title': '窗口练习', 'due_at': '2026-10-18T23:59:00+08:00',
         'due_at_label': '后天', 'source_url': 'https://private.example/assignment'},
        {'title': '复习题', 'due_at': '', 'due_at_label': '下周课前'},
        {'title': '课堂任务'},
        {},
        None,
    ], ensure_ascii=False), 'utf8')
    expected = [
        {'title': '窗口练习', 'due_at': '2026-10-18T23:59:00+08:00'},
        {'title': '复习题', 'due_at': '下周课前'},
        {'title': '课堂任务', 'due_at': ''},
        {'title': '', 'due_at': ''},
    ]
    for summary in (local_knowledge.get_course_summary('networks'),
                    local_knowledge.get_course_summary()['courses'][0]):
        assert summary['assignments_count'] == len(expected)
        assert summary['assignments'] == expected
        assert summary['markdown']
        assert 'total_lessons' in summary and 'summarized_lessons' in summary


@pytest.mark.parametrize('content', [None, '{broken', 'null', '{}'])
def test_course_summary_tolerates_missing_or_corrupted_assignments(local_knowledge, content):
    if content is not None:
        folder = local_knowledge.data_dir / 'Networks' / 'assignments'
        folder.mkdir()
        (folder / 'index.json').write_text(content, 'utf8')
    summary = local_knowledge.get_course_summary('networks')
    assert summary['assignments_count'] == 0
    assert summary['assignments'] == []
    assert summary['markdown']


@pytest.mark.parametrize('error', [OSError, ValueError])
def test_course_summary_isolates_assignment_read_failure(local_knowledge, monkeypatch, error):
    def fail(course_folder):
        assert course_folder == 'Networks'
        raise error('assignment index unavailable')

    monkeypatch.setattr(local_knowledge, 'get_course_assignments', fail)
    summary = local_knowledge.get_course_summary('networks')
    assert summary['assignments_count'] == 0
    assert summary['assignments'] == []
    assert summary['markdown']


def test_missing_assignment_index_returns_empty_list(local_knowledge):
    assert local_knowledge.get_course_assignments('Networks') == []


@pytest.mark.parametrize('content', ['{broken', 'null', '{}'])
def test_existing_invalid_assignment_index_raises(local_knowledge, content):
    folder = local_knowledge.data_dir / 'Networks' / 'assignments'
    folder.mkdir()
    (folder / 'index.json').write_text(content, 'utf8')
    with pytest.raises(ValueError):
        local_knowledge.get_course_assignments('Networks')


@pytest.mark.parametrize('query', ['Lec03_SlidingWindow.pdf', '第三讲课件', '确认机制', '课堂课件'])
def test_search_local_materials_and_attachment_filenames(local_knowledge, query):
    hits = local_knowledge.search_course_knowledge(query, 'networks')
    assert len(hits) == 1
    assert hits[0]['source_type'] == 'materials'
    assert 'Lec03_SlidingWindow.pdf' in hits[0]['snippet']
    assert 'secret-material-url' not in hits[0]['snippet']
    assert 'private-material-token' not in hits[0]['snippet']


def test_search_materials_excludes_private_metadata(local_knowledge):
    assert local_knowledge.search_course_knowledge('secret-material-url') == []
    assert local_knowledge.search_course_knowledge('private-material-token') == []


def test_knowledge_search_priority_at_equal_scores(tmp_path):
    course = tmp_path / 'Networks'
    course.mkdir()
    (course / 'course.json').write_text('{"course_id":"networks"}', 'utf8')
    (course / 'course-summary.md').write_text('TCP', 'utf8')
    recording = course / 'recordings' / 'lecture'
    recording.mkdir(parents=True)
    (recording / 'notes.md').write_text('TCP', 'utf8')
    (recording / 'transcript.json').write_text('{"segments":[{"text":"TCP"}]}', 'utf8')
    for section in ('assignments', 'announcements', 'materials'):
        (course / section).mkdir()
        (course / section / 'index.json').write_text('[{"title":"TCP"}]', 'utf8')
    hits = LocalCourseTools(tmp_path).search_course_knowledge('TCP')
    assert [row['source_type'] for row in hits] == [
        'summary', 'notes', 'assignments', 'announcements', 'materials', 'transcript']
