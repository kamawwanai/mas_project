"""Read-only release gate for the reviewed snapshot and complete scenario suite."""
import argparse
import json
from pathlib import Path
from _bootstrap import ROOT
from src.audit import readiness
from src.catalog import load_catalogs, load_public_scenario, DataLoadError
from src.contracts import digest, read_json
from src.experiments import validate_case


def check(root):
    result = readiness(root)
    suite = read_json(root / "suite.json")
    expected_ids = {f"dev_{i:02d}" for i in range(1,5)} | {f"test_{i:02d}" for i in range(1,13)}
    if {c["id"] for c in suite["cases"]} != expected_ids or len(suite["cases"]) != 16:
        result["issues"].append("Expected exactly four dev and twelve test scenarios")
    catalogs = load_catalogs(root / "snapshot")
    cases = []
    for case in suite["cases"]:
        try:
            request = load_public_scenario(root / "scenarios" / case["split"] / f"{case['id']}.json")
            validate_case(root, case, catalogs, request)
            if case.get("profile_hash") != digest(read_json(ROOT / "evaluation/profiles" / f"{case['id']}.json")):
                raise ValueError("Profile hash differs")
            cases.append({"id": case["id"], "ready": True})
        except (OSError, ValueError, DataLoadError) as exc:
            cases.append({"id": case["id"], "ready": False, "reason": str(exc)})
    result["cases"] = cases
    result["ready"] = not result["issues"] and all(c["ready"] for c in cases)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/benchmark")
    args = parser.parse_args()
    try:
        value = check(args.data_dir)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0 if value["ready"] else 3
    except (OSError, ValueError, DataLoadError) as exc:
        print(json.dumps({"ready": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
