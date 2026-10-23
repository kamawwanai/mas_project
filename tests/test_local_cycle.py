from pathlib import Path
from copy import deepcopy
from unittest.mock import patch
import json
import socket
import unittest

from src.batch import run_batch, verify_run
from src.contracts import read_json, digest
from src.experiments import run_one
from src.llm import ScriptedClient, Reply
from src.local_cycle import local_cycle
from src.offline import deny_network, NetworkDisabled
from src.reporting import export_runs
from src.runtime import Runtime, Limits
from tests.full_fixtures import parsed
from tests.test_validation import working_directory
from tests.mock_clients import conversation, summary
from tests.test_architectures import final_reply, tool_reply


class LocalCycleTests(unittest.TestCase):
    def test_full_cycle_reports_traces_charts_errors_and_resume(self):
        with working_directory() as temp:
            result = local_cycle(temp)
            self.assertEqual(4, result["completed"])
            self.assertEqual(6, result["svg_charts"])
            rows = read_json(Path(temp)/"report/runs.json")
            self.assertEqual({"test"}, {r["mode"] for r in rows})
            self.assertTrue(all(verify_run(p.parent) for p in (Path(temp)/"runs").glob("*/complete.json")))
            before = {str(p): p.read_bytes() for p in (Path(temp)/"runs").glob("*/*") if p.is_file()}
            self.assertEqual(result, local_cycle(temp, resume=True))
            self.assertEqual(before, {str(p): p.read_bytes() for p in (Path(temp)/"runs").glob("*/*") if p.is_file()})
            chart = (Path(temp)/"report/success_at_1.svg").read_text(encoding="utf-8")
            self.assertIn("MOCK / SYNTHETIC", chart)
            self.assertIn("workflow", chart)
            self.assertIn("mode", (Path(temp)/"report/comparison.csv").read_text(encoding="utf-8-sig"))
            self.assertIn("MOCK / SYNTHETIC", (Path(temp)/"report/traces.md").read_text(encoding="utf-8"))
            for name in ("summary.json", "candidates.json"):
                self.assertEqual("test", read_json(Path(temp)/"runs/case-mas-r1"/name)["mode"])

    def test_incomplete_run_is_preserved_and_signature_change_is_refused(self):
        with working_directory() as temp:
            root = Path(temp)/"runs"
            jobs = [{"scenario_id": "case", "architecture": "workflow", "repeat": 1}]
            def interrupt(job, key):
                (root/key).mkdir()
                (root/key/"partial.txt").write_text("preserve")
                raise RuntimeError("simulated interruption")
            with self.assertRaises(RuntimeError):
                run_batch(root, jobs, "hash", interrupt, mode="test")
            with self.assertRaisesRegex(ValueError, "Incomplete"):
                run_batch(root, jobs, "hash", interrupt, resume=True, mode="test")
            with self.assertRaises(ValueError):
                run_batch(root, jobs, "changed", interrupt, resume=True, mode="test")
            self.assertEqual("preserve", (root/"case-workflow-r1/partial.txt").read_text())

    def test_interrupted_batch_resumes_between_jobs_without_repeating_completed(self):
        from src import local_cycle as module
        original = module.persist_run
        calls = []
        def interrupted(*args, **kwargs):
            calls.append(args[1])
            if len(calls) == 2:
                raise RuntimeError("stop before job 2")
            return original(*args, **kwargs)
        with working_directory() as temp:
            with patch("src.local_cycle.persist_run", side_effect=interrupted):
                with self.assertRaises(RuntimeError):
                    local_cycle(temp)
            first = (Path(temp)/"runs/case-workflow-r1/events.jsonl").read_bytes()
            local_cycle(temp, resume=True)
            self.assertEqual(first, (Path(temp)/"runs/case-workflow-r1/events.jsonl").read_bytes())

    def test_network_is_disabled_before_dns_or_socket_creation(self):
        deny_network()
        with self.assertRaises(NetworkDisabled):
            socket.getaddrinfo("example.invalid", 443)
        with self.assertRaises(NetworkDisabled):
            socket.socket()

    def test_budget_exhaustion_still_saves_complete_error_evidence(self):
        from src.experiments import persist_run
        c, r, plan = parsed()
        with working_directory() as temp:
            directory = Path(temp)/"limited"
            runtime = Runtime("limited", limits=Limits(llm_attempts=0))
            result = persist_run(directory, "single_agent", r, c,
                                 ScriptedClient(conversation("single_agent", plan)), runtime,
                                 {"weights": {"culture": 1}}, {"mode": "test", "synthetic": True})
            self.assertEqual("budget_exhausted", result["status"])
            self.assertTrue(verify_run(directory))

    def test_mixed_report_modes_are_rejected(self):
        from src.contracts import write_json
        with working_directory() as temp:
            root = Path(temp)
            for mode in ("real", "test"):
                write_json(root/mode/"evaluation.json", {"run_id": mode, "scenario_id": "fixture", "architecture": "mas", "mode": mode, "metrics": {}})
            with self.assertRaisesRegex(ValueError, "mix"):
                export_runs(root, root/"report", include_test=True)


class ArchitectureSafetyTests(unittest.TestCase):
    def execute(self, architecture, replies, limits=None):
        catalogs, request, plan = parsed()
        client = ScriptedClient(replies)
        runtime = Runtime("synthetic", limits=limits)
        profile = {"weights": {"food": 1}, "answers": {"pace": "relaxed"}, "private_marker": "NEVER_IN_CONTEXT"}
        result, details, _, _ = run_one(architecture, request, catalogs, client, runtime, profile)
        return result, client, runtime

    def test_all_architectures_hide_profile_and_share_three_question_limit(self):
        _, _, plan = parsed()
        for architecture in ("workflow", "single_agent", "mas"):
            with self.subTest(architecture=architecture):
                result, client, runtime = self.execute(architecture, conversation(architecture, plan, questions=3))
                self.assertEqual("completed", result.status)
                context = json.dumps(client.requests)
                self.assertNotIn("NEVER_IN_CONTEXT", context)
                self.assertNotIn('"weights"', context)
                self.assertIn("relaxed", context)
                self.assertEqual(3, runtime.counts["questions"])
                result, _, runtime = self.execute(architecture, conversation(architecture, plan, questions=4))
                self.assertEqual("completed" if architecture == "workflow" else "budget_exhausted", result.status)
                self.assertEqual(3, runtime.counts["questions"])

    def test_all_architectures_obey_resource_limit(self):
        _, _, plan = parsed()
        for architecture in ("workflow", "single_agent", "mas"):
            result, _, runtime = self.execute(architecture, conversation(architecture, plan), Limits(llm_attempts=1))
            self.assertEqual("budget_exhausted", result.status)
            self.assertEqual(1, runtime.counts["llm_attempts"])

    def test_all_architectures_replan_with_shared_questions_and_private_history(self):
        catalogs, request, plan = parsed()
        for architecture in ("workflow", "single_agent", "mas"):
            for questions, expected in ((1, "completed"), (2, "budget_exhausted")):
                client = ScriptedClient(conversation(architecture, plan, questions) + conversation(architecture, plan, questions))
                runtime = Runtime("versions-test")
                profile = {"weights": {"culture": 1}, "private_marker": "NEVER_IN_CONTEXT", "answers": {"pace": "relaxed"}}
                result, details, _, _ = run_one(architecture, request, catalogs, client, runtime, profile,
                                              changes=[{"type": "budget", "budget_kopecks": 180000}])
                self.assertEqual("completed" if architecture == "workflow" else expected, result.status, architecture)
                self.assertEqual(2, result.request_version)
                self.assertEqual(min(3, questions*2), runtime.counts["questions"])
                self.assertNotIn("NEVER_IN_CONTEXT", str(client.requests))
                self.assertNotIn("'weights'", str(client.requests))
                if architecture == "mas" and questions == 1:
                    verifications = [e for e in runtime.events if e["kind"] == "tool_call" and e["actor"] == "verification" and e.get("name") == "validate_plan"]
                    self.assertEqual(2, len(verifications))

    def test_invalid_format_is_retried_then_recovered(self):
        _, _, plan = parsed()
        for architecture in ("workflow", "single_agent", "mas"):
            replies = conversation(architecture, plan)
            replies.insert(0, Reply(text="bad-json"))
            result, _, runtime = self.execute(architecture, replies)
            self.assertEqual("completed", result.status)
            self.assertTrue(any(e["kind"] == "invalid_output" for e in runtime.events))

    def test_mas_requires_real_verifier_tool_receipt_for_exact_final_plan(self):
        _, _, plan = parsed()
        replies = conversation("mas", plan)
        replies[-2] = summary("I claim it is verified without checking")
        replies[-1] = final_reply(plan)
        result, _, runtime = self.execute("mas", replies)
        self.assertNotEqual("completed", result.status)
        self.assertFalse(any(e["kind"] == "tool_call" and e.get("name") == "validate_plan" for e in runtime.events))

    def test_mas_rechecks_repaired_plan_and_keeps_separate_histories(self):
        _, _, plan = parsed()
        invalid = deepcopy(plan)
        invalid["items"][0]["quantity"] = 1
        replies = conversation("mas", invalid)
        replies += [tool_reply("delegate", {"specialist": "transport", "task": "Repair quantity"}, 100),
                    summary("Correct group quantity"), final_reply(plan),
                    tool_reply("validate_plan", {"plan_json": json.dumps(plan)}, 101), summary("Repaired plan checked")]
        result, client, runtime = self.execute("mas", replies)
        self.assertEqual("completed", result.status)
        checks = [e for e in runtime.events if e["kind"] == "tool_call" and e.get("name") == "validate_plan"]
        self.assertEqual(2, len(checks))
        self.assertEqual({"verification"}, {e["actor"] for e in checks})
        # Coordinator and specialist contexts have different role instructions.
        systems = {r["messages"][0]["content"] for r in client.requests}
        self.assertEqual(6, len(systems))
