import argparse
import json
from _bootstrap import ROOT
from src.experiments import load_config, real_run
from src.llm import APIUnavailable


def main():
    parser = argparse.ArgumentParser(description="Run a real planner only after explicit API authorization and data verification")
    parser.add_argument("--architecture", choices=["workflow", "single_agent", "mas"], required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    parser.add_argument("--config", default=str(ROOT / "configs/experiment.toml"))
    parser.add_argument("--allow-api", action="store_true")
    args = parser.parse_args()
    try:
        result = real_run(load_config(args.config), args.architecture, args.scenario, args.split, args.allow_api)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "completed" else 1
    except (APIUnavailable, ValueError, OSError, StopIteration) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
