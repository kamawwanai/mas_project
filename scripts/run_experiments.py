import argparse
import json
from _bootstrap import ROOT
from src.contracts import digest, read_json, write_json
from src.experiments import load_config, matrix, real_run, version_hash
from src.llm import APIUnavailable


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
        cases = [c for c in suite["cases"] if c["split"] == args.split and (not args.scenario or c["id"] == args.scenario)]
        if not cases:
            raise ValueError("No matching scenarios")
        jobs = matrix([c["id"] for c in cases], args.repeats, config.get("seed", 42))
        if args.dry_run:
            print(json.dumps({"mode": "matrix_preview", "jobs": jobs, "count": len(jobs), "pending_cases": [c["id"] for c in cases if c["status"] != "verified"]}, indent=2))
            return 0
        if not args.allow_api or not config["llm"].get("api_enabled"):
            raise APIUnavailable("Real experiments disabled; --dry-run works without API")
        ceiling = config.get("experiment_cost_usd")
        if not isinstance(ceiling, (int, float)) or ceiling <= 0:
            raise ValueError("Set a positive experiment_cost_usd before real experiments")
        root = ROOT / config["runs_dir"]
        signature = digest({"config": config, "suite": suite, "code": version_hash(), "jobs": jobs})
        batch_file = root / "batch.json"
        if batch_file.exists():
            if not args.resume or read_json(batch_file)["signature"] != signature:
                raise ValueError("Batch exists or configuration changed; use a new runs_dir")
        else:
            write_json(batch_file, {"signature": signature, "jobs": jobs})
        for job in jobs:
            key = f"{job['scenario_id']}-{job['architecture']}-r{job['repeat']}"
            target = root / key
            if target.exists():
                if args.resume:
                    if not (target / "result.json").exists():
                        raise ValueError(f"Interrupted attempt {key}: preserve and reconcile before retry")
                    continue
                raise ValueError("Refusing to overwrite run")
            summaries = [read_json(p) for p in root.glob("*/summary.json")]
            if any(s["cost_usd"] is None for s in summaries):
                raise ValueError("Unknown prior request cost; reconcile billing before continuing")
            spent = sum(s["cost_usd"] for s in summaries)
            per_run = config.get("limits", {}).get("cost_usd")
            if not per_run or spent + per_run > ceiling:
                raise ValueError("Batch ceiling reached or per-run monetary limit missing")
            real_run(config, job["architecture"], job["scenario_id"], args.split, True, key)
        print(json.dumps({"completed_jobs": len(jobs)}))
        return 0
    except (APIUnavailable, OSError, ValueError, StopIteration) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
