import pytest

from pku_sync.knowledge_notes import source_span, timed_unit_markdown, unit_payload


def test_late_lecture_time_comes_from_source_and_model_body_is_unchanged():
    segments = [{"start": 6326.1, "end": 6400, "text": "原讲述"},
                {"start": 6997.25, "end": 7013.1, "text": "末段"}]
    body = "### 一个自然标题\n\n实验背景及推断。"
    rendered = timed_unit_markdown("后天刺激", segments, body)
    assert rendered == "## 后天刺激 [105:26–116:53]\n\n" + body + "\n"
    assert source_span(segments) == (6326.1, 7013.1)


def test_payload_keeps_full_original_records_and_separates_reference_context():
    original = [{"start": 1768.37, "end": 1772.99, "text": "我妈妈单位一个同事的小孩"}]
    reference = [{"start": 1700, "end": 1710, "text": "上下文"}]
    payload = unit_payload("课堂案例", original, reference_context=reference)
    assert payload['original_transcript'] == original
    assert payload['reference_context'] == reference
    payload['original_transcript'][0]['text'] = "改动副本"
    assert original[0]['text'] == "我妈妈单位一个同事的小孩"


@pytest.mark.parametrize("start,end", [(True, 10), (float('nan'), 10), (1, float('inf')), (10, 9), (-1, 1)])
def test_invalid_source_time_cannot_become_a_replay_position(start, end):
    with pytest.raises(ValueError):
        source_span([{"start": start, "end": end, "text": "仍有原文"}])


def test_no_source_does_not_fabricate_a_zero_time_range():
    with pytest.raises(ValueError):
        source_span([])
