"""Explicit synthetic smoke cycle; never selected by real experiment entry points."""
from pathlib import Path
from .batch import run_batch
from .catalog import load_dataset
from .contracts import digest, read_json, write_json
from .evaluator import Evaluator
from .experiments import persist_run, version_hash, replay_saved_run
from .llm import ScriptedClient
from .offline import deny_network
from .reporting import export_runs, build_report
from .runtime import Runtime


def local_cycle(output, *, resume=False):
    # Fixtures are deliberately separate from data/benchmark and marked test in every artifact.
    from tests.fixtures import write_dataset
    from tests.full_fixtures import full_data
    from tests.mock_clients import conversation
    deny_network()
    root = Path(output).resolve()
    fixture = root / "inputs"
    if not fixture.exists():
        write_dataset(fixture, full_data())
    dataset = load_dataset(fixture, "case")
    if dataset.input_errors:
        raise ValueError("Synthetic fixture failed to load")
    plan = read_json(fixture / "reference_plans/case.json")
    profile = {"weights": {"culture": 1}, "answers": {"pace": "relaxed"}, "private_marker": "SYNTHETIC_PRIVATE_SENTINEL"}
    jobs = [{"scenario_id": "case", "architecture": arch, "repeat": 1} for arch in ("workflow", "single_agent", "mas")]
    jobs.append({"scenario_id": "case", "architecture": "single_agent", "repeat": 2, "fault": "exhausted_script"})
    signature = digest({"mode": "test", "jobs": jobs, "catalogs": dataset.catalogs, "request": dataset.scenario, "profile": profile, "plan": plan, "code": version_hash()})

    def execute(job, key):
        directory = root / "runs" / key
        runtime = Runtime(key, directory / "events.jsonl", pricing={"input_per_million": 0, "cached_input_per_million": 0, "output_per_million": 0})
        client = ScriptedClient([] if job.get("fault") else conversation(job["architecture"], plan, questions=1))
        manifest = {"run_id": key, "mode": "test", "synthetic": True, "architecture": job["architecture"],
                    "snapshot_hash": digest(dataset.catalogs), "request_hash": digest(dataset.scenario), "profile_hash": digest(profile)}
        result = persist_run(directory, job["architecture"], dataset.scenario, dataset.catalogs, client, runtime, profile, manifest)
        context = str(client.requests)
        if "SYNTHETIC_PRIVATE_SENTINEL" in context or "'weights'" in context:
            raise AssertionError("Private profile leaked")
        if result["status"] != ("error" if job.get("fault") else "completed"):
            raise AssertionError(f"Unexpected synthetic result: {result['status']}")

    completed = run_batch(root / "runs", jobs, signature, execute, resume=resume, mode="test")
    for key in completed:
        replay_saved_run(root / "runs" / key, dataset.catalogs, dataset.scenario, profile, {"expected": "feasible", "changes": []})
    rows = export_runs(root / "runs", root / "report", include_test=True)
    build_report(root / "report", root / "report")
    if export_runs(root / "runs", root / "real_export"):
        raise AssertionError("Synthetic runs entered default real export")
    result = {"mode": "test", "synthetic": True, "completed": len(completed), "expected_errors": 1,
              "report_rows": len(rows), "svg_charts": len(list((root/"report").glob("*.svg"))), "network": "disabled"}
    write_json(root / "smoke-summary.json", result)
    return result
