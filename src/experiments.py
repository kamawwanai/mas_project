"""Real-run orchestration with explicit API and data gates; offline matrix preview."""
from __future__ import annotations
from dataclasses import replace
from pathlib import Path
import json
import random
import tomllib
import uuid

from .architectures import PLANNERS
from .audit import readiness
from .catalog import load_catalogs, load_public_scenario, load_plan
from .checks import validate_trip
from .contracts import RunResult, digest, jsonable, read_json, write_json
from .evaluator import Evaluator, verify_conflict
from .llm import OpenAIClient, APIUnavailable
from .runtime import Runtime, Limits, LimitExceeded
from .selection import select
from .simulator import UserSimulator
from .tools import ToolRegistry

ROOT = Path(__file__).resolve().parents[1]


def load_config(path):
    path = Path(path).resolve()
    with path.open("rb") as stream:
        config = tomllib.load(stream)
    llm_path = ROOT / config.get("llm_config", "configs/llm.toml")
    with llm_path.open("rb") as stream:
        config["llm"] = tomllib.load(stream)
    return config


def matrix(scenario_ids, repeats=2, seed=42):
    jobs = []
    for repeat in range(1, repeats+1):
        block = [{"scenario_id": case, "architecture": architecture, "repeat": repeat} for case in scenario_ids for architecture in PLANNERS]
        random.Random(seed+repeat).shuffle(block)
        jobs.extend(block)
    return jobs


def version_hash():
    files = sorted(p for folder in ("src", "prompts", "configs") for p in (ROOT/folder).rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    return digest({str(p.relative_to(ROOT)): p.read_bytes().hex() for p in files})


def validate_case(root, case, catalogs, request):
    if case.get("status") != "verified":
        raise ValueError(f"Scenario not verified: {case['id']}")
    if case.get("public_hash") != digest(request):
        raise ValueError("Public scenario differs from reviewed case")
    if case["expected"] == "feasible":
        plan = load_plan(ROOT / "evaluation/reference_plans" / f"{request.id}.json")
        if validate_trip(catalogs, request, plan).exit_code:
            raise ValueError("Feasible scenario witness does not pass")
    elif case["expected"] == "infeasible":
        proof = read_json(ROOT / "evaluation/feasibility" / f"{request.id}.json")
        if not verify_conflict(catalogs, request, proof):
            raise ValueError("Conflict certificate not established")
    else:
        raise ValueError("Unknown feasibility label")
    if case.get("changes"):
        probe = ToolRegistry(catalogs, request, Runtime("preflight"))
        for change in case["changes"]:
            apply_change(probe, change)
        final_witness = load_plan(ROOT / "evaluation/reference_plans" / f"{request.id}.final.json")
        if validate_trip(probe.catalogs, probe.request, final_witness).exit_code:
            raise ValueError("Changed conditions lack a valid final witness")


def run_one(architecture, request, catalogs, client, runtime, profile, *, changes=None):
    """Does not load private files. Test clients must be supplied explicitly."""
    registry = ToolRegistry(catalogs, request, runtime, UserSimulator(profile))
    result = RunResult(runtime.run_id, architecture, request.id, digest(catalogs), mode=client.mode,
                       trace=str(runtime.path or ""))
    rejected, submitted, versions = [], [], []
    try:
        for version, change in enumerate([None, *(changes or [])], 1):
            if change:
                request = apply_change(registry, change)
                registry.previous_result = result.to_dict()
                runtime.event("runner", "condition_change", version=version, change=change)
            result.request_version = version
            rejected, submitted = [], []
            submission = PLANNERS[architecture]().run(client, registry)
            submitted = submission["plans"]
            runtime.event("runner", "submission", request_version=version, submission=submission)
            plans, rejected = select(registry.catalogs, registry.request, submitted, registry.revealed)
            result.plans = plans
            result.status = submission["status"]
            if submitted and not plans:
                result.status = "insufficient_data"
            if not submitted and result.status == "completed":
                result.status = "insufficient_data"
            result.explanation, result.unresolved = submission["explanation"], submission["unresolved"]
            versions.append({"version": version, "result": result.to_dict(), "resources_cumulative": runtime.summary()})
    except LimitExceeded as exc:
        result.status, result.plans = "budget_exhausted", []
        result.unresolved.append(str(exc))
    except Exception as exc:
        runtime.counts["errors"] += 1
        runtime.event("runner", "run_error", error_type=type(exc).__name__, message=str(exc) if client.mode == "test" else "See typed error; provider payload excluded")
        result.status, result.plans = "error", []
        result.unresolved.append(type(exc).__name__)
    result.resources = runtime.summary()
    result.resources["submitted_count"] = len(submitted)
    result.resources["submitted_valid_fraction"] = (len(submitted)-len(rejected))/len(submitted) if submitted else 0
    result.resources["candidate_valid_fraction"] = sum(c["valid"] for c in registry.candidates)/len(registry.candidates) if registry.candidates else None
    return result, {"submitted_plans": submitted, "rejected": rejected, "versions": versions}, registry.request, registry.catalogs


def apply_change(registry, change):
    from .schemas import parse_scenario
    if change["type"] == "budget":
        raw = jsonable(registry.request)
        raw["budget_kopecks"] = change["budget_kopecks"]
        registry.request = parse_scenario(raw)
    elif change["type"] == "unavailable":
        key = change["reference_id"]
        found = False
        for kind, doc in list(registry.catalogs.items()):
            items = []
            for item in doc.items:
                if item.id == key:
                    if not hasattr(item, "availability_status"):
                        raise ValueError("This object has no availability field")
                    item = replace(item, availability_status="unavailable")
                    found = True
                items.append(item)
            registry.catalogs = {**registry.catalogs, kind: replace(doc, items=items)}
        if not found:
            raise ValueError("Changed offer does not exist")
        registry.rows = {k: [registry._row(i) for i in d.items] for k, d in registry.catalogs.items()}
    else:
        raise ValueError("Unknown condition change")
    return registry.request


def real_run(config, architecture, scenario_id, split="dev", allow_api=False, run_id=None):
    # Fail closed before creating a provider client or any experiment artifacts.
    if not allow_api or not config["llm"].get("api_enabled"):
        raise APIUnavailable("Real requests disabled: API key and explicit later authorization required")
    root = ROOT / config.get("data_dir", "data/benchmark")
    ready = readiness(root)
    if not ready["ready"]:
        raise ValueError("Benchmark is not ready: " + json.dumps(ready["issues"], ensure_ascii=False))
    suite = read_json(root / "suite.json")
    case = next(c for c in suite["cases"] if c["id"] == scenario_id and c["split"] == split)
    catalogs = load_catalogs(root / "snapshot")
    request = load_public_scenario(root / "scenarios" / split / f"{scenario_id}.json")
    validate_case(root, case, catalogs, request)
    profile = read_json(ROOT / "evaluation/profiles" / f"{scenario_id}.json")
    if case.get("profile_hash") != digest(profile):
        raise ValueError("Private profile differs from reviewed case")
    pricing = read_json(ROOT / config.get("pricing", "configs/pricing.json"))
    if pricing.get("model") != config["llm"]["model"] or not pricing.get("verified_at") or any(pricing.get(k) is None for k in ("input_per_million", "cached_input_per_million", "output_per_million")):
        raise ValueError("Verified model-specific pricing required")
    client = OpenAIClient(config["llm"], allow_api=allow_api)
    run_id = run_id or f"{scenario_id}-{architecture}-{uuid.uuid4().hex[:8]}"
    directory = ROOT / config.get("runs_dir", "runs/main") / run_id
    if directory.exists():
        raise ValueError("Run directory already exists; refusing to overwrite evidence")
    limits = Limits(**config.get("limits", {}))
    if limits.cost_usd is None or limits.cost_usd <= 0:
        raise ValueError("Set a positive per-run cost_usd limit before real runs")
    runtime = Runtime(run_id, directory / "events.jsonl", limits, pricing)
    manifest = {"run_id": run_id, "mode": "real", "architecture": architecture, "scenario_id": scenario_id,
                "config": config, "code_hash": version_hash(), "snapshot_hash": digest(catalogs),
                "request_hash": digest(request), "profile_hash": digest(profile), "case_hash": digest(case), "pricing": pricing}
    write_json(directory / "manifest.json", manifest)
    result, details, final_request, final_catalogs = run_one(architecture, request, catalogs, client, runtime, profile, changes=case.get("changes"))
    write_json(directory / "result.json", result)
    write_json(directory / "candidates.json", details)
    proof = read_json(ROOT / "evaluation/feasibility" / f"{scenario_id}.json") if case["expected"] == "infeasible" else None
    evaluation = Evaluator().evaluate(final_catalogs, final_request, result.to_dict(), profile, case["expected"], proof)
    evaluation["versions"] = evaluate_versions(catalogs, request, details, case, profile, proof)
    write_json(directory / "evaluation.json", evaluation)
    write_json(directory / "summary.json", result.resources)
    return result.to_dict()


def evaluate_versions(catalogs, request, details, case, profile, proof):
    registry = ToolRegistry(catalogs, request, Runtime("version-evaluation"))
    values = []
    for version in details.get("versions", []):
        if version["version"] > 1:
            apply_change(registry, case["changes"][version["version"]-2])
        result = dict(version["result"], resources=version["resources_cumulative"])
        values.append({"version": version["version"], "evaluation": Evaluator().evaluate(registry.catalogs, registry.request, result, profile, case["expected"], proof)})
    return values


def reevaluate_runs(runs):
    """Recompute from hashed inputs, never from model-asserted scores."""
    completed = 0
    for path in sorted(Path(runs).glob("*/manifest.json")):
        manifest = read_json(path)
        if manifest.get("mode") != "real" or not (path.parent / "result.json").exists():
            continue
        config = manifest["config"]
        root = ROOT / config["data_dir"]
        catalogs = load_catalogs(root / "snapshot")
        suite = read_json(root / "suite.json")
        case = next(c for c in suite["cases"] if c["id"] == manifest["scenario_id"])
        request = load_public_scenario(root / "scenarios" / case["split"] / f"{case['id']}.json")
        profile = read_json(ROOT / "evaluation/profiles" / f"{case['id']}.json")
        if (digest(catalogs), digest(request), digest(profile), digest(case)) != (manifest["snapshot_hash"], manifest["request_hash"], manifest["profile_hash"], manifest["case_hash"]):
            raise ValueError(f"Saved input hashes changed for {manifest['run_id']}")
        registry = ToolRegistry(catalogs, request, Runtime("offline-evaluation"))
        for change in case.get("changes", []):
            apply_change(registry, change)
        proof = read_json(ROOT / "evaluation/feasibility" / f"{case['id']}.json") if case["expected"] == "infeasible" else None
        value = Evaluator().evaluate(registry.catalogs, registry.request, read_json(path.parent / "result.json"), profile, case["expected"], proof)
        value["versions"] = evaluate_versions(catalogs, request, read_json(path.parent / "candidates.json"), case, profile, proof)
        write_json(path.parent / "evaluation.json", value)
        completed += 1
    return completed
