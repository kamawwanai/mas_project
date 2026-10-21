import json
from .common import session, public_task, SUMMARY_SCHEMA


class Workflow:
    def run(self, client, registry):
        context = public_task(registry)
        for stage, allowed in (
            ("analysis", []),
            ("search", [d["name"] for d in registry.definitions if d["name"].startswith("search_") or d["name"] in {"get_item", "get_reviews"}]),
            ("clarification", ["ask_user"]),
        ):
            registry.runtime.event("workflow", "stage", stage=stage)
            context[stage] = session(client, registry, "workflow", {"stage": stage, **context}, schema=SUMMARY_SCHEMA, allowed=allowed)
        registry.runtime.event("workflow", "stage", stage="generation")
        submission = session(client, registry, "workflow", {"stage": "generation", **context}, allowed=[])
        plans = []
        for candidate in submission["plans"]:
            for attempt in range(3):
                registry.runtime.event("workflow", "stage", stage="validation", repair_attempt=attempt)
                report = registry.execute("validate_plan", {"plan_json": json.dumps(candidate)}, "workflow")
                if report.get("exit_code") == 0 or attempt == 2:
                    break
                registry.runtime.event("workflow", "stage", stage="repair", repair_attempt=attempt+1)
                repair = session(client, registry, "workflow", {"stage": "repair", **context, "candidate": candidate, "validation": report,
                    "instruction": "Return one corrected candidate; select replacement facts using tools if needed."})
                if not repair["plans"]:
                    break
                candidate = repair["plans"][0]
            plans.append(candidate)
        registry.runtime.event("workflow", "stage", stage="final")
        return {**submission, "plans": plans}
