"""Run-wide resource accounting and append-only action tracing."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
import json
import math
import threading
import time

from .contracts import jsonable


class LimitExceeded(RuntimeError):
    pass


@dataclass
class Limits:
    llm_attempts: int = 40
    input_tokens: int = 150000
    output_tokens: int = 30000
    tools: int = 100
    questions: int = 3
    seconds: int = 600
    max_output_tokens: int = 4000
    cost_usd: float | None = None

    def __post_init__(self):
        for key in ("llm_attempts", "input_tokens", "output_tokens", "tools", "questions"):
            if type(getattr(self, key)) is not int or getattr(self, key) < 0:
                raise ValueError(f"{key} must be a nonnegative integer")
        if self.questions > 3:
            raise ValueError("At most three questions per run")
        if type(self.seconds) not in {int, float} or not math.isfinite(self.seconds) or self.seconds <= 0:
            raise ValueError("seconds must be positive and finite")
        if type(self.max_output_tokens) is not int or self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if self.cost_usd is not None and (type(self.cost_usd) not in {int, float} or not math.isfinite(self.cost_usd) or self.cost_usd <= 0):
            raise ValueError("cost_usd must be positive and finite")


class Runtime:
    def __init__(self, run_id, path=None, limits=None, pricing=None):
        self.run_id = run_id
        self.path = Path(path) if path else None
        self.limits = limits or Limits()
        self.pricing = pricing or {}
        if self.pricing:
            rates = [self.pricing.get(k) for k in ("input_per_million", "cached_input_per_million", "output_per_million")]
            if any(type(v) not in {int, float} or not math.isfinite(v) or v < 0 for v in rates):
                raise ValueError("Pricing must contain finite nonnegative rates")
        self.started = time.monotonic()
        self.events = []
        self.lock = threading.RLock()
        self.counts = dict(llm_calls=0, llm_attempts=0, input_tokens=0, cached_input_tokens=0,
                           output_tokens=0, tools=0, questions=0, errors=0, cost_usd=0.0, usage_complete=True,
                           token_usage_complete=True)

    def check(self):
        if not self.counts["token_usage_complete"]:
            raise LimitExceeded("unknown_billable_usage")
        if time.monotonic() - self.started >= self.limits.seconds:
            raise LimitExceeded("wall_time")
        for key in ("llm_attempts", "input_tokens", "output_tokens", "tools", "questions"):
            if self.counts[key] > getattr(self.limits, key):
                raise LimitExceeded(key)
        if self.limits.cost_usd is not None and not self.counts["usage_complete"]:
            raise LimitExceeded("unknown_billable_usage")
        if self.limits.cost_usd is not None and self.counts["cost_usd"] >= self.limits.cost_usd:
            raise LimitExceeded("cost_usd")

    def consume(self, resource, amount=1):
        with self.lock:
            self.check()
            if self.counts[resource] + amount > getattr(self.limits, resource):
                raise LimitExceeded(resource)
            self.counts[resource] += amount

    def reserve_llm(self, input_tokens):
        with self.lock:
            self.check()
            output = min(self.limits.max_output_tokens, self.limits.output_tokens - self.counts["output_tokens"])
            if output <= 0 or self.counts["input_tokens"] + input_tokens > self.limits.input_tokens:
                raise LimitExceeded("token_reservation")
            if self.limits.cost_usd is not None:
                if not self.pricing:
                    raise ValueError("Pricing required for monetary limit")
                input_rate = max(self.pricing["input_per_million"], self.pricing["cached_input_per_million"])
                reserve = (input_tokens * input_rate + output * self.pricing["output_per_million"]) / 1e6
                if self.counts["cost_usd"] + reserve > self.limits.cost_usd:
                    raise LimitExceeded("cost_reservation")
            self.consume("llm_attempts")
            return output

    def record_usage(self, usage):
        if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0 for k in ("input_tokens", "output_tokens")):
            self.mark_unknown_usage()
            return
        incoming, outgoing = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        details = usage.get("input_tokens_details") or {}
        cached = details.get("cached_tokens", 0) if isinstance(details, dict) else None
        if type(cached) is not int or not 0 <= cached <= incoming:
            self.mark_unknown_usage()
            return
        self.counts["input_tokens"] += incoming
        self.counts["output_tokens"] += outgoing
        self.counts["cached_input_tokens"] += cached
        if self.pricing:
            self.counts["cost_usd"] += ((incoming-cached)*self.pricing["input_per_million"] + cached*self.pricing["cached_input_per_million"] + outgoing*self.pricing["output_per_million"]) / 1e6
        else:
            self.counts["usage_complete"] = False
        self.check()

    def mark_unknown_usage(self):
        self.counts["usage_complete"] = False
        self.counts["token_usage_complete"] = False
        self.check()

    def event(self, actor, kind, **payload):
        with self.lock:
            value = {"run_id": self.run_id, "seq": len(self.events)+1,
                     "at": datetime.now(timezone.utc).isoformat(), "actor": actor, "kind": kind,
                     "parent_id": payload.pop("parent_id", None), **jsonable(payload)}
            self.events.append(value)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(value, ensure_ascii=False) + "\n")
            return value["seq"]

    def summary(self):
        return {**self.counts, "cost_usd": self.counts["cost_usd"] if self.counts["usage_complete"] else None,
                "seconds": time.monotonic()-self.started, "limits": asdict(self.limits)}
