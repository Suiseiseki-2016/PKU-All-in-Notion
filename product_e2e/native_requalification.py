"""Admit a new writing policy while preserving the original native failure history."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
from types import SimpleNamespace

from .core import ResourceBlocked, need


CHANGED = {
    "pku_sync/config.py", "pku_sync/media.py", "pku_sync/note_sources.py",
    "pku_sync/panel/note_videos.py", "pku_sync/panel/recordings_api.py",
}
ADDED = {"pku_sync/note_editor.py"}


def load(run, legacy_loader):
    from .learning_journeys import _safe_review_file
    from .native_driver import _repair_json
    from scripts.freeze_native_candidate import runtime_sources

    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    try:
        spec = run.config["main_chain_requalification"]
        proof, proof_bound = _repair_json(spec)
        if proof.get("kind") == "faithful-transcript-native-requalification":
            from .native_faithful_requalification import load as load_faithful
            return load_faithful(run, legacy_loader)
        if (type(proof.get("schema")) is not int or proof["schema"] != 1
                or proof.get("kind") != "contextual-note-native-requalification"
                or proof.get("policy") != "contextual-note-editor-v1"
                or proof.get("legacy_version") != "0.1.50"
                or proof.get("current_version") != "0.1.51"
                or run.context["version"] != "0.1.51" or run.context["target"] != "windows"):
            raise ValueError
        root = Path(run.context["root"]).resolve()
        test_root = _safe_review_file(run.need("test_root"))
        installed = _safe_review_file(run.context["installed_path"], str(test_root))
        legacy_installed = _safe_review_file(proof["legacy_installed_path"], str(test_root))
        if (installed == legacy_installed or installed.name.lower() != "pku-desktop.exe"
                or legacy_installed.name.lower() != "pku-desktop.exe"
                or sha(legacy_installed) != proof["legacy_installed_sha256"]):
            raise ValueError
        legacy, legacy_bound = _repair_json(proof["legacy_proof"])
        if legacy["current_version"] != "0.1.50":
            raise ValueError
        # The old verifier sees its physical frozen installation, not the new
        # workspace's module list. Its ten failures, ledgers and AST guards run
        # unchanged; no old assertion is reinterpreted as a success.
        config = deepcopy(run.config)
        config.pop("main_chain_requalification")
        config["main_chain_resume"] = {"proof_path": str(legacy_bound[0]), "proof_sha256": legacy_bound[1]}
        legacy_root = legacy_installed.parent / "resources/runtime/Lib/site-packages"
        proxy = SimpleNamespace(config=config, context={**run.context, "root": legacy_root,
            "version": "0.1.50", "artifact_sha256": legacy["current_artifact_sha256"],
            "installed_path": str(legacy_installed)},
            need=lambda key: need(config, key), credential=run.credential)
        resume = legacy_loader(proxy)
        if resume is None or len(resume["additional_summary_attempt"]["stages"]) != 10:
            raise ValueError
        build, build_bound = _repair_json(proof["build_manifest"])
        receipt, receipt_bound = _repair_json(proof["installation_receipt"])
        artifact = _safe_review_file(str(run.context["artifact"]))
        signature = _safe_review_file(str(artifact) + ".sig")
        artifact_sha = sha(artifact)
        if (artifact_sha != run.context["artifact_sha256"]
                or build.get("kind") != "local-signed-candidate-not-published"
                or build.get("version") != "0.1.51" or build.get("build_exit_code") != 0
                or build.get("source_scope") != "all-client-python-and-static-plus-build-metadata"
                or build.get("artifact_sha256") != artifact_sha
                or build.get("signature_sha256") != sha(signature)
                or receipt.get("version") != "0.1.51" or receipt.get("generated_by") != "os-installer"
                or receipt.get("student_product_registry_values_restored") is not True
                or receipt.get("artifact_sha256") != artifact_sha
                or _safe_review_file(receipt["installed_path"]) != installed
                or receipt.get("installed_sha256") != sha(installed)):
            raise ValueError
        packaged = installed.parent / "resources/runtime/Lib/site-packages"
        python = _safe_review_file(str(packaged.parent.parent / "python.exe"))
        if sha(python) != receipt["installed_bundled_python_sha256"]:
            raise ValueError
        expected = runtime_sources(root)
        if (not expected or runtime_sources(packaged) != expected
                or {key for key in build["source_sha256"] if key.startswith("pku_sync/")} != expected):
            raise ValueError
        bound = [proof_bound, legacy_bound, build_bound, receipt_bound,
                 (legacy_installed, proof["legacy_installed_sha256"]),
                 (artifact, artifact_sha), (signature, build["signature_sha256"]),
                 (installed, receipt["installed_sha256"]), (python, receipt["installed_bundled_python_sha256"])]
        for relative, digest in build["source_sha256"].items():
            source = _safe_review_file(str(root / relative), str(root))
            if sha(source) != digest:
                raise ValueError
            bound.append((source, digest))
            if relative in expected:
                target = _safe_review_file(str(packaged / relative), str(packaged))
                if sha(target) != digest:
                    raise ValueError
                bound.append((target, digest))
        # Limit this admission to the actual reviewed client change. Other
        # product changes require their own frozen candidate requalification.
        old_files = runtime_sources(legacy_root)
        if expected - old_files != ADDED or old_files - expected:
            raise ValueError
        changed = {key for key in expected & old_files if sha(packaged / key) != sha(legacy_root / key)}
        if changed != CHANGED:
            raise ValueError
        # CSS was not in the historical module manifest. Bind the extra old
        # static bytes here too, so none of the compared inputs can drift.
        bound.extend((legacy_root / key, sha(legacy_root / key)) for key in sorted(old_files))
        resume["bound"] = [*resume["bound"], *bound]
        resume["legacy_proof"] = resume["proof"]
        resume["proof"] = {**resume["proof"], "current_version": "0.1.51",
                           "current_artifact_sha256": artifact_sha,
                           "current_product_manifest": proof["build_manifest"]}
        resume["requalification"] = {"policy": proof["policy"], "legacy_version": "0.1.50",
            "current_version": "0.1.51", "proof_sha256": proof_bound[1],
            "packaged_source_files": len(expected), "legacy_paid_stages_preserved": 10,
            "new_asr_authorized": False, "native_quality_passed": False}
        return resume
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        raise ResourceBlocked("contextual_note_native_requalification_invalid") from None
