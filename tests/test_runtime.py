import json
import unittest
from unittest.mock import patch

from src.llm import OpenAIClient, APIUnavailable
from src.runtime import Runtime, Limits, LimitExceeded
from src.simulator import UserSimulator
from src.tools import ToolRegistry
from tests.full_fixtures import parsed


class RuntimeTests(unittest.TestCase):
    def registry(self, limits=None):
        catalogs, request, _ = parsed()
        runtime = Runtime("test", limits=limits)
        return ToolRegistry(catalogs, request, runtime, UserSimulator({"answers": {"pace": "relaxed"}, "secret": "private"}))

    def test_real_api_fail_closed_even_with_key(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "never-send-this"}):
            with self.assertRaises(APIUnavailable):
                OpenAIClient({"api_enabled": False, "model": "x"}, allow_api=True)
            with self.assertRaises(APIUnavailable):
                OpenAIClient({"api_enabled": True, "model": "x"})

    def test_three_questions_global(self):
        tools = self.registry()
        for actor in ("transport", "stay", "coordinator"):
            answer = tools.execute("ask_user", {"topic": "pace", "text": "Choose pace", "options": ["relaxed", "busy"]}, actor)
            self.assertEqual("relaxed", answer["answer"])
            self.assertNotIn("secret", json.dumps(answer))
        with self.assertRaises(LimitExceeded):
            tools.execute("ask_user", {"topic": "pace", "text": "Again", "options": []})
        self.assertEqual(3, tools.runtime.counts["questions"])

    def test_search_direction_pagination_and_argument_validation(self):
        tools = self.registry()
        result = tools.execute("search_transport", {"filters_json": '{"direction":"return"}', "offset": 0, "limit": 1})
        self.assertEqual(["train_back"], [i["id"] for i in result["items"]])
        self.assertIn("error", tools.execute("search_transport", {"filters_json": "{}", "offset": -1, "limit": 1}))

    def test_search_contract_exposes_exact_dev_filters(self):
        tools = self.registry()
        definitions = {item["name"]: item for item in tools.definitions}
        self.assertIn("direction ('outbound' or 'return')", definitions["search_transport"]["description"])
        self.assertIn("request.destination_city", definitions["search_stays"]["description"])
        self.assertIn("Food is in search_food", definitions["search_places"]["description"])
        self.assertIn("Do not use category", definitions["search_food"]["description"])
        city_filter = json.dumps({"city": tools.request.destination_city}, ensure_ascii=False)
        for name in ("search_stays", "search_places", "search_food"):
            result = tools.execute(name, {"filters_json": city_filter, "offset": 0, "limit": 20})
            self.assertGreater(result["total"], 0, name)
        outbound = tools.execute("search_transport", {
            "filters_json": '{"direction":"outbound"}', "offset": 0, "limit": 20})
        self.assertGreater(outbound["total"], 0)
        error = tools.execute("search_stays", {"filters_json": '{"available_from":"2026-10-17"}', "offset": 0, "limit": 5})
        self.assertIn("allowed fields", error["message"])
        self.assertIn("error", tools.execute("get_item", {"id": "missing"}))
        self.assertIn("error", tools.execute("read_file", {"path": "evaluation/profiles"}))

    def test_token_reservation_and_cached_cost(self):
        runtime = Runtime("test", pricing={"input_per_million": 2, "cached_input_per_million": 1, "output_per_million": 4})
        runtime.record_usage({"input_tokens": 100, "output_tokens": 20, "input_tokens_details": {"cached_tokens": 50}})
        self.assertAlmostEqual(.00023, runtime.summary()["cost_usd"])
        limited = Runtime("limited", limits=Limits(input_tokens=2))
        with self.assertRaises(LimitExceeded):
            limited.reserve_llm(3)

    def test_unknown_usage_not_zero_cost(self):
        runtime = Runtime("test")
        with self.assertRaises(LimitExceeded):
            runtime.record_usage(None)
        self.assertIsNone(runtime.summary()["cost_usd"])

    def test_validation_tool_has_no_repair_side_effects(self):
        tools = self.registry()
        _, _, plan = parsed()
        value = tools.execute("validate_plan", {"plan_json": json.dumps(plan)})
        self.assertEqual(0, value["exit_code"])
        self.assertEqual(1, len(tools.candidates))


if __name__ == "__main__":
    unittest.main()
