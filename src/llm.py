"""Lazy OpenAI adapter. Constructing a client never performs a request."""
from __future__ import annotations
from dataclasses import dataclass, field
from .runtime import LimitExceeded
import json
import math
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
        runtime.reserve_llm(0)
        runtime.counts["llm_calls"] += 1
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
        provider = config.get("provider", "openai")
        transport = config.get("transport", "responses")
        if provider not in {"openai", "openrouter"}:
            raise APIUnavailable("Unsupported LLM provider")
        expected = ({"transport": "chat_completions", "base_url": "https://openrouter.ai/api/v1",
                     "api_key_env": "OPENROUTER_API_KEY"} if provider == "openrouter" else
                    {"transport": "responses", "base_url": None, "api_key_env": "OPENAI_API_KEY"})
        for key, value in expected.items():
            if config.get(key, value) != value:
                raise APIUnavailable(f"Invalid {provider} {key}")
        key_env = expected["api_key_env"]
        api_key = os.environ.get(key_env)
        if not api_key:
            raise APIUnavailable(f"{key_env} is required; no simulated fallback")
        if set(config.get("generation", {})) - {"temperature", "top_p", "reasoning"}:
            raise APIUnavailable("Generation config may only contain temperature, top_p, reasoning")
        timeout = config.get("timeout_seconds", 60)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise APIUnavailable("timeout_seconds must be positive and finite")
        attempts = config.get("max_attempts", 3)
        backoff = config.get("retry_backoff_seconds", 1)
        if type(attempts) is not int or not 1 <= attempts <= 3:
            raise APIUnavailable("max_attempts must be an integer from one to three")
        if type(backoff) not in (int, float) or not math.isfinite(backoff) or not 0 <= backoff <= 10:
            raise APIUnavailable("retry_backoff_seconds must be finite and between zero and ten")
        routing = config.get("provider_routing", {})
        if provider == "openrouter":
            if set(routing) - {"order", "allow_fallbacks", "require_parameters"}:
                raise APIUnavailable("Unsupported OpenRouter provider routing option")
            if (not isinstance(routing.get("order"), list) or
                    not routing["order"] or not all(isinstance(v, str) and v for v in routing["order"]) or
                    type(routing.get("allow_fallbacks")) is not bool or
                    type(routing.get("require_parameters")) is not bool):
                raise APIUnavailable("OpenRouter routing must explicitly define order, fallbacks and parameter support")
        try:
            import openai
        except ImportError as exc:
            raise APIUnavailable("Install the api dependency: pip install -e '.[api]'") from exc
        self.sdk = openai
        client_args = {"api_key": api_key, "max_retries": 0, "timeout": timeout}
        if expected["base_url"]:
            client_args["base_url"] = expected["base_url"]
        self.client = openai.OpenAI(**client_args)
        self.config = config
        self.provider = provider
        self.transport = transport

    @staticmethod
    def _chat_tools(tools):
        return [{"type": "function", "function": {key: tool[key] for key in
                 ("name", "description", "parameters", "strict") if key in tool}} for tool in tools]

    @staticmethod
    def _chat_messages(messages):
        result = []
        index = 0
        while index < len(messages):
            item = messages[index]
            if item.get("type") == "function_call":
                calls = []
                while index < len(messages) and messages[index].get("type") == "function_call":
                    call = messages[index]
                    calls.append({"id": call["call_id"], "type": "function",
                                  "function": {"name": call["name"], "arguments": call["arguments"]}})
                    index += 1
                result.append({"role": "assistant", "content": None, "tool_calls": calls})
                continue
            if item.get("type") == "function_call_output":
                result.append({"role": "tool", "tool_call_id": item["call_id"], "content": item["output"]})
            elif item.get("role") in {"system", "user", "assistant"}:
                result.append(item)
            else:
                raise APIUnavailable("Unsupported message shape for OpenRouter chat transport")
            index += 1
        return result

    @staticmethod
    def _chat_usage(usage):
        if not isinstance(usage, dict):
            return None
        details = usage.get("prompt_tokens_details") or {}
        return {"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"),
                "input_tokens_details": {"cached_tokens": details.get("cached_tokens", 0)}}

    def _request(self, messages, tools, schema, maximum, timeout):
        generation = self.config.get("generation", {})
        if self.transport == "responses":
            args = {"model": self.config["model"], "input": messages, "tools": tools,
                    "max_output_tokens": maximum, "store": False, "truncation": "disabled", **generation}
            if schema:
                args["text"] = {"format": {"type": "json_schema", "name": "result", "strict": True, "schema": schema}}
            response = self.client.responses.create(**args, timeout=timeout)
            value = response.model_dump(mode="json")
            return response, value, value.get("usage"), value.get("output"), response.output_text, value.get("status")
        args = {"model": self.config["model"], "messages": self._chat_messages(messages),
                "tools": self._chat_tools(tools), "max_tokens": maximum, "stream": False, **generation,
                "extra_body": {"provider": self.config["provider_routing"]}}
        if schema:
            args["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "result", "strict": True, "schema": schema}}
        response = self.client.chat.completions.create(**args, timeout=timeout)
        value = response.model_dump(mode="json")
        choices = value.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ValueError("OpenRouter response must contain exactly one choice")
        message = choices[0].get("message") or {}
        output = [{"type": "function_call", "name": call["function"]["name"],
                   "arguments": call["function"]["arguments"], "call_id": call["id"]}
                  for call in message.get("tool_calls") or []]
        text = message.get("content") or ""
        if text and not output:
            output = [{"role": "assistant", "content": text}]
        status = "completed" if choices[0].get("finish_reason") in {"stop", "tool_calls"} else choices[0].get("finish_reason")
        return response, value, self._chat_usage(value.get("usage")), output, text, status

    def generate(self, messages, tools, runtime, actor, schema=None):
        # UTF-8 bytes are a conservative reservation, not reported as actual usage.
        reservation = len(json.dumps([messages, tools, schema], ensure_ascii=False).encode("utf-8")) + 1024
        attempts = self.config.get("max_attempts", 3)
        for attempt in range(attempts):
            maximum = runtime.reserve_llm(reservation)
            if attempt == 0:
                runtime.counts["llm_calls"] += 1
            start = time.monotonic()
            parent = runtime.event(actor, "llm_request", attempt=attempt+1, provider=self.provider,
                                   transport=self.transport, model=self.config["model"], input=messages, tools=tools)
            accounted = False
            try:
                remaining = runtime.limits.seconds - (time.monotonic()-runtime.started)
                if remaining <= 0:
                    raise LimitExceeded("wall_time")
                response, value, usage, output, text, status = self._request(
                    messages, tools, schema, maximum, min(self.config.get("timeout_seconds", 60), remaining))
                # Account a received response before inspecting optional/protocol fields.
                runtime.record_usage(usage)
                accounted = True
                runtime.event(actor, "llm_response", parent_id=parent, duration=time.monotonic()-start,
                              response_id=response.id, model=value.get("model"), output=output,
                              usage=usage, status=status)
                if status != "completed":
                    raise APIUnavailable("Provider response did not complete")
                return Reply(output, text, usage, value.get("model", self.config["model"]))
            except LimitExceeded:
                raise
            except Exception as exc:
                runtime.counts["errors"] += 1
                runtime.event(actor, "llm_error", parent_id=parent, error_type=type(exc).__name__, attempt=attempt+1)
                # Only explicit rate rejection / client HTTP rejection establishes
                # no generation. Transport, server and decoding failures may bill.
                status = getattr(exc, "status_code", 0) or 0
                rejected = isinstance(exc, self.sdk.RateLimitError) or status in {400, 401, 403, 404, 413, 422, 429}
                if not accounted and not rejected:
                    runtime.mark_unknown_usage()
                retryable = isinstance(exc, self.sdk.RateLimitError) and not accounted
                if not retryable or attempt == attempts-1:
                    raise
                time.sleep(self.config.get("retry_backoff_seconds", 1)*(attempt+1))
