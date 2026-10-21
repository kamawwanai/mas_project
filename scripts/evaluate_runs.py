import argparse
from _bootstrap import ROOT
from src.reporting import export_runs
from src.experiments import reevaluate_runs


def main():
    parser = argparse.ArgumentParser(description="Export saved independent evaluations, without LLM requests")
    parser.add_argument("--runs", default="runs/main")
    parser.add_argument("--output", default="reports/main")
    parser.add_argument("--include-test", action="store_true", help="Explicitly include labeled structural test artifacts")
    args = parser.parse_args()
    reevaluate_runs(args.runs)
    print(f"Exported {len(export_runs(args.runs, args.output, args.include_test))} runs")


if __name__ == "__main__":
    main()
