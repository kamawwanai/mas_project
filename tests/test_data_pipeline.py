from pathlib import Path
from hashlib import sha256
from datetime import date
import json
import subprocess
import sys
import unittest

from src.audit import readiness
from src.contracts import read_json
from src.experiments import load_config, validate_pricing
from src.prepare import prepare_snapshot
from tests.test_validation import working_directory
from tests.fixtures import write_dataset
from tests.full_fixtures import full_data

ROOT = Path(__file__).resolve().parents[1]


class DataPipelineTests(unittest.TestCase):
    def test_preparation_twice_is_byte_identical_without_source_changes(self):
        with working_directory() as temp:
            workspace = Path(temp)
            write_dataset(workspace/"data", full_data())
            specs = [{"kind": k, "path": f"data/snapshot/{k}.json", "sha256": sha256((workspace/f"data/snapshot/{k}.json").read_bytes()).hexdigest()}
                     for k in ("transport", "stays", "places", "food", "transfers")]
            from src.contracts import write_json
            manifest = workspace/"data/benchmark/manifest.json"
            write_json(manifest, {"sources": specs, "snapshot_id": "prepared_fixture", "timezone": "Europe/Moscow", "status": "pending_verification"})
            before = {s["path"]: (workspace/s["path"]).read_bytes() for s in specs}
            self.assertFalse(prepare_snapshot(manifest)["problems"])
            generated = {p.name: p.read_bytes() for p in (manifest.parent/"snapshot").glob("*.json")}
            self.assertFalse(prepare_snapshot(manifest)["problems"])
            self.assertEqual(generated, {p.name: p.read_bytes() for p in (manifest.parent/"snapshot").glob("*.json")})
            self.assertEqual(before, {s["path"]: (workspace/s["path"]).read_bytes() for s in specs})
            self.assertFalse(prepare_snapshot(manifest, check=True)["problems"])
            target = manifest.parent/"snapshot/places.json"
            target.write_bytes(target.read_bytes()+b" ")
            self.assertTrue(prepare_snapshot(manifest, check=True)["problems"])
            (workspace/specs[0]["path"]).write_bytes(b"{}")
            self.assertTrue(prepare_snapshot(manifest)["problems"])

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
        self.assertTrue(all(i["price"]["price_kind"] == "synthetic_assumption" and i["price"]["amount_kopecks"] is not None for i in transport["items"]))
        self.assertNotIn("train_return_10", {i["id"] for i in transport["items"]})

    def test_synthetic_readiness_never_claims_real_data_verification(self):
        result = readiness(ROOT / "data/benchmark")
        self.assertTrue(result["ready"], result)
        self.assertFalse(result["real_data_verified"])
        suite = read_json(ROOT / "data/benchmark/suite.json")
        self.assertEqual(4, sum(c["split"] == "dev" for c in suite["cases"]))
        self.assertEqual(12, sum(c["split"] == "test" for c in suite["cases"]))
        self.assertTrue(all(c["status"] == "synthetic_validated" for c in suite["cases"]))

    def test_cli_matrix_and_api_disabled_without_provider_import(self):
        preview = subprocess.run([sys.executable, "-B", "scripts/run_experiments.py", "--dry-run"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(0, preview.returncode, preview.stderr)
        self.assertEqual(72, json.loads(preview.stdout)["count"])
        denied = subprocess.run([sys.executable, "-B", "scripts/run_trip.py", "--architecture", "single_agent", "--scenario", "dev_01"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(2, denied.returncode)
        self.assertEqual("APIUnavailable", json.loads(denied.stdout)["error"])

        five = subprocess.run([sys.executable, "-B", "scripts/run_experiments.py", "--config",
                               "configs/five_scenario.example.toml", "--split", "test", "--repeats", "1", "--dry-run"],
                              cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(0, five.returncode, five.stderr)
        preview = json.loads(five.stdout)
        self.assertEqual(15, preview["count"])
        self.assertEqual({"test_01", "test_02", "test_04", "test_09", "test_12"},
                         {job["scenario_id"] for job in preview["jobs"]})

    def test_openrouter_configuration_pricing_and_secret_exclusions(self):
        config = load_config(ROOT/"configs/pilot.example.toml")
        llm = config["llm"]
        self.assertFalse(llm["api_enabled"])
        self.assertEqual("openrouter", llm["provider"])
        self.assertEqual("chat_completions", llm["transport"])
        self.assertEqual("openai/gpt-4.1-mini", llm["model"])
        self.assertEqual("OPENROUTER_API_KEY", llm["api_key_env"])
        self.assertEqual(0.0, llm["generation"]["temperature"])
        self.assertEqual(8000, config["limits"]["max_output_tokens"])
        self.assertEqual(.5, config["limits"]["cost_usd"])
        self.assertEqual(.6, config["experiment_cost_usd"])
        checked_on = date(2026, 10, 22)
        validate_pricing(config, read_json(ROOT/"configs/pricing.json"), today=checked_on)
        stale = {**read_json(ROOT/"configs/pricing.json"), "verified_at": "2020-01-01"}
        with self.assertRaisesRegex(ValueError, "stale"):
            validate_pricing(config, stale, today=checked_on)
        mismatched = {**read_json(ROOT/"configs/pricing.json"), "model": "another/model"}
        with self.assertRaisesRegex(ValueError, "provider/model"):
            validate_pricing(config, mismatched, today=checked_on)
        self.assertEqual(0, subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT).returncode)
        self.assertNotEqual(0, subprocess.run(["git", "check-ignore", "-q", ".env.example"], cwd=ROOT).returncode)
        self.assertEqual("", subprocess.check_output(["git", "ls-files", ".env"], cwd=ROOT, text=True).strip())
        self.assertEqual("OPENROUTER_API_KEY=", [line for line in (ROOT/".env.example").read_text().splitlines()
                                                 if line.startswith("OPENROUTER_API_KEY=")][0])
        smoke = subprocess.run([sys.executable, "-B", "scripts/smoke_openrouter.py"], cwd=ROOT,
                               capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(2, smoke.returncode)
        self.assertEqual("APIUnavailable", json.loads(smoke.stdout)["error"])


if __name__ == "__main__":
    unittest.main()
