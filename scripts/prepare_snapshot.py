import argparse
import json
from _bootstrap import ROOT
from src.prepare import prepare_snapshot


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default=str(ROOT / "data/benchmark/manifest.json"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        value = prepare_snapshot(args.manifest, args.check)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 2 if value["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
