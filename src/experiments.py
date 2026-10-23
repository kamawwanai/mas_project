"""Real-run orchestration with explicit API and data gates; offline matrix preview."""
from __future__ import annotations
from dataclasses import replace
from datetime import date
from pathlib import Path
import json
import math
import random
import tomllib
import uuid

from .architectures import PLANNERS
from .audit import readiness
from .catalog import load_catalogs, load_public_scenario, load_plan
from .checks import validate_trip
from .contracts import RunResult, digest, jsonable, read_json, write_json
from .evaluator import Evaluator, verify_conflict, validate_profile
from .llm import OpenAIClient, APIUnavailable
from .evidence import valid_url
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


def validate_pricing(config, pricing, *, max_age_days=7, today=None):
    """Fail closed when provider pricing is mismatched, malformed or stale."""
    try:
        checked = date.fromisoformat(pricing["verified_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Pricing verification date must be ISO YYYY-MM-DD") from exc
    rates = [pricing.get(k) for k in ("input_per_million", "cached_input_per_million", "output_per_million")]
    source = pricing.get("source_url")
    if (pricing.get("provider") != config["llm"].get("provider") or
            pricing.get("model") != config["llm"].get("model") or
            pricing.get("currency") != "USD" or pricing.get("unit") != "tokens_per_million" or
            not valid_url(source) or config["llm"].get("provider") == "openrouter" and
            not source.startswith("https://openrouter.ai/") or
            any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in rates)):
        raise ValueError("Verified provider/model pricing required")
    age = ((today or date.today())-checked).days
    if age < 0 or age > max_age_days:
        raise ValueError("Pricing is stale; recheck the official provider page before paid requests")
    return pricing


def matrix(scenario_ids, repeats=2, seed=42):
    jobs = []
    for repeat in range(1, repeats+1):
        block = [{"scenario_id": case, "architecture": architecture, "repeat": repeat} for case in scenario_ids for architecture in PLANNERS]
        random.Random(seed+repeat).shuffle(block)
        jobs.extend(block)
    return jobs


def version_hash():
    files = sorted(p for folder in ("src", "prompts", "configs", "scripts") for p in (ROOT/folder).rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    files += [ROOT / name for name in ("pyproject.toml", "requirements.lock.txt")]
    return digest({str(p.relative_to(ROOT)): p.read_bytes().hex() for p in files})


def validate_case(root, case, catalogs, request, *, context=None):
    if case.get("status") != ("synthetic_validated" if context else "verified"):
        raise ValueError(f"Scenario not verified: {case['id']}")
    if case.get("public_hash") != digest(request):
        raise ValueError("Public scenario differs from reviewed case")
    if case["expected"] == "feasible":
        plan = load_plan(ROOT / ("evaluation/benchmark/reference_plans" if context else "evaluation/reference_plans") / f"{request.id}.json")
        if validate_trip(catalogs, request, plan, context=context).exit_code:
            raise ValueError("Feasible scenario witness does not pass")
    elif case["expected"] == "infeasible":
        proof = read_json(ROOT / ("evaluation/benchmark/feasibility" if context else "evaluation/feasibility") / f"{request.id}.json")
        if not verify_conflict(catalogs, request, proof, context=context):
            raise ValueError("Conflict certificate not established")
    else:
        raise ValueError("Unknown feasibility label")
    if case.get("changes"):
        probe = ToolRegistry(catalogs, request, Runtime("preflight"), context=context)
        for change in case["changes"]:
            apply_change(probe, change)
        final_witness = load_plan(ROOT / ("evaluation/benchmark/reference_plans" if context else "evaluation/reference_plans") / f"{request.id}.final.json")
        if validate_trip(probe.catalogs, probe.request, final_witness, context=probe.context).exit_code:
            raise ValueError("Changed conditions lack a valid final witness")


def run_one(architecture, request, catalogs, client, runtime, profile, *, changes=None, context=None):
    """Does not load private files. Test clients must be supplied explicitly."""
    registry = ToolRegistry(catalogs, request, runtime, UserSimulator(profile), context=context)
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
            plans, rejected = select(registry.catalogs, registry.request, submitted, registry.revealed, context=registry.context)
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
    before_catalogs = registry.catalogs
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
        if registry.context:
            registry.context = registry.context.changed(before_catalogs, registry.catalogs, change)
        registry.rows = {k: [registry._row(i) for i in d.items] for k, d in registry.catalogs.items()}
    else:
        raise ValueError("Unknown condition change")
    registry.latest_change = dict(change)
    return registry.request


def persist_run(directory, architecture, request, catalogs, client, runtime, profile, manifest,
                *, changes=None, expected="feasible", proof=None, context=None):
    """Same artifact/evaluation path for explicit test doubles and authorized real calls."""
    from .batch import seal_run
    directory = Path(directory)
    if directory.exists():
        raise ValueError("Run directory already exists; preserve existing evidence")
    if manifest.get("mode") != client.mode:
        raise ValueError("Client and artifact modes must match")
    if context and (manifest.get("evaluation_mode") != "benchmark_assumptions" or manifest.get("assumptions_hash") != context.registry_hash):
        raise ValueError("Synthetic environment must be explicitly labelled and hash-bound")
    validate_profile(profile)
    labels = {"mode": client.mode, "mock_client": client.mode != "real",
              "synthetic": context is not None or client.mode != "real",
              "evaluation_mode": "benchmark_assumptions" if context else "strict",
              "assumptions_hash": context.registry_hash if context else None}
    runtime.path = directory / "events.jsonl"
    write_json(directory / "manifest.json", {**manifest, **labels})
    runtime.event("runner", "run_start", **labels)
    result, details, final_request, final_catalogs = run_one(
        architecture, request, catalogs, client, runtime, profile, changes=changes, context=context)
    write_json(directory / "result.json", result)
    write_json(directory / "candidates.json", {**labels, **details})
    final_context = context
    if context:
        probe = ToolRegistry(catalogs, request, Runtime("context-replay"), context=context)
        for change in (changes or [])[:max(0, result.request_version-1)]:
            apply_change(probe, change)
        final_context = probe.context
    evaluation = Evaluator().evaluate(final_catalogs, final_request, result.to_dict(), profile, expected, proof, context=final_context)
    evaluation["versions"] = evaluate_versions(catalogs, request, details, {"changes": changes or [], "expected": expected}, profile, proof, context=context)
    write_json(directory / "evaluation.json", evaluation)
    write_json(directory / "summary.json", {**labels, **result.resources})
    seal_run(directory)
    return result.to_dict()


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
    from .assumptions import load_context
    context = load_context(root, catalogs, config.get("evaluation_mode", "strict"))
    validate_case(root, case, catalogs, request, context=context)
    private = ROOT / ("evaluation/benchmark" if context else "evaluation")
    profile = read_json(private / "profiles" / f"{scenario_id}.json")
    validate_profile(profile)
    if case.get("profile_hash") != digest(profile):
        raise ValueError("Private profile differs from reviewed case")
    pricing = read_json(ROOT / config.get("pricing", "configs/pricing.json"))
    validate_pricing(config, pricing)
    run_id = run_id or f"{scenario_id}-{architecture}-{uuid.uuid4().hex[:8]}"
    directory = ROOT / config.get("runs_dir", "runs/main") / run_id
    if directory.exists():
        raise ValueError("Run directory already exists; refusing to overwrite evidence")
    limits = Limits(**config.get("limits", {}))
    if limits.cost_usd is None or limits.cost_usd <= 0:
        raise ValueError("Set a positive per-run cost_usd limit before real runs")
    runtime = Runtime(run_id, directory / "events.jsonl", limits, pricing)
    client = OpenAIClient(config["llm"], allow_api=allow_api)
    manifest = {"run_id": run_id, "mode": "real", "architecture": architecture, "scenario_id": scenario_id,
                "config": config, "code_hash": version_hash(), "snapshot_hash": digest(catalogs),
                "request_hash": digest(request), "profile_hash": digest(profile), "case_hash": digest(case), "pricing": pricing,
                "evaluation_mode": "benchmark_assumptions" if context else "strict", "assumptions_hash": context.registry_hash if context else None}
    proof = read_json(private / "feasibility" / f"{scenario_id}.json") if case["expected"] == "infeasible" else None
    return persist_run(directory, architecture, request, catalogs, client, runtime, profile, manifest,
                       changes=case.get("changes"), expected=case["expected"], proof=proof, context=context)


def evaluate_versions(catalogs, request, details, case, profile, proof, *, context=None):
    registry = ToolRegistry(catalogs, request, Runtime("version-evaluation"), context=context)
    values = []
    for version in details.get("versions", []):
        if version["version"] > 1:
            apply_change(registry, case["changes"][version["version"]-2])
        result = dict(version["result"], resources=version["resources_cumulative"])
        values.append({"version": version["version"], "evaluation": Evaluator().evaluate(registry.catalogs, registry.request, result, profile, case["expected"], proof, context=registry.context)})
    return values


def replay_saved_run(directory, catalogs, request, profile, case, proof=None, *, context=None):
    """Recompute evaluation from sealed artifacts and explicitly supplied hashed inputs."""
    from .batch import verify_run, seal_run
    directory = Path(directory)
    if not verify_run(directory):
        raise ValueError("Run artifacts are incomplete or changed")
    manifest = read_json(directory / "manifest.json")
    if manifest.get("evaluation_mode", "strict") != ("benchmark_assumptions" if context else "strict") or manifest.get("assumptions_hash") != (context.registry_hash if context else None):
        raise ValueError("Saved evaluation environment differs")
    for key, value in (("snapshot_hash", catalogs), ("request_hash", request), ("profile_hash", profile)):
        if manifest.get(key) != digest(value):
            raise ValueError("Saved input hash changed: " + key)
    if "case_hash" in manifest and manifest["case_hash"] != digest(case):
        raise ValueError("Saved case hash changed")
    registry = ToolRegistry(catalogs, request, Runtime("offline-evaluation"), context=context)
    saved_result = read_json(directory / "result.json")
    for change in case.get("changes", [])[:max(0, saved_result.get("request_version", 1)-1)]:
        apply_change(registry, change)
    value = Evaluator().evaluate(registry.catalogs, registry.request, saved_result, profile, case["expected"], proof, context=registry.context)
    value["versions"] = evaluate_versions(catalogs, request, read_json(directory / "candidates.json"), case, profile, proof, context=context)
    write_json(directory / "evaluation.json", value)
    seal_run(directory)
    return value


def reevaluate_runs(runs):
    """Recompute from hashed inputs, never from model-asserted scores."""
    completed = 0
    for path in sorted(Path(runs).glob("*/manifest.json")):
        manifest = read_json(path)
        if manifest.get("mode") != "real" or not (path.parent / "result.json").exists():
            continue
        from .batch import verify_run
        if not verify_run(path.parent):
            raise ValueError(f"Run artifacts are incomplete or changed: {manifest['run_id']}")
        config = manifest["config"]
        root = ROOT / config["data_dir"]
        catalogs = load_catalogs(root / "snapshot")
        suite = read_json(root / "suite.json")
        case = next(c for c in suite["cases"] if c["id"] == manifest["scenario_id"])
        request = load_public_scenario(root / "scenarios" / case["split"] / f"{case['id']}.json")
        from .assumptions import load_context
        context = load_context(root, catalogs, manifest.get("evaluation_mode", "strict"))
        private = ROOT / ("evaluation/benchmark" if context else "evaluation")
        profile = read_json(private / "profiles" / f"{case['id']}.json")
        proof = read_json(private / "feasibility" / f"{case['id']}.json") if case["expected"] == "infeasible" else None
        replay_saved_run(path.parent, catalogs, request, profile, case, proof, context=context)
        completed += 1
    return completed
