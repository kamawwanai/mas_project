"""Provider protocol tests with an in-memory fake module, no SDK or network."""
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from src.llm import OpenAIClient
from src.runtime import Runtime


class RateLimit(Exception):
    pass


class ConnectionFailure(Exception):
    pass


class TimeoutFailure(ConnectionFailure):
    pass


class LLMAdapterTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
