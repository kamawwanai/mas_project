from pathlib import Path
from hashlib import sha256
import json
import subprocess
import sys
import unittest

from src.audit import readiness
from src.contracts import read_json
from src.prepare import prepare_snapshot
from tests.test_validation import working_directory
from tests.fixtures import write_dataset
from tests.full_fixtures import full_data

ROOT = Path(__file__).resolve().parents[1]


class DataPipelineTests(unittest.TestCase):
    def test_full_validation_cli_on_separate_fixture(self):
        with working_directory() as temp:
            root = write_dataset(temp, full_data())
            result = subprocess.run([sys.executable, "-B", "scripts/check_scenario.py", "--scope", "full", "--scenario", "case", "--data-dir", str(root), "--plan", str(root/"reference_plans/case.json"), "--json"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(130000, json.loads(result.stdout)["cost"]["total_kopecks"])

    def test_normalization_is_reproducible_and_raw_sources_unchanged(self):
        manifest = read_json(ROOT / "data/benchmark/manifest.json")
        for source in manifest["sources"]:
            self.assertEqual(source["sha256"], sha256((ROOT/source["path"]).read_bytes()).hexdigest())
        report = prepare_snapshot(ROOT / "data/benchmark/manifest.json", check=True)
        self.assertEqual([], report["problems"])
        self.assertFalse(report["experiment_ready"])
        transport = read_json(ROOT / "data/benchmark/snapshot/transport.json")
        self.assertTrue(all(i["price"]["amount_kopecks"] is None for i in transport["items"]))
        self.assertNotIn("train_return_10", {i["id"] for i in transport["items"]})

    def test_unverified_dataset_is_blocked_not_success(self):
        self.assertFalse(readiness(ROOT / "data/benchmark")["ready"])
        suite = read_json(ROOT / "data/benchmark/suite.json")
        self.assertEqual(4, sum(c["split"] == "dev" for c in suite["cases"]))
        self.assertEqual(12, sum(c["split"] == "test" for c in suite["cases"]))
        self.assertTrue(all(c["status"] == "pending_verification" for c in suite["cases"]))

    def test_cli_matrix_and_api_disabled_without_provider_import(self):
        preview = subprocess.run([sys.executable, "-B", "scripts/run_experiments.py", "--dry-run"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(0, preview.returncode, preview.stderr)
        self.assertEqual(72, json.loads(preview.stdout)["count"])
        denied = subprocess.run([sys.executable, "-B", "scripts/run_trip.py", "--architecture", "single_agent", "--scenario", "dev_01"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(2, denied.returncode)
        self.assertEqual("APIUnavailable", json.loads(denied.stdout)["error"])


if __name__ == "__main__":
    unittest.main()
