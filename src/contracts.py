"""Shared serializable contracts. Private evaluation data never belongs here."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
import hashlib
import json


def jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(jsonable(value), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(jsonable(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@dataclass
class RunResult:
    run_id: str
    architecture: str
    scenario_id: str
    snapshot_hash: str
    request_version: int = 1
    status: str = "completed"
    plans: list[dict] = field(default_factory=list)
    explanation: str = ""
    unresolved: list[str] = field(default_factory=list)
    resources: dict = field(default_factory=dict)
    trace: str = ""
    mode: str = "test"

    def to_dict(self) -> dict:
        return jsonable(self)


RESULT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["completed", "no_feasible_plan", "insufficient_data"]},
        "plans_json": {"type": "string", "description": "JSON array of at most five complete ManualPlan objects. Use catalog IDs and schema_version 0.2.0."},
        "explanation": {"type": "string"},
        "unresolved": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "plans_json", "explanation", "unresolved"],
}


def parse_submission(value: dict) -> dict:
    if set(value) != {"status", "plans_json", "explanation", "unresolved"}:
        raise ValueError("Submission has unexpected or missing fields")
    plans = json.loads(value["plans_json"])
    if not isinstance(plans, list) or len(plans) > 5 or not all(isinstance(p, dict) for p in plans):
        raise ValueError("Expected zero to five plan objects")
    if value["status"] not in {"completed", "no_feasible_plan", "insufficient_data"}:
        raise ValueError("Invalid result status")
    if not isinstance(value["explanation"], str) or not isinstance(value["unresolved"], list) or not all(isinstance(x, str) for x in value["unresolved"]):
        raise ValueError("Invalid explanation or unresolved items")
    if value["status"] != "completed" and plans:
        raise ValueError("Only completed submissions may contain plans")
    return {**value, "plans": plans}
