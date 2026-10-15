"""Required fault phases must not be hidden by a live-only resource check."""
from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from product_e2e import catalog_journeys as journeys
from product_e2e.core import ResourceBlocked
from scripts.product_e2e_gate import required_assertions


class ReachedMandatoryFault(RuntimeError):
    pass


def fixture_run(monkeypatch, mode):
    scenario = next(row for row in json.loads((Path(__file__).resolve().parents[1]
        / 'docs/PRODUCT_E2E_SCENARIOS.json').read_text('utf-8'))['scenarios'] if row['id'] == 'JOURNEY-06')
    ui = SimpleNamespace(visible=lambda selector: True,
        request_read=lambda path: {'courses': [{'id': '_123_1'}]})
    @contextmanager
    def fault(variant):
        if variant == 'offline':
            raise ReachedMandatoryFault('First required network fault reached')
        yield
    run = SimpleNamespace(mode=mode, scenario=scenario, session=lambda: ui, config={},
        need=lambda key: '_123_1', check=lambda *args: None, fault=fault)
    monkeypatch.setattr(journeys, '_catalog_scope_preflight', lambda run: None)
    monkeypatch.setattr(journeys, '_effects', lambda run: {'charged_points': 0})
    monkeypatch.setattr(journeys, '_action', lambda *args: None)
    monkeypatch.setattr(journeys, '_open', lambda *args: {'id': '_123_1', 'lessons': []})
    monkeypatch.setattr(journeys, '_task', lambda *args: {'state': 'cancelled'})
    monkeypatch.setattr(journeys, '_start_refresh', lambda *args: {'kind': 'sync', 'state': 'running'})
    monkeypatch.setattr(journeys, '_cancel_refresh', lambda *args: {'kind': 'sync', 'state': 'cancelled'})
    monkeypatch.setattr(journeys, '_file_hashes', lambda *args, **kwargs: {'student-answer': 'fixture-hash'})
    def require_actual_dpi(*args):
        raise ResourceBlocked('actual-native-supported-dpi-required')
    monkeypatch.setattr(journeys, '_layout_probe', require_actual_dpi)
    return run


def test_fault_journey_reaches_mandatory_fault_despite_absent_live_only_dpi(monkeypatch):
    run = fixture_run(monkeypatch, 'fault')
    assert 'welcome-course-list-left-account-and-course-centers-work-at-supported-dpi-with-keyboard-scroll' not in required_assertions(run.scenario, 'fault')
    with pytest.raises(ReachedMandatoryFault):
        journeys.run_resilience(run)


def test_live_journey_still_requires_actual_dpi_and_cannot_pass_without_it(monkeypatch):
    run = fixture_run(monkeypatch, 'live')
    assert 'welcome-course-list-left-account-and-course-centers-work-at-supported-dpi-with-keyboard-scroll' in required_assertions(run.scenario, 'live')
    with pytest.raises(ResourceBlocked, match='actual-native-supported-dpi-required'):
        journeys.run_resilience(run)


def test_missing_physical_dpi_keeps_failure_but_executes_independent_cost_phase(monkeypatch):
    run = fixture_run(monkeypatch, 'live')
    run.blockers = []
    checks, costs = [], []
    run.check = lambda key, passed, *args: checks.append((key, passed))
    def missing(*args):
        raise ResourceBlocked('native-os-dpi-controller-or-prior-measured-runs-required')
    monkeypatch.setattr(journeys, '_layout_probe', missing)
    monkeypatch.setattr(journeys, '_resilience_cost_receipt', lambda value: costs.append('actual-cost-phase'))
    journeys.run_resilience(run)
    assert costs == ['actual-cost-phase']
    layout = 'welcome-course-list-left-account-and-course-centers-work-at-supported-dpi-with-keyboard-scroll'
    assert (layout, False) in checks
    assert run.blockers == ['native-os-dpi-controller-or-prior-measured-runs-required']


@pytest.mark.parametrize('reason', ['native-webview-dpi-disagrees-with-windows-monitor-or-window',
    'native-dpi-report-candidate-or-source-mismatch', 'native-dpi-physical-screenshot-invalid'])
def test_invalid_dpi_identity_or_evidence_cannot_continue_to_cost_actions(monkeypatch, reason):
    run = fixture_run(monkeypatch, 'live')
    run.blockers = []
    def invalid(*args):
        raise ResourceBlocked(reason)
    monkeypatch.setattr(journeys, '_layout_probe', invalid)
    monkeypatch.setattr(journeys, '_resilience_cost_receipt', lambda *args: pytest.fail('unsafe evidence continued'))
    with pytest.raises(ResourceBlocked, match=reason):
        journeys.run_resilience(run)
