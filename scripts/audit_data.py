import argparse
import json
from _bootstrap import ROOT
from src.audit import audit_data, readiness


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--readiness", action="store_true")
    args = parser.parse_args()
    result = readiness(args.data_dir) if args.readiness else audit_data(args.data_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return (0 if result["ready"] else 3) if args.readiness else result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
