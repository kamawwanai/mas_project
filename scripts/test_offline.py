"""Run every local test with network disabled, including Python CLI subprocesses."""
import os
import sys
import unittest
from _bootstrap import ROOT
from src.offline import deny_network


if __name__ == "__main__":
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    deny_network()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
