import json
import unittest

from src.experiments import run_one
from src.llm import Reply, ScriptedClient
from src.runtime import Runtime
from src.architectures.mas import SPECIALISTS
from tests.full_fixtures import parsed


def final_reply(plan=None, status="completed"):
    return Reply(text=json.dumps({"status": status, "plans_json": json.dumps([plan] if plan else []),
                                  "explanation": "Test assertion only", "unresolved": []}))


def tool_reply(name, args, number=1):
    return Reply(output=[{"type": "function_call", "name": name, "arguments": json.dumps(args), "call_id": f"call{number}"}])


class ArchitectureTests(unittest.TestCase):
    def run_planner(self, architecture, replies, profile=None, changes=None):
        catalogs, request, _ = parsed()
        client = ScriptedClient(replies)
        runtime = Runtime("structural-test")
        result, details, _, _ = run_one(architecture, request, catalogs, client, runtime,
                                      profile or {"answers": {}, "weights": {"culture": 1}}, changes=changes)
        return result, details, client, runtime

    def test_single_agent_chooses_tool_order_and_repairs(self):
        _, _, plan = parsed()
        invalid = json.loads(json.dumps(plan))
        invalid["items"][0]["quantity"] = 1
        result, _, client, runtime = self.run_planner("single_agent", [
            tool_reply("validate_plan", {"plan_json": json.dumps(invalid)}),
            tool_reply("search_transport", {"filters_json": "{}", "offset": 0, "limit": 2}, 2),
            tool_reply("validate_plan", {"plan_json": json.dumps(plan)}, 3), final_reply(plan)])
        self.assertEqual("completed", result.status)
        self.assertEqual(1, len(result.plans))
        self.assertEqual(4, runtime.counts["llm_calls"])
        self.assertEqual(["validate_plan", "search_transport", "validate_plan"], [e["name"] for e in runtime.events if e["kind"] == "tool_call"])

    def test_workflow_order_is_fixed(self):
        _, _, plan = parsed()
        summary = Reply(text='{"summary":"Test facts"}')
        result, _, _, runtime = self.run_planner("workflow", [summary, summary, summary, final_reply(plan)])
        self.assertEqual("completed", result.status)
        stages = [e["stage"] for e in runtime.events if e["kind"] == "stage"]
        self.assertEqual(["analysis", "search", "clarification", "generation", "validation", "final"], stages)

    def test_workflow_repairs_only_twice(self):
        _, _, plan = parsed()
        plan["items"][0]["quantity"] = 1
        summary = Reply(text='{"summary":"Test facts"}')
        result, _, _, runtime = self.run_planner("workflow", [summary]*3 + [final_reply(plan)]*3)
        self.assertEqual("insufficient_data", result.status)
        self.assertEqual(2, sum(e.get("stage") == "repair" for e in runtime.events))

    def test_mas_specialists_decide_their_tools_and_addressed_repair(self):
        _, _, plan = parsed()
        replies = []
        for number, specialist in enumerate(sorted(SPECIALISTS), 1):
            replies.extend([tool_reply("delegate", {"specialist": specialist, "task": "Inspect domain"}, number),
                            tool_reply("get_item", {"id": "stay_1"}, number+10), Reply(text='{"summary":"Exact domain facts"}')])
        replies.extend([tool_reply("delegate", {"specialist": "transport", "task": "Repair station compatibility"}, 99),
                        Reply(text='{"summary":"Repaired train proposal"}'), final_reply(plan),
                        tool_reply("validate_plan", {"plan_json": json.dumps(plan)}, 100), Reply(text='{"summary":"Checked exact candidate"}')])
        result, _, client, runtime = self.run_planner("mas", replies)
        self.assertEqual("completed", result.status)
        delegated = [e["specialist"] for e in runtime.events if e["kind"] == "delegation"]
        self.assertEqual(sorted(SPECIALISTS)+["transport", "verification"], delegated)
        self.assertTrue(any(e["kind"] == "tool_call" and e["actor"] == "transport" for e in runtime.events))

    def test_no_private_profile_in_model_context(self):
        profile = {"answers": {"pace": "relaxed"}, "weights": {"culture": 1}, "private_marker": "DO_NOT_DISCLOSE"}
        _, _, plan = parsed()
        result, _, client, _ = self.run_planner("single_agent", [tool_reply("ask_user", {"topic": "pace", "text": "Pace?", "options": []}), final_reply(plan)], profile)
        text = json.dumps(client.requests)
        self.assertNotIn("DO_NOT_DISCLOSE", text)
        self.assertNotIn('"weights"', text)
        self.assertIn("relaxed", text)
        self.assertEqual("test", result.mode)

    def test_condition_changes_share_resources_and_versions(self):
        _, _, plan = parsed()
        result, _, _, runtime = self.run_planner("single_agent", [final_reply(plan), final_reply(status="no_feasible_plan")], changes=[{"type": "budget", "budget_kopecks": 1}])
        self.assertEqual(2, result.request_version)
        self.assertEqual(2, runtime.counts["llm_calls"])
        self.assertEqual("no_feasible_plan", result.status)


if __name__ == "__main__":
    unittest.main()
