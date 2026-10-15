"""Actual installed process restart after independently settled operations."""
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sys
from .core import ResourceBlocked


def settled_scope(snapshot, operation_ids):
    try:
        if (type(snapshot.get('schema')) is not int or snapshot['schema'] != 1
                or snapshot.get('complete') is not True
                or snapshot.get('source') != 'account_cumulative_usage_rows'
                or not isinstance(operation_ids, list) or not operation_ids
                or not all(isinstance(op, str) and op for op in operation_ids)
                or len(set(operation_ids)) != len(operation_ids)):
            raise ValueError('Incomplete charge scope')
        for field in ('jobs', 'settlements', 'submission_posts', 'oss_objects'):
            rows = snapshot[field]
            if not isinstance(rows, list) or len({row['id'] for row in rows}) != len(rows):
                raise ValueError('Ambiguous independent rows')
        paid = False
        for op in operation_ids:
            jobs = [row for row in snapshot['jobs'] if row.get('operation_id') == op]
            charges = [row for row in snapshot['settlements'] if row.get('operation_id') == op]
            if len(jobs) != 1 or len(charges) != 1:
                raise ValueError('Not one settled job')
            job, charge = jobs[0], charges[0]
            if op.startswith('asr:'):
                seconds = charge['charged_seconds']
                if (job['status'] != 'done' or type(seconds) is not int or seconds <= 0
                        or type(job['seconds_charged']) is not int
                        or job['seconds_charged'] != seconds or charge.get('charged_points') is not None):
                    raise ValueError('Unsettled ASR')
                paid = True
            else:
                amount, recorded = charge['charged_points'], job['points_charged']
                if type(amount) not in (int, float) or type(recorded) not in (int, float):
                    raise ValueError('Invalid amount type')
                points = Decimal(str(amount))
                if (job['status'] != 'succeeded' or not points.is_finite() or points < 0
                        or points != Decimal(str(recorded)) or charge.get('charged_seconds') is not None):
                    raise ValueError('Unsettled model job')
                paid = paid or points > 0
        if not paid:
            raise ValueError('No real charge before restart')
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
        raise ResourceBlocked('paid_result_independent_charge_scope_invalid') from None


def owned_runtime(run):
    runtime = run.runtime('primary')
    installed = Path(run.need('instances.primary.installed_path')).resolve()
    root = Path(run.need('test_root')).resolve()
    if (sys.platform != 'win32' or not installed.is_relative_to(root)
            or runtime.get('alive') is not True or runtime.get('single_instance') is not True
            or runtime.get('artifact_sha256') != run.context['artifact_sha256']
            or not runtime.get('process')
            or Path(runtime['process'].get('path', '')).resolve() != installed):
        raise ResourceBlocked('paid_result_restart_native_ownership_unproven')
    return runtime


@contextmanager
def inject(run):
    run.session()
    operation_ids = run.context.get('_restart_after_charge_operations')
    before = run.oracle.operations()
    settled_scope(before, operation_ids)
    old_runtime = owned_runtime(run)
    old_process = run.processes['primary']
    start = len(run.actions)
    receipt = {'variant': 'restart-after-charge', 'boundary': 'actual-owned-installed-process-after-real-settlement',
        'observed': False, 'old_pid': old_process.pid, 'operation_ids': list(operation_ids)}
    try:
        yield receipt
        current = run.processes.get('primary')
        if (old_process.poll() is None or current is None or current is old_process
                or current.pid == old_process.pid or current.poll() is not None):
            raise ResourceBlocked('paid_result_actual_native_restart_required')
        new_runtime = owned_runtime(run)
        quits = [row['native_quit'] for row in run.actions[start:] if 'native_quit' in row]
        if not any(q.get('pid') == old_process.pid and q.get('instance') == 'primary'
                and q.get('graceful') is True and q.get('method') == 'owned-windows-WM_CLOSE' for q in quits):
            raise ResourceBlocked('paid_result_actual_native_restart_required')
        after = run.oracle.operations()
        settled_scope(after, operation_ids)
        if any(before[field] != after[field] for field in (
                'jobs', 'settlements', 'submission_posts', 'oss_objects', 'charged_points', 'charged_seconds')):
            raise ResourceBlocked('paid_result_restart_changed_independent_effects')
        if new_runtime['process']['path'] != old_runtime['process']['path']:
            raise ResourceBlocked('paid_result_restart_native_ownership_unproven')
        receipt.update(observed=True, new_pid=current.pid, graceful=True, independent_effects_unchanged=True)
    finally:
        run.actions.append({'fault_receipt': dict(receipt)})
