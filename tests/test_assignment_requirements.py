"""Development regressions. These do not constitute native E2E evidence."""
import json
from types import SimpleNamespace

import pytest

from pku_sync import platform
from pku_sync.panel import assignment_requirements as requirements
from pku_sync.panel.assignment_workflow import PreparationError


def fixture(tmp_path, monkeypatch):
    settings = SimpleNamespace(data_dir=tmp_path, pku_username='student', platform_token='test-session')
    official = {'content_id': '_42_1', 'source_id': 'assignment:42', 'title': '正式作业',
                'instructions': '答案和程序打成一个 ZIP，文件名为学号姓名.zip。', 'due_at': '2026-10-01'}
    monkeypatch.setattr(requirements, '_assignment', lambda *args: official)
    monkeypatch.setattr(platform, 'auth_me', lambda *args: {'available': True, 'email_verified': True, 'email': 'test@example.com'})
    monkeypatch.setattr(platform, 'quota', lambda *args: {'available': True, 'llm_points_remaining': 10})
    calls = []
    result = {'requirements': [{'quote': '答案和程序打成一个 ZIP'}],
              'file_formats': [{'value': 'ZIP', 'quote': '答案和程序打成一个 ZIP'}],
              'filename_rules': [{'quote': '文件名为学号姓名.zip'}], 'uncertainties': []}
    def model(operation, prompt, settings, **kwargs):
        calls.append((operation, kwargs))
        return {'content': json.dumps(result, ensure_ascii=False), 'points_charged': .01}
    monkeypatch.setattr(platform, 'llm', model)
    return settings, official, calls, result


def test_confirmed_requirements_are_grounded_reused_and_correlated(tmp_path, monkeypatch):
    settings, official, calls, _ = fixture(tmp_path, monkeypatch)
    one = requirements.analyze(settings, '_101_1', '_42_1', confirmed=True)
    two = requirements.analyze(settings, '_101_1', '_42_1', confirmed=True)
    assert one['due_at'] == official['due_at'] and one['source_grounding_verified']
    assert one['points_charged'] == .01 and two['points_charged'] == 0 and two['reused']
    assert len(calls) == 1
    context = calls[0][1]['operation_context']
    assert context['purpose'] == 'assignment-requirements' and context['content_id'] == '_42_1'
    assert context['source_id'] == one['requirements_sha256']


def test_unconfirmed_analysis_never_calls_identity_or_model(tmp_path, monkeypatch):
    settings, _, calls, _ = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(platform, 'auth_me', lambda *args: pytest.fail('unconfirmed request read account'))
    with pytest.raises(PreparationError, match='确认'):
        requirements.analyze(settings, '_101_1', '_42_1')
    assert not calls


@pytest.mark.parametrize('kind', ['invented_quote', 'invented_format'])
def test_unsupported_paid_result_is_preserved_without_recharge(tmp_path, monkeypatch, kind):
    settings, _, calls, result = fixture(tmp_path, monkeypatch)
    if kind == 'invented_quote':
        result['requirements'][0]['quote'] = '必须提交 PDF'
    else:
        result['file_formats'][0]['value'] = 'PDF'
    for _ in range(2):
        with pytest.raises(PreparationError, match='原文'):
            requirements.analyze(settings, '_101_1', '_42_1', confirmed=True)
    assert len(calls) == 1
    record = json.loads(next((tmp_path / 'panel/assignment-requirements').glob('*/*.json')).read_text('utf-8'))
    assert record['phase'] == 'relay_succeeded' and record['response']['points_charged'] == .01


def test_changed_official_requirements_do_not_reuse_prior_result(tmp_path, monkeypatch):
    settings, official, calls, result = fixture(tmp_path, monkeypatch)
    one = requirements.analyze(settings, '_101_1', '_42_1', confirmed=True)
    official['instructions'] = '只上传一个 PDF。'
    result.update(requirements=[{'quote': '只上传一个 PDF'}], file_formats=[{'value': 'PDF', 'quote': '只上传一个 PDF'}], filename_rules=[])
    two = requirements.analyze(settings, '_101_1', '_42_1', confirmed=True)
    assert one['requirements_sha256'] != two['requirements_sha256'] and not two['reused']
    assert len(calls) == 2


def test_exact_quote_strings_are_canonicalized_without_rewriting_or_recharge(tmp_path, monkeypatch):
    settings, official, calls, result = fixture(tmp_path, monkeypatch)
    result['requirements'] = ['答案和程序打成一个 ZIP']
    result['filename_rules'] = ['文件名为学号姓名.zip']
    one = requirements.analyze(settings, '_101_1', '_42_1', confirmed=True)
    two = requirements.analyze(settings, '_101_1', '_42_1', confirmed=True)
    assert one['analysis']['requirements'] == [{'quote': '答案和程序打成一个 ZIP'}]
    assert one['analysis']['filename_rules'] == [{'quote': '文件名为学号姓名.zip'}]
    assert all(row['quote'] in official['instructions'] for row in one['analysis']['requirements'])
    assert two['reused'] and two['points_charged'] == 0 and len(calls) == 1
    record = json.loads(next((tmp_path / 'panel/assignment-requirements').glob('*/*.json')).read_text('utf-8'))
    assert json.loads(record['response']['content']) == result  # Preserve raw paid response.


@pytest.mark.parametrize('quote', ['必须提交 PDF', '', '答案和程序打成一个 zip'])
def test_quote_string_conversion_retains_exact_official_grounding(tmp_path, monkeypatch, quote):
    settings, _, calls, result = fixture(tmp_path, monkeypatch)
    result['requirements'] = [quote]
    for _ in range(2):
        with pytest.raises(PreparationError, match='原文'):
            requirements.analyze(settings, '_101_1', '_42_1', confirmed=True)
    assert len(calls) == 1


def test_saved_paid_response_recovery_reports_zero_current_cost(tmp_path, monkeypatch):
    settings, _, calls, _ = fixture(tmp_path, monkeypatch)
    requirements.analyze(settings, '_101_1', '_42_1', confirmed=True)
    path = next((tmp_path / 'panel/assignment-requirements').glob('*/*.json'))
    record = json.loads(path.read_text('utf-8'))
    record['phase'] = 'relay_succeeded'  # Interrupted after durable paid response.
    path.write_text(json.dumps(record), 'utf-8')
    resumed = requirements.analyze(settings, '_101_1', '_42_1', confirmed=True)
    assert resumed['reused'] is True and resumed['points_charged'] == 0
    assert resumed['receipt_points_charged'] == .01 and len(calls) == 1
