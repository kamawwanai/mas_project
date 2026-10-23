"""Atomic local tools; no route generator, repair solver or private evaluator."""
from __future__ import annotations
import json
import time

from .contracts import jsonable, digest
from .checks import calculate_cost, validate_trip
from .schemas import parse_plan
from .runtime import LimitExceeded


def function(name, description, properties, required):
    return {"type": "function", "name": name, "description": description, "strict": True,
            "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}


class ToolRegistry:
    def __init__(self, catalogs, request, runtime, simulator=None, *, context=None):
        self.context = context
        self.catalogs, self.request, self.runtime, self.simulator = catalogs, request, runtime, simulator
        self.candidates = []
        self.verification_receipts = set()
        self.revealed = {}
        self.rows = {kind: [self._row(item) for item in doc.items] for kind, doc in catalogs.items()}

    @staticmethod
    def _row(item):
        row = jsonable(item)
        row.update(row.pop("source"))
        return row

    @property
    def definitions(self):
        definitions = []
        search_help = {
            "transport": ("Allowed filters: direction ('outbound' or 'return'), service_date (YYYY-MM-DD), "
                          "mode, seat_class, id. Use direction='outbound' for origin to destination and "
                          "direction='return' for destination to origin; do not write city names in direction."),
            "stays": ("Allowed filters: city, check_in_date, check_out_date, availability_status, room_type, id. "
                      "Copy city exactly from request.destination_city (for dev_01: 'Нижний Новгород'). "
                      "Do not use service_date, available_from or available_to."),
            "places": ("Allowed filters: city, category, availability_status, booking_required, ticket_required, id. "
                       "Copy city exactly from request.destination_city. Categories are 'museum', 'site', 'park', "
                       "or 'church'. Food is in search_food, not category='local_food'."),
            "food": ("Allowed filters: city, reservation_required, price_basis, id. Copy city exactly from "
                     "request.destination_city. Do not use category, service_date or available_date."),
            "transfers": ("Allowed filters: from_id, to_id, mode, applicable_date, availability_status, id. "
                          "Use exact catalog IDs for endpoints."),
        }
        for kind in ("transport", "stays", "places", "food", "transfers"):
            definitions.append(function(f"search_{kind}",
                "Filter saved facts by exact field values. " + search_help[kind] +
                " Start with {} or one documented filter when unsure; never invent fields or translate saved values.",
                {"filters_json": {"type": "string", "description": search_help[kind]},
                 "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, ["filters_json", "offset", "limit"]))
        definitions += [
            function("get_item", "Get one uniquely identified saved object.", {"id": {"type": "string"}}, ["id"]),
            function("get_reviews", "Get only saved review sources and supported attributes.", {"id": {"type": "string"}}, ["id"]),
            function("calculate_cost", "Compute the proposed plan cost; does not build or repair it.", {"plan_json": {"type": "string"}}, ["plan_json"]),
            function("validate_plan", "Check the proposed complete plan against public hard constraints and evidence.", {"plan_json": {"type": "string"}}, ["plan_json"]),
            function("ask_user", "Ask ONE preference topic; global maximum three distinct topics. Never repeat a topic: unknown is a final answer. Hard constraints cannot be changed.",
                     {"topic": {"type": "string", "enum": ["interests", "pace", "comfort", "economy", "quiet", "location"]}, "text": {"type": "string"},
                      "options": {"type": "array", "items": {"type": "string"}}}, ["topic", "text", "options"]),
        ]
        return definitions

    def verification_key(self, plan):
        return (digest(plan), digest(self.request), digest(self.catalogs))

    def _validate(self, name, arguments):
        definition = next((d for d in self.definitions if d["name"] == name), None)
        if definition is None:
            raise ValueError("Unknown tool")
        properties = definition["parameters"]["properties"]
        if not isinstance(arguments, dict) or set(arguments) != set(properties):
            raise ValueError("Tool arguments must exactly match schema")
        for key, value in arguments.items():
            rule = properties[key]
            expected = {"integer": int, "string": str, "array": list}[rule["type"]]
            if type(value) is not expected:
                raise ValueError(f"Wrong type for {key}")
            if "enum" in rule and value not in rule["enum"]:
                raise ValueError(f"Unsupported {key}")
            if expected is int and not rule.get("minimum", value) <= value <= rule.get("maximum", value):
                raise ValueError(f"Out of range {key}")
            if expected is list and not all(isinstance(v, str) for v in value):
                raise ValueError("Options must be strings")

    def execute(self, name, arguments, actor="planner"):
        self.runtime.consume("tools")
        parent = self.runtime.event(actor, "tool_call", name=name, arguments=arguments)
        start = time.monotonic()
        try:
            self._validate(name, arguments)
            value = self._execute(name, arguments, actor)
        except LimitExceeded:
            raise
        except (ValueError, TypeError, KeyError) as exc:
            self.runtime.counts["errors"] += 1
            value = {"error": type(exc).__name__, "message": str(exc)}
        self.runtime.event(actor, "tool_result", parent_id=parent, name=name, result=value, duration=time.monotonic()-start)
        return value

    def _execute(self, name, args, actor="planner"):
        if name.startswith("search_"):
            kind = name.removeprefix("search_")
            filters = json.loads(args["filters_json"])
            if not isinstance(filters, dict):
                raise ValueError("filters_json must be an object")
            fields = set().union(*(set(r) for r in self.rows[kind])) if self.rows[kind] else set()
            unknown = sorted(set(filters) - fields)
            if unknown:
                raise ValueError(f"Unknown filter fields {unknown}; allowed fields: {sorted(fields)}")
            matches = sorted((r for r in self.rows[kind] if all(r.get(k) == v for k, v in filters.items())), key=lambda r: r["id"])
            return {"total": len(matches), "items": matches[args["offset"]:args["offset"]+args["limit"]]}
        if name in {"get_item", "get_reviews"}:
            matches = [r for rows in self.rows.values() for r in rows if r["id"] == args["id"]]
            if len(matches) != 1:
                raise ValueError("Object not found or ambiguous")
            return matches[0] if name == "get_item" else {"id": args["id"], "reviews": matches[0]["reviews"]}
        if name == "ask_user":
            self.runtime.consume("questions")
            value = self.simulator.answer(args) if self.simulator else {"status": "unknown"}
            if value["status"] == "answered":
                self.revealed[args["topic"]] = value["answer"]
            return value
        plan = parse_plan(json.loads(args["plan_json"]))
        if name == "calculate_cost":
            return calculate_cost(self.catalogs, self.request, plan, context=self.context)
        value = validate_trip(self.catalogs, self.request, plan, context=self.context).to_dict()
        raw = json.loads(args["plan_json"])
        self.candidates.append({"plan": raw, "valid": value["exit_code"] == 0})
        if actor == "verification" and value["exit_code"] == 0:
            self.verification_receipts.add(self.verification_key(raw))
        return value
