"""One-request OpenRouter protocol smoke test. Never run without explicit authorization."""
import argparse
import json
from _bootstrap import ROOT
from src.contracts import read_json
from src.experiments import load_config, validate_pricing
from src.llm import APIUnavailable, OpenAIClient
from src.runtime import Limits, Runtime


def main():
    parser = argparse.ArgumentParser(description="One paid tool-calling request; no trip experiment")
    parser.add_argument("--config", default=str(ROOT / "configs/pilot.example.toml"))
    parser.add_argument("--allow-api", action="store_true")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if not args.allow_api or not config["llm"].get("api_enabled"):
            raise APIUnavailable("Smoke request disabled; it requires both api_enabled=true and --allow-api")
        pricing = read_json(ROOT / config.get("pricing", "configs/pricing.json"))
        validate_pricing(config, pricing)
        runtime = Runtime("openrouter-smoke", limits=Limits(
            llm_attempts=1, input_tokens=4000, output_tokens=256, tools=0, questions=0,
            seconds=180, max_output_tokens=128, cost_usd=.01), pricing=pricing)
        client = OpenAIClient(config["llm"], allow_api=True)
        tool = {"type": "function", "name": "health_check",
                "description": "Return a fixed local health status. Request this tool exactly once.", "strict": True,
                "parameters": {"type": "object", "properties": {"token": {"type": "string", "enum": ["ok"]}},
                               "required": ["token"], "additionalProperties": False}}
        reply = client.generate([{"role": "system", "content": "Call the supplied health_check tool exactly once."},
                                 {"role": "user", "content": "Perform the protocol health check."}],
                                [tool], runtime, "smoke")
        calls = [row for row in reply.output if row.get("type") == "function_call"]
        if len(calls) != 1 or calls[0].get("name") != "health_check" or json.loads(calls[0]["arguments"]) != {"token": "ok"}:
            raise ValueError("Model did not return the expected tool call")
        print(json.dumps({"status": "ok", "provider": config["llm"]["provider"],
                          "model": reply.model, "tool_call": "health_check", "resources": runtime.summary()},
                         ensure_ascii=False, indent=2))
        return 0
    except (APIUnavailable, OSError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "blocked", "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
