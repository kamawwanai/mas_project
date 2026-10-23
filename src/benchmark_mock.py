"""Oracle-scripted integration of all benchmark cases, not an agent-quality experiment."""
import json
from pathlib import Path
from .contracts import digest, read_json, write_json
from .catalog import load_catalogs, load_public_scenario
from .assumptions import load_context
from .experiments import persist_run, replay_saved_run, version_hash
from .llm import ScriptedClient, Reply
from .runtime import Runtime
from .batch import run_batch
from .reporting import export_runs, build_report
from .offline import deny_network

ROOT = Path(__file__).resolve().parents[1]


def scripted(architecture, plan, topic="pace", status="completed"):
    def call(name, args, number):
        return Reply(output=[{"type": "function_call", "name": name, "arguments": json.dumps(args), "call_id": f"call_{number}"}])
    def summary():
        return Reply(text=json.dumps({"summary": "Oracle-scripted synthetic integration fixture; not model reasoning."}))
    final = Reply(text=json.dumps({"status": status, "plans_json": json.dumps([plan] if plan else []),
                                  "explanation": "Oracle-scripted synthetic fixture.", "unresolved": []}))
    ask = call("ask_user", {"topic": topic, "text": "Which preference applies?", "options": []}, 500)
    if architecture == "workflow":
        return [summary(), summary(), ask, summary(), final]
    if architecture == "single_agent":
        return [ask, *([call("validate_plan", {"plan_json": json.dumps(plan)}, 501)] if plan else []), final]
    if not plan:
        return [final]
    from .architectures.mas import SPECIALISTS
    replies = []
    for n, specialist in enumerate(sorted(SPECIALISTS)):
        replies.append(call("delegate", {"specialist": specialist, "task": "Inspect the scripted candidate and saved environment."}, n))
        if specialist == "transport":
            replies.append(ask)
        if specialist == "verification":
            replies.append(call("validate_plan", {"plan_json": json.dumps(plan)}, 502))
        replies.append(summary())
    return replies+[final]


def mock_cycle(output, *, resume=False):
    deny_network()
    root = ROOT/"data/benchmark"
    catalogs = load_catalogs(root/"snapshot")
    context = load_context(root, catalogs, "benchmark_assumptions")
    suite = read_json(root/"suite.json")
    jobs = [{"scenario_id": case["id"], "architecture": arch, "repeat": 1}
            for case in suite["cases"] for arch in ("workflow", "single_agent", "mas")]
    profiles = {case["id"]: read_json(ROOT/"evaluation/benchmark/profiles"/f"{case['id']}.json") for case in suite["cases"]}
    requests = {case["id"]: load_public_scenario(root/"scenarios"/case["split"]/f"{case['id']}.json") for case in suite["cases"]}
    output = Path(output).resolve()
    oracle_inputs = {str(p.relative_to(ROOT)): digest(read_json(p)) for folder in ("reference_plans", "feasibility")
                     for p in sorted((ROOT/"evaluation/benchmark"/folder).glob("*.json"))}
    signature = digest({"mode": "test", "environment": context.registry_hash, "catalogs": catalogs,
                        "suite": suite, "requests": requests, "profiles": profiles, "oracle_inputs": oracle_inputs,
                        "code": version_hash(), "jobs": jobs})

    def inputs(case):
        key = case["id"]
        proof = read_json(ROOT/"evaluation/benchmark/feasibility"/f"{key}.json") if case["expected"] == "infeasible" else None
        return key, requests[key], profiles[key], proof

    def execute(job, key):
        case = next(c for c in suite["cases"] if c["id"] == job["scenario_id"])
        case_id, request, profile, proof = inputs(case)
        plan = read_json(ROOT/"evaluation/benchmark/reference_plans"/f"{case_id}.json") if case["expected"] == "feasible" else None
        topic = next(iter(profile["answers"]), "pace")
        replies = scripted(job["architecture"], plan, topic, "completed" if plan else "no_feasible_plan")
        if case["changes"]:
            final = read_json(ROOT/"evaluation/benchmark/reference_plans"/f"{case_id}.final.json")
            replies += scripted(job["architecture"], final, topic)
        client = ScriptedClient(replies)
        directory = output/"runs"/key
        runtime = Runtime(key, pricing={"input_per_million": 0, "cached_input_per_million": 0, "output_per_million": 0})
        manifest = {"run_id": key, "mode": "test", "synthetic": True, "evaluation_mode": "benchmark_assumptions",
                    "assumptions_hash": context.registry_hash, "architecture": job["architecture"], "scenario_id": case_id,
                    "snapshot_hash": digest(catalogs), "request_hash": digest(request), "profile_hash": digest(profile), "case_hash": digest(case)}
        result = persist_run(directory, job["architecture"], request, catalogs, client, runtime, profile, manifest,
                             context=context, expected=case["expected"], proof=proof, changes=case["changes"])
        public_context = json.dumps(client.requests)
        if profile["private_marker"] in public_context or '"weights"' in public_context:
            raise AssertionError("Hidden profile leaked")
        if result["status"] != ("completed" if plan else "no_feasible_plan"):
            raise AssertionError("Mock integration failed: " + key + " " + result["status"])
        evaluation = read_json(directory/"evaluation.json")
        if plan and not evaluation["metrics"]["feasible_at_1"] or not plan and not evaluation["metrics"]["conflict_correct"]:
            raise AssertionError("Independent evaluation failed: " + key)
        if runtime.counts["questions"] > 3:
            raise AssertionError("Question limit exceeded")

    completed = run_batch(output/"runs", jobs, signature, execute, resume=resume, mode="test")
    for job, key in zip(jobs, completed):
        case = next(c for c in suite["cases"] if c["id"] == job["scenario_id"])
        _, request, profile, proof = inputs(case)
        replay_saved_run(output/"runs"/key, catalogs, request, profile, case, proof, context=context)
    rows = export_runs(output/"runs", output/"report", include_test=True)
    build_report(output/"report", output/"report")
    if export_runs(output/"runs", output/"real_export"):
        raise AssertionError("Mock results leaked into real export")
    result = {"mode": "test", "evaluation_mode": "benchmark_assumptions", "synthetic": True,
              "oracle_scripted": True, "not_llm_performance": True, "completed": len(completed),
              "report_rows": len(rows), "network": "disabled", "private_profile_leaks": 0,
              "charts": len(list((output/"report").glob("*.svg")))}
    write_json(output/"smoke-summary.json", result)
    return result
