"""Run every mandatory installed-native journey; absent drivers fail, never skip."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from product_e2e_gate import (
    deferred_coverage,
    load_manifest,
    required_assertions,
    required_cases,
    sha256,
    source_digest,
    verify,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("windows", "macos"), required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--installed-path", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--main-chain", action="store_true",
                        help="Execute the real learning bootstrap; produces no release report or full-case pass")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    # Never overwrite a previous run/evidence; every attempt has a separate directory.
    args.output_dir.mkdir(parents=True, exist_ok=False)
    manifest = load_manifest(root)
    report = {"schema_version": 1, "target": args.target, "version": args.version,
              "entry": "installed-native", "installed_path": args.installed_path,
              "runner_generated": True, "started_at": datetime.now(timezone.utc).isoformat(),
              "artifact_sha256": sha256(args.artifact), "source_digest": source_digest(root),
              "manifest_sha256": sha256(root / "docs/PRODUCT_E2E_SCENARIOS.json"),
              "deferred_coverage": deferred_coverage(manifest, args.target),
              "observed_live_hosts": [], "metrics": {"download_bytes": 0, "upload_bytes": 0,
                                                     "charged_points": 0, "charged_seconds": 0}, "cases": []}
    driver = None
    driver_error = None
    try:
        driver_path = root / "product_e2e/native_driver.py"
        spec = importlib.util.spec_from_file_location("product_e2e.native_driver", driver_path)
        driver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(driver)
        if not callable(getattr(driver, "run_case", None)):
            raise ValueError("native driver must implement run_case")
        config = json.loads(args.config.read_text("utf-8"))
    except Exception as exc:
        driver_error = type(exc).__name__
    if args.main_chain:
        # This report deliberately cannot satisfy the full release report schema.
        # The mandatory runner below is unchanged and never selects a subset.
        main_report = {key: report[key] for key in ("target", "version", "artifact_sha256", "source_digest", "manifest_sha256", "started_at")}
        main_report.update(kind=("installed-native-main-chain-repair-continuation-not-release-verdict"
                                if not driver_error and config.get('main_chain_resume') is not None
                                else "installed-native-main-chain-not-release-verdict"),
                           complete_main_chains_passed=0, complete_cases_passed=0,
                           complete_journeys_passed=0, full_release_gate_passed=False)
        try:
            if driver_error:
                raise RuntimeError("Native main chain driver/config unavailable")
            context = {"root": root, "run_dir": args.output_dir, "artifact": args.artifact.resolve(),
                       "artifact_sha256": report["artifact_sha256"], "target": args.target,
                       "version": args.version, "installed_path": args.installed_path, "config": config}
            result = driver.run_main_chain(context)
            main_report["result"] = result
            if result.get('repair_continuation'):
                main_report['repair_continuation'] = result['repair_continuation']
            checks = result["assertions"]
            metrics = result["metrics"]
            passed = (not result.get("blockers") and result.get("entry") == "installed-native"
                      and result.get("mocked") is False and result.get("injection") is None
                      and set(checks) == set(driver.MAIN_CHECKS) and all(value is True for value in checks.values())
                      and all(type(metrics.get(field)) in (int, float) and 0 <= metrics[field] < float("inf")
                              for field in report["metrics"]))
            main_report["complete_main_chains_passed"] = int(passed)
        except Exception as exc:
            main_report["error_type"] = type(exc).__name__
            main_report["error_location"] = [f"{Path(frame.filename).name}:{frame.lineno}"
                                            for frame in traceback.extract_tb(exc.__traceback__)]
        main_report["finished_source_digest"] = source_digest(root)
        if main_report["finished_source_digest"] != main_report["source_digest"]:
            main_report["complete_main_chains_passed"] = 0
            main_report["input_changed_during_execution"] = True
        main_report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report_path = args.output_dir / "main-chain-report.json"
        report_path.write_text(json.dumps(main_report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Native main chain: {main_report['complete_main_chains_passed']}/1; full release cases: 0/12; report: {report_path}")
        return 0 if main_report["complete_main_chains_passed"] == 1 else 1
    hosts = set()
    for (cid, mode), scenario in required_cases(manifest, args.target).items():
        case = {"id": cid, "mode": mode, "entry": "installed-native",
                "artifact_sha256": report["artifact_sha256"],
                "started_at": datetime.now(timezone.utc).isoformat(), "outcome": "failed"}
        try:
            if driver_error:
                raise RuntimeError("Native driver/config unavailable: " + driver_error)
            context = {"root": root, "run_dir": args.output_dir, "artifact": args.artifact.resolve(),
                       "artifact_sha256": report["artifact_sha256"], "target": args.target,
                       "version": args.version, "installed_path": args.installed_path, "config": config}
            result = driver.run_case(scenario, mode, context)
            # Driver cannot override case identity, timestamps, package, or set passed itself.
            for field in ("entry", "mocked", "injection", "evidence", "assertions"):
                case[field] = result[field]
            case["metrics"] = result["metrics"]
            hosts.update(result["observed_live_hosts"])
            for field in report["metrics"]:
                value = result["metrics"][field]
                if type(value) not in {int, float} or not 0 <= value < float("inf"):
                    report["metrics"][field] = None
                elif report["metrics"][field] is not None:
                    report["metrics"][field] += value
            if mode == "fault":
                case["fault_variants"] = result["fault_variants"]
            if result.get("blockers"):
                case["blockers"] = result["blockers"]
                case["blocker"] = "native_resources_or_capabilities_unavailable"
                raise RuntimeError("Native journey blocked")
            if set(case["assertions"]) != set(scenario["assertions"]) or not all(
                    case["assertions"][key] is True for key in required_assertions(scenario, mode)):
                raise AssertionError("Mandatory journey assertion failed")
            for field in report["metrics"]:
                value = result["metrics"][field]
                if type(value) not in {int, float} or not 0 <= value < float("inf"):
                    raise ValueError("Invalid measured case metric")
            case["outcome"] = "passed"
        except Exception as exc:
            # Do not serialize exception message/config; they may contain credentials.
            case["error_type"] = type(exc).__name__
            case["error_location"] = [f"{Path(frame.filename).name}:{frame.lineno}"
                                      for frame in traceback.extract_tb(exc.__traceback__)]
            if driver_error:
                case["blocker"] = "native_driver_or_private_config_missing"
                for field in report["metrics"]:
                    report["metrics"][field] = None
        case["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["cases"].append(case)
        print(f"{cid}/{mode}: {case['outcome']}", flush=True)
    report["observed_live_hosts"] = sorted(hosts)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report_path = args.output_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        result = verify(root, report_path, args.artifact, args.target, args.version)
    except Exception as exc:
        print(f"PRODUCT RELEASE BLOCKED ({type(exc).__name__}); report: {report_path}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
