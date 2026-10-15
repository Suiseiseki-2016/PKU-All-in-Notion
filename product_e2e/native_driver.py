"""Dispatch installed-native journeys and retain truthful partial evidence."""
from __future__ import annotations

import json
import ast
from copy import deepcopy
import hashlib
import math
import time
import traceback
from pathlib import Path
from decimal import Decimal
from urllib.parse import unquote, urlsplit

from .catalog_journeys import run_catalog, run_identity, run_resilience
from .core import NativeRun, ResourceBlocked
from .learning_journeys import run_assignment, run_cross_platform, run_notes, run_quiz


MAIN_CHECKS = (
    "selected-course-synchronized-within-original-deadline",
    "home-course-center-lesson-parent-and-marker-readback",
    "explicit-original-download-correct-source-and-playable-on-windows",
    "short-real-asr-and-summary-reviewed-and-published",
    "summary-link-and-source-acceptance-correct",
    "retry-reuses-artifacts-one-settlement-and-oss-audio-cleanup",
    "only-selected-accepted-stable-summary-sources",
    "choice-blank-short-answer-and-reference-grounding",
    "notion-editable-answers-without-answer-key-leak",
    "answers-survive-reopen-and-local-restart",
    "objective-oracle-and-bounded-subjective-grade",
    "original-answers-history-and-review-content-preserved",
    "changed-question-or-answer-needs-confirmed-regrade",
    "concurrent-generate-grade-and-recovery-one-job-one-settlement",
)


def _repair_json(spec):
    from .learning_journeys import _safe_review_file
    try:
        path = _safe_review_file(spec['path'])
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != spec['sha256']:
            raise ValueError
        return json.loads(raw), (path, spec['sha256'])
    except (OSError, KeyError, TypeError, ValueError):
        raise ResourceBlocked('main_repair_frozen_input_invalid') from None


def _repair_modules(run, proof, prior_installed_path):
    """Verify both physical installations against the version's exact reviewed fixes."""
    from .learning_journeys import _safe_review_file
    try:
        expected = {p.relative_to(run.context['root']).as_posix()
                    for p in (Path(run.context['root']) / 'pku_sync').rglob('*.py')}
        expected.add('pku_sync/panel/static/app.js')
        values, bound = [], []
        for key, artifact, binary in [('prior_product_manifest', proof['prior_artifact_sha256'], prior_installed_path),
                              ('current_product_manifest', run.context['artifact_sha256'], run.context['installed_path'])]:
            manifest, item = _repair_json(proof[key]); bound.append(item)
            product_root = _safe_review_file(manifest['product_root'])
            if (not product_root.is_relative_to(Path(proof['test_root']).resolve())
                    or product_root != _safe_review_file(str(Path(binary).parent / 'resources/runtime/Lib/site-packages'))
                    or manifest['artifact_sha256'] != artifact or set(manifest['files']) != expected):
                raise ValueError
            actual = {p.relative_to(product_root).as_posix()
                      for p in (product_root / 'pku_sync').rglob('*.py')}
            actual.add('pku_sync/panel/static/app.js')
            if actual != expected:
                raise ValueError
            for relative, digest in manifest['files'].items():
                path = _safe_review_file(str(product_root / relative))
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise ValueError
                bound.append((path, digest))
            values.append(manifest['files'])
        changes = {key for key in expected if values[0][key] != values[1][key]}
        if proof.get('current_version') == '0.1.38':
            allowed = {'pku_sync/lecture_source_download.py'}
            declared = proof.get('allowed_changed_file') == 'pku_sync/lecture_source_download.py'
        elif proof.get('current_version') == '0.1.39':
            allowed = {'pku_sync/lecture_source_download.py', 'pku_sync/panel/notion_workspace.py'}
            declared_files = proof.get('allowed_changed_files')
            declared = isinstance(declared_files, list) and len(declared_files) == 2 and set(declared_files) == allowed
        elif proof.get('current_version') == '0.1.40':
            allowed = {'pku_sync/lecture_source_download.py', 'pku_sync/panel/notion_workspace.py',
                       'pku_sync/panel/static/app.js'}
            declared_files = proof.get('allowed_changed_files')
            declared = isinstance(declared_files, list) and len(declared_files) == 3 and set(declared_files) == allowed
        elif proof.get('current_version') == '0.1.41':
            allowed = {'pku_sync/lecture_source_download.py', 'pku_sync/panel/notion_workspace.py',
                       'pku_sync/panel/static/app.js', 'pku_sync/media.py'}
            declared_files = proof.get('allowed_changed_files')
            declared = isinstance(declared_files, list) and len(declared_files) == 4 and set(declared_files) == allowed
            prior_root = Path(prior_installed_path).parent / 'resources/runtime/Lib/site-packages'
            current_root = Path(run.context['installed_path']).parent / 'resources/runtime/Lib/site-packages'
            if not _repair_media_citation_ast(prior_root / 'pku_sync/media.py', current_root / 'pku_sync/media.py'):
                raise ValueError
        elif proof.get('current_version') in {'0.1.42', '0.1.43'}:
            allowed = {'pku_sync/lecture_source_download.py', 'pku_sync/panel/notion_workspace.py',
                       'pku_sync/panel/static/app.js', 'pku_sync/media.py', 'pku_sync/claim_audit.py'}
            declared_files = proof.get('allowed_changed_files')
            declared = isinstance(declared_files, list) and len(declared_files) == 5 and set(declared_files) == allowed
            prior_root = Path(prior_installed_path).parent / 'resources/runtime/Lib/site-packages'
            current_root = Path(run.context['installed_path']).parent / 'resources/runtime/Lib/site-packages'
            if (not _repair42_media_ast(prior_root / 'pku_sync/media.py', current_root / 'pku_sync/media.py')
                    or not _repair_claim_audit_ast(prior_root / 'pku_sync/claim_audit.py', current_root / 'pku_sync/claim_audit.py')):
                raise ValueError
        elif proof.get('current_version') in {'0.1.44', '0.1.45', '0.1.46'}:
            allowed = {'pku_sync/lecture_source_download.py', 'pku_sync/panel/notion_workspace.py',
                       'pku_sync/panel/static/app.js', 'pku_sync/media.py', 'pku_sync/claim_audit.py'}
            declared_files = proof.get('allowed_changed_files')
            declared = isinstance(declared_files, list) and len(declared_files) == 5 and set(declared_files) == allowed
            prior_root = Path(prior_installed_path).parent / 'resources/runtime/Lib/site-packages'
            current_root = Path(run.context['installed_path']).parent / 'resources/runtime/Lib/site-packages'
            if not _repair44_media_ast(prior_root / 'pku_sync/media.py', current_root / 'pku_sync/media.py'):
                raise ValueError
            stages = proof.get('additional_summary_attempts')
            expected_count = {'0.1.44': 4, '0.1.45': 5, '0.1.46': 6}[proof['current_version']]
            if not isinstance(stages, list) or len(stages) != expected_count:
                raise ValueError
            previous, item = _repair_json(stages[expected_count - 1]['product_manifest']); bound.append(item)
            previous_root = _safe_review_file(previous['product_root'])
            if set(previous['files']) != expected:
                raise ValueError
            for name, digest in previous['files'].items():
                path = _safe_review_file(str(previous_root / name))
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise ValueError
                bound.append((path, digest))
                if name != 'pku_sync/media.py' and values[1][name] != digest:
                    raise ValueError
            if proof['current_version'] in {'0.1.45', '0.1.46'}:
                if not _repair_function_bodies_ast(previous_root / 'pku_sync/media.py', current_root / 'pku_sync/media.py',
                                                   ('write_notes', '_repair_factual_conflicts')):
                    raise ValueError
            elif not _repair44_media_ast(previous_root / 'pku_sync/media.py', current_root / 'pku_sync/media.py', since43=True):
                raise ValueError
        elif proof.get('current_version') == '0.1.47':
            allowed = {'pku_sync/lecture_source_download.py', 'pku_sync/panel/notion_workspace.py',
                       'pku_sync/panel/static/app.js', 'pku_sync/media.py', 'pku_sync/claim_audit.py', 'pku_sync/platform.py'}
            declared_files = proof.get('allowed_changed_files')
            declared = isinstance(declared_files, list) and len(declared_files) == 6 and set(declared_files) == allowed
            stages = proof.get('additional_summary_attempts')
            if not isinstance(stages, list) or len(stages) != 7:
                raise ValueError
            previous, item = _repair_json(stages[6]['product_manifest']); bound.append(item)
            previous_root = _safe_review_file(previous['product_root'])
            current_root = Path(run.context['installed_path']).parent / 'resources/runtime/Lib/site-packages'
            prior_root = Path(prior_installed_path).parent / 'resources/runtime/Lib/site-packages'
            if set(previous['files']) != expected:
                raise ValueError
            reviewed = {'pku_sync/media.py', 'pku_sync/claim_audit.py', 'pku_sync/platform.py'}
            if {name for name in expected if previous['files'][name] != values[1][name]} != reviewed:
                raise ValueError
            for name, digest in previous['files'].items():
                path = _safe_review_file(str(previous_root / name))
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise ValueError
                bound.append((path, digest))
            if (not _repair44_media_ast(prior_root / 'pku_sync/media.py', current_root / 'pku_sync/media.py')
                    or not _repair_function_bodies_ast(previous_root / 'pku_sync/media.py', current_root / 'pku_sync/media.py',
                                                       ('write_notes', '_repair_factual_conflicts'))
                    or not _repair_claim_audit_ast(previous_root / 'pku_sync/claim_audit.py', current_root / 'pku_sync/claim_audit.py')
                    or not _repair47_platform_ast(previous_root / 'pku_sync/platform.py', current_root / 'pku_sync/platform.py')):
                raise ValueError
        elif proof.get('current_version') == '0.1.48':
            allowed = {'pku_sync/lecture_source_download.py', 'pku_sync/panel/notion_workspace.py',
                       'pku_sync/panel/static/app.js', 'pku_sync/media.py', 'pku_sync/claim_audit.py', 'pku_sync/platform.py'}
            declared_files = proof.get('allowed_changed_files')
            declared = isinstance(declared_files, list) and len(declared_files) == 6 and set(declared_files) == allowed
            stages = proof.get('additional_summary_attempts')
            if not isinstance(stages, list) or len(stages) != 8:
                raise ValueError
            previous, item = _repair_json(stages[7]['product_manifest']); bound.append(item)
            previous_root = _safe_review_file(previous['product_root'])
            current_root = Path(run.context['installed_path']).parent / 'resources/runtime/Lib/site-packages'
            if set(previous['files']) != expected or {name for name in expected if previous['files'][name] != values[1][name]} != {'pku_sync/media.py'}:
                raise ValueError
            # Reapply the original 37->47 protocol, then only the new media scope.
            class Installation47:
                context = dict(run.context, installed_path=str(previous_root.parents[3] / 'pku-desktop.exe'),
                               artifact_sha256=previous['artifact_sha256'])
            proof47 = dict(proof, current_version='0.1.47', current_product_manifest=stages[7]['product_manifest'],
                           additional_summary_attempts=stages[:7])
            bound += _repair_modules(Installation47(), proof47, prior_installed_path)
            if not _repair48_media_ast(previous_root / 'pku_sync/media.py', current_root / 'pku_sync/media.py'):
                raise ValueError
        elif proof.get('current_version') == '0.1.49':
            allowed = {'pku_sync/lecture_source_download.py', 'pku_sync/panel/notion_workspace.py',
                       'pku_sync/panel/static/app.js', 'pku_sync/media.py', 'pku_sync/claim_audit.py', 'pku_sync/platform.py'}
            declared_files = proof.get('allowed_changed_files')
            declared = isinstance(declared_files, list) and len(declared_files) == 6 and set(declared_files) == allowed
            stages = proof.get('additional_summary_attempts')
            if not isinstance(stages, list) or len(stages) != 9:
                raise ValueError
            previous, item = _repair_json(stages[8]['product_manifest']); bound.append(item)
            previous_root = _safe_review_file(previous['product_root'])
            current_root = Path(run.context['installed_path']).parent / 'resources/runtime/Lib/site-packages'
            if set(previous['files']) != expected or {name for name in expected if previous['files'][name] != values[1][name]} != {'pku_sync/media.py'}:
                raise ValueError
            class Installation48:
                context = dict(run.context, installed_path=str(previous_root.parents[3] / 'pku-desktop.exe'),
                               artifact_sha256=previous['artifact_sha256'])
            proof48 = dict(proof, current_version='0.1.48', current_product_manifest=stages[8]['product_manifest'],
                           additional_summary_attempts=stages[:8])
            bound += _repair_modules(Installation48(), proof48, prior_installed_path)
            if not _repair_function_body_ast(previous_root / 'pku_sync/media.py', current_root / 'pku_sync/media.py',
                                              '_verified_ledger_sentences'):
                raise ValueError
        elif proof.get('current_version') == '0.1.50':
            allowed = {'pku_sync/lecture_source_download.py', 'pku_sync/panel/notion_workspace.py',
                       'pku_sync/panel/static/app.js', 'pku_sync/media.py', 'pku_sync/claim_audit.py', 'pku_sync/platform.py'}
            declared_files = proof.get('allowed_changed_files')
            declared = isinstance(declared_files, list) and len(declared_files) == 6 and set(declared_files) == allowed
            stages = proof.get('additional_summary_attempts')
            if not isinstance(stages, list) or len(stages) != 10:
                raise ValueError
            previous, item = _repair_json(stages[9]['product_manifest']); bound.append(item)
            previous_root = _safe_review_file(previous['product_root'])
            current_root = Path(run.context['installed_path']).parent / 'resources/runtime/Lib/site-packages'
            if set(previous['files']) != expected or {name for name in expected if previous['files'][name] != values[1][name]} != {'pku_sync/media.py'}:
                raise ValueError
            class Installation49:
                context = dict(run.context, installed_path=str(previous_root.parents[3] / 'pku-desktop.exe'),
                               artifact_sha256=previous['artifact_sha256'])
            proof49 = dict(proof, current_version='0.1.49', current_product_manifest=stages[9]['product_manifest'],
                           additional_summary_attempts=stages[:9])
            bound += _repair_modules(Installation49(), proof49, prior_installed_path)
            if not _repair_function_body_ast(previous_root / 'pku_sync/media.py', current_root / 'pku_sync/media.py',
                                              '_verified_ledger_sentences'):
                raise ValueError
        else:
            raise ValueError
        if not declared or changes != allowed:
            raise ValueError
        return bound
    except (OSError, KeyError, TypeError, ValueError):
        raise ResourceBlocked('main_repair_asr_software_or_scope_changed') from None


def _repair_media_citation_ast(previous, current):
    """Only one existing citation checker body can differ; signature stays exact."""
    return _repair_function_body_ast(previous, current, '_check_source_time_citations')


def _repair42_media_ast(previous, current):
    """42 adds only the reviewed bounded recovery inside write_notes."""
    return _repair_function_bodies_ast(previous, current, ('_check_source_time_citations', 'write_notes'))


def _repair44_media_ast(previous, current, *, since43=False):
    """Only44 permits the exact optional source-time keyword on the old helper."""
    try:
        trees = []
        allowed = ('write_notes', '_repair_factual_conflicts') if since43 else (
            '_check_source_time_citations', 'write_notes', '_repair_factual_conflicts')
        expected_arg = ast.parse('def f(*, source_time_context: dict | None = None): pass').body[0].args.kwonlyargs[0]
        for index, path in enumerate((previous, current)):
            tree = ast.parse(Path(path).read_bytes())
            for name in allowed:
                matches = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
                if len(matches) != 1 or not isinstance(matches[0], ast.FunctionDef) or matches[0] not in tree.body:
                    return False
                node = matches[0]
                if name == '_repair_factual_conflicts' and index == 1:
                    if (not node.args.kwonlyargs or ast.dump(node.args.kwonlyargs[-1], include_attributes=False)
                            != ast.dump(expected_arg, include_attributes=False)
                            or not isinstance(node.args.kw_defaults[-1], ast.Constant)
                            or node.args.kw_defaults[-1].value is not None):
                        return False
                    node.args.kwonlyargs.pop(); node.args.kw_defaults.pop()
                node.body = [ast.Pass()]
            trees.append(ast.dump(tree, include_attributes=False))
        return trees[0] == trees[1]
    except (OSError, SyntaxError, ValueError, TypeError, IndexError):
        return False


def _repair_claim_audit_ast(previous, current):
    return _repair_function_body_ast(previous, current, 'audit_chapter')


def _repair47_platform_ast(previous, current):
    """Only the three durable notes functions and two exact optional keywords."""
    try:
        trees = []
        expected_arg = ast.parse('def f(*, model: str | None = None): pass').body[0].args.kwonlyargs[0]
        for index, path in enumerate((previous, current)):
            tree = ast.parse(Path(path).read_bytes())
            for name in ('_llm_payload_hash', '_llm_job_key', '_llm_durable_job'):
                matches = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
                if len(matches) != 1 or not isinstance(matches[0], ast.FunctionDef) or matches[0] not in tree.body:
                    return False
                node = matches[0]
                if index == 1 and name in {'_llm_payload_hash', '_llm_job_key'}:
                    if (not node.args.kwonlyargs or ast.dump(node.args.kwonlyargs[-1], include_attributes=False)
                            != ast.dump(expected_arg, include_attributes=False)
                            or not isinstance(node.args.kw_defaults[-1], ast.Constant)
                            or node.args.kw_defaults[-1].value is not None):
                        return False
                    node.args.kwonlyargs.pop(); node.args.kw_defaults.pop()
                node.body = [ast.Pass()]
            trees.append(ast.dump(tree, include_attributes=False))
        return trees[0] == trees[1]
    except (OSError, SyntaxError, ValueError, TypeError, IndexError):
        return False


def _repair48_media_ast(previous, current):
    """The ten reviewed old functions; only three exact optional None keywords."""
    try:
        trees = [ast.parse(Path(path).read_bytes()) for path in (previous, current)]
        allowed = ('write_notes', '_verified_ledger_sentences', '_ledger_claim_citation',
                   '_ledger_claim_text', '_ledger_claim_line', '_ledger_group_citation',
                   '_ledger_render_guard', '_compose_verified_recovery_claims',
                   '_recover_chapter_with_ledger', '_chapters')
        additions = {'_verified_ledger_sentences': ast.parse('def f(*, evidence_blocks: list[dict] | None = None, sources: list[dict] | None = None): pass').body[0].args,
                     '_recover_chapter_with_ledger': ast.parse('def f(*, full_sources: list[dict] | None = None): pass').body[0].args}
        old_chapters = next(n for n in trees[0].body if isinstance(n, ast.FunctionDef) and n.name == '_chapters')
        new_chapters = next(n for n in trees[1].body if isinstance(n, ast.FunctionDef) and n.name == '_chapters')
        # The original partitioning and budget calculation stay exact. Only a
        # bounded coalescing pass may be inserted before the original return.
        if ([ast.dump(n, include_attributes=False) for n in old_chapters.body[:-1]]
                != [ast.dump(n, include_attributes=False) for n in new_chapters.body[:len(old_chapters.body)-1]]
                or ast.dump(old_chapters.body[-1], include_attributes=False)
                != ast.dump(ast.parse('return chapters').body[0], include_attributes=False)
                or ast.dump(new_chapters.body[-1], include_attributes=False)
                != ast.dump(ast.parse('return grouped').body[0], include_attributes=False)):
            return False
        for index, tree in enumerate(trees):
            for name in allowed:
                matches = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
                if len(matches) != 1 or not isinstance(matches[0], ast.FunctionDef) or matches[0] not in tree.body:
                    return False
                node = matches[0]
                if index == 1 and name in additions:
                    expected = additions[name]; count = len(expected.kwonlyargs)
                    if ([ast.dump(n, include_attributes=False) for n in node.args.kwonlyargs[-count:]]
                            != [ast.dump(n, include_attributes=False) for n in expected.kwonlyargs]
                            or any(not isinstance(n, ast.Constant) or n.value is not None for n in node.args.kw_defaults[-count:])):
                        return False
                    del node.args.kwonlyargs[-count:]; del node.args.kw_defaults[-count:]
                node.body = [ast.Pass()]
        return ast.dump(trees[0], include_attributes=False) == ast.dump(trees[1], include_attributes=False)
    except (OSError, SyntaxError, ValueError, TypeError, IndexError, StopIteration):
        return False


def _repair_function_body_ast(previous, current, function):
    return _repair_function_bodies_ast(previous, current, (function,))


def _repair_function_bodies_ast(previous, current, functions):
    try:
        trees = []
        for path in (previous, current):
            tree = ast.parse(Path(path).read_bytes())
            for function in functions:
                matches = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                           and node.name == function]
                if len(matches) != 1 or not isinstance(matches[0], ast.FunctionDef) or matches[0] not in tree.body:
                    return False
                matches[0].body = [ast.Pass()]
            trees.append(ast.dump(tree, include_attributes=False))
        return trees[0] == trees[1]
    except (OSError, SyntaxError, ValueError, TypeError):
        return False


def _repair_ledger_equal(previous, current):
    # Only the timestamp of this GET is transport metadata. Every business field,
    # including entire historical rows and unknown/new fields, must remain equal.
    def business(value):
        clean = {k: v for k, v in value.items() if k != 'observed_at'}
        for name in ('oss_objects', 'oss_history'):
            if isinstance(clean.get(name), list):
                clean[name] = [{**row, 'current_head': {k: v for k, v in row['current_head'].items()
                                    if k not in {'checked_at', 'observed_at'}}}
                               if isinstance(row, dict) and isinstance(row.get('current_head'), dict) else row
                               for row in clean[name]]
        return json.dumps(clean, sort_keys=True)
    return (isinstance(previous, dict) and isinstance(current, dict)
            and type(previous.get('schema')) is type(current.get('schema')) is int
            and previous.get('schema') == current.get('schema') == 1
            and previous.get('complete') is current.get('complete') is True
            and previous.get('source') == current.get('source') == 'account_cumulative_usage_rows'
            and business(previous) == business(current))


def _repair_verified_ledger_extension(previous, current, source_id, course_id):
    from copy import deepcopy
    from decimal import Decimal, InvalidOperation
    try:
        reduced = deepcopy(current)
        additions = {}
        for key in ('jobs', 'settlements'):
            def identities(value):
                rows = value[key]
                if (not isinstance(rows, list) or any(not isinstance(row, dict)
                        or not isinstance(row.get('id'), str) or not row['id'] for row in rows)):
                    raise ValueError('invalid identities')
                mapped = {row['id']: row for row in rows}
                if len(mapped) != len(rows):
                    raise ValueError('duplicate identities')
                return mapped
            old, now = identities(previous), identities(current)
            if any(now.get(identity) != row for identity, row in old.items()):
                return False
            additions[key] = [row for identity, row in now.items() if identity not in old]
            reduced[key] = [row for row in current[key] if row['id'] in old]
        jobs, fees = additions['jobs'], additions['settlements']
        job_ops = [row['operation_id'] for row in jobs]
        fee_ops = [row['operation_id'] for row in fees]
        historical_ops = {row.get('operation_id') for row in previous['jobs'] + previous['settlements']}
        if (len(set(job_ops)) != len(job_ops) or len(set(fee_ops)) != len(fee_ops)
                or set(job_ops) != set(fee_ops) or set(job_ops) & historical_ops):
            return False
        fee_by_op = {row['operation_id']: row for row in fees}
        points = Decimal('0')
        for job in jobs:
            op = job['operation_id']
            if (not isinstance(op, str) or not op or op.lower().startswith('asr:')
                    or job.get('status') != 'succeeded'
                    or job.get('source_id') != source_id or job.get('course_id') != course_id
                    or job.get('purpose') != 'lecture-notes'):
                return False
            fee = fee_by_op[op]
            charged = Decimal(str(fee['charged_points']))
            if (not charged.is_finite() or charged < 0 or fee.get('charged_seconds') is not None
                    or Decimal(str(job['points_charged'])) != charged):
                return False
            points += charged
        if (type(previous['charged_seconds']) is not int
                or type(current['charged_seconds']) is not int
                or current['charged_seconds'] != previous['charged_seconds']
                or Decimal(str(current['charged_points']))
                    != Decimal(str(previous['charged_points'])) + points):
            return False
        reduced['charged_points'] = previous['charged_points']
        # Preserve every other field and all historical rows; only provider
        # GET/HEAD timestamps are normalized by the existing verifier.
        return _repair_ledger_equal(previous, reduced)
    except (KeyError, ValueError, TypeError, InvalidOperation, AttributeError):
        return False

def _repair_fresh_head_times(snapshot, started, finished):
    # The independent Oracle has a fixed 20-second HTTP timeout. Keep that
    # physical read budget for source timestamps; do not reuse a historic HEAD.
    def fresh(stamp):
        return type(stamp) in (int, float) and math.isfinite(stamp) \
            and stamp > 0 and started - 20 <= stamp <= finished + 20
    return (fresh(snapshot.get('observed_at')) and all(
        fresh(head[key]) for name in ('oss_objects', 'oss_history') for row in snapshot.get(name, [])
        if isinstance(row, dict) for head in [row.get('current_head', {})]
        if isinstance(head, dict) for key in ('checked_at', 'observed_at') if key in head))


def _repair_no_new_asr(previous, current):
    try:
        def rows(value, key):
            result = value[key]
            if not isinstance(result, list) or any(not isinstance(row, dict)
                    or not isinstance(row.get('id'), str) or not row['id'] for row in result):
                raise ValueError
            mapped = {row['id']: row for row in result}
            if len(mapped) != len(result):
                raise ValueError
            return mapped
        collections = []
        for key in ('jobs', 'settlements'):
            old_rows, now_rows = rows(previous, key), rows(current, key)
            # Legacy settlements can have operation_id=null. Keep every such
            # historical row exactly, rather than omitting it from the guard.
            if any(identity not in now_rows or json.dumps(now_rows[identity], sort_keys=True)
                    != json.dumps(row, sort_keys=True) for identity, row in old_rows.items()):
                return False
            for identity, row in now_rows.items():
                if identity in old_rows:
                    continue
                operation = row.get('operation_id')
                if (not isinstance(operation, str) or not operation
                        or operation.lower().startswith('asr:')
                        or any(row.get(field) is not None for field in ('charged_seconds', 'seconds_charged'))):
                    return False
            def asr(mapped):
                return {identity: row for identity, row in mapped.items()
                        if isinstance(row.get('operation_id'), str) and row['operation_id'].lower().startswith('asr:')}
            collections.append((asr(old_rows), asr(now_rows)))
        return (all(bool(old) and old == now for old, now in collections)
                and type(previous['charged_seconds']) is type(current['charged_seconds']) is int
                and previous['charged_seconds'] == current['charged_seconds'])
    except (KeyError, TypeError, AttributeError):
        return False
    except ValueError:
        return False


def _repair_summary_attempt_ledger(before, after, source, course, *, stage='r11'):
    """Only the two reviewed notes boundaries; preserve every unrelated row/field."""
    try:
        if stage == 'r11':
            count, expected_points = 22, Decimal('0.297')
        elif stage == 'r12':
            count, expected_points = 16, Decimal('0.243')
        elif stage == 'r13':
            count, expected_points = 43, Decimal('0.868')
        elif stage == 'r14':
            count, expected_points = 10, Decimal('0.227')
        elif stage == 'r15':
            count, expected_points = 1, Decimal('0.086')
        elif stage == 'r16':
            count, expected_points = 1, Decimal('0.112')
        elif stage == 'r17':
            count, expected_points = 16, Decimal('0.387')
        elif stage == 'r19':
            count, expected_points = 49, Decimal('1.260')
        elif stage == 'r20':
            count, expected_points = 30, Decimal('0.730')
        elif stage == 'r21':
            count, expected_points = 4, Decimal('0.076')
        else:
            return False
        if not _repair_no_new_asr(before, after):
            return False
        old_jobs = {row['id'] for row in before['jobs']}
        old_settlements = {row['id'] for row in before['settlements']}
        jobs = [row for row in after['jobs'] if row['id'] not in old_jobs]
        settlements = [row for row in after['settlements'] if row['id'] not in old_settlements]
        if len(jobs) != count or len(settlements) != count:
            return False
        operations = {row['operation_id'] for row in jobs}
        if len(operations) != count or any(not isinstance(operation, str) or not operation for operation in operations):
            return False
        if any(row.get('operation') != 'notes' or row.get('purpose') != 'lecture-notes'
               or row.get('status') != 'succeeded' or row.get('course_id') != course
               or row.get('source_id') != source for row in jobs):
            return False
        charges = {row['operation_id']: row for row in settlements}
        if len(charges) != count or set(charges) != operations:
            return False
        total = Decimal('0')
        for job in jobs:
            settlement = charges[job['operation_id']]
            if (settlement.get('operation') != 'notes'
                    or type(job.get('points_charged')) not in (int, float)
                    or type(settlement.get('charged_points')) not in (int, float)):
                return False
            points = Decimal(str(job['points_charged']))
            if not points.is_finite() or points < 0 or points != Decimal(str(settlement['charged_points'])):
                return False
            total += points
        if (total != expected_points
                or Decimal(str(after['charged_points'])) - Decimal(str(before['charged_points'])) != total):
            return False
        preserved = deepcopy(after)
        preserved['jobs'] = [row for row in after['jobs'] if row['id'] in old_jobs]
        preserved['settlements'] = [row for row in after['settlements'] if row['id'] in old_settlements]
        preserved['charged_points'] = before['charged_points']
        return _repair_ledger_equal(before, preserved)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False


def _repair_additional_summary_attempt(run, proof, original_processed, prior_binary):
    attempt = proof.get('additional_summary_attempt')
    if proof['current_version'] in {'0.1.42', '0.1.43', '0.1.44', '0.1.45', '0.1.46', '0.1.47', '0.1.48', '0.1.49', '0.1.50'}:
        stages = proof.get('additional_summary_attempts')
        expected_count = {'0.1.42': 2, '0.1.43': 3, '0.1.44': 4, '0.1.45': 5, '0.1.46': 6, '0.1.47': 7, '0.1.48': 8, '0.1.49': 9, '0.1.50': 10}[proof['current_version']]
        if 'additional_summary_attempt' in proof or not isinstance(stages, list) or len(stages) != expected_count:
            raise ValueError
        first = _repair_summary_attempt_stage(run, proof, stages[0], original_processed, prior_binary, stage='r11')
        second = _repair_summary_attempt_stage(run, proof, stages[1], first['processed'], prior_binary,
                                              stage='r12', previous_stage=first)
        if proof['current_version'] in {'0.1.43', '0.1.44', '0.1.45', '0.1.46', '0.1.47', '0.1.48', '0.1.49', '0.1.50'}:
            third = _repair_summary_attempt_stage(run, proof, stages[2], second['processed'], prior_binary,
                                                 stage='r13', previous_stages=(first, second))
            if proof['current_version'] in {'0.1.44', '0.1.45', '0.1.46', '0.1.47', '0.1.48', '0.1.49', '0.1.50'}:
                fourth = _repair_summary_attempt_stage(run, proof, stages[3], third['processed'], prior_binary,
                                                      stage='r14', previous_stages=(first, second, third))
                if proof['current_version'] in {'0.1.45', '0.1.46', '0.1.47', '0.1.48', '0.1.49', '0.1.50'}:
                    fifth = _repair_summary_attempt_stage(run, proof, stages[4], fourth['processed'], prior_binary,
                                                         stage='r15', previous_stages=(first, second, third, fourth))
                    if proof['current_version'] in {'0.1.46', '0.1.47', '0.1.48', '0.1.49', '0.1.50'}:
                        sixth = _repair_summary_attempt_stage(run, proof, stages[5], fifth['processed'], prior_binary,
                                                             stage='r16', previous_stages=(first, second, third, fourth, fifth))
                        if proof['current_version'] in {'0.1.47', '0.1.48', '0.1.49', '0.1.50'}:
                            seventh = _repair_summary_attempt_stage(run, proof, stages[6], sixth['processed'], prior_binary,
                                                                   stage='r17', previous_stages=(first, second, third, fourth, fifth, sixth))
                            if proof['current_version'] in {'0.1.48', '0.1.49', '0.1.50'}:
                                items = (first, second, third, fourth, fifth, sixth, seventh)
                                eighth = _repair_summary_attempt_stage(run, proof, stages[7], seventh['processed'], prior_binary,
                                                                      stage='r19', previous_stages=items)
                                items += (eighth,)
                                final = eighth
                                if proof['current_version'] in {'0.1.49', '0.1.50'}:
                                    ninth = _repair_summary_attempt_stage(run, proof, stages[8], eighth['processed'], prior_binary,
                                                                         stage='r20', previous_stages=items)
                                    items += (ninth,)
                                    final = ninth
                                    if proof['current_version'] == '0.1.50':
                                        tenth = _repair_summary_attempt_stage(run, proof, stages[9], ninth['processed'], prior_binary,
                                                                             stage='r21', previous_stages=items)
                                        items += (tenth,)
                                        final = tenth
                                return {'bound': [entry for item in items for entry in item['bound']],
                                        'processed': final['processed'],
                                        'stages': [{key: value for key, value in item.items() if key not in {'bound', 'processed'}}
                                                   for item in items]}
                            return {'bound': first['bound'] + second['bound'] + third['bound'] + fourth['bound'] + fifth['bound'] + sixth['bound'] + seventh['bound'],
                                    'processed': seventh['processed'],
                                    'stages': [{key: value for key, value in item.items() if key not in {'bound', 'processed'}}
                                               for item in (first, second, third, fourth, fifth, sixth, seventh)]}
                        return {'bound': first['bound'] + second['bound'] + third['bound'] + fourth['bound'] + fifth['bound'] + sixth['bound'],
                                'processed': sixth['processed'],
                                'stages': [{key: value for key, value in item.items() if key not in {'bound', 'processed'}}
                                           for item in (first, second, third, fourth, fifth, sixth)]}
                    return {'bound': first['bound'] + second['bound'] + third['bound'] + fourth['bound'] + fifth['bound'],
                            'processed': fifth['processed'],
                            'stages': [{key: value for key, value in item.items() if key not in {'bound', 'processed'}}
                                       for item in (first, second, third, fourth, fifth)]}
                return {'bound': first['bound'] + second['bound'] + third['bound'] + fourth['bound'],
                        'processed': fourth['processed'],
                        'stages': [{key: value for key, value in item.items() if key not in {'bound', 'processed'}}
                                   for item in (first, second, third, fourth)]}
            return {'bound': first['bound'] + second['bound'] + third['bound'], 'processed': third['processed'],
                    'stages': [{key: value for key, value in item.items() if key not in {'bound', 'processed'}}
                               for item in (first, second, third)]}
        return {'bound': first['bound'] + second['bound'], 'processed': second['processed'],
                'stages': [{key: value for key, value in item.items() if key not in {'bound', 'processed'}}
                           for item in (first, second)]}
    if 'additional_summary_attempts' in proof:
        raise ValueError
    if proof['current_version'] != '0.1.41':
        if attempt is not None:
            raise ValueError
        return None
    return _repair_summary_attempt_stage(run, proof, attempt, original_processed, prior_binary, stage='r11')


def _repair_summary_attempt_stage(run, proof, attempt, original_processed, prior_binary, *, stage,
                                  previous_stage=None, previous_stages=None):
    from .learning_journeys import _safe_review_file
    if stage == 'r11' and previous_stage is None:
        version, count, points = '0.1.40', 22, 0.297
        scope = ['pku_sync/lecture_source_download.py', 'pku_sync/panel/notion_workspace.py', 'pku_sync/panel/static/app.js']
    elif stage == 'r12' and previous_stage is not None:
        version, count, points = '0.1.41', 16, 0.243
        scope = ['pku_sync/lecture_source_download.py', 'pku_sync/panel/notion_workspace.py', 'pku_sync/panel/static/app.js', 'pku_sync/media.py']
    elif stage == 'r13' and previous_stage is None and isinstance(previous_stages, tuple) and len(previous_stages) == 2:
        version, count, points = '0.1.42', 43, 0.868
        scope = ['pku_sync/lecture_source_download.py', 'pku_sync/panel/notion_workspace.py', 'pku_sync/panel/static/app.js',
                 'pku_sync/media.py', 'pku_sync/claim_audit.py']
    elif stage == 'r14' and previous_stage is None and isinstance(previous_stages, tuple) and len(previous_stages) == 3:
        version, count, points = '0.1.43', 10, 0.227
        scope = ['pku_sync/lecture_source_download.py', 'pku_sync/panel/notion_workspace.py', 'pku_sync/panel/static/app.js',
                 'pku_sync/media.py', 'pku_sync/claim_audit.py']
    elif stage == 'r15' and previous_stage is None and isinstance(previous_stages, tuple) and len(previous_stages) == 4:
        version, count, points = '0.1.44', 1, 0.086
        scope = ['pku_sync/lecture_source_download.py', 'pku_sync/panel/notion_workspace.py', 'pku_sync/panel/static/app.js',
                 'pku_sync/media.py', 'pku_sync/claim_audit.py']
    elif stage == 'r16' and previous_stage is None and isinstance(previous_stages, tuple) and len(previous_stages) == 5:
        version, count, points = '0.1.45', 1, 0.112
        scope = ['pku_sync/lecture_source_download.py', 'pku_sync/panel/notion_workspace.py', 'pku_sync/panel/static/app.js',
                 'pku_sync/media.py', 'pku_sync/claim_audit.py']
    elif stage == 'r17' and previous_stage is None and isinstance(previous_stages, tuple) and len(previous_stages) == 6:
        version, count, points = '0.1.46', 16, 0.387
        scope = ['pku_sync/lecture_source_download.py', 'pku_sync/panel/notion_workspace.py', 'pku_sync/panel/static/app.js',
                 'pku_sync/media.py', 'pku_sync/claim_audit.py']
    elif stage == 'r19' and previous_stage is None and isinstance(previous_stages, tuple) and len(previous_stages) == 7:
        version, count, points = '0.1.47', 49, 1.26
        scope = ['pku_sync/lecture_source_download.py', 'pku_sync/panel/notion_workspace.py', 'pku_sync/panel/static/app.js',
                 'pku_sync/media.py', 'pku_sync/claim_audit.py', 'pku_sync/platform.py']
    elif stage == 'r20' and previous_stage is None and isinstance(previous_stages, tuple) and len(previous_stages) == 8:
        version, count, points = '0.1.48', 30, 0.73
        scope = ['pku_sync/lecture_source_download.py', 'pku_sync/panel/notion_workspace.py', 'pku_sync/panel/static/app.js',
                 'pku_sync/media.py', 'pku_sync/claim_audit.py', 'pku_sync/platform.py']
    elif stage == 'r21' and previous_stage is None and isinstance(previous_stages, tuple) and len(previous_stages) == 9:
        version, count, points = '0.1.49', 4, 0.076
        scope = ['pku_sync/lecture_source_download.py', 'pku_sync/panel/notion_workspace.py', 'pku_sync/panel/static/app.js',
                 'pku_sync/media.py', 'pku_sync/claim_audit.py', 'pku_sync/platform.py']
    else:
        raise ValueError
    fields = {'report', 'artifact', 'product_manifest', 'before_ledger', 'after_ledger', 'source_digest', 'native_task_id'}
    if stage in {'r14', 'r15', 'r16', 'r17', 'r19', 'r20', 'r21'}:
        fields.add('safe_closeout')
    if not isinstance(attempt, dict) or set(attempt) != fields:
        raise ValueError
    report, report_bound = _repair_json(attempt['report'])
    result = report['result']
    expected_digest = attempt['source_digest']
    if (not isinstance(expected_digest, str) or len(expected_digest) != 64
            or any(c not in '0123456789abcdef' for c in expected_digest)
            or report['source_digest'] != expected_digest or report['finished_source_digest'] != expected_digest
            or report['kind'] != 'installed-native-main-chain-repair-continuation-not-release-verdict'
            or report['target'] != 'windows' or report['version'] != version
            or any(type(report[key]) is not int or report[key] != 0 for key in
                   ('complete_main_chains_passed', 'complete_cases_passed', 'complete_journeys_passed'))
            or report['full_release_gate_passed'] is not False
            or result['entry'] != 'installed-native' or result['mocked'] is not False or result['injection'] is not None
            or result['blockers'] != ['execution_failed_AssertionError']
            or set(result['assertions']) != set(MAIN_CHECKS)
            or any(result['assertions'][key] is not (index < 3) for index, key in enumerate(MAIN_CHECKS))):
        raise ValueError
    continuation = result['repair_continuation']
    task = continuation['current_native_task']
    if (continuation['kind'] != 'same-goal-main-chain-repair-continuation'
            or continuation['prior_version'] != '0.1.37' or continuation['current_version'] != version
            or continuation['prior_report_sha256'] != proof['prior_report_sha256']
            or continuation['prior_artifact_sha256'] != proof['prior_artifact_sha256']
            or continuation['current_artifact_sha256'] != report['artifact_sha256']
            or continuation['source_id'] != proof['source_id'] or continuation['no_new_asr'] is not True
            or continuation.get('prior_native_task_terminal_recorded') is not False
            or continuation['original_asr_reused_by_verified_hash_and_original_ledger'] is not False
            or not isinstance(attempt['native_task_id'], str) or not attempt['native_task_id']
            or task != {'id': attempt['native_task_id'], 'kind': 'process', 'state': 'failed',
                        'recording_id': proof['recording_id']}
            or set(continuation['repair_summary_cost']) != {'charged_points', 'charged_seconds'}
            or type(continuation['repair_summary_cost']['charged_points']) not in (int, float)
            or continuation['repair_summary_cost']['charged_points'] != points
            or type(continuation['repair_summary_cost']['charged_seconds']) is not int
            or continuation['repair_summary_cost']['charged_seconds'] != 0):
        raise ValueError
    if previous_stage is not None:
        expected_previous = {key: value for key, value in previous_stage.items() if key not in {'bound', 'processed'}}
        if continuation.get('additional_summary_attempt') != expected_previous:
            raise ValueError
    if stage in {'r13', 'r14', 'r15', 'r16', 'r17', 'r19', 'r20', 'r21'}:
        expected_previous = [{key: value for key, value in item.items() if key not in {'bound', 'processed'}}
                             for item in previous_stages]
        if json.dumps(continuation.get('additional_summary_attempts'), sort_keys=True) != json.dumps(expected_previous, sort_keys=True):
            raise ValueError
    bound = [report_bound]
    runtimes, evidence_hashes = [], set()
    for entry in result['evidence']:
        relative = Path(entry['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError
        path = _safe_review_file(str(report_bound[0].parent / relative))
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError
        bound.append((path, entry['sha256'])); evidence_hashes.add(entry['sha256'])
        if entry.get('role') == 'runtime':
            runtimes.append(json.loads(raw))
    if len(runtimes) != 1:
        raise ValueError
    installed = [row for row in runtimes[0]['processes'] if row.get('target') == 'windows'
                 and row.get('version') == version and row.get('artifact_sha256') == report['artifact_sha256']]
    if len(installed) != 1 or installed[0].get('alive') is not False:
        raise ValueError
    binary = _safe_review_file(installed[0]['installed_path'])
    if (not binary.is_relative_to(Path(proof['test_root']).resolve()) or binary.name.lower() != 'pku-desktop.exe'
            or hashlib.sha256(binary.read_bytes()).hexdigest() != installed[0]['installed_sha256']):
        raise ValueError
    artifact = _safe_review_file(attempt['artifact']['path'])
    if (attempt['artifact']['sha256'] != report['artifact_sha256']
            or hashlib.sha256(artifact.read_bytes()).hexdigest() != report['artifact_sha256']):
        raise ValueError
    bound += [(binary, installed[0]['installed_sha256']), (artifact, report['artifact_sha256'])]
    # Reverify each retained installation using its original exact scope.
    context40 = dict(run.context, installed_path=str(binary), artifact_sha256=report['artifact_sha256'])
    proof40 = dict(proof, current_version=version, current_product_manifest=attempt['product_manifest'],
                   allowed_changed_files=scope)
    if stage == 'r15':
        proof40['additional_summary_attempts'] = proof['additional_summary_attempts'][:4]
    elif stage == 'r16':
        proof40['additional_summary_attempts'] = proof['additional_summary_attempts'][:5]
    elif stage == 'r17':
        proof40['additional_summary_attempts'] = proof['additional_summary_attempts'][:6]
    elif stage == 'r19':
        proof40['additional_summary_attempts'] = proof['additional_summary_attempts'][:7]
    elif stage == 'r20':
        proof40['additional_summary_attempts'] = proof['additional_summary_attempts'][:8]
    elif stage == 'r21':
        proof40['additional_summary_attempts'] = proof['additional_summary_attempts'][:9]
    class Installation40:
        context = context40
    bound += _repair_modules(Installation40(), proof40, str(prior_binary))
    ledgers = []
    for name in ('before_ledger', 'after_ledger'):
        record, item = _repair_json(attempt[name]); bound.append(item)
        if (item[1] not in evidence_hashes or record.get('service') != 'account'
                or record.get('method') != 'GET' or record.get('status') != 200 or record.get('path') != '/v1/operations'):
            raise ValueError
        ledgers.append(record['readback'])
    if (not _repair_ledger_equal(original_processed, ledgers[0])
            or not _repair_summary_attempt_ledger(ledgers[0], ledgers[1], proof['source_id'], proof['course_id'], stage=stage)):
        raise ValueError
    if stage in {'r14', 'r15', 'r16', 'r17', 'r19', 'r20', 'r21'}:
        closeout, item = _repair_json(attempt['safe_closeout']); bound.append(item)
        zero_fields = ('charged_seconds', 'active_oss_objects', 'complete_main_chains_passed', 'complete_cases_passed')
        if stage == 'r16':
            # The actual r16 closeout omitted this field; the complete report above
            # still independently proves zero journeys. Do not invent a value.
            if 'complete_journeys_passed' in closeout:
                raise ValueError
        else:
            zero_fields += ('complete_journeys_passed',)
        if (closeout.get('kind') != f'actual-installed{version.removeprefix("0.1.")}-summary-failure-safe-closeout-not-pass'
                or closeout.get('report_sha256') != report_bound[1]
                or any(closeout.get(key) is not True for key in ('source_digest_unchanged', 'normal_window_quit_confirmed',
                    'original_notes_and_transcript_unchanged', 'new_jobs_terminal', 'no_new_asr'))
                or closeout.get('pending_intents') != [] or closeout.get('projection_journals') != []
                or any(type(closeout.get(key)) is not int or closeout[key] != count for key in ('new_jobs', 'new_settlements'))
                or closeout.get('job_status_counts') != {'succeeded': count}
                or closeout.get('charged_points') != ('1.260' if stage == 'r19' else '0.730' if stage == 'r20' else str(points))
                or any(type(closeout.get(key)) is not int or closeout[key] != 0 for key in zero_fields)
                or closeout.get('full_release_gate_passed') is not False or ledgers[1].get('oss_objects') != []):
            raise ValueError
        closeout_hashes = {row['sha256'] for row in closeout['billing_ops_evidence']}
        if any(attempt[key]['sha256'] not in closeout_hashes for key in ('before_ledger', 'after_ledger')):
            raise ValueError
        if stage in {'r15', 'r16', 'r17', 'r19', 'r20', 'r21'}:
            expected_locks = {
                'data/.projection-permits/16c1426fe665d592e7752e0390dfba878efcc9484c0a085ac9e18c06ed8b183f/e3e9f4caaf80d6bb6346802083e17bd01796c8eb63bff2abe3e8adbad7f4b198.lock.sqlite',
                'data/.projection-receipts/55e54ce2bfafc81d77311a479fd4c038a5d59e4abd45df56ea4db960211fbc67/e3e9f4caaf80d6bb6346802083e17bd01796c8eb63bff2abe3e8adbad7f4b198.lock.sqlite',
                'data/.projection-receipts/cf72c64376179358bca34b2aaaf96189ae92674c359e575ada60eaf4912e4a6b/e3e9f4caaf80d6bb6346802083e17bd01796c8eb63bff2abe3e8adbad7f4b198.lock.sqlite'}
            locks = closeout.get('empty_locks_preserved')
            if (not isinstance(locks, list) or len(locks) != 3
                    or {row['relative_path'].replace('\\', '/') for row in locks} != expected_locks):
                raise ValueError
            for row in locks:
                if (set(row) != {'relative_path', 'bytes', 'sha256'} or type(row['bytes']) is not int or row['bytes'] != 0
                        or row['sha256'] != hashlib.sha256(b'').hexdigest()):
                    raise ValueError
                path = _safe_review_file(str(Path(proof['app_dir']) / row['relative_path']))
                if path.stat().st_size != 0 or hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
                    raise ValueError
                bound.append((path, row['sha256']))
    return {'bound': bound, 'processed': ledgers[1], 'report_sha256': report_bound[1],
            'source_digest': expected_digest, 'native_task_id': attempt['native_task_id'],
            'version': version, 'charged_points': points, 'charged_seconds': 0, 'jobs': count}


def _repair_source_files(run, proof):
    from .learning_journeys import _data_root, _safe_review_file
    try:
        matches = []
        for path in _data_root(run).rglob('recording.json'):
            recording = json.loads(_safe_review_file(str(path)).read_text('utf-8'))
            source = recording.get('source_id')
            if not source and recording.get('recorded_at'):
                key = json.dumps({'course': recording.get('course_id'), 'start': recording['recorded_at']},
                                 ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                source = 'pku:recording:' + hashlib.sha256(key.encode()).hexdigest()[:32]
            if source == proof['source_id']:
                if recording.get('course_id') != proof['course_id']:
                    raise ValueError
                matches.append(path.parent)
        if len(matches) != 1:
            raise ValueError
        bound = []
        for filename, field in [('video.mp4', 'original_media_sha256'), ('transcript.json', 'transcript_sha256')]:
            path = _safe_review_file(str(matches[0] / filename))
            if not path.stat().st_size or hashlib.sha256(path.read_bytes()).hexdigest() != proof[field]:
                raise ValueError
            bound.append((path, proof[field]))
        return bound
    except (OSError, KeyError, TypeError, ValueError):
        raise ResourceBlocked('main_repair_original_media_or_transcript_changed') from None


def _load_main_chain_resume(run):
    if getattr(run, 'config', {}).get('main_chain_requalification') is not None:
        from .native_requalification import load
        return load(run, _load_main_chain_resume)
    spec = getattr(run, 'config', {}).get('main_chain_resume')
    if spec is None:
        return None
    from .learning_journeys import _safe_review_file
    try:
        proof, proof_bound = _repair_json({'path': spec['proof_path'], 'sha256': spec['proof_sha256']})
        if type(proof.get('schema')) is not int or proof['schema'] != 1 \
                or proof.get('kind') != 'same-goal-main-chain-repair-continuation':
            raise ValueError
        if proof.get('current_version') not in {'0.1.38', '0.1.39', '0.1.40', '0.1.41', '0.1.42', '0.1.43', '0.1.44', '0.1.45', '0.1.46', '0.1.47', '0.1.48', '0.1.49', '0.1.50'}:
            raise ValueError
        for key, expected in [('prior_version', '0.1.37'),
                              ('course_id', run.need('learning.course_id')),
                              ('recording_id', run.need('learning.short_recording_id')),
                              ('source_id', run.need('learning.source_id')),
                              ('original_media_sha256', run.need('learning.original_media_sha256')),
                              ('current_artifact_sha256', run.context['artifact_sha256'])]:
            if proof.get(key) != expected:
                raise ValueError
        if run.context['version'] != proof['current_version'] or run.context['target'] != 'windows':
            raise ValueError
        if proof['notion_test_parent_id'].replace('-', '') != run.need('learning.notion_test_parent_id').replace('-', ''):
            raise ValueError
        for key, value in [('test_root', run.need('test_root')), ('app_dir', run.need('instances.primary.app_dir'))]:
            if _safe_review_file(proof[key]) != _safe_review_file(value):
                raise ValueError
        owner = run.credential('primary_email').strip().lower()
        if hashlib.sha256(owner.encode()).hexdigest() != proof['owner_email_sha256']:
            raise ValueError
        report, report_bound = _repair_json({'path': proof['prior_report_path'], 'sha256': proof['prior_report_sha256']})
        result = report['result']
        if (report['kind'] != 'installed-native-main-chain-not-release-verdict'
                or report['target'] != 'windows' or report['version'] != proof['prior_version']
                or report['artifact_sha256'] != proof['prior_artifact_sha256']
                or report['complete_main_chains_passed'] != 0 or report['complete_cases_passed'] != 0
                or report['complete_journeys_passed'] != 0 or report['full_release_gate_passed'] is not False
                or result['entry'] != 'installed-native' or result['mocked'] is not False
                or result['injection'] is not None or set(result['assertions']) != set(MAIN_CHECKS)
                or result['blockers'] != ['generated_summary_source_version_missing']):
            raise ValueError
        artifact = _safe_review_file(proof['prior_artifact_path'])
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != proof['prior_artifact_sha256']:
            raise ValueError
        bound = [proof_bound, report_bound, (artifact, proof['prior_artifact_sha256'])]
        evidence_hashes = set()
        runtimes = []
        for entry in result['evidence']:
            relative = Path(entry['path'])
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError
            path = _safe_review_file(str(report_bound[0].parent / relative))
            if hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
                raise ValueError
            bound.append((path, entry['sha256'])); evidence_hashes.add(entry['sha256'])
            if entry.get('role') == 'runtime':
                runtimes.append(json.loads(path.read_text('utf-8')))
        if len(runtimes) != 1:
            raise ValueError
        installed = [row for row in runtimes[0]['processes'] if row.get('target') == 'windows'
                     and row.get('version') == '0.1.37'
                     and row.get('artifact_sha256') == proof['prior_artifact_sha256']]
        if len(installed) != 1 or installed[0].get('alive') is not False:
            raise ValueError
        prior_binary = _safe_review_file(installed[0]['installed_path'])
        if not prior_binary.is_relative_to(Path(proof['test_root']).resolve()) \
                or prior_binary.name.lower() != 'pku-desktop.exe' \
                or hashlib.sha256(prior_binary.read_bytes()).hexdigest() != installed[0]['installed_sha256']:
            raise ValueError
        bound.append((prior_binary, installed[0]['installed_sha256']))
        ledgers = []
        for key in ['prior_before_ledger', 'prior_processed_ledger']:
            record, item = _repair_json(proof[key]); bound.append(item)
            if item[1] not in evidence_hashes or record.get('service') != 'account' \
                    or record.get('method') != 'GET' or record.get('status') != 200 \
                    or record.get('path') != '/v1/operations':
                raise ValueError
            ledgers.append(record['readback'])
        bound += _repair_modules(run, proof, str(prior_binary))
        attempt = _repair_additional_summary_attempt(run, proof, ledgers[1], prior_binary)
        if attempt is not None:
            bound += attempt['bound']
        source_files = _repair_source_files(run, proof)
        bound += source_files
        return {'proof': proof, 'before': ledgers[0], 'processed': ledgers[1], 'bound': bound,
                'fresh_baseline': attempt['processed'] if attempt is not None else ledgers[1],
                'additional_summary_attempt': attempt,
                'transcript': source_files[1], 'owner_email_sha256': proof['owner_email_sha256']}
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        raise ResourceBlocked('main_repair_prior_native_provenance_invalid') from None


def _assert_main_repair_unchanged(resume):
    from .learning_journeys import _safe_review_file
    try:
        for path, digest in resume['bound']:
            if hashlib.sha256(_safe_review_file(str(path)).read_bytes()).hexdigest() != digest:
                raise ValueError
    except (OSError, ValueError, TypeError):
        raise ResourceBlocked('main_repair_frozen_provenance_changed') from None


def _main_workspace_binding(run, course_id):
    """Observe actual local bytes immediately before refresh; never serialize student text."""
    from .learning_journeys import _data_root, _safe_review_file
    try:
        path = _safe_review_file(str(_data_root(run) / 'notion-workspace-v2.json'))
        raw = path.read_bytes()
        value = json.loads(raw)
        course = value.get('courses', {}).get(course_id, {})
        def uuid(value):
            normalized = str(value or '').replace('-', '').lower()
            return normalized if len(normalized) == 32 and all(c in '0123456789abcdef' for c in normalized) else None
        return {'event': 'main_workspace_before_selected_refresh', 'bytes': len(raw),
                'sha256': hashlib.sha256(raw).hexdigest(), 'course_id': course_id,
                'binding': {'schema': value.get('schema') if type(value.get('schema')) is int else None,
                            'home_id': uuid(value.get('id')), 'parent_id': uuid(value.get('parent_id')),
                            'course_page_id': uuid(course.get('id')),
                            'account_namespace_sha256': hashlib.sha256(str(value.get('account_namespace', '')).encode()).hexdigest(),
                            'profile_revision': value.get('profile_revision') if type(value.get('profile_revision')) is int else None,
                            'active_lecture_count': len(course.get('lectures', {})),
                            'historical_lecture_count': len(course.get('historical_lectures', {}))}}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ResourceBlocked('main_actual_workspace_binding_not_readable') from None


def _main_recording_action(run, ui, selector, path, kind, recording, *, timeout):
    from . import learning_journeys as learning
    if ui.page is None:
        raise ResourceBlocked("actual_windows_response_observer_required")
    with ui.page.expect_response(lambda response: response.request.method == "POST"
            and urlsplit(response.url).path == path, timeout=30000) as captured:
        learning._confirm(run, ui, selector)
    response = captured.value
    if response.status != 200:
        raise AssertionError("Actual native main action was rejected")
    accepted = response.json()
    if (not isinstance(accepted, dict) or not accepted.get("id")
            or accepted.get("kind") != kind or accepted.get("recording_id") != recording):
        raise AssertionError("Main action response belongs to another task")
    ui.wait(lambda: learning._read(ui, "/api/recordings/task").get("id") == accepted["id"], timeout=30)
    task = learning._task(ui, timeout=timeout)
    if task.get("id") != accepted["id"] or task.get("kind") != kind or task.get("recording_id") != recording:
        raise AssertionError("Main action observed a different task terminal")
    return task


def _fresh_main_chain(run):
    """Bootstrap one real lesson; full journey fixtures remain mandatory separately."""
    from urllib.parse import quote
    from . import learning_journeys as learning
    from .catalog_journeys import _catalog_setup, _refresh_course
    cfg = run.need("learning")
    quiz = run.need("quiz")
    source = run.need("learning.source_id")
    if quiz["course_id"] != cfg["course_id"] or quiz["source_ids"] != [source]:
        raise AssertionError("Main chain changed its selected source")
    # Freeze the independent source facts before ASR or summary model calls,
    # as well as before the later quiz-generation call.
    learning._reviewed_quiz_preflight(run, quiz)
    resume = _load_main_chain_resume(run)
    ui = run.session("primary")
    _catalog_setup(run, ui, selected_only=True)
    if resume is not None:
        _assert_main_repair_unchanged(resume)
        identity = learning._read(ui, '/api/auth/me')
        owner = run.oracle.get('account', '/v1/auth/me')
        if (identity.get('active') is not True or identity.get('email_verified') is not True
                or hashlib.sha256(str(identity.get('email', '')).strip().lower().encode()).hexdigest()
                    != resume['owner_email_sha256']
                or hashlib.sha256(str(owner.get('email', '')).strip().lower().encode()).hexdigest()
                    != resume['owner_email_sha256']):
            raise ResourceBlocked('main_repair_account_identity_changed')
        run._repair_continuation = {'kind': 'same-goal-main-chain-repair-continuation',
            'prior_version': '0.1.37', 'current_version': resume['proof']['current_version'],
            'prior_report_sha256': resume['proof']['prior_report_sha256'],
            'prior_artifact_sha256': resume['proof']['prior_artifact_sha256'],
            'current_artifact_sha256': run.context['artifact_sha256'],
            'source_id': source, 'prior_native_task_terminal_recorded': False,
            'original_asr_reused_by_verified_hash_and_original_ledger': False,
            'prior_processing_cost': {
                'charged_points': float(Decimal(str(resume['processed']['charged_points']))
                                       - Decimal(str(resume['before']['charged_points']))),
                'charged_seconds': resume['processed']['charged_seconds'] - resume['before']['charged_seconds']}}
        if resume['additional_summary_attempt'] is not None:
            if resume['proof']['current_version'] in {'0.1.42', '0.1.43', '0.1.44', '0.1.45', '0.1.46', '0.1.47', '0.1.48', '0.1.49', '0.1.50', '0.1.51'}:
                run._repair_continuation['additional_summary_attempts'] = resume['additional_summary_attempt']['stages']
            else:
                run._repair_continuation['additional_summary_attempt'] = {key: value
                    for key, value in resume['additional_summary_attempt'].items() if key not in {'bound', 'processed'}}
        if 'requalification' in resume:
            run._repair_continuation['requalification'] = resume['requalification']
        run.actions.append({'event': 'main_repair_continuation_frozen', **run._repair_continuation})
    ui, course = learning._open_course(run, cfg["course_id"])
    workspace = learning._workspace(run, ui, selected_only=True)
    _, root_ok = learning._page(run, workspace["id"], cfg["notion_test_parent_id"])
    if not root_ok:
        raise AssertionError("Main chain workspace parent is unverified")
    run.actions.append(_main_workspace_binding(run, cfg['course_id']))
    task = _refresh_course(ui, cfg["course_id"])
    run.actions.append({'event': 'main_actual_selected_refresh_terminal', **{key: task.get(key)
        for key in ('id', 'kind', 'state', 'course_id', 'scope', 'stage', 'failure_id', 'observed_elapsed_seconds')}})
    run.check(MAIN_CHECKS[0], task.get("state") == "done"
              and task["observed_elapsed_seconds"] < 90)
    if task.get("state") != "done":
        raise AssertionError("Selected course refresh failed")
    ui, course = learning._open_course(run, cfg["course_id"])
    projection = learning._workspace(run, ui, selected_only=True)["courses"][cfg["course_id"]]
    _, course_ok = learning._page(run, projection["id"], workspace["id"])
    centers = projection.get("centers", {})
    parents_ok = course_ok and {"每节课", "作业中心", "通知中心", "小测中心"} <= centers.keys()
    parents_ok = parents_ok and all(learning._page(run, row["id"], projection["id"])[1]
                                    for row in centers.values())
    lessons = [row for row in course["lessons"]
               if row["id"] == cfg["short_recording_id"] and row["source_id"] == source]
    if len(lessons) != 1:
        raise AssertionError("Main chain selected source is not unique")
    lesson = lessons[0]
    targets = [row for row in projection.get("lectures", {}).values() if row.get("source_id") == source]
    parents_ok = parents_ok and len(targets) == 1
    if parents_ok:
        parents_ok = learning._page(run, targets[0]["id"], centers["每节课"]["id"])[1]
        text = unquote(json.dumps(learning._blocks(run, targets[0]["id"]), ensure_ascii=False))
        parents_ok = parents_ok and source in text
    run.check(MAIN_CHECKS[1], root_ok and parents_ok)
    if not parents_ok:
        raise AssertionError("Main chain selected projection is unverified")
    learning._lesson_page(ui, lesson["id"])
    if lesson.get("video_available") is True:
        downloaded = learning._download_cached_original(run, ui, lesson["id"])
    else:
        downloaded = _main_recording_action(run, ui, learning._action("recording-download", recording=lesson["id"]),
            "/api/recordings/" + quote(lesson["id"], safe="") + "/download", "download", lesson["id"], timeout=180)
    media = learning._media_probe(run, lesson)
    run.actions.append({"main_chain_original_media": media})
    # An independently reviewed original hash and source facts are required before
    # any model call. Missing facts retain the actual download evidence, then stop.
    expected_hash = run.need("learning.original_media_sha256")
    learning._review_text_preflight(run, "learning.expected_summary_anchors")
    verified = (downloaded.get("state") == "done" and media.get("source_id") == source
                and media.get("sha256") == expected_hash and media.get("playable") is True)
    if lesson.get("video_available") is True:
        verified = verified and downloaded.get("sha256") == expected_hash and downloaded.get("bytes") == media.get("bytes")
    run.check(MAIN_CHECKS[2], verified)
    if not verified:
        raise AssertionError("Refuse paid work on unverified original media")
    refreshed = learning._read(ui, "/api/campus/courses/" + quote(cfg["course_id"], safe=""))
    current = [row for row in refreshed["lessons"] if row["id"] == lesson["id"] and row["source_id"] == source]
    if len(current) != 1:
        raise AssertionError("Downloaded source changed before paid processing")
    duration = current[0].get("duration_seconds")
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise ResourceBlocked("short_media_duration_unverified")
    ledger_started = time.time()
    stage_before = learning._ledger(run) if resume is not None else None
    ledger_finished = time.time()
    if resume is not None:
        _assert_main_repair_unchanged(resume)
        if not _repair_fresh_head_times(stage_before, ledger_started, ledger_finished) \
                or not _repair_ledger_equal(resume['fresh_baseline'], stage_before) or not learning._verified_processing_charges(
                resume['before'], resume['processed'], resume['processed'], source, cfg['course_id'], duration):
            raise ResourceBlocked('main_repair_fresh_account_ledger_changed_or_original_charges_invalid')
    run.authorize_cost(points=5, seconds=0 if resume is not None else duration)
    before = resume['before'] if resume is not None else learning._ledger(run)
    ui.click("[data-recording-direct-oss]")
    processed = _main_recording_action(run, ui, learning._action(
        "campus-regenerate" if resume is not None else "campus-process", recording=lesson["id"]),
        "/api/campus/courses/" + quote(cfg["course_id"], safe="") + "/recordings/"
        + quote(lesson["id"], safe="") + "/process", "process", lesson["id"], timeout=3600)
    ledger_processed = learning._ledger(run)
    if resume is not None:
        _assert_main_repair_unchanged(resume)
        if not _repair_no_new_asr(stage_before, ledger_processed):
            raise ResourceBlocked('main_repair_duplicate_asr_or_original_transcript_changed')
        run._repair_continuation.update(current_native_task={key: processed.get(key)
            for key in ('id', 'kind', 'state', 'recording_id')}, no_new_asr=True,
            original_asr_reused_by_verified_hash_and_original_ledger=processed.get('state') == 'done',
            repair_summary_cost={'charged_points': float(Decimal(str(ledger_processed['charged_points']))
                                            - Decimal(str(stage_before['charged_points']))),
                                 'charged_seconds': ledger_processed['charged_seconds'] - stage_before['charged_seconds']})
        run.actions.append({'event': 'main_repair_native_regenerate_completed', **run._repair_continuation})
    updated = learning._read(ui, "/api/campus/courses/" + quote(cfg["course_id"], safe=""))
    result = next(row for row in updated["lessons"] if row["id"] == lesson["id"] and row["source_id"] == source)
    note_id = learning._published_note_id(run, result, targets[0]['id'], cfg['course_id'])
    published = (processed.get("state") == "done" and result.get("transcript_available") is True
                 and bool(result.get("notion_note_url"))
                 and learning._page(run, note_id, targets[0]["id"])[1]
                 and learning._review_text(run, note_id, "learning.expected_summary_anchors"))
    run.check(MAIN_CHECKS[3], published)
    if not published:
        raise AssertionError("Real published summary failed source review")
    learning._confirm(run, ui, learning._action("campus-accept-summary", recording=lesson["id"]))
    accepted = learning._markers(learning._blocks(run, note_id), "PKU 总结验收版本 · ")
    run.check(MAIN_CHECKS[4], bool(accepted) and accepted[-1].get("source_id") == source
              and learning._identity(accepted[-1].get("note_id")) == learning._identity(note_id)
              and bool(accepted[-1].get("accepted_at")))
    if run.assertions[MAIN_CHECKS[4]] is not True:
        raise AssertionError("Published summary acceptance was not independently confirmed")
    run.restart("primary")
    ui, _ = learning._open_course(run, cfg["course_id"])
    learning._lesson_page(ui, lesson["id"])
    ui.click('[data-action="campus-open-lecture"][data-url="' + result["notion_note_url"] + '"]')
    run.external_page(result["notion_note_url"])
    after = learning._ledger(run)
    operations = [row["operation_id"] for row in learning._new_rows(before, after, "settlements")
                  if row.get("charged_seconds") is not None]
    reused = (learning._verified_processing_charges(before, ledger_processed, after, source, cfg["course_id"], duration)
              and not any(row.get("source_id") == source for row in after["oss_objects"])
              and learning.verified_audio_cleanup(before, after, source, operations))
    run.check(MAIN_CHECKS[5], reused)
    if not reused:
        raise AssertionError("Real processing settlement, reuse or OSS cleanup failed")
    run_quiz(run)

JOURNEYS = {
    "JOURNEY-01": run_identity, "JOURNEY-02": run_catalog,
    "JOURNEY-03": run_notes, "JOURNEY-04": run_assignment,
    "JOURNEY-05": run_quiz, "JOURNEY-06": run_resilience,
    "XPLAT-01": run_cross_platform,
}


def run_case(scenario, mode, context):
    if scenario["id"] not in JOURNEYS or mode not in {"live", "fault"}:
        raise ValueError("unsupported_native_journey")
    run = NativeRun(scenario, mode, context)
    return _execute(run, JOURNEYS[scenario["id"]], paid=scenario["id"] in {"JOURNEY-03", "JOURNEY-04", "JOURNEY-05"})


def run_main_chain(context):
    if context["target"] != "windows":
        raise ValueError("main_chain_requires_current_windows_scope")
    scenario = {"id": "MAIN-CHAIN", "assertions": list(MAIN_CHECKS), "device_scope": "single-machine"}
    run = NativeRun(scenario, "live", context)
    result = _execute(run, _fresh_main_chain, paid=True)
    if getattr(run, '_repair_continuation', None) is not None:
        result['repair_continuation'] = run._repair_continuation
        result['repair_continuation']['current_candidate_metrics'] = result['metrics']
    return result


def _execute(run, action, *, paid):
    try:
        # Every journey needs actual before/after accounting, including those
        # expected to cost zero. Missing accounting still must not hide native
        # startup failures; charge-producing journeys cannot proceed without it.
        try:
            run._baseline = run.oracle.snapshot(include_billing=True)
        except ResourceBlocked as exc:
            if paid:
                raise
            run.blockers.append(exc.code)
        except Exception as exc:
            if paid:
                raise
            run.blockers.append("measurement_baseline_failed_" + type(exc).__name__)
        action(run)
    except ResourceBlocked as exc:
        run.blockers.append(exc.code)
    except Exception as exc:
        # Exception text may include authenticated URLs or student content.
        run.blockers.append("execution_failed_" + type(exc).__name__)
        # Only code locations are retained: never exception messages or locals.
        run.actions.append({"execution_failure": {
            "type": type(exc).__name__,
            "locations": [{"file": Path(frame.filename).name,
                           "function": frame.name, "line": frame.lineno}
                          for frame in traceback.extract_tb(exc.__traceback__)]}})
        for name, ui in list(run.sessions.items()):
            try:
                ui.screenshot("execution-failure-" + name)
            except Exception:
                # An unavailable screen cannot erase the original failure.
                pass
    # Runtime evidence must describe the completed native run. Sealing before
    # close omitted quit actions and captured still-live processes, even when
    # cleanup later succeeded. A blocked journey still requires normal exit;
    # forced cleanup cannot erase a failed graceful-quit observation.
    try:
        for name in list(run.processes):
            try:
                run.quit(name, require_graceful=True)
            except Exception as exc:
                run.blockers.append("native_graceful_quit_failed_" + type(exc).__name__)
    finally:
        try:
            run.close()
        except Exception as exc:
            run.blockers.append("native_cleanup_failed_" + type(exc).__name__)
    result = run.finish()
    result["blockers"] = sorted(set(run.blockers))
    return result
