"""Saved synthetic integration evidence; no provider, no benchmark replacement."""
import argparse
import json
from pathlib import Path
from _bootstrap import ROOT
from src.local_cycle import local_cycle

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT/"reports/local/synthetic-cycle")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    print(json.dumps(local_cycle(args.output, resume=args.resume), indent=2))
