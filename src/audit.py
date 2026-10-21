"""Read-only input audit and benchmark readiness checks."""
from pathlib import Path
import hashlib
import json

from .catalog import load_catalog, DataLoadError
from .schemas import CATALOG_KINDS


def audit_data(root):
    root = Path(root)
    files = []
    errors = []
    for kind in CATALOG_KINDS:
        path = root / "snapshot" / f"{kind}.json"
        entry = {"file": str(path), "kind": kind}
        try:
            content = path.read_bytes()
            entry["sha256"] = hashlib.sha256(content).hexdigest()
            raw = json.loads(content.decode("utf-8"))
            rows = raw.get("items", []) if isinstance(raw, dict) else raw
            entry["raw_count"] = len(rows) if isinstance(rows, list) else None
            if isinstance(rows, list):
                entry["missing_sources"] = [r.get("id") for r in rows if not r.get("source_url") or not r.get("checked_at")]
            doc = load_catalog(path, kind)
            entry["loaded_count"] = len(doc.items)
            entry["is_template"] = doc.is_template
        except (OSError, UnicodeError, json.JSONDecodeError, DataLoadError) as exc:
            problem = exc.problem.to_dict() if isinstance(exc, DataLoadError) else {"code": "INPUT_ERROR", "message": str(exc), "file": str(path)}
            entry["error"] = problem
            errors.append(problem)
        files.append(entry)
    return {"files": files, "input_errors": errors, "exit_code": 2 if errors else 0}


def readiness(root):
    """A reviewed manifest is necessary, never sufficient: validate actual witnesses too."""
    from .catalog import load_catalogs, load_public_scenario, load_plan
    from .checks import validate_trip
    from .contracts import read_json, digest
    root = Path(root)
    issues = []
    try:
        manifest = read_json(root / "manifest.json")
        if manifest.get("status") != "verified" or not manifest.get("reviewed_by") or not manifest.get("reviewed_at"):
            issues.append("Snapshot lacks completed human verification")
        docs = load_catalogs(root / "snapshot")
        if any(d.is_template for d in docs.values()):
            issues.append("Template catalogs are not experiment-ready")
        if manifest.get("catalog_hash") != digest(docs):
            issues.append("Catalog hash differs from reviewed manifest")
        root_case = load_public_scenario(root / "scenarios/dev/dev_01.json")
        witness_path = root / "../../evaluation/reference_plans/dev_01.json"
        witness = load_plan(witness_path.resolve())
        report = validate_trip(docs, root_case, witness)
        if report.exit_code:
            issues.append({"dev_01_validation": report.to_dict()})
    except (OSError, ValueError, DataLoadError) as exc:
        issues.append(str(exc))
    return {"ready": not issues, "issues": issues}
