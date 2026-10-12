"""Loss-free assembly, paid-cache reuse, and recovery through product entry points."""
import json
import threading
from pathlib import Path

import pytest

from pku_sync import knowledge_workflow as notes
from pku_sync.note_editor import report_path
from test_knowledge_workflow import config, lecture, plan


def test_default_assembly_preserves_every_parallel_response_in_source_order(tmp_path):
    barrier = threading.Barrier(3)
    returned = []
    last_finished = threading.Event()
    bodies = ['解释及完整实验经过。\n\n数量是860亿。',
              '人物例子，结局及条件。\n\n```python\nprint("课堂原话")\n```',
              '没有复现成功，博士生最后换了方向。']
    def ask(system, prompt):
        if system == notes.PLAN_SYSTEM:
            return plan()
        i = json.loads(prompt)['unit']
        barrier.wait(3)
        if i != 2:
            assert last_finished.wait(3)
        returned.append(i)
        if i == 2:
            last_finished.set()
        return bodies[i]
    target = tmp_path / 'notes.md'
    notes.write_notes(lecture(), target, config(), '课程', ask=ask,
                      merge_ask=lambda *a: pytest.fail('whole-lecture rewrite was called'))
    assert returned[0] == 2
    body, sections = notes.saved_sections(target, lecture())
    assert body == target.read_text('utf-8')
    assert len(sections) == 3
    assert [body.index(text) for text in bodies] == sorted(body.index(text) for text in bodies)
    assert all(body.count(text) == 1 for text in bodies)
    assert [(s['start'], s['end']) for s in sections] == [
        (row['start'], row['end']) for row in lecture()['segments']]
    report = json.loads(report_path(target).read_text('utf-8'))
    assert report['assembly'] == 'source-order'
    assert report['merge_request'] is report['merge_response'] is report['merge_model'] is None
    assert notes.record_issues(target, lecture()) == []


def test_existing_paid_units_reassemble_without_calls_or_changing_previous_note(tmp_path):
    original = tmp_path / 'previous.md'
    def ask(system, prompt):
        return plan() if system == notes.PLAN_SYSTEM else f"全部段落{json.loads(prompt)['unit']}。"
    notes.write_notes(lecture(), original, config(), '课程', ask=ask)
    protected = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    target = tmp_path / 'complete.md'
    notes.write_notes(lecture(), target, config(), '课程',
                      ask=lambda *a: pytest.fail('cached unit or plan was charged again'))
    assert target.read_bytes() == original.read_bytes()
    assert all(p.read_bytes() == raw for p, raw in protected.items())
    assert notes.record_issues(target, lecture()) == []


def test_disk_full_after_assembly_restores_exact_note_through_media_without_credentials(tmp_path, monkeypatch):
    from pku_sync import media
    target = tmp_path / 'notes.md'
    replace = Path.replace
    def disk_full(path, dest):
        if Path(dest) == target:
            raise OSError('disk full after assembly')
        return replace(path, dest)
    monkeypatch.setattr(Path, 'replace', disk_full)
    with pytest.raises(OSError, match='disk full'):
        notes.write_notes(lecture(), target, config(), '课程',
            ask=lambda system, prompt: plan() if system == notes.PLAN_SYSTEM else '完整已付费正文。')
    assert not target.exists() and report_path(target).exists()
    expected = notes.saved_sections(target, lecture())[0]
    protected = {p: p.read_bytes() for p in tmp_path.rglob('*.json')}
    monkeypatch.setattr(Path, 'replace', replace)
    cfg = config()
    cfg.platform_token = ''
    cfg.notes_strategy = 'topics'
    assert media.write_notes(lecture(), [], target, cfg, '课程') == target
    assert target.read_text('utf-8') == expected
    assert all(p.read_bytes() == raw for p, raw in protected.items())


def test_complete_unit_responses_need_no_whole_lecture_merge_capacity(tmp_path):
    # Each source unit is valid; the concatenated drafts exceed request capacity.
    huge = '详细解释、例子以及完整实验经过。' * 12000
    target = tmp_path / 'notes.md'
    notes.write_notes(lecture(), target, config(), '课程',
        ask=lambda system, prompt: plan() if system == notes.PLAN_SYSTEM else huge)
    body, sections = notes.saved_sections(target, lecture())
    assert body.count(huge) == 3 and len(sections) == 3
    assert notes.record_issues(target, lecture()) == []


def test_product_topics_entry_assembles_without_model_merge_and_keeps_image_intervals(tmp_path, monkeypatch):
    from pku_sync import media
    from pku_sync.notion import markdown_to_blocks
    from pku_sync.panel import note_images
    calls = []
    def model_call(llm, model, prompt, system, settings):
        calls.append(system)
        assert system in {notes.PLAN_SYSTEM, notes.UNIT_SYSTEM}
        return plan() if system == notes.PLAN_SYSTEM else f"完整知识点{json.loads(prompt)['unit']}。"
    monkeypatch.setattr(media, '_recovery_llm', model_call)
    cfg = config()
    cfg.platform_token = 'offline-fixture'
    cfg.notes_strategy = 'topics'
    target = tmp_path / 'notes.md'
    assert media.write_notes(lecture(), [], target, cfg, '课程') == target
    assert calls.count(notes.PLAN_SYSTEM) == 1 and calls.count(notes.UNIT_SYSTEM) == 3
    blocks = markdown_to_blocks(target.read_text('utf-8'))
    for i, block in enumerate(blocks):
        block['id'] = f'b{i}'
    frames = [{'timestamp': row['start'] + 1, 'time': str(i), 'path': tmp_path / f'{i}.jpg'}
              for i, row in enumerate(lecture()['segments'])]
    monkeypatch.setattr(note_images, '_window_frame',
                        lambda frames, start, end, directory: next(f for f in frames if start <= f['timestamp'] < end))
    placements = note_images._knowledge_placements(blocks, frames, target, 0)
    assert [frame for _, frame in placements] == frames
    headings = {b['id']: note_images._text(b) for b in blocks if b['type'] == 'heading_2'}
    assert [headings[key] for key, _ in placements] == [
        '知识点0 [105:26–106:40]', '知识点1 [108:20–110:10]', '知识点2 [116:37–116:53]']
