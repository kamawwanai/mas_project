import argparse
from _bootstrap import ROOT
from src.reporting import build_report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="reports/main")
    parser.add_argument("--output", default="reports/main")
    args = parser.parse_args()
    print(f"Compared {len(build_report(args.input, args.output))} architectures")


if __name__ == "__main__":
    main()
