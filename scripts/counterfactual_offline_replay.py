"""Offline replay of technically rejected plans from sealed experiment artifacts.

This script never constructs a provider client and never mutates run artifacts.  It
copies recoverable drafts into a separate report directory, repairs only fields
whose values are fixed by the scenario/result contract, then runs the repository's
deterministic full-trip validator and private evaluator.
"""
from __future__ import annotations

import copy
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from _bootstrap import ROOT
from src.assumptions import load_context
from src.catalog import load_catalogs, load_public_scenario
from src.checks.trip import validate_trip
from src.contracts import digest, read_json, write_json
from src.evaluator import Evaluator
from src.experiments import apply_change
from src.runtime import Runtime
from src.schemas import parse_plan
from src.tools import ToolRegistry


SOURCE_ROOTS = (ROOT / "runs/pilot-v2", ROOT / "runs/five-scenario-pilot")
OUTPUT = ROOT / "reports/counterfactual_offline_replay"
TECHNICAL_FIELDS = {
    "schema_version",
    "snapshot_id",
    "is_template",
    "timezone",
    "scenario_id",
    "total_cost_kopecks",
    "validation_status",
    "unresolved_items",
}


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def maybe_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def walk(value: Any):
    """Yield nested values, including JSON encoded inside strings."""
    decoded = maybe_json(value)
    if decoded is not value:
        yield from walk(decoded)
        return
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def plan_like(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("items"), list) and isinstance(value.get("selected_stay"), dict)


def plans_from(value: Any) -> list[dict[str, Any]]:
    found, seen = [], set()
    for nested in walk(value):
        if plan_like(nested):
            key = canonical(nested)
            if key not in seen:
                seen.add(key)
                found.append(copy.deepcopy(nested))
    return found


def restore(plan: dict[str, Any], request) -> tuple[dict[str, Any], dict[str, Any]]:
    """Repair contract metadata only; never alter itinerary choices or events."""
    fixed = copy.deepcopy(plan)
    deterministic = {
        "schema_version": request.schema_version,
        "snapshot_id": request.snapshot_id,
        "is_template": False,
        "timezone": request.timezone,
        "scenario_id": request.id,
        "total_cost_kopecks": None,
        "validation_status": "not_checked",
        "unresolved_items": [],
    }
    changes = {}
    for field, expected in deterministic.items():
        if field not in fixed:
            fixed[field] = copy.deepcopy(expected)
            changes[field] = {"from": "<missing>", "to": expected}
        elif field in {"schema_version", "snapshot_id", "is_template", "timezone", "scenario_id"} and fixed[field] != expected:
            changes[field] = {"from": fixed[field], "to": expected}
            fixed[field] = copy.deepcopy(expected)
    return fixed, changes


def structural_error(plan: dict[str, Any]) -> str | None:
    try:
        parse_plan(plan)
        return None
    except (ValueError, TypeError) as exc:
        return str(exc)


def load_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def event_evidence(events: list[dict[str, Any]]):
    evidence = defaultdict(lambda: {"plan": None, "sources": [], "first_seq": 10**9, "version": 1})
    version = 1
    validation_calls = {}
    verification_receipts = set()
    for event in events:
        if event.get("kind") == "condition_change":
            version = int(event.get("version", version + 1))
        seq = int(event.get("seq", 0))
        kind = event.get("kind")
        actor = event.get("actor")
        if kind == "tool_call" and event.get("name") in {"validate_plan", "calculate_cost"}:
            args = event.get("arguments", {})
            for plan in plans_from(args.get("plan_json")):
                key = canonical(plan)
                row = evidence[key]
                row.update(plan=plan, first_seq=min(row["first_seq"], seq), version=version)
                row["sources"].append({"kind": kind, "tool": event.get("name"), "actor": actor, "seq": seq})
                if event.get("name") == "validate_plan":
                    validation_calls[event.get("seq")] = (key, actor)
        if kind == "tool_result" and event.get("name") == "validate_plan":
            parent = event.get("parent_id")
            call = validation_calls.get(parent)
            if call and call[1] == "verification" and event.get("result", {}).get("exit_code") == 0:
                verification_receipts.add(call[0])
        if kind == "llm_response":
            # A content response with the result schema is a final model submission;
            # function-call arguments are handled above when the host executes them.
            for output in event.get("output", []):
                if output.get("type") not in {"message", "text", "output_text"}:
                    continue
                content = output.get("content", output.get("text", ""))
                decoded = maybe_json(content)
                if not isinstance(decoded, dict) or "status" not in decoded or "plans_json" not in decoded:
                    continue
                for plan in plans_from(decoded.get("plans_json")):
                    key = canonical(plan)
                    row = evidence[key]
                    row.update(plan=plan, first_seq=min(row["first_seq"], seq), version=version)
                    row["sources"].append({"kind": "final_llm_submission", "actor": actor, "seq": seq})
    return evidence, verification_receipts


def bool_text(value: Any) -> str:
    if value is None:
        return "N/A"
    return "1" if value else "0"


def main() -> int:
    data = ROOT / "data/benchmark"
    base_catalogs = load_catalogs(data / "snapshot")
    base_context = load_context(data, base_catalogs, "benchmark_assumptions")
    suite = read_json(data / "suite.json")
    cases = {case["id"]: case for case in suite["cases"]}
    rows, all_details = [], []
    copies = OUTPUT / "repaired_plans"

    for source_root in SOURCE_ROOTS:
        if not source_root.exists():
            continue
        for run_dir in sorted(path for path in source_root.iterdir() if path.is_dir()):
            manifest = read_json(run_dir / "manifest.json")
            result = read_json(run_dir / "result.json")
            candidates = read_json(run_dir / "candidates.json")
            actual_evaluation = read_json(run_dir / "evaluation.json")
            events = load_events(run_dir / "events.jsonl")
            scenario_id = manifest["scenario_id"]
            architecture = manifest["architecture"]
            case = cases[scenario_id]
            request = load_public_scenario(data / "scenarios" / case["split"] / f"{scenario_id}.json")
            profile = read_json(ROOT / "evaluation/benchmark/profiles" / f"{scenario_id}.json")
            proof_path = ROOT / "evaluation/benchmark/feasibility" / f"{scenario_id}.json"
            proof = read_json(proof_path) if proof_path.exists() else None

            # Refuse to replay against silently changed inputs.
            input_mismatch = []
            if manifest.get("snapshot_hash") != digest(base_catalogs):
                input_mismatch.append("snapshot_hash")
            if manifest.get("request_hash") != digest(request):
                input_mismatch.append("request_hash")
            if manifest.get("profile_hash") != digest(profile):
                input_mismatch.append("profile_hash")
            if manifest.get("case_hash") != digest(case):
                input_mismatch.append("case_hash")

            evidence, verification_receipts = event_evidence(events)
            final_version = int(result.get("request_version", 1))
            submitted = candidates.get("submitted_plans", [])
            rejected = candidates.get("rejected", [])
            for index, plan in enumerate(submitted):
                key = canonical(plan)
                row = evidence[key]
                row.update(plan=copy.deepcopy(plan), first_seq=min(row["first_seq"], 10**8 + index), version=final_version)
                row["sources"].append({"kind": "submitted_plan", "index": index})
            for index, item in enumerate(rejected):
                plan = item.get("plan")
                if not plan_like(plan):
                    continue
                key = canonical(plan)
                row = evidence[key]
                row.update(plan=copy.deepcopy(plan), first_seq=min(row["first_seq"], 10**8 + index), version=final_version)
                row["sources"].append({"kind": "rejected_plan", "index": index, "reason": item.get("reason"), "message": item.get("message")})

            rejected_keys = set()
            for key, item in evidence.items():
                if any(source["kind"] == "rejected_plan" for source in item["sources"]):
                    rejected_keys.add(key)
                if any(source.get("tool") == "validate_plan" for source in item["sources"]):
                    rejected_keys.add(key)

            recoverable, eligible = [], []
            unrecoverable_schema = set()
            for key, item in sorted(evidence.items(), key=lambda pair: pair[1]["first_seq"]):
                raw = item["plan"]
                version = item["version"]
                registry = ToolRegistry(base_catalogs, request, Runtime("counterfactual"), context=base_context)
                for change in case.get("changes", [])[: max(0, version - 1)]:
                    apply_change(registry, change)
                fixed, changes = restore(raw, registry.request)
                before_error = structural_error(raw)
                after_error = structural_error(fixed)
                technical = bool(changes) and after_error is None
                report = validate_trip(registry.catalogs, registry.request, fixed, context=registry.context).to_dict() if after_error is None else None
                source_kinds = {source["kind"] for source in item["sources"]}
                is_final = bool(source_kinds & {"submitted_plan", "rejected_plan", "final_llm_submission"})
                if result.get("status") == "budget_exhausted" and "final_llm_submission" not in source_kinds and not (source_kinds & {"submitted_plan", "rejected_plan"}):
                    is_final = False
                has_mas_receipt = key in verification_receipts
                passed = bool(report and report["exit_code"] == 0 and (architecture != "mas" or has_mas_receipt))
                detail = {
                    "raw_digest": digest(raw),
                    "repaired_digest": digest(fixed),
                    "version": version,
                    "sources": item["sources"],
                    "structural_error_before": before_error,
                    "restored_fields": changes,
                    "structural_error_after": after_error,
                    "technical_recoverable": technical,
                    "final_candidate_evidence": is_final,
                    "mas_exact_verification_receipt": has_mas_receipt if architecture == "mas" else None,
                    "full_validation": report,
                    "counterfactual_pass": passed,
                }
                all_details.append({"run_id": run_dir.name, **detail})
                if key in rejected_keys and after_error is not None:
                    unrecoverable_schema.add("UNRECOVERABLE_SCHEMA: " + after_error)
                if technical and key in rejected_keys:
                    recoverable.append((fixed, detail, registry.catalogs, registry.request, registry.context))
                    target = copies / run_dir.name / f"{len(recoverable):02d}-{digest(fixed)[:12]}.json"
                    write_json(target, {"label": "counterfactual_offline_replay", "source_run": run_dir.name, "restored_fields": changes, "plan": fixed})
                if technical and is_final:
                    eligible.append((fixed, detail, registry.catalogs, registry.request, registry.context))

            # Preserve model order. Evaluate only fully valid restored final candidates;
            # evaluator independently recomputes utility and Success thresholds.
            counterfactual_plans = [plan for plan, detail, *_ in eligible if detail["counterfactual_pass"]]
            synthetic_result = {
                "run_id": run_dir.name + "-counterfactual",
                "architecture": architecture,
                "mode": "real",
                "plans": counterfactual_plans,
                "status": "completed" if counterfactual_plans else result.get("status"),
                "resources": {},
            }
            eval_catalogs, eval_request, eval_context = base_catalogs, request, base_context
            for change in case.get("changes", [])[: max(0, final_version - 1)]:
                probe = ToolRegistry(eval_catalogs, eval_request, Runtime("counterfactual-eval"), context=eval_context)
                apply_change(probe, change)
                eval_catalogs, eval_request, eval_context = probe.catalogs, probe.request, probe.context
            counter = Evaluator().evaluate(eval_catalogs, eval_request, synthetic_result, profile, case["expected"], proof, context=eval_context)
            actual_metrics = actual_evaluation["metrics"]
            remaining = set(input_mismatch)
            remaining.update(unrecoverable_schema)
            for _, detail, *_ in recoverable:
                report = detail["full_validation"]
                if report:
                    remaining.update(problem["code"] for problem in report["violations"])
                    remaining.update(problem["code"] for problem in report["incomplete_data"])
                    remaining.update("SCHEMA_ERROR: " + problem["message"] for problem in report["input_errors"])
                if architecture == "mas" and not detail["mas_exact_verification_receipt"]:
                    remaining.add("MAS_EXACT_VERIFICATION_MISSING")
            if not evidence:
                remaining.add("NO_RECOVERABLE_PLAN_IN_TRACE")
            if result.get("status") == "budget_exhausted" and not eligible:
                remaining.add("BUDGET_EXHAUSTED_WITHOUT_FINAL_CANDIDATE")
            if result.get("status") == "error":
                remaining.add("RUN_ERROR")
            # The scored counterfactual remains zero when no eligible final plan
            # exists, as required.  Separately flag latent outcomes that cannot be
            # reconstructed because the model never persisted a final plan object.
            final_plan_evidence = any(
                {source["kind"] for source in item["sources"]}
                & {"submitted_plan", "rejected_plan", "final_llm_submission"}
                for item in evidence.values()
            )
            impossible = bool(input_mismatch) or not final_plan_evidence
            if architecture == "mas" and any(detail["technical_recoverable"] and detail["final_candidate_evidence"] and not detail["mas_exact_verification_receipt"] for detail in (x[1] for x in eligible)):
                impossible = True
            rows.append({
                "source_batch": source_root.name,
                "run_id": run_dir.name,
                "scenario": scenario_id,
                "architecture": architecture,
                "actual_candidates": len(submitted),
                "found_rejected_drafts": len(rejected_keys),
                "technically_recoverable_drafts": len(recoverable),
                "full_validation_passes": sum(1 for _, detail, *_ in recoverable if detail["counterfactual_pass"]),
                "actual_success_at_1": actual_metrics.get("success_at_1"),
                "actual_success_at_5": actual_metrics.get("success_at_5"),
                "counterfactual_success_at_1": counter["metrics"].get("success_at_1"),
                "counterfactual_success_at_5": counter["metrics"].get("success_at_5"),
                "actual_conflict_correct": actual_metrics.get("conflict_correct"),
                "counterfactual_conflict_correct": counter["metrics"].get("conflict_correct"),
                "indeterminate": impossible,
                "remaining_reasons": sorted(remaining),
            })

    OUTPUT.mkdir(parents=True, exist_ok=True)
    write_json(OUTPUT / "results.json", {
        "label": "counterfactual_offline_replay",
        "method": "metadata-only deterministic repair; no LLM/API; original artifacts unchanged",
        "technical_fields": sorted(TECHNICAL_FIELDS),
        "rows": rows,
        "draft_details": all_details,
    })

    headers = ["Batch", "Scenario", "Architecture", "Actual candidates", "Rejected drafts", "Recoverable", "Full pass", "Actual S@1/S@5", "Counterfactual S@1/S@5", "Unobservable latent result", "Remaining reasons"]
    lines = [
        "# counterfactual_offline_replay",
        "",
        "Offline counterfactual replay. No API or LLM calls were made; original run artifacts were not modified. Only deterministic contract metadata was repaired. Itinerary content was preserved byte-for-byte at the field level.",
        "",
        "| " + " | ".join(headers) + " |",
        "|" + "---|" * len(headers),
    ]
    for row in rows:
        reasons = ", ".join(row["remaining_reasons"]) or "—"
        lines.append("| " + " | ".join([
            row["source_batch"], row["scenario"], row["architecture"], str(row["actual_candidates"]),
            str(row["found_rejected_drafts"]), str(row["technically_recoverable_drafts"]), str(row["full_validation_passes"]),
            f"{bool_text(row['actual_success_at_1'])}/{bool_text(row['actual_success_at_5'])}",
            f"{bool_text(row['counterfactual_success_at_1'])}/{bool_text(row['counterfactual_success_at_5'])}",
            "yes" if row["indeterminate"] else "no", reasons,
        ]) + " |")
    lines.extend([
        "",
        "## Interpretation",
        "",
        "`Recoverable` means the saved draft became structurally parseable after changing only deterministic contract metadata. `Full pass` additionally requires full domain validation; MAS also requires a successful verification receipt for the exact raw plan version. Intermediate validation calls are counted as drafts but do not become counterfactual final submissions by themselves.",
        "",
        "For infeasible scenarios Success@K is N/A by design; `actual_conflict_correct` and `counterfactual_conflict_correct` are available in `results.json`.",
    ])
    (OUTPUT / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"label": "counterfactual_offline_replay", "output": str(OUTPUT), "runs": len(rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
