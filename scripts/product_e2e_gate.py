"""Fail-closed product release gate; no skip/force/allow-missing option.

Evidence must be produced by native E2E execution. This verifies report
integrity, not the honesty of a local administrator; controlled CI promotion
must authenticate the runner and independently execute/verify the evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

GROUPS = {"JOURNEY": 6, "XPLAT": 1}
EXPECTED_IDS = {f"{group}-{number:02}" for group, count in GROUPS.items()
                for number in range(1, count + 1)}
# Coverage changes require explicit review and an update to this fingerprint.
# 2026-10-15: user-approved hidden quiz UI replaces its retired visible flows.
# Hash the normalized contract instead of duplicating every assertion here.
COVERAGE_FINGERPRINT = "4d82d2f280755edf7262030beb4286a2cc1112fd57bff5344936914afa2a4939"
EXCLUDED = {".git", ".venv", "node_modules", "target", "dist", "artifacts", "data",
            "__pycache__", ".pytest_cache", ".ruff_cache"}
GENERATED_TREES = {"desktop/src-tauri/resources", "desktop/src-tauri/binaries"}
SOURCE_SUFFIXES = {".py", ".rs", ".js", ".css", ".html", ".json", ".toml", ".lock",
                   ".ps1", ".sh", ".nsi", ".png", ".ico", ".icns"}
FAULT_VARIANTS = {
    "JOURNEY-01": ["wrong-password", "unverified", "expired-session", "oauth-cancel", "switch-account", "upgrade-restart"],
    "JOURNEY-02": ["zero-null", "missing-revision", "bool-revision", "string-revision", "negative-revision", "zero-nonempty", "positive-null", "partial-snapshot", "wrong-course", "login-html", "profile-ok-catalog-fails", "legacy-extra-course"],
    "JOURNEY-03": ["create-lost-response", "append-lost-response", "readback-fails", "permission-archive-move", "expired-media-url", "restart-after-charge"],
    "JOURNEY-04": ["double-click", "lost-post-response", "two-devices", "changed-file", "changed-target", "changed-deadline", "restart-pending-submission"],
    "JOURNEY-05": ["kill-process-and-restart"],
    "JOURNEY-06": ["offline", "dns", "proxy", "timeout", "network-restored", "403", "409", "429", "5xx", "low-balance", "disk-full", "malformed-json", "permit-acquire-lost-response", "permit-release-lost-response", "kill-process-and-restart"],
    "XPLAT-01": ["fresh-mac", "legacy-mac-extra-course", "oauth-reconnect", "restart"],
}


class GateError(ValueError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_digest(root: Path) -> str:
    """Includes dirty source, tests, build scripts, dependency locks and policy."""
    files = []
    for directory in ("pku_sync", "desktop", "scripts", "tests", "backend_changes", "product_e2e"):
        base = root / directory
        if not base.exists():
            continue
        # Prune caches before walking, particularly desktop's multi-GB targets.
        import os
        for current, directories, names in os.walk(base, followlinks=False):
            directories[:] = [name for name in directories if name not in EXCLUDED
                              and (Path(current) / name).relative_to(root).as_posix() not in GENERATED_TREES
                              and not (Path(current) / name).is_symlink()]
            for name in names:
                path = Path(current) / name
                if (path.suffix in SOURCE_SUFFIXES and not name.startswith(".env")
                        and not path.is_symlink()):
                    files.append(path)
    for name in ("pyproject.toml", "uv.lock", "docs/PRODUCT_E2E_SCENARIOS.json",
                 "docs/PRODUCT_E2E_RELEASE_GATE.md", "docs/PRODUCT_INCREMENTAL_SYNC_ACCEPTANCE.md",
                 "docs/WINDOWS_E2E_GOAL_PROMPT.md"):
        path = root / name
        if path.is_file():
            files.append(path)
    if not files:
        raise GateError("No source inputs found")
    digest = hashlib.sha256()
    for path in sorted(set(files)):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(sha256(path).encode() + b"\n")
    return digest.hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise GateError(message)


def coverage_digest(rows: list[dict]) -> str:
    contract = [{"id": row["id"], "targets": sorted(row["targets"]),
                 "assertions": sorted(row["assertions"]),
                 "fault_variants": sorted(FAULT_VARIANTS[row["id"]]),
                 "deferrals": row.get("deferrals", {}),
                 "assertions_by_mode": {mode: sorted(required_assertions(row, mode))
                                        for mode in ("live", "fault")}}
                for row in sorted(rows, key=lambda row: row["id"])]
    normalized = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def required_assertions(row: dict, mode: str) -> list[str]:
    return row.get("assertions_by_mode", {}).get(mode, row["assertions"])


def scoped_scenario(row: dict, target: str) -> dict:
    """Only checked-in, fingerprinted user-approved policy can defer coverage."""
    deferral = row.get("deferrals", {}).get(target, {})
    removed = set(deferral.get("assertions", []))
    replacements = deferral.get("assertion_replacements", {})
    result = {**row, "assertions": [replacements.get(key, key) for key in row["assertions"] if key not in removed]}
    if "assertions_by_mode" in row:
        result["assertions_by_mode"] = {mode: [replacements.get(key, key) for key in keys if key not in removed]
                                        for mode, keys in row["assertions_by_mode"].items()}
    result["fault_variants"] = [key for key in FAULT_VARIANTS[row["id"]]
                                if key not in deferral.get("fault_variants", [])]
    result["deferred_assertions"] = sorted(removed)
    result["assertion_replacements"] = replacements
    result["device_scope"] = "single-machine" if target == "windows" else "multi-device"
    result["supported_dpi"] = deferral.get("physical_dpi", {}).get("required", [96, 120, 144])
    return result


def deferred_coverage(manifest: dict, target: str) -> list[dict]:
    return [{"id": row["id"], **row["deferrals"][target], "status": "deferred-unverified"}
            for row in manifest["scenarios"] if target in row.get("deferrals", {})]


def load_manifest(root: Path) -> dict:
    value = json.loads((root / "docs/PRODUCT_E2E_SCENARIOS.json").read_text("utf-8"))
    rows = value["scenarios"]
    require(value["schema_version"] == 1, "Unsupported scenario schema")
    require(len(rows) == 7 and {row["id"] for row in rows} == EXPECTED_IDS,
            "Mandatory scenario policy was removed or duplicated")
    require(value["required_modes"] == ["live", "fault"], "Mandatory modes changed")
    require(set(value["required_evidence_roles"]) == {"ui", "oracle", "network", "runtime"},
            "Mandatory evidence policy changed")
    for row in rows:
        expected_targets = ["macos"] if row["id"].startswith("XPLAT-") else ["windows", "macos"]
        require(row["targets"] == expected_targets, f"Target coverage reduced: {row['id']}")
        require(len(row["assertions"]) >= 3 and len(set(row["assertions"])) == len(row["assertions"]),
                f"Missing assertions: {row['id']}")
        for target, deferral in row.get("deferrals", {}).items():
            require(target in row["targets"] and bool(deferral.get("reason")), "Unjustified target deferral")
            require(set(deferral["assertions"]) < set(row["assertions"]), "Deferral removed whole journey")
            require(set(deferral["fault_variants"]) < set(FAULT_VARIANTS[row["id"]]), "Deferral removed all faults")
            replacements = deferral.get("assertion_replacements", {})
            require(isinstance(replacements, dict) and set(replacements) <= set(row["assertions"])
                    and not set(replacements) & set(deferral["assertions"])
                    and all(isinstance(key, str) and key and key not in row["assertions"]
                            for key in replacements.values())
                    and len(set(replacements.values())) == len(replacements), "Invalid target assertion replacements")
            require(len(scoped_scenario(row, target)["assertions"]) >= 3, "Too little active journey coverage")
            if "physical_dpi" in deferral:
                dpi = deferral["physical_dpi"]
                require(row["id"] == "JOURNEY-06" and target == "windows"
                        and dpi == {"required": [96], "deferred": [120, 144]}
                        and deferral["reason"] == "user-approved-2026-10-07-defer-125-and-150-percent-physical-windows-scaling",
                        "Unapproved physical DPI deferral")
        if "assertions_by_mode" in row:
            groups = row["assertions_by_mode"]
            require(set(groups) == {"live", "fault"}, "Both assertion modes are mandatory")
            require(all(isinstance(keys, list) and keys and len(keys) == len(set(keys))
                        and set(keys) <= set(row["assertions"]) for keys in groups.values()),
                    f"Invalid mode assertions: {row['id']}")
            require(set().union(*map(set, groups.values())) == set(row["assertions"]),
                    f"Mode assignment drops required coverage: {row['id']}")
    require(coverage_digest(rows) == COVERAGE_FINGERPRINT,
            "Mandatory assertion coverage changed; explicit policy review is required")
    return value


def required_cases(manifest: dict, target: str) -> dict:
    return {(row["id"], mode): scoped_scenario(row, target) for row in manifest["scenarios"] if target in row["targets"]
            for mode in manifest["required_modes"]}


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None, "Report timestamps must have timezone")
    return result.astimezone(timezone.utc)


def _verify(root: Path, report_path: Path, artifact: Path, target: str,
           version: str, now: datetime | None = None) -> dict:
    require(target in {"windows", "macos"}, "Unsupported target")
    root, report_path, artifact = root.resolve(), report_path.resolve(), artifact.resolve()
    require(report_path.is_file(), "Missing product E2E report")
    require(artifact.is_file() and artifact.stat().st_size > 0, "Missing candidate artifact")
    manifest = load_manifest(root)
    expected = required_cases(manifest, target)
    report = json.loads(report_path.read_text("utf-8"))
    require(report["schema_version"] == 1, "Unsupported report schema")
    require(report["target"] == target and report["version"] == version, "Wrong target/version")
    artifact_hash = sha256(artifact)
    require(report["artifact_sha256"] == artifact_hash, "Report is for another installer")
    require(report["source_digest"] == source_digest(root), "Source/dependencies changed since E2E")
    require(report["manifest_sha256"] == sha256(root / "docs/PRODUCT_E2E_SCENARIOS.json"),
            "Scenario policy changed since E2E")
    now = now or datetime.now(timezone.utc)
    start, end = timestamp(report["started_at"]), timestamp(report["finished_at"])
    require(start < end <= now, "Invalid or future report timestamps")
    require(now - start <= timedelta(hours=24), "E2E evidence is older than 24 hours")
    require(report["entry"] == "installed-native", "Source/browser/API-only is not native E2E")
    require(bool(report["installed_path"]), "Missing installed application path")
    require(report["runner_generated"] is True, "Report must come from actual execution")
    require(report.get("deferred_coverage", []) == deferred_coverage(manifest, target),
            "Deferred coverage must be explicitly reported as unverified")
    require(isinstance(report["observed_live_hosts"], list)
            and set(manifest["service_hosts"]) <= set(report["observed_live_hosts"]),
            "Required live services were not observed")
    metrics = report["metrics"]
    for field in ("download_bytes", "upload_bytes", "charged_points", "charged_seconds"):
        require(type(metrics[field]) in {int, float} and 0 <= metrics[field] < float("inf"),
                f"Missing/invalid measured metric: {field}")
    keys, evidence_paths, covered = [], set(), {}
    for case in report["cases"]:
        key = (case["id"], case["mode"])
        require(key in expected and key not in keys, f"Unknown/duplicate case: {key}")
        keys.append(key)
        require(case["outcome"] == "passed", f"Required case is not passed: {key}")
        require(case["entry"] == "installed-native", f"Wrong case entry: {key}")
        require(case["artifact_sha256"] == artifact_hash, f"Wrong package in case: {key}")
        case_start, case_end = timestamp(case["started_at"]), timestamp(case["finished_at"])
        require(start <= case_start < case_end <= end, f"Invalid case time: {key}")
        assertions = case["assertions"]
        require(set(assertions) == set(expected[key]["assertions"]), f"Missing/extra assertions: {key}")
        require(all(type(value) is bool for value in assertions.values()), f"Nonboolean assertion: {key}")
        require(all(assertions[name] is True for name in required_assertions(expected[key], case["mode"])),
                f"Failed mandatory mode assertion: {key}")
        covered.setdefault(case["id"], set()).update(name for name, value in assertions.items() if value is True)
        if case["mode"] == "live":
            require(case["mocked"] is False and case.get("injection") is None,
                    f"A substitute/fault run is labelled live: {key}")
        else:
            require(isinstance(case["injection"], str) and bool(case["injection"].strip()),
                    f"Missing boundary fault description: {key}")
            variants = expected[key]["fault_variants"]
            require(set(case["fault_variants"]) == set(variants)
                    and all(value is True for value in case["fault_variants"].values()),
                    f"Missing/failed mandatory fault variants: {key}")
        roles = set()
        for evidence in case["evidence"]:
            roles.add(evidence["role"])
            relative = Path(evidence["path"])
            require(not relative.is_absolute(), f"Evidence path must be relative: {key}")
            path = (report_path.parent / relative).resolve()
            require(path.is_relative_to(report_path.parent) and path != report_path,
                    f"Evidence path escapes run directory: {key}")
            require(path.is_file() and path.stat().st_size > 0, f"Missing/empty evidence: {key}")
            require(evidence["sha256"] == sha256(path), f"Evidence hash mismatch: {key}")
            # Every case/role needs its own evidence; shared empty boilerplate cannot cover cases.
            require(path not in evidence_paths, f"Evidence reused across case/roles: {key}")
            evidence_paths.add(path)
        require(set(manifest["required_evidence_roles"]) <= roles, f"Missing evidence roles: {key}")
    require(set(keys) == set(expected), f"Required cases missing: {sorted(set(expected) - set(keys))}")
    for row in manifest["scenarios"]:
        if target in row["targets"]:
            require(covered.get(row["id"], set()) == set(scoped_scenario(row, target)["assertions"]),
                    f"Live/fault union misses required assertions: {row['id']}")
    return {"target": target, "version": version, "passed_cases": len(keys),
            "covered_assertions": sum(len(scoped_scenario(row, target)["assertions"])
                                      for row in manifest["scenarios"] if target in row["targets"]),
            "deferred_coverage": deferred_coverage(manifest, target),
            "artifact_sha256": artifact_hash, "release_gate": "passed"}


def verify(*args, **kwargs) -> dict:
    try:
        return _verify(*args, **kwargs)
    except GateError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise GateError(f"Invalid/incomplete E2E report: {type(exc).__name__}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--fingerprint", action="store_true", help="Print inputs; never passes a release")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--target", choices=("windows", "macos"))
    parser.add_argument("--version")
    args = parser.parse_args()
    try:
        if args.fingerprint:
            manifest = load_manifest(args.root)
            print(json.dumps({"source_digest": source_digest(args.root),
                              "manifest_sha256": sha256(args.root / "docs/PRODUCT_E2E_SCENARIOS.json"),
                              "required_cases": {target: len(required_cases(manifest, target))
                                                 for target in ("windows", "macos")}}))
            return 0
        require(all((args.report, args.artifact, args.target, args.version)),
                "Report, installer, target and version are mandatory; no E2E bypass")
        print(json.dumps(verify(args.root, args.report, args.artifact, args.target, args.version)))
        return 0
    except (GateError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"PRODUCT E2E RELEASE BLOCKED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
