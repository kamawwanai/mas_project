"""Offline CSV/JSON aggregation. Test doubles are excluded by default."""
from collections import defaultdict
from pathlib import Path
import csv
import json
from statistics import mean

from .contracts import read_json, write_json


def export_runs(runs, output, include_test=False):
    rows = []
    for path in sorted(Path(runs).glob("*/evaluation.json")):
        value = read_json(path)
        if value["mode"] != "real" and not include_test:
            continue
        rows.append({k: value[k] for k in ("run_id", "scenario_id", "architecture", "mode")} | {
            k: v for k, v in value["metrics"].items() if not isinstance(v, (list, dict))} |
                    {"trace_file": str(path.parent / "events.jsonl")})
    root = Path(output)
    write_json(root / "runs.json", rows)
    keys = sorted(set().union(*(r.keys() for r in rows))) if rows else ["run_id", "scenario_id", "architecture", "mode"]
    with (root / "runs.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def build_report(source, output):
    rows = read_json(Path(source) / "runs.json")
    groups = defaultdict(list)
    for row in rows:
        groups[row["architecture"]].append(row)
    metrics = ["success_at_1", "success_at_5", "feasible_at_1", "feasible_at_5", "valid_fraction", "best_utility", "questions", "llm_calls", "llm_attempts", "input_tokens", "output_tokens", "tools", "seconds", "cost_usd", "conflict_correct"]
    summary = []
    for architecture, values in sorted(groups.items()):
        result = {"architecture": architecture, "runs": len(values)}
        for key in metrics:
            numbers = [v[key] for v in values if isinstance(v.get(key), (int, float))]
            result[key] = mean(numbers) if numbers else None
            result[f"{key}_n"] = len(numbers)
            result[f"{key}_min"] = min(numbers) if numbers else None
            result[f"{key}_max"] = max(numbers) if numbers else None
        summary.append(result)
    root = Path(output)
    write_json(root / "comparison.json", summary)
    columns = ["architecture", "runs", *metrics]
    with (root / "comparison.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(summary)
    lines = ["# Architecture comparison", "", "No real runs are available." if not rows else "Means are descriptive; inspect scenario-level results and ranges.", "",
             "| " + " | ".join(columns) + " |", "|" + "---|"*len(columns)]
    for row in summary:
        lines.append("| " + " | ".join(str(round(row[c], 4)) if isinstance(row[c], float) else str(row[c]) for c in columns) + " |")
    (root / "comparison.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    trace_lines = ["# Saved run traces", ""]
    chosen = []
    for success in (True, False):
        for row in rows:
            if bool(row.get("success_at_5")) == success and row not in chosen:
                chosen.append(row)
                if sum(bool(r.get("success_at_5")) == success for r in chosen) >= 2:
                    break
    if not chosen:
        trace_lines.append("No real runs are available; no success/failure traces are asserted.")
    for row in chosen:
        trace_lines.extend([f"## {row['run_id']}", "", f"Architecture: {row['architecture']}; Success@5: {row.get('success_at_5')}; best utility: {row.get('best_utility')}", ""])
        path = Path(row["trace_file"])
        if not path.exists():
            trace_lines.append("Trace file missing.")
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event["kind"] in {"stage", "tool_call", "delegation", "condition_change", "run_error", "invalid_output"}:
                action = event.get("name", event.get("stage", event.get("specialist", "")))
                trace_lines.append(f"- {event['seq']}: {event['actor']} / {event['kind']} / {action}")
        trace_lines.append("")
    (root / "traces.md").write_text("\n".join(trace_lines)+"\n", encoding="utf-8")
    if rows:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        for name in ("success_at_1", "success_at_5", "valid_fraction", "best_utility", "cost_usd", "seconds"):
            values = [(r["architecture"], r[name]) for r in summary if r[name] is not None]
            if not values:
                continue
            figure, ax = plt.subplots(figsize=(6, 4))
            ax.bar([v[0] for v in values], [v[1] for v in values])
            ax.set_ylabel(name)
            figure.tight_layout()
            for extension in ("png", "svg"):
                figure.savefig(root / f"{name}.{extension}")
            plt.close(figure)
    return summary
