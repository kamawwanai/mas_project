import json
from .common import session, public_task, SUMMARY_SCHEMA
from ..tools import function

SPECIALISTS = {"transport", "stay", "places", "logistics", "verification"}


class MAS:
    def run(self, client, registry):
        histories = {role: [] for role in SPECIALISTS}
        delegated = set()
        base_task = public_task(registry)
        task = base_task
        delegate = function("delegate", "Assign an addressed subtask to a specialist. Specialist decides its own tool sequence and returns proposals/evidence in a summary.",
            {"specialist": {"type": "string", "enum": sorted(SPECIALISTS)}, "task": {"type": "string"}}, ["specialist", "task"])

        def dispatch(name, args):
            if not isinstance(args, dict) or set(args) != {"specialist", "task"} or args["specialist"] not in SPECIALISTS or not isinstance(args["task"], str):
                raise ValueError("Invalid addressed subtask")
            registry.runtime.consume("tools")
            role = args["specialist"]
            delegated.add(role)
            parent = registry.runtime.event("coordinator", "delegation", specialist=role, task=args["task"])
            value = session(client, registry, role, {**base_task, "subtask": args["task"]}, schema=SUMMARY_SCHEMA, history=histories[role])
            registry.runtime.event(role, "specialist_result", parent_id=parent, result=value)
            return value

        history = []
        while True:
            result = session(client, registry, "coordinator", task, history=history, extra_tools=[delegate], dispatch=dispatch)
            missing = SPECIALISTS-delegated
            if result["status"] != "completed":
                return result
            if missing:
                task = {"instruction": "Before completing, request the missing specialties", "missing": sorted(missing)}
                continue
            unchecked = [p for p in result["plans"] if registry.verification_key(p) not in registry.verification_receipts]
            if unchecked:
                verification = dispatch("delegate", {"specialist": "verification", "task":
                    "Validate EACH exact candidate using validate_plan. Do not merely assert verification. Candidates: " + json.dumps(unchecked)})
                unchecked = [p for p in result["plans"] if registry.verification_key(p) not in registry.verification_receipts]
                if unchecked:
                    task = {"instruction": "Final candidates lack successful verification tool receipts. Repair the issues, then reverify exact repaired candidates.",
                            "candidates": unchecked, "verification_feedback": verification}
                    continue
            return result
