from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import json
import unittest

from src.assumptions import load_context, AssumptionContext, condition_digest
from src.catalog import load_catalogs, load_public_scenario, load_plan
from src.checks import validate_trip
from src.contracts import read_json, digest, jsonable
from src.evaluator import verify_conflict
from src.selection import features, select, distinct
from src.synthetic_benchmark import compile_snapshot, encoded
from src.tools import ToolRegistry
from src.runtime import Runtime
from src.experiments import apply_change, run_one
from src.llm import ScriptedClient
from src.benchmark_mock import scripted

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT/"data/benchmark"
PRIVATE = ROOT/"evaluation/benchmark"


class AssumptionModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalogs = load_catalogs(BENCH/"snapshot")
        cls.context = load_context(BENCH, cls.catalogs, "benchmark_assumptions")
        cls.request = load_public_scenario(BENCH/"scenarios/dev/dev_01.json")
        cls.plan = read_json(PRIVATE/"reference_plans/dev_01.json")

    def test_originals_are_byte_preserved_and_45_event_copy_passes_only_explicit_mode(self):
        for path, expected in read_json(BENCH/"manifest.json")["protected_sources"].items():
            self.assertEqual(expected, sha256((ROOT/path).read_bytes()).hexdigest())
        self.assertEqual(45, len(self.plan["items"]))
        report = validate_trip(self.catalogs, self.request, self.plan, context=self.context)
        self.assertEqual(0, report.exit_code, report.to_dict())
        self.assertEqual(2534500, report.cost["total_kopecks"])
        self.assertEqual("benchmark_assumptions", report.to_dict()["evaluation_mode"])
        self.assertTrue(all(not row["full_price_confirmed"] for row in report.cost["lines"]))
        strict = validate_trip(self.catalogs, self.request, self.plan)
        self.assertEqual(3, strict.exit_code)
        self.assertIsNone(strict.cost["total_kopecks"])
        original = validate_trip(load_catalogs(ROOT/"data/snapshot"),
            load_public_scenario(ROOT/"data/scenarios/dev/dev_01.json"), load_plan(ROOT/"data/reference_plans/dev_01.json"))
        self.assertEqual(3, original.exit_code)

    def test_full_registry_has_no_fake_evidence_and_exact_assigned_values(self):
        registry = read_json(BENCH/"assumptions.json")
        self.assertTrue(registry["closed_world"])
        for doc in self.catalogs.values():
            for item in doc.items:
                self.assertEqual(condition_digest(item), digest(item))
                self.assertIsNone(item.source.source_url)
                self.assertIsNone(item.source.checked_at)
                self.assertEqual([], item.source.verified_fields)
                self.assertEqual("synthetic_assumption", item.price.price_kind)
                self.assertTrue(item.price.includes_mandatory_fees)
        altered = deepcopy(registry)
        altered["assumptions"][0]["values"]["id"] = "forged"
        with self.assertRaises(ValueError):
            AssumptionContext.load(self.catalogs, altered)

    def test_catalog_mutations_and_wrong_snapshot_are_rejected(self):
        altered = dict(self.catalogs)
        doc = altered["stays"]
        changed = replace(doc.items[0], price=replace(doc.items[0].price, amount_kopecks=1))
        altered["stays"] = replace(doc, items=[changed, *doc.items[1:]])
        with self.assertRaises(ValueError):
            validate_trip(altered, self.request, self.plan, context=self.context)
        with self.assertRaises(ValueError):
            validate_trip(self.catalogs, replace(self.request, snapshot_id="other"), self.plan, context=self.context)

    def test_hard_constraints_are_not_relaxed(self):
        for mutation, expected in (("quantity", "WRONG_QUANTITY"), ("overlap", "EVENT_OVERLAP")):
            plan = deepcopy(self.plan)
            if mutation == "quantity":
                next(e for e in plan["items"] if e["type"] == "transport")["quantity"] = 1
            else:
                plan["items"][1]["start_at"] = plan["items"][0]["start_at"]
            report = validate_trip(self.catalogs, self.request, plan, context=self.context)
            self.assertEqual(1, report.exit_code)
            codes = {p["code"] for p in report.to_dict()["violations"]}
            self.assertTrue(any(expected in c for c in codes), codes)
        report = validate_trip(self.catalogs, replace(self.request, budget_kopecks=1000000), self.plan, context=self.context)
        self.assertIn("BUDGET_EXCEEDED", {p["code"] for p in report.to_dict()["violations"]})

    def test_both_certificates_are_valid_only_with_explicit_conditions(self):
        for key in ("test_09", "test_10"):
            request = load_public_scenario(BENCH/"scenarios/test"/f"{key}.json")
            proof = read_json(PRIVATE/"feasibility"/f"{key}.json")
            self.assertTrue(verify_conflict(self.catalogs, request, proof, context=self.context))
            self.assertFalse(verify_conflict(self.catalogs, request, proof))
            self.assertFalse(verify_conflict(self.catalogs, request, dict(proof, public_hash="forged"), context=self.context))
        request = load_public_scenario(BENCH/"scenarios/test/test_09.json")
        self.assertFalse(verify_conflict(self.catalogs, replace(request, budget_kopecks=3000000), {"type": "budget_lower_bound"}, context=self.context))

    def test_hours_checkin_walking_and_departure_windows_stay_mandatory(self):
        walking = replace(self.request, constraints=replace(self.request.constraints, max_walking_minutes_per_day=0))
        report = validate_trip(self.catalogs, walking, self.plan, context=self.context)
        self.assertIn("WALKING_LIMIT", {p["code"] for p in report.to_dict()["violations"]})
        for kind, ref, start, end, code in (
            ("check_in", None, "2026-10-17T13:00:00+03:00", "2026-10-17T13:15:00+03:00", "CHECK_IN_TIME"),
            ("visit", "place_01", "2026-10-17T19:00:00+03:00", "2026-10-17T20:00:00+03:00", "OUTSIDE_OPENING_HOURS"),
            ("transfer", "transfer_14", "2026-10-18T00:20:00+03:00", "2026-10-18T00:45:00+03:00", "TRANSFER_DEPARTURE_WINDOW")):
            plan = deepcopy(self.plan)
            event = next(e for e in plan["items"] if e["type"] == kind and (ref is None or e["reference_id"] == ref))
            event.update(start_at=start, end_at=end)
            report = validate_trip(self.catalogs, self.request, plan, context=self.context)
            self.assertIn(code, {p["code"] for p in report.to_dict()["violations"]})

    def test_synthetic_booking_must_match_saved_slot_and_group(self):
        request = load_public_scenario(BENCH/"scenarios/dev/dev_03.json")
        plan = read_json(PRIVATE/"reference_plans/dev_03.json")
        self.assertEqual(0, validate_trip(self.catalogs, request, plan, context=self.context).exit_code)
        event = next(e for e in plan["items"] if e["reference_id"] == "place_06" and e["type"] == "visit")
        event["booking_id"] = "invented"
        report = validate_trip(self.catalogs, request, plan, context=self.context)
        self.assertIn("INVALID_BOOKING_REFERENCE", {p["code"] for p in report.to_dict()["violations"]})

    def test_changes_reject_initial_result_and_accept_replanned_result(self):
        suite = read_json(BENCH/"suite.json")
        for key in ("dev_04", "test_11", "test_12"):
            case = next(c for c in suite["cases"] if c["id"] == key)
            request = load_public_scenario(BENCH/"scenarios"/case["split"]/f"{key}.json")
            probe = ToolRegistry(self.catalogs, request, Runtime("change"), context=self.context)
            initial = read_json(PRIVATE/"reference_plans"/f"{key}.json")
            for change in case["changes"]:
                apply_change(probe, change)
            self.assertEqual(1, validate_trip(probe.catalogs, probe.request, initial, context=probe.context).exit_code)
            final = read_json(PRIVATE/"reference_plans"/f"{key}.final.json")
            self.assertEqual(0, validate_trip(probe.catalogs, probe.request, final, context=probe.context).exit_code)

    def test_future_interventions_are_not_in_initial_agent_task(self):
        from src.architectures.common import public_task
        case = next(c for c in read_json(BENCH/"suite.json")["cases"] if c["id"] == "test_12")
        request = load_public_scenario(BENCH/"scenarios/test/test_12.json")
        registry = ToolRegistry(self.catalogs, request, Runtime("public-change"), context=self.context)
        self.assertIsNone(public_task(registry)["condition_change"])
        self.assertNotIn("stay_01", request.notes)
        apply_change(registry, case["changes"][0])
        self.assertEqual(case["changes"][0], public_task(registry)["condition_change"])

    def test_all_architectures_receive_only_public_environment_and_answer(self):
        profile = read_json(PRIVATE/"profiles/dev_01.json")
        for architecture in ("workflow", "single_agent", "mas"):
            client = ScriptedClient(scripted(architecture, self.plan, "comfort"))
            result, _, _, _ = run_one(architecture, self.request, self.catalogs, client, Runtime("assumed-architecture"), profile, context=self.context)
            self.assertEqual("completed", result.status)
            messages = json.dumps(client.requests)
            self.assertNotIn(profile["private_marker"], messages)
            self.assertNotIn('"weights"', messages)
            self.assertNotIn(".success.json", messages)
            self.assertIn("benchmark_assumptions", messages)

    def test_preparation_is_deterministic_and_cannot_modify_original_sources(self):
        before = {p: (ROOT/p).read_bytes() for p in read_json(BENCH/"manifest.json")["protected_sources"]}
        docs1, registry1 = compile_snapshot(BENCH/"manifest.json")
        docs2, registry2 = compile_snapshot(BENCH/"manifest.json")
        self.assertEqual(encoded(registry1), encoded(registry2))
        for kind in docs1:
            self.assertEqual(encoded(docs1[kind]), encoded(docs2[kind]))
            self.assertEqual(encoded(docs1[kind]), (BENCH/"snapshot"/f"{kind}.json").read_bytes())
        self.assertEqual(before, {p: (ROOT/p).read_bytes() for p in before})

    def test_quality_has_distinct_solutions_and_real_tradeoffs(self):
        report = read_json(BENCH/"quality.json")
        self.assertEqual(16, report["scenario_count"])
        for row in report["cases"]:
            if row["expected"] == "feasible":
                self.assertGreater(row["distinct_alternatives"], 1)
                self.assertGreaterEqual(row["best_utility"], .7)
        rows = self.catalogs["stays"].items
        cheap = min(rows, key=lambda h: h.price.amount_kopecks)
        central = max(rows, key=lambda h: h.source.features["location"])
        self.assertLess(cheap.price.amount_kopecks, central.price.amount_kopecks)
        self.assertGreater(cheap.source.features["quiet"], central.source.features["quiet"])

    def test_cli_resolves_experimental_reference_without_touching_original(self):
        import subprocess
        import sys
        result = subprocess.run([sys.executable, "-B", "scripts/check_scenario.py", "--scenario", "dev_01", "--scope", "full",
            "--data-dir", "data/benchmark", "--mode", "benchmark_assumptions", "--json"], cwd=ROOT, capture_output=True, encoding="utf-8")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("benchmark_assumptions", json.loads(result.stdout)["evaluation_mode"])

    def test_chart_separates_model_client_mode_from_synthetic_environment(self):
        from src.charts import bar_chart
        model = bar_chart([("workflow", .5)], "metric", "real", "benchmark_assumptions")
        self.assertIn("REAL MODEL / SYNTHETIC CONDITIONS", model)
        self.assertNotIn("MOCK", model)
        mock = bar_chart([("workflow", .5)], "metric", "test", "benchmark_assumptions")
        self.assertIn("MOCK / SYNTHETIC", mock)


if __name__ == "__main__":
    unittest.main()
