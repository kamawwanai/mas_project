import argparse
import json
from _bootstrap import ROOT
from src.benchmark_mock import mock_cycle

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Oracle-scripted synthetic benchmark integration; no API")
    parser.add_argument("--output", default=str(ROOT/"reports/local/benchmark-mock"))
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    print(json.dumps(mock_cycle(args.output, resume=args.resume), ensure_ascii=False, indent=2))
