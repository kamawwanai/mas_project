"""Explicit scripted conversations for synthetic integration tests only."""
import json
from src.llm import Reply
from src.architectures.mas import SPECIALISTS
from tests.test_architectures import final_reply, tool_reply


def summary(text="Synthetic fixture evidence"):
    return Reply(text=json.dumps({"summary": text}))


def conversation(architecture, plan, questions=0):
    asks = [tool_reply("ask_user", {"topic": "pace", "text": "Preferred pace?", "options": []}, i+100) for i in range(questions)]
    if architecture == "workflow":
        return [summary(), summary(), *asks, summary(), final_reply(plan)]
    if architecture == "single_agent":
        return [*asks, tool_reply("validate_plan", {"plan_json": json.dumps(plan)}), final_reply(plan)]
    replies = []
    for i, specialist in enumerate(sorted(SPECIALISTS)):
        replies += [tool_reply("delegate", {"specialist": specialist, "task": "Inspect saved synthetic domain facts"}, i)]
        if specialist == "transport":
            replies += asks
        replies += [summary()]
    return replies + [final_reply(plan), tool_reply("validate_plan", {"plan_json": json.dumps(plan)}, 90), summary("Exact candidate validated")]
