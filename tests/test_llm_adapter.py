"""Provider protocol tests with an in-memory fake module, no SDK or network."""
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from src.llm import OpenAIClient, APIUnavailable
from src.runtime import Runtime, Limits


class RateLimit(Exception):
    pass


class ConnectionFailure(Exception):
    pass


class TimeoutFailure(ConnectionFailure):
    pass


class LLMAdapterTests(unittest.TestCase):
    @staticmethod
    def openrouter_config():
        return {"api_enabled": True, "provider": "openrouter", "transport": "chat_completions",
                "base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY",
                "model": "openai/gpt-4.1-mini", "timeout_seconds": 120, "max_attempts": 3,
                "retry_backoff_seconds": 1, "generation": {"temperature": 0.0},
                "provider_routing": {"order": ["openai"], "allow_fallbacks": False,
                                     "require_parameters": True}}

    def test_function_protocol_usage_and_bounded_retry_without_network(self):
        response = SimpleNamespace(id="fake-response", output_text='{"summary":"test"}', model_dump=lambda **kw: {
            "model": "test-model", "output": [], "status": "completed",
            "usage": {"input_tokens": 20, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 10}}})
        create = Mock(side_effect=[RateLimit(), response])
        constructor = Mock(return_value=SimpleNamespace(responses=SimpleNamespace(create=create)))
        sdk = SimpleNamespace(OpenAI=constructor, RateLimitError=RateLimit, APIConnectionError=ConnectionFailure, APITimeoutError=TimeoutFailure)
        with patch.dict("sys.modules", {"openai": sdk}), patch.dict("os.environ", {"OPENAI_API_KEY": "test-only-never-network"}), patch("src.llm.time.sleep") as sleep:
            client = OpenAIClient({"api_enabled": True, "model": "test-model"}, allow_api=True)
            runtime = Runtime("test", pricing={"input_per_million": 1, "cached_input_per_million": .5, "output_per_million": 2})
            reply = client.generate([], [], runtime, "worker")
        self.assertEqual(2, runtime.counts["llm_attempts"])
        self.assertEqual(1, runtime.counts["llm_calls"])
        self.assertEqual(20, runtime.counts["input_tokens"])
        self.assertEqual(10, runtime.counts["cached_input_tokens"])
        self.assertEqual(5, runtime.counts["output_tokens"])
        self.assertEqual('"test"', reply.text[-7:-1])
        self.assertFalse(create.call_args.kwargs["store"])
        self.assertEqual(0, constructor.call_args.kwargs["max_retries"])
        sleep.assert_called_once_with(1)

    def test_openrouter_chat_transport_converts_tools_messages_schema_and_usage(self):
        response = SimpleNamespace(id="openrouter-fixture", model_dump=lambda **kw: {
            "model": "openai/gpt-4.1-mini", "choices": [{"finish_reason": "tool_calls", "message": {
                "content": None, "tool_calls": [{"id": "call_2", "type": "function",
                    "function": {"name": "lookup", "arguments": '{"id":"x"}'}}]}}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 7,
                      "prompt_tokens_details": {"cached_tokens": 5}}})
        create = Mock(return_value=response)
        constructor = Mock(return_value=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
        sdk = SimpleNamespace(OpenAI=constructor, RateLimitError=RateLimit,
                              APIConnectionError=ConnectionFailure, APITimeoutError=TimeoutFailure)
        messages = [{"role": "system", "content": "fixture"},
                    {"type": "function_call", "name": "lookup", "arguments": '{"id":"old"}', "call_id": "call_1"},
                    {"type": "function_call_output", "call_id": "call_1", "output": '{"ok":true}'}]
        tools = [{"type": "function", "name": "lookup", "description": "fixture lookup", "strict": True,
                  "parameters": {"type": "object", "properties": {"id": {"type": "string"}},
                                 "required": ["id"], "additionalProperties": False}}]
        schema = {"type": "object", "properties": {"done": {"type": "boolean"}},
                  "required": ["done"], "additionalProperties": False}
        with patch.dict("sys.modules", {"openai": sdk}), patch.dict("os.environ", {"OPENROUTER_API_KEY": "fixture-only"}, clear=True):
            client = OpenAIClient(self.openrouter_config(), allow_api=True)
            runtime = Runtime("test", limits=Limits(cost_usd=1),
                              pricing={"input_per_million": .4, "cached_input_per_million": .1, "output_per_million": 1.6})
            reply = client.generate(messages, tools, runtime, "worker", schema)
        self.assertEqual("lookup", reply.output[0]["name"])
        self.assertEqual(30, runtime.counts["input_tokens"])
        self.assertEqual(5, runtime.counts["cached_input_tokens"])
        self.assertEqual(7, runtime.counts["output_tokens"])
        self.assertEqual("https://openrouter.ai/api/v1", constructor.call_args.kwargs["base_url"])
        sent = create.call_args.kwargs
        self.assertEqual("lookup", sent["tools"][0]["function"]["name"])
        self.assertEqual("assistant", sent["messages"][1]["role"])
        self.assertEqual("tool", sent["messages"][2]["role"])
        self.assertEqual("json_schema", sent["response_format"]["type"])
        self.assertEqual({"order": ["openai"], "allow_fallbacks": False,
                          "require_parameters": True}, sent["extra_body"]["provider"])
        self.assertEqual(0.0, sent["temperature"])

    def test_openrouter_requires_its_key_and_locked_endpoint(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(APIUnavailable, "OPENROUTER_API_KEY"):
                OpenAIClient(self.openrouter_config(), allow_api=True)
        for change in ({"base_url": "https://example.test"}, {"transport": "responses"},
                       {"api_key_env": "SOME_OTHER_KEY"}):
            config = {**self.openrouter_config(), **change}
            with patch.dict("os.environ", {"OPENROUTER_API_KEY": "fixture-only"}, clear=True):
                with self.assertRaises(APIUnavailable):
                    OpenAIClient(config, allow_api=True)


if __name__ == "__main__":
    unittest.main()
