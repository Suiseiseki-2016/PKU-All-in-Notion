"""Admit the plain-text editor without replacing any earlier failure evidence."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import hashlib
from pathlib import Path
from types import SimpleNamespace

from .core import ResourceBlocked, need

CHANGED = {
    'pku_sync/media.py', 'pku_sync/note_editor.py',
    'pku_sync/panel/note_videos.py', 'pku_sync/platform.py',
}
ADDED = {'pku_sync/transcript_notes.py'}
IMAGE_CHANGED = {'pku_sync/panel/note_images.py', 'pku_sync/panel/recordings_api.py'}
TOPIC_CHANGED = {'pku_sync/config.py', 'pku_sync/panel/static/app.js',
                 'pku_sync/notion.py', 'pku_sync/pipeline.py'}
SCOPE = {'main_chains': 1, 'journeys': 6, 'complete_cases': 12,
         'independent_source_windows': 15, 'risks': 69, 'frame_facts': 7, 'general_criteria': 4}


def load(run, legacy_loader):
    from .learning_journeys import _safe_review_file
    from .native_driver import _repair_json
    from .native_requalification import load as load_legacy
    from pku_sync.transcript_notes import POLICY, TOPIC_POLICY
    from scripts.freeze_native_candidate import runtime_sources

    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    bound = []

    def bind(spec):
        path = _safe_review_file(spec['path'])
        if sha(path) != spec['sha256']:
            raise ValueError('changed preserved input')
        bound.append((path, spec['sha256']))
        return path

    def bind_tree(value):
        if isinstance(value, dict):
            if 'path' in value and 'sha256' in value:
                bind(value)
            for child in value.values():
                bind_tree(child)
        elif isinstance(value, list):
            for child in value:
                bind_tree(child)

    try:
        proof, proof_bound = _repair_json(run.config['main_chain_requalification'])
        if (type(proof.get('schema')) is not int or proof['schema'] != 1
                or proof.get('kind') != 'faithful-transcript-native-requalification'
                or proof.get('policy') != POLICY or proof.get('legacy_version') != '0.1.51'
                or proof.get('current_version') != '0.1.52'
                or run.context['version'] != '0.1.52' or run.context['target'] != 'windows'):
            raise ValueError('wrong candidate')
        image_layout = proof.get('image_layout')
        if image_layout not in (None, 'ordered-original-packet-images-v1'):
            raise ValueError('unknown image layout')
        topic_workflow = proof.get('topic_workflow')
        if (topic_workflow not in (None, TOPIC_POLICY)
                or (topic_workflow and not image_layout)):
            raise ValueError('unknown topic workflow or missing picture scope')
        reviewed_changes = CHANGED | (IMAGE_CHANGED if image_layout else set())
        if topic_workflow:
            reviewed_changes |= TOPIC_CHANGED
        preserved, preserved_bound = _repair_json(proof['preserved_inputs'])
        if (preserved.get('kind') != 'preserved-note52-requalification-inputs-not-admission'
                or preserved.get('legacy_version') != '0.1.51'
                or preserved.get('future_candidate_version') != '0.1.52'
                or preserved.get('scope') != SCOPE
                or preserved.get('all_pre51_failure_validation_still_required') is not True
                or preserved.get('native_quality_passed') is not False
                or preserved.get('release_gate_passed') is not False
                or len(preserved['legacy51_paid_responses']) != 13
                or len(preserved['original_source_context_and_facts']) != 4
                or not preserved['diagnostic_files']):
            raise ValueError('missing original scope or failure history')
        bind_tree(preserved)
        bind_tree(proof.get('additional_evidence', []))
        legacy_config, legacy_config_bound = _repair_json(preserved['legacy_native_config'])
        legacy_proof, _ = _repair_json(preserved['legacy_requalification_proof'])
        learning = deepcopy(run.config['learning'])
        old_learning = deepcopy(legacy_config['learning'])
        # A new run owns a fresh receipt path; the source and all review
        # criteria stay exactly the same. This is evidence storage, not scope.
        for value in (learning, old_learning):
            value.get('contextual_review', {}).pop('receipt_path', None)
        if (legacy_config['main_chain_requalification'] != preserved['legacy_requalification_proof']
                or legacy_proof['legacy_proof'] != preserved['legacy_pre51_proof']
                or legacy_proof['build_manifest'] != preserved['legacy_build']
                or legacy_proof['installation_receipt'] != preserved['legacy_install']
                or learning != old_learning
                or run.config['budget'] != legacy_config['budget']):
            raise ValueError('original source, review scope or budget changed')
        frozen, _ = _repair_json(preserved['source_frozen_before_new_build'])
        legacy_build, _ = _repair_json(preserved['legacy_build'])
        legacy_install, _ = _repair_json(preserved['legacy_install'])
        frozen_root = Path(preserved['source_frozen_before_new_build']['path']).parent
        if (frozen['files'] != legacy_build['source_sha256']
                or frozen['build_manifest_sha256'] != preserved['legacy_build']['sha256']):
            raise ValueError('old physical source freeze changed')
        for relative, digest in frozen['files'].items():
            source = _safe_review_file(str(frozen_root / relative), str(frozen_root))
            if sha(source) != digest:
                raise ValueError('old physical source changed')
            bound.append((source, digest))
        # Execute the existing 51 admission and original 50 verifier unchanged.
        # Their ten failed paid stages remain failures; no new quality verdict.
        old_config = deepcopy(run.config)
        old_config['main_chain_requalification'] = preserved['legacy_requalification_proof']
        old_artifact = _safe_review_file(proof['legacy_artifact'])
        old_installed = _safe_review_file(legacy_install['installed_path'], run.need('test_root'))
        proxy = SimpleNamespace(config=old_config, context={**run.context,
            'root': frozen_root, 'version': '0.1.51', 'artifact': old_artifact,
            'artifact_sha256': legacy_build['artifact_sha256'], 'installed_path': str(old_installed)},
            need=lambda key: need(old_config, key), credential=run.credential)
        resume = load_legacy(proxy, legacy_loader)
        if len(resume['additional_summary_attempt']['stages']) != 10:
            raise ValueError('original paid stages missing')
        review, _ = _repair_json(preserved['legacy51_failed_review'])
        closeout, _ = _repair_json(preserved['legacy51_closeout'])
        bind_tree(closeout)
        if (review.get('assessment') != 'needs_revision'
                or review.get('final_native_main_chain_passed') is not False
                or review.get('release_gate_passed') is not False
                or closeout.get('all_existing_jobs_terminal') is not True
                or closeout.get('original_files_unchanged') is not True
                or closeout.get('main_chain_passed') is not False
                or closeout.get('release_gate_passed') is not False
                or closeout.get('new_jobs') != 13 or closeout.get('charged_seconds') != 0):
            raise ValueError('old native failure or closure missing')
        if {(r['path'], r['sha256']) for r in closeout['responses']} != {
                (r['path'], r['sha256']) for r in preserved['legacy51_paid_responses']}:
            raise ValueError('old paid responses missing')
        ledger, _ = _repair_json(closeout['final_ledger'])
        ids = {r['job_id'] for r in closeout['responses']}
        jobs = [j for j in ledger['readback']['jobs'] if j['id'] in ids]
        fees = [f for f in ledger['readback']['settlements'] if f['operation_id'] in ids]
        if (len(ids) != 13 or len(jobs) != 13 or len(fees) != 13
                or any(j['status'] != 'succeeded' for j in jobs)
                or {j['operation_id'] for j in jobs} != {f['operation_id'] for f in fees}
                or sum(Decimal(str(f['charged_points'])) for f in fees) != Decimal(str(closeout['charged_points']))):
            raise ValueError('old paid jobs and settlements mismatch')
        root = Path(run.context['root']).resolve()
        installed = _safe_review_file(run.context['installed_path'], run.need('test_root'))
        artifact = _safe_review_file(str(run.context['artifact']))
        signature = _safe_review_file(str(artifact) + '.sig')
        build, build_bound = _repair_json(proof['build_manifest'])
        receipt, receipt_bound = _repair_json(proof['installation_receipt'])
        if (installed == old_installed or installed.name.lower() != 'pku-desktop.exe'
                or build.get('kind') != 'local-signed-candidate-not-published'
                or build.get('version') != '0.1.52' or build.get('build_exit_code') != 0
                or build.get('source_scope') != 'all-client-python-and-static-plus-build-metadata'
                or build['artifact_sha256'] != sha(artifact) or sha(artifact) != run.context['artifact_sha256']
                or build['signature_sha256'] != sha(signature)
                or receipt.get('version') != '0.1.52' or receipt.get('generated_by') != 'os-installer'
                or receipt.get('student_product_registry_values_restored') is not True
                or receipt['artifact_sha256'] != sha(artifact)
                or _safe_review_file(receipt['installed_path']) != installed
                or receipt['installed_sha256'] != sha(installed)):
            raise ValueError('new signed installation differs')
        packaged = installed.parent / 'resources/runtime/Lib/site-packages'
        python = _safe_review_file(str(packaged.parent.parent / 'python.exe'))
        if sha(python) != receipt['installed_bundled_python_sha256']:
            raise ValueError('new bundled runtime changed')
        expected = runtime_sources(root)
        old_files = runtime_sources(frozen_root)
        if (not expected or runtime_sources(packaged) != expected
                or {k for k in build['source_sha256'] if k.startswith('pku_sync/')} != expected
                or expected - old_files != ADDED or old_files - expected
                or {k for k in old_files if sha(packaged / k) != sha(frozen_root / k)} != reviewed_changes):
            raise ValueError('unrelated or missing client changes')
        for relative, digest in build['source_sha256'].items():
            source = _safe_review_file(str(root / relative), str(root))
            if sha(source) != digest:
                raise ValueError('workspace changed after build')
            bound.append((source, digest))
            if relative in expected:
                target = _safe_review_file(str(packaged / relative), str(packaged))
                if sha(target) != digest:
                    raise ValueError('installed source changed')
                bound.append((target, digest))
        bound.extend([proof_bound, preserved_bound, legacy_config_bound, build_bound, receipt_bound,
            (artifact, sha(artifact)), (signature, sha(signature)), (installed, sha(installed)), (python, sha(python))])
        resume['bound'] = [*resume['bound'], *bound]
        resume['legacy51_proof'] = resume['proof']
        resume['proof'] = {**resume['proof'], 'current_version': '0.1.52',
            'current_artifact_sha256': sha(artifact), 'current_product_manifest': proof['build_manifest']}
        resume['requalification'] = {'policy': proof['policy'], 'legacy_version': '0.1.51',
            'current_version': '0.1.52', 'proof_sha256': proof_bound[1], 'scope': SCOPE,
            'packaged_source_files': len(expected), 'legacy_paid_stages_preserved': 10,
            'legacy51_paid_responses_preserved': 13, 'new_asr_authorized': False, 'native_quality_passed': False}
        if image_layout:
            resume['requalification']['image_layout'] = image_layout
        if topic_workflow:
            resume['requalification']['topic_workflow'] = topic_workflow
        # Later, already-settled work on this same source is a newer baseline,
        # not a reason to repeat the original ASR or erase its fee history.
        baseline_spec = proof.get('current_operation_baseline')
        if baseline_spec is not None:
            from .native_driver import _repair_verified_ledger_extension
            record, baseline_bound = _repair_json(baseline_spec)
            if (record.get('service') != 'account' or record.get('method') != 'GET'
                    or record.get('path') != '/v1/operations' or record.get('status') != 200
                    or not _repair_verified_ledger_extension(resume['fresh_baseline'],
                        record['readback'], run.need('learning.source_id'), run.need('learning.course_id'))):
                raise ValueError('unverified current operation baseline')
            resume['fresh_baseline'] = record['readback']
            resume['bound'].append(baseline_bound)
        return resume
    except (OSError, KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        raise ResourceBlocked('faithful_transcript_native_requalification_invalid') from None
