import json
from copy import deepcopy
from pathlib import Path
import unittest

from src.contracts import RunResult, write_json, read_json
from src.evaluator import Evaluator, verify_conflict
from src.experiments import matrix
from src.reporting import export_runs, build_report
from src.selection import select, features
from tests.full_fixtures import parsed, full_data
from tests.test_validation import working_directory


class EvaluationTests(unittest.TestCase):
    def result(self, plans):
        return RunResult("test", "single_agent", "case", "testhash", plans=plans).to_dict()

    def test_success_at_k_uses_one_run_rank_and_no_duplicate_gain(self):
        catalogs, request, plan = parsed()
        invalid = json.loads(json.dumps(plan))
        invalid["items"][0]["quantity"] = 1
        value = Evaluator().evaluate(catalogs, request, self.result([invalid, plan, plan]), {"weights": {"culture": 1}})
        self.assertFalse(value["metrics"]["success_at_1"])
        self.assertTrue(value["metrics"]["success_at_5"])
        self.assertFalse(value["plans"][2]["distinct"])
        self.assertAlmostEqual(2/3, value["metrics"]["valid_fraction"])

    def test_hidden_profile_changes_utility_not_feasibility(self):
        catalogs, request, plan = parsed()
        evaluator = Evaluator()
        cultural = evaluator.evaluate(catalogs, request, self.result([plan]), {"weights": {"culture": 1}})
        economy = evaluator.evaluate(catalogs, request, self.result([plan]), {"weights": {"economy": 1}})
        self.assertEqual(cultural["metrics"]["feasible_at_1"], economy["metrics"]["feasible_at_1"])
        self.assertTrue(cultural["metrics"]["success_at_1"])
        self.assertFalse(economy["metrics"]["success_at_1"])

    def test_duplicate_filter_and_hard_constraint_filter(self):
        catalogs, request, plan = parsed()
        invalid = json.loads(json.dumps(plan))
        invalid["items"][0]["quantity"] = 1
        selected, rejected = select(catalogs, request, [invalid, plan, plan])
        self.assertEqual(1, len(selected))
        self.assertEqual(1, len(rejected))

    def test_pareto_removes_more_expensive_equivalent_hotel(self):
        data = full_data()
        stay = deepcopy(data["stays"]["items"][0])
        stay["id"] = "stay_2"
        stay["price"]["amount_kopecks"] = 20000
        data["stays"]["items"].append(stay)
        transfers = []
        for transfer in data["transfers"]["items"]:
            other = deepcopy(transfer)
            other["id"] += "_hotel2"
            for field in ("from_id", "to_id"):
                if other[field] == "stay_1":
                    other[field] = "stay_2"
            transfers.append(other)
        data["transfers"]["items"].extend(transfers)
        catalogs, request, cheap = parsed(data)
        expensive = deepcopy(cheap)
        expensive["id"] = "expensive"
        expensive["selected_stay"]["reference_id"] = "stay_2"
        for event in expensive["items"]:
            if event["reference_id"] == "stay_1":
                event["reference_id"] = "stay_2"
            if event["type"] == "transfer":
                event["reference_id"] += "_hotel2"
        self.assertTrue(features(catalogs, request, expensive)["valid"])
        selected, _ = select(catalogs, request, [expensive, cheap])
        self.assertEqual([cheap], selected)

    def test_conflict_requires_checkable_certificate(self):
        from dataclasses import replace
        catalogs, request, _ = parsed()
        request = replace(request, budget_kopecks=1)
        self.assertTrue(verify_conflict(catalogs, request, {"type": "budget_lower_bound"}))
        result = self.result([])
        result["status"] = "no_feasible_plan"
        evaluation = Evaluator().evaluate(catalogs, request, result, {"weights": {"culture": 1}}, "infeasible", {"type": "budget_lower_bound"})
        self.assertIsNone(evaluation["metrics"]["success_at_1"])
        self.assertTrue(evaluation["metrics"]["conflict_correct"])
        self.assertFalse(verify_conflict(catalogs, request, {"type": "agent_failed"}))

    def test_matrix_is_complete_reproducible_and_balanced(self):
        cases = [f"test_{i:02d}" for i in range(1, 13)]
        jobs = matrix(cases)
        self.assertEqual(72, len(jobs))
        self.assertEqual(jobs, matrix(cases))
        self.assertEqual(72, len({(j["scenario_id"], j["architecture"], j["repeat"]) for j in jobs}))

    def test_reports_exclude_scripted_results_and_no_fake_charts(self):
        with working_directory() as temp:
            root = Path(temp)
            value = {"run_id": "t", "scenario_id": "case", "architecture": "mas", "mode": "test", "metrics": {"success_at_1": True}}
            write_json(root / "runs/t/evaluation.json", value)
            rows = export_runs(root / "runs", root / "report")
            self.assertEqual([], rows)
            self.assertEqual([], build_report(root / "report", root / "report"))
            self.assertFalse(list((root / "report").glob("*.png")))
            self.assertEqual(1, len(export_runs(root / "runs", root / "test-report", include_test=True)))


if __name__ == "__main__":
    unittest.main()
