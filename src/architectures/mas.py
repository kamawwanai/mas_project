from .common import session, public_task, SUMMARY_SCHEMA
from ..tools import function

SPECIALISTS = {"transport", "stay", "places", "logistics", "verification"}


class MAS:
    def run(self, client, registry):
        histories = {role: [] for role in SPECIALISTS}
        delegated = set()
        task = public_task(registry)
        delegate = function("delegate", "Assign an addressed subtask to a specialist. Specialist decides its own tool sequence and returns proposals/evidence in a summary.",
            {"specialist": {"type": "string", "enum": sorted(SPECIALISTS)}, "task": {"type": "string"}}, ["specialist", "task"])

        def dispatch(name, args):
            if not isinstance(args, dict) or set(args) != {"specialist", "task"} or args["specialist"] not in SPECIALISTS or not isinstance(args["task"], str):
                raise ValueError("Invalid addressed subtask")
            registry.runtime.consume("tools")
            role = args["specialist"]
            delegated.add(role)
            parent = registry.runtime.event("coordinator", "delegation", specialist=role, task=args["task"])
            value = session(client, registry, role, {**task, "subtask": args["task"]}, schema=SUMMARY_SCHEMA, history=histories[role])
            registry.runtime.event(role, "specialist_result", parent_id=parent, result=value)
            return value

        history = []
        while True:
            result = session(client, registry, "coordinator", task, history=history, extra_tools=[delegate], dispatch=dispatch)
            missing = SPECIALISTS-delegated
            if result["status"] != "completed" or not missing:
                return result
            task = {"instruction": "Before completing, request the missing specialties", "missing": sorted(missing)}
