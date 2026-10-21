"""Lazy OpenAI adapter. Constructing a client never performs a request."""
from __future__ import annotations
from dataclasses import dataclass, field
import json
import os
import time


class APIUnavailable(RuntimeError):
    pass


@dataclass
class Reply:
    output: list[dict] = field(default_factory=list)
    text: str = ""
    usage: dict | None = None
    model: str = "scripted-test-client"


class ScriptedClient:
    """Explicit test double, never a fallback for real experiments."""
    mode = "test"

    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def generate(self, messages, tools, runtime, actor, schema=None):
        runtime.counts["llm_calls"] += 1
        runtime.reserve_llm(0)
        self.requests.append({"messages": json.loads(json.dumps(messages)), "tools": tools, "schema": schema})
        reply = next(self.replies)
        if not isinstance(reply, Reply):
            raise TypeError("Scripted replies must be Reply objects")
        runtime.record_usage(reply.usage or {"input_tokens": 0, "output_tokens": 0})
        runtime.event(actor, "llm_response", mode="test", output=reply.output, text=reply.text, usage=reply.usage)
        return reply


class OpenAIClient:
    mode = "real"

    def __init__(self, config, allow_api=False):
        if not allow_api or not config.get("api_enabled", False):
            raise APIUnavailable("Real API calls are disabled. Supply --allow-api and api_enabled=true only after authorization.")
        if not config.get("model"):
            raise APIUnavailable("Set an exact model ID before real runs")
        if not os.environ.get("OPENAI_API_KEY"):
            raise APIUnavailable("OPENAI_API_KEY is required; no simulated fallback")
        if set(config.get("generation", {})) - {"temperature", "top_p", "reasoning"}:
            raise APIUnavailable("Generation config may only contain temperature, top_p, reasoning")
        try:
            import openai
        except ImportError as exc:
            raise APIUnavailable("Install the api dependency: pip install -e '.[api]'") from exc
        self.sdk = openai
        self.client = openai.OpenAI(max_retries=0, timeout=config.get("timeout_seconds", 60))
        self.config = config

    def generate(self, messages, tools, runtime, actor, schema=None):
        runtime.counts["llm_calls"] += 1
        # UTF-8 bytes are a conservative reservation, not reported as actual usage.
        reservation = len(json.dumps([messages, tools, schema], ensure_ascii=False).encode("utf-8")) + 1024
        for attempt in range(3):
            maximum = runtime.reserve_llm(reservation)
            args = {"model": self.config["model"], "input": messages, "tools": tools,
                    "max_output_tokens": maximum, "store": False, "truncation": "disabled"}
            args.update(self.config.get("generation", {}))
            if schema:
                args["text"] = {"format": {"type": "json_schema", "name": "result", "strict": True, "schema": schema}}
            start = time.monotonic()
            parent = runtime.event(actor, "llm_request", attempt=attempt+1, model=args["model"], input=messages, tools=tools)
            try:
                remaining = runtime.limits.seconds - (time.monotonic()-runtime.started)
                response = self.client.responses.create(**args, timeout=max(.1, min(self.config.get("timeout_seconds", 60), remaining)))
                value = response.model_dump(mode="json")
                runtime.record_usage(value.get("usage"))
                runtime.event(actor, "llm_response", parent_id=parent, duration=time.monotonic()-start,
                              response_id=response.id, model=value.get("model"), output=value["output"],
                              usage=value.get("usage"), status=value.get("status"))
                return Reply(value["output"], response.output_text, value.get("usage"), value.get("model", args["model"]))
            except Exception as exc:
                runtime.counts["errors"] += 1
                runtime.event(actor, "llm_error", parent_id=parent, error_type=type(exc).__name__, attempt=attempt+1)
                # A timed-out request might have been billed. Never report its cost as zero.
                if isinstance(exc, (self.sdk.APITimeoutError, self.sdk.APIConnectionError)) or getattr(exc, "status_code", 0) >= 500:
                    runtime.counts["usage_complete"] = False
                retryable = isinstance(exc, (self.sdk.RateLimitError, self.sdk.APIConnectionError)) or getattr(exc, "status_code", 0) >= 500
                if not retryable or attempt == 2:
                    raise
                time.sleep(attempt+1)
