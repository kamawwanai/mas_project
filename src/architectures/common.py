"""Shared transport of agent messages; contains no planning strategy."""
from pathlib import Path
import json
import sys

from ..contracts import RESULT_SCHEMA, jsonable, parse_submission

PROMPTS = Path(__file__).resolve().parents[2] / "prompts"
SUMMARY_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}},
                  "required": ["summary"], "additionalProperties": False}


def instruction(role):
    root = PROMPTS if PROMPTS.exists() else Path(sys.prefix) / "prompts"
    base = (root / "common.txt").read_text(encoding="utf-8")
    return base + "\n" + (root / f"{role}.txt").read_text(encoding="utf-8")


def session(client, registry, role, task, *, schema=RESULT_SCHEMA, allowed=None, history=None, extra_tools=None, dispatch=None):
    messages = history if history is not None else []
    if not messages:
        messages.append({"role": "system", "content": instruction(role)})
    messages.append({"role": "user", "content": json.dumps(jsonable(task), ensure_ascii=False)})
    definitions = [d for d in registry.definitions if allowed is None or d["name"] in allowed]
    definitions += extra_tools or []
    names = {d["name"] for d in definitions}
    while True:
        reply = client.generate(messages, definitions, registry.runtime, role, schema)
        messages.extend(reply.output)
        calls = [o for o in reply.output if o.get("type") == "function_call"]
        if not calls:
            try:
                value = json.loads(reply.text)
                if schema == RESULT_SCHEMA:
                    return parse_submission(value)
                if not isinstance(value, dict) or set(value) != {"summary"} or not isinstance(value["summary"], str):
                    raise ValueError("Expected summary object")
                return value
            except (ValueError, TypeError) as exc:
                registry.runtime.event(role, "invalid_output", message=str(exc))
                messages.append({"role": "user", "content": "Invalid structured result. Correct the JSON format: " + str(exc)})
                continue
        for call in calls:
            name = call["name"]
            try:
                args = json.loads(call["arguments"])
                if name not in names:
                    result = {"error": "Tool unavailable in this stage"}
                elif dispatch and name in {d["name"] for d in extra_tools or []}:
                    result = dispatch(name, args)
                else:
                    result = registry.execute(name, args, role)
            except (ValueError, TypeError) as exc:
                result = {"error": str(exc)}
            messages.append({"type": "function_call_output", "call_id": call["call_id"], "output": json.dumps(result, ensure_ascii=False)})


def public_task(registry):
    return {"request": jsonable(registry.request), "revealed_preferences": registry.revealed,
            "previous_result": getattr(registry, "previous_result", None), "plan_contract": {
        "schema_version": "0.2.0", "snapshot_id": registry.request.snapshot_id, "is_template": False,
        "timezone": registry.request.timezone, "id": "unique plan id", "scenario_id": registry.request.id,
        "selected_stay": {"reference_id": "catalog stay ID", "rooms": 1, "nights": registry.request.nights},
        "items": [{"id": "unique event id", "day": 1, "type": "transport|transfer|visit|meal|check_in|check_out|rest|buffer|included_breakfast|luggage_drop|luggage_pickup",
                   "reference_id": "catalog ID", "label": "description", "start_at": "ISO8601 with offset", "end_at": "ISO8601 with offset",
                   "quantity": "required ONLY for transport/transfer/visit/meal", "meal_type": "required ONLY for meal"}],
        "total_cost_kopecks": None, "validation_status": "not_checked", "unresolved_items": []}}
