import argparse
import json
import math
from _bootstrap import ROOT
from src.contracts import digest, read_json, write_json
from src.experiments import load_config, matrix, real_run, version_hash
from src.llm import APIUnavailable
from src.batch import run_batch, require_safe_continuation
from src.catalog import load_catalogs, load_public_scenario
from src.experiments import validate_case
from src.runtime import Limits
from src.evaluator import validate_profile
from src.assumptions import load_context


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(ROOT / "configs/experiment.toml"))
    parser.add_argument("--split", choices=["dev", "test"], default="test")
    parser.add_argument("--scenario")
    parser.add_argument("--repeats", type=int, choices=[1, 2], default=2)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print job matrix, no API calls or run artifacts")
    parser.add_argument("--allow-api", action="store_true")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        suite = read_json(ROOT / config["data_dir"] / "suite.json")
        selected = config.get("scenario_ids")
        if selected is not None and (not isinstance(selected, list) or not selected or
                                     len(selected) != len(set(selected)) or
                                     not all(isinstance(value, str) and value for value in selected)):
            raise ValueError("scenario_ids must be a non-empty unique string list")
        cases = [c for c in suite["cases"] if c["split"] == args.split and
                 (selected is None or c["id"] in selected) and
                 (not args.scenario or c["id"] == args.scenario)]
        if not cases:
            raise ValueError("No matching scenarios")
        if selected is not None:
            missing = sorted(set(selected) - {c["id"] for c in cases})
            if missing and not args.scenario:
                raise ValueError(f"Configured scenarios are absent from split {args.split}: {missing}")
        jobs = matrix([c["id"] for c in cases], args.repeats, config.get("seed", 42))
        if args.dry_run:
            expected_status = "synthetic_validated" if config.get("evaluation_mode") == "benchmark_assumptions" else "verified"
            print(json.dumps({"mode": "matrix_preview", "evaluation_mode": config.get("evaluation_mode", "strict"), "jobs": jobs, "count": len(jobs), "pending_cases": [c["id"] for c in cases if c["status"] != expected_status]}, indent=2))
            return 0
        if not args.allow_api or not config["llm"].get("api_enabled"):
            raise APIUnavailable("Real experiments disabled; --dry-run works without API")
        ceiling = config.get("experiment_cost_usd")
        if type(ceiling) not in (int, float) or not math.isfinite(ceiling) or ceiling <= 0:
            raise ValueError("Set a positive experiment_cost_usd before real experiments")
        root = ROOT / config["runs_dir"]
        # Preflight every selected case before a paid request or batch artifact.
        data_root = ROOT / config["data_dir"]
        catalogs = load_catalogs(data_root / "snapshot")
        context = load_context(data_root, catalogs, config.get("evaluation_mode", "strict"))
        inputs = {}
        for case in cases:
            request = load_public_scenario(data_root / "scenarios" / case["split"] / f"{case['id']}.json")
            validate_case(data_root, case, catalogs, request, context=context)
            profile = read_json(ROOT / ("evaluation/benchmark/profiles" if context else "evaluation/profiles") / f"{case['id']}.json")
            validate_profile(profile)
            if digest(profile) != case.get("profile_hash"):
                raise ValueError("Private profile hash changed")
            inputs[case["id"]] = {"request": digest(request), "profile": digest(profile)}
        limits = Limits(**config.get("limits", {}))
        if limits.cost_usd is None:
            raise ValueError("Set a per-run monetary limit")
        signature = digest({"config": config, "suite": suite, "code": version_hash(), "jobs": jobs,
                            "catalogs": digest(catalogs), "inputs": inputs})

        def execute(job, key):
            summaries = [read_json(p) for p in root.glob("*/summary.json")]
            if any(s["cost_usd"] is None for s in summaries):
                raise ValueError("Unknown prior request cost; reconcile billing before continuing")
            spent = sum(s["cost_usd"] for s in summaries)
            per_run = config.get("limits", {}).get("cost_usd")
            if not per_run or spent + per_run > ceiling:
                raise ValueError("Batch ceiling reached or per-run monetary limit missing")
            result = real_run(config, job["architecture"], job["scenario_id"], args.split, True, key)
            require_safe_continuation(result)
        completed = run_batch(root, jobs, signature, execute, resume=args.resume, mode="real")
        print(json.dumps({"completed_jobs": len(completed)}))
        return 0
    except (APIUnavailable, OSError, ValueError, StopIteration) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
