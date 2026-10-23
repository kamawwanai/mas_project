"""Offline structural/data-blocker report. No generation or source verification."""
from collections import Counter
from pathlib import Path
import ast
import hashlib
import json
import re
from _bootstrap import ROOT
from src.offline import deny_network
from src.catalog import load_catalogs, load_scenario, load_plan
from src.checks import validate_trip
from src.contracts import read_json, write_json, digest
from src.prepare import prepare_snapshot
from src.audit import readiness


def unique_object(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise ValueError("Duplicate JSON key: " + k)
        result[k] = v
    return result


def check():
    deny_network()
    errors, json_count, python_count = [], 0, 0
    paths = [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts]
    for p in paths:
        try:
            if p.suffix == ".json":
                json.loads(p.read_text(encoding="utf-8"), object_pairs_hook=unique_object,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON number")))
                json_count += 1
            if p.suffix == ".py":
                ast.parse(p.read_text(encoding="utf-8")); python_count += 1
        except (ValueError, UnicodeError, SyntaxError) as exc:
            errors.append({"file": str(p.relative_to(ROOT)), "error": str(exc)})
    patterns = [re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{24,}"),
                re.compile(r"AKIA[0-9A-Z]{16}"), re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")]
    secrets = []
    for p in paths:
        if p.suffix in {".png", ".pyc"}:
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except UnicodeError:
            continue
        if any(pattern.search(content) for pattern in patterns):
            secrets.append(str(p.relative_to(ROOT)))
        if p.name == ".env" and re.search(r"(?m)^(?:OPENAI|OPENROUTER)_API_KEY\s*=\s*[^\s#]+", content):
            secrets.append(str(p.relative_to(ROOT)))
    preparation = prepare_snapshot(ROOT/"data/benchmark/manifest.json", check=True)
    catalogs = load_catalogs(ROOT/"data/snapshot")
    request = load_scenario(ROOT/"data/scenarios/dev/dev_01.json")
    plan = load_plan(ROOT/"data/reference_plans/dev_01.json")
    validation = validate_trip(catalogs, request, plan).to_dict()
    suite = read_json(ROOT/"data/benchmark/suite.json")
    cases = []
    for case in suite["cases"]:
        r = load_scenario(ROOT/"data/benchmark/scenarios"/case["split"]/f"{case['id']}.json")
        private = "evaluation/benchmark/profiles" if case.get("evaluation_mode") == "benchmark_assumptions" else "evaluation/profiles"
        if case["public_hash"] != digest(r) or case["profile_hash"] != digest(read_json(ROOT/private/f"{case['id']}.json")):
            errors.append({"file": case["id"], "error": "Scenario/profile hash mismatch"})
        cases.append({"id": case["id"], "status": "SYNTHETIC_VALIDATED" if case.get("status") == "synthetic_validated" else "DATA_BLOCKED", "expected_target": case["expected"], "blockers": case["pending"]})
    blockers = [{"code": p["code"], "object_id": p["object_id"], "message": p["message"]} for p in validation["incomplete_data"]]
    out = ROOT/"reports/local"
    write_json(out/"dev_01-validation.json", validation)
    write_json(out/"benchmark-readiness.json", readiness(ROOT/"data/benchmark"))
    write_json(out/"data-blockers.json", {"dev_01": blockers, "scenarios": cases})
    result = {"json_files": json_count, "python_files": python_count, "errors": errors, "secret_findings_files": sorted(set(secrets)),
              "preparation": preparation, "dev_01_exit_code": validation["exit_code"], "confirmed_total": validation["cost"]["total_kopecks"],
              "estimate_kopecks": validation["cost"]["estimate_kopecks"], "unknowns": dict(Counter(p["code"] for p in validation["incomplete_data"])),
              "scenarios": cases, "network": "disabled"}
    write_json(out/"repository-checks.json", result)
    return result


if __name__ == "__main__":
    value = check()
    print(json.dumps({k: v for k, v in value.items() if k != "scenarios"}, ensure_ascii=False, indent=2))
    raise SystemExit(1 if value["errors"] or value["secret_findings_files"] or value["preparation"]["problems"] else 0)
