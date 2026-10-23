"""Offline deterministic synthetic benchmark preparation; no LLM execution."""
import json
from _bootstrap import ROOT
from src.offline import deny_network
from src.synthetic_benchmark import build

if __name__ == "__main__":
    deny_network()
    result = build(ROOT/"data/benchmark")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ready"] else 1)
