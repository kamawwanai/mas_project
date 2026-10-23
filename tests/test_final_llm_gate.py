"""Final launch regressions. All provider replies are in-memory fixtures."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.contracts import digest, read_json
from src.experiments import run_one, persist_run, version_hash
from src.llm import APIUnavailable, OpenAIClient, ScriptedClient
from src.runtime import LimitExceeded, Limits, Runtime
from src.tools import ToolRegistry
from tests.full_fixtures import parsed
from tests.mock_clients import conversation, summary
from tests.test_architectures import final_reply, tool_reply
from tests.test_llm_adapter import RateLimit, TimeoutFailure, ConnectionFailure
from tests.test_validation import working_directory

RATES = {"input_per_million": 1, "cached_input_per_million": .5, "output_per_million": 2}
ROOT = Path(__file__).resolve().parents[1]


class ProviderSafetyTests(unittest.TestCase):
    def invoke(self, create, runtime, config=None):
        sdk = SimpleNamespace(OpenAI=Mock(return_value=SimpleNamespace(responses=SimpleNamespace(create=create))),
                              RateLimitError=RateLimit, APIConnectionError=ConnectionFailure, APITimeoutError=TimeoutFailure)
        with patch.dict("sys.modules", {"openai": sdk}), patch.dict("os.environ", {"OPENAI_API_KEY": "fixture-only"}), patch("src.llm.time.sleep"):
            client = OpenAIClient(config or {"api_enabled": True, "model": "fixture-model"}, allow_api=True)
            return client.generate([], [], runtime, "fixture")

    @staticmethod
    def response(**changes):
        raw = {"model": "fixture-model", "output": [], "status": "completed",
               "usage": {"input_tokens": 20, "output_tokens": 5}}
        raw.update(changes)
        return SimpleNamespace(id="fixture-response", output_text="{}", model_dump=lambda **kw: raw)

    def test_decoding_failure_is_unknown_billable_usage_and_never_retried(self):
        for money in (None, 1):
            response = SimpleNamespace(model_dump=Mock(side_effect=ValueError("fixture decoding failure")))
            create = Mock(return_value=response)
            runtime = Runtime("fixture", limits=Limits(cost_usd=money), pricing=RATES)
            with self.assertRaisesRegex(LimitExceeded, "unknown_billable_usage"):
                self.invoke(create, runtime)
            self.assertEqual(1, create.call_count)
            self.assertIsNone(runtime.summary()["cost_usd"])
            with self.assertRaises(LimitExceeded):
                runtime.reserve_llm(0)

    def test_missing_usage_and_timeout_stop_even_without_money_limit(self):
        for response in (self.response(usage=None), TimeoutFailure(), ConnectionFailure()):
            create = Mock(side_effect=response) if isinstance(response, Exception) else Mock(return_value=response)
            runtime = Runtime("fixture", pricing=RATES)
            with self.assertRaisesRegex(LimitExceeded, "unknown_billable_usage"):
                self.invoke(create, runtime)
            self.assertEqual(1, create.call_count)
            self.assertFalse(runtime.summary()["token_usage_complete"])

    def test_known_usage_survives_missing_protocol_field(self):
        response = self.response()
        del response.id
        runtime = Runtime("fixture", limits=Limits(cost_usd=1), pricing=RATES)
        with self.assertRaises(AttributeError):
            self.invoke(Mock(return_value=response), runtime)
        self.assertEqual(20, runtime.counts["input_tokens"])
        self.assertEqual(5, runtime.counts["output_tokens"])
        self.assertAlmostEqual(.00003, runtime.summary()["cost_usd"])

    def test_incomplete_and_failed_responses_are_accounted_but_not_returned(self):
        for status in ("incomplete", "failed", "cancelled", None):
            runtime = Runtime("fixture", limits=Limits(cost_usd=1), pricing=RATES)
            create = Mock(return_value=self.response(status=status))
            with self.assertRaisesRegex(APIUnavailable, "did not complete"):
                self.invoke(create, runtime)
            self.assertEqual(1, create.call_count)
            self.assertAlmostEqual(.00003, runtime.summary()["cost_usd"])

    def test_explicit_rate_rejections_have_bounded_retries(self):
        runtime = Runtime("fixture", limits=Limits(cost_usd=1), pricing=RATES)
        create = Mock(side_effect=RateLimit())
        with self.assertRaises(RateLimit):
            self.invoke(create, runtime)
        self.assertEqual(3, create.call_count)
        self.assertEqual(3, runtime.counts["llm_attempts"])
        self.assertEqual(1, runtime.counts["llm_calls"])
        self.assertEqual(0, runtime.summary()["cost_usd"])

    def test_retry_cannot_bypass_attempt_limit(self):
        runtime = Runtime("fixture", limits=Limits(cost_usd=1, llm_attempts=1), pricing=RATES)
        create = Mock(side_effect=RateLimit())
        with self.assertRaisesRegex(LimitExceeded, "llm_attempts"):
            self.invoke(create, runtime)
        self.assertEqual(1, create.call_count)

    def test_cash_reservation_covers_either_input_rate(self):
        runtime = Runtime("fixture", limits=Limits(cost_usd=.01),
                          pricing={**RATES, "cached_input_per_million": 100})
        with self.assertRaisesRegex(LimitExceeded, "cost_reservation"):
            runtime.reserve_llm(1000)
        self.assertEqual(0, runtime.counts["llm_attempts"])

    def test_invalid_timeouts_are_rejected_before_sdk_construction(self):
        for timeout in (0, -1, float("nan"), float("inf"), True, "60"):
            sdk = SimpleNamespace(OpenAI=Mock())
            with patch.dict("sys.modules", {"openai": sdk}), patch.dict("os.environ", {"OPENAI_API_KEY": "fixture-only"}):
                with self.assertRaises(APIUnavailable):
                    OpenAIClient({"api_enabled": True, "model": "fixture-model", "timeout_seconds": timeout}, allow_api=True)
            sdk.OpenAI.assert_not_called()

    def test_call_timeout_does_not_exceed_remaining_wall_budget(self):
        runtime = Runtime("fixture", limits=Limits(seconds=.05), pricing=RATES)
        create = Mock(return_value=self.response())
        self.invoke(create, runtime)
        self.assertGreater(create.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(create.call_args.kwargs["timeout"], .05)


class ComparisonSafetyTests(unittest.TestCase):
    def test_real_series_stops_on_error_or_unknown_cost(self):
        from src.batch import require_safe_continuation
        for result in ({"status": "error", "resources": {"cost_usd": 0}},
                       {"status": "budget_exhausted", "resources": {"cost_usd": None}}):
            with self.assertRaises(ValueError):
                require_safe_continuation(result)
        require_safe_continuation({"status": "completed", "resources": {"cost_usd": .01}})
        require_safe_continuation({"status": "budget_exhausted", "resources": {"cost_usd": .01}})

    def test_common_snapshot_tools_limits_and_no_private_file_loading(self):
        catalogs, request, plan = parsed()
        original_hash = digest(catalogs)
        definitions = {d["name"]: d for d in ToolRegistry(catalogs, request, Runtime("fixture")).definitions}
        for architecture in ("workflow", "single_agent", "mas"):
            client = ScriptedClient(conversation(architecture, plan))
            runtime = Runtime("fixture", pricing=RATES)
            profile = {"weights": {"culture": 1}, "answers": {"pace": "UNASKED_SECRET"}, "private_marker": "PRIVATE"}
            with patch("src.contracts.read_json", side_effect=AssertionError("Private file access")), patch("src.catalog.load_plan", side_effect=AssertionError("Witness access")):
                result, _, _, final_catalogs = run_one(architecture, request, catalogs, client, runtime, profile)
            self.assertEqual("completed", result.status)
            self.assertEqual(original_hash, result.snapshot_hash)
            self.assertEqual(original_hash, digest(final_catalogs))
            self.assertEqual(Limits(), runtime.limits)
            context = json.dumps(client.requests)
            for secret in ("UNASKED_SECRET", '"weights"', '"private_marker"', "evaluation/", "read_file"):
                self.assertNotIn(secret, context)
            for call in client.requests:
                for tool in call["tools"]:
                    if tool["name"] == "delegate":
                        self.assertEqual("mas", architecture)
                    else:
                        self.assertEqual(definitions[tool["name"]], tool)

    def test_mas_accounts_for_coordinator_and_all_specialists(self):
        catalogs, request, plan = parsed()
        replies = conversation("mas", plan)
        for reply in replies:
            reply.usage = {"input_tokens": 11, "output_tokens": 7}
        runtime = Runtime("fixture", limits=Limits(cost_usd=1), pricing=RATES)
        result, _, _, _ = run_one("mas", request, catalogs, ScriptedClient(replies), runtime, {"weights": {"culture": 1}})
        self.assertEqual("completed", result.status)
        actors = {e["actor"] for e in runtime.events if e["kind"] == "llm_response"}
        self.assertEqual({"coordinator", "transport", "stay", "places", "logistics", "verification"}, actors)
        self.assertEqual(11*len(replies), result.resources["input_tokens"])
        self.assertEqual(7*len(replies), result.resources["output_tokens"])
        self.assertAlmostEqual(25*len(replies)/1e6, result.resources["cost_usd"])
        self.assertEqual(len(replies), result.resources["llm_attempts"])

    def test_verification_receipts_bind_plan_request_and_catalogs(self):
        catalogs, request, plan = parsed()
        registry = ToolRegistry(catalogs, request, Runtime("fixture"))
        registry.execute("validate_plan", {"plan_json": json.dumps(plan)}, "verification")
        self.assertIn(registry.verification_key(plan), registry.verification_receipts)
        changed = deepcopy(plan)
        changed["id"] += "-changed"
        self.assertNotIn(registry.verification_key(changed), registry.verification_receipts)
        registry.request = replace(request, budget_kopecks=request.budget_kopecks+1)
        self.assertNotIn(registry.verification_key(plan), registry.verification_receipts)
        registry.request = request
        hotel = catalogs["stays"]
        registry.catalogs = {**catalogs, "stays": replace(hotel, items=[replace(hotel.items[0], availability_status="unavailable")])}
        self.assertNotIn(registry.verification_key(plan), registry.verification_receipts)

    def test_late_mas_delegation_retains_public_request(self):
        catalogs, request, plan = parsed()
        replies = [final_reply(plan)] + conversation("mas", plan)
        client = ScriptedClient(replies)
        result, _, _, _ = run_one("mas", request, catalogs, client, Runtime("fixture"), {"weights": {"culture": 1}})
        self.assertEqual("completed", result.status)
        specialist_tasks = [json.loads(c["messages"][1]["content"]) for c in client.requests
                            if '"subtask"' in c["messages"][1]["content"]]
        self.assertTrue(specialist_tasks)
        for task in specialist_tasks:
            self.assertEqual(request.id, task["request"]["id"])

    def test_real_client_on_synthetic_data_is_explicitly_labelled(self):
        from src.assumptions import load_context
        from src.catalog import load_catalogs, load_public_scenario
        catalogs = load_catalogs(ROOT/"data/benchmark/snapshot")
        request = load_public_scenario(ROOT/"data/benchmark/scenarios/dev/dev_01.json")
        context = load_context(ROOT/"data/benchmark", catalogs, "benchmark_assumptions")
        plan = read_json(ROOT/"evaluation/benchmark/reference_plans/dev_01.json")
        # A memory-only fake tagged real tests artifact labelling, not provider performance.
        client = ScriptedClient(conversation("single_agent", plan))
        client.mode = "real"
        with working_directory() as temp:
            path = Path(temp)/"fixture"
            persist_run(path, "single_agent", request, catalogs, client, Runtime("fixture", pricing=RATES),
                        {"weights": {"culture": 1}}, {"mode": "real", "evaluation_mode": "benchmark_assumptions", "assumptions_hash": context.registry_hash}, context=context)
            for name in ("manifest.json", "summary.json", "candidates.json"):
                saved = read_json(path/name)
                self.assertEqual("real", saved["mode"])
                self.assertFalse(saved["mock_client"])
                self.assertTrue(saved["synthetic"])
                self.assertEqual("benchmark_assumptions", saved["evaluation_mode"])

    def test_protocol_scripts_are_bound_into_code_hash(self):
        before = version_hash()
        original = Path.read_bytes
        def altered(path):
            value = original(path)
            return value+b"\n# fixture protocol change" if path == ROOT/"scripts/run_experiments.py" else value
        with patch.object(Path, "read_bytes", altered):
            self.assertNotEqual(before, version_hash())


if __name__ == "__main__":
    unittest.main()
