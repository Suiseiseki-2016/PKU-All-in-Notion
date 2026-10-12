"""Formal CLI submissions share panel drafts/confirmation; network is mocked."""
import json

import pytest
import typer
from rich.console import Console

from pku_sync import cli
from tests.test_panel_assignment_workflow import _client


def setup(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    settings = client.app.state.recording_manager.settings
    monkeypatch.setattr('pku_sync.config.settings', settings)
    console = Console(record=True)
    monkeypatch.setattr(cli, 'console', console)
    monkeypatch.setattr('pku_sync.auth.get_session', lambda *a, **kw: pytest.fail('preview or cancelled confirmation must not open portal'))
    answer = tmp_path / 'answer.pdf'
    answer.write_bytes(b'previewed answer')
    return settings, answer, console


def invoke(mode, answer, yes):
    if mode == 'file':
        return cli.submit_file_cmd(course='_103987_1', content='_1714169_1', file=str(answer), yes=yes)
    return cli.submit_text_cmd(course='_103987_1', content='_1714169_1', text='我的完整答案', file=None, yes=yes)


@pytest.mark.parametrize('mode', ['file', 'text'])
def test_cli_yes_still_requires_second_confirmation_before_portal(mode, tmp_path, monkeypatch):
    settings, answer, console = setup(tmp_path, monkeypatch)
    confirmations = []
    monkeypatch.setattr(typer, 'confirm', lambda prompt, **kw: confirmations.append(prompt) or False)
    invoke(mode, answer, True)
    assert len(confirmations) == 1
    metadata = [json.loads(path.read_text('utf-8')) for path in (tmp_path / 'submission-drafts').glob('*/draft.json')]
    assert len(metadata) == 1 and metadata[0]['state'] == 'prepared'
    output = console.export_text()
    assert '目标' in output and metadata[0]['sha256'] in output


@pytest.mark.parametrize('mode', ['file', 'text'])
def test_cli_preview_writes_exact_draft_and_manifest_without_portal(mode, tmp_path, monkeypatch):
    settings, answer, console = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(typer, 'confirm', lambda *a, **kw: pytest.fail('dry preview must not request submission confirmation'))
    invoke(mode, answer, False)
    path = next((tmp_path / 'submission-drafts').glob('*/draft.json'))
    metadata = json.loads(path.read_text('utf-8'))
    raw = (path.parent / metadata['archive_name']).read_bytes()
    assert raw == (b'previewed answer' if mode == 'file' else '我的完整答案'.encode('utf-8'))
    assert metadata['portal_account_sha256'] and metadata['requirements_sha256']
    assert str(path.parent) in console.export_text()


def test_cli_nonformal_mapping_is_rejected_before_preview_or_login(tmp_path, monkeypatch):
    settings, answer, console = setup(tmp_path, monkeypatch)
    (tmp_path / 'course/assignments/index.json').write_text('[]', 'utf-8')
    with pytest.raises(typer.Exit):
        invoke('file', answer, True)
    assert not list((tmp_path / 'submission-drafts').glob('*/draft.json'))


@pytest.mark.parametrize('mode', ['file', 'text'])
def test_confirmed_cli_uses_shared_fence_and_saves_actual_verified_receipt(mode, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from pku_sync import catalog_snapshots as catalog
    from pku_sync.panel import assignment_workflow
    from pku_sync.submit import SubmissionForm
    from tests.test_submission_permit import setup as fence_setup
    settings, answer, console = setup(tmp_path, monkeypatch)
    _, _, states, _, call = fence_setup(monkeypatch, tmp_path)
    rows = json.loads((tmp_path / 'course/assignments/index.json').read_text('utf-8'))
    monkeypatch.setattr(catalog, 'current', lambda *a: {'revision': 1, 'snapshot': {'sections': {'assignments': {'items': rows}}}})
    monkeypatch.setattr(typer, 'confirm', lambda *a, **kw: True)
    posts, closed = [], []
    class Campus:
        def post(self, path, **kwargs):
            assert list(states.values())[0]['state'] == 'verification_pending'
            posts.append(kwargs)
            return SimpleNamespace(status_code=200, text='{}', json=lambda: {'destinationUrl': '/review'})
        def get(self, path, params):
            return SimpleNamespace(status_code=200, text='<div>复查提交历史记录</div>',
                url='https://course.pku.edu.cn/webapps/assignment/uploadAssignment?course_id=_103987_1&content_id=_1714169_1')
        def close(self): closed.append(True)
    monkeypatch.setattr(assignment_workflow, 'get_session', lambda **kw: Campus())
    monkeypatch.setattr(assignment_workflow, 'fetch_form', lambda *a, **kw: SubmissionForm('_103987_1', '_1714169_1', {}))
    invoke(mode, answer, True)
    metadata = json.loads(next((tmp_path / 'submission-drafts').glob('*/draft.json')).read_text('utf-8'))
    assert len(posts) == 1 and closed == [True]
    assert list(states.values())[0]['state'] == metadata['state'] == 'submitted'
    assert metadata['receipt']['review_history_html'] == '<div>复查提交历史记录</div>'
    assert metadata['submission_fence']['protocol'] == 1
    if mode == 'text':
        assert posts[0]['data']['studentSubmission.text'] == '我的完整答案'
    else:
        assert posts[0]['files']['newFile_LocalFile0'][1] == b'previewed answer'


def test_confirmed_cli_cannot_fall_back_when_shared_service_is_unavailable(tmp_path, monkeypatch):
    from pku_sync import catalog_snapshots as catalog
    from pku_sync.panel import assignment_workflow
    settings, answer, console = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(typer, 'confirm', lambda *a, **kw: True)
    def unavailable(*args):
        raise catalog.CatalogUnavailable('service unavailable')
    monkeypatch.setattr(catalog, 'current', unavailable)
    monkeypatch.setattr(assignment_workflow, 'get_session', lambda **kw: pytest.fail('no shared service means no portal login or POST'))
    with pytest.raises(typer.Exit):
        invoke('file', answer, True)
    metadata = json.loads(next((tmp_path / 'submission-drafts').glob('*/draft.json')).read_text('utf-8'))
    assert metadata['state'] == 'prepared'


def test_actual_cli_yes_flag_does_not_bypass_interactive_confirmation(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    settings, answer, console = setup(tmp_path, monkeypatch)
    result = CliRunner().invoke(cli.app, ['submit', 'file', '--course', '_103987_1',
        '--content', '_1714169_1', '--file', str(answer), '--yes'], input='n\n')
    assert result.exit_code == 0, result.output
    assert '确认向教学网正式提交' in result.output
    metadata = json.loads(next((tmp_path / 'submission-drafts').glob('*/draft.json')).read_text('utf-8'))
    assert metadata['state'] == 'prepared' and 'submission_fence' not in metadata


def test_failed_portal_snapshot_remains_read_only_for_confirmed_cli(tmp_path, monkeypatch):
    from pku_sync import catalog_snapshots as catalog
    from pku_sync.panel import assignment_workflow
    settings, answer, console = setup(tmp_path, monkeypatch)
    catalog.read_status(tmp_path, False, 'incomplete official read')
    monkeypatch.setattr(typer, 'confirm', lambda *a, **kw: True)
    monkeypatch.setattr(catalog, 'current', lambda *a: pytest.fail('failed portal read must not acquire a shared write permit'))
    monkeypatch.setattr(assignment_workflow, 'get_session', lambda **kw: pytest.fail('failed portal read must remain read only'))
    with pytest.raises(typer.Exit):
        invoke('file', answer, True)
