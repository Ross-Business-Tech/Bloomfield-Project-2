"""Shared helpers for Part B tests: a database with Part B's tables, and a settings dict."""
import copy

import pytest

from llm import cost
from llm.settings import load_reader_config
from scout_reader_schema import create_scout_reader_tables


def make_cfg(**overrides):
    """The real reader.yaml with a few values changed for a test (top-level keys only)."""
    cfg = copy.deepcopy(load_reader_config())
    cfg.update(overrides)
    return cfg


@pytest.fixture
def bcon(con):
    """Part A's in-memory database plus Part B's tables."""
    create_scout_reader_tables(con)
    cost.reset_run_spend()
    return con


# ---------------------------------------------------------------------------
# A pretend AI service, for tests that run the reader without an API key or network.
# ---------------------------------------------------------------------------
import json  # noqa: E402

from llm import client  # noqa: E402


class FakeLLM:
    """
    Set `.reply` to a dict/str (same answer every time) or a function(user_text) -> dict/str,
    or put answers in `.queue` to use them one at a time. `.calls` records every request.
    """

    def __init__(self):
        self.reply = {}
        self.queue = []
        self.status = 200
        self.calls = []
        self.requests = []          # (url, headers, payload) for every call

    def __call__(self, url, headers, payload, timeout):
        self.requests.append((url, headers, payload))
        if "generateContent" in url:                                    # Gemini
            user_text = payload["contents"][0]["parts"][0]["text"]
        elif "system" in payload:                                       # Anthropic
            user_text = payload["messages"][0]["content"]
        else:                                                           # OpenAI-style (system is message 0)
            user_text = payload["messages"][-1]["content"]
        self.calls.append(user_text)
        if self.status != 200:
            return self.status, {"error": {"message": "fake failure"}}, {}
        if self.queue:
            r = self.queue.pop(0)
        else:
            r = self.reply(user_text) if callable(self.reply) else self.reply
        text = r if isinstance(r, str) else json.dumps(r)
        if "generateContent" in url:
            return 200, {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
                         "usageMetadata": {"promptTokenCount": 200, "candidatesTokenCount": 100,
                                           "totalTokenCount": 300}}, {}
        if "chat/completions" in url:
            return 200, {"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 200, "completion_tokens": 100}}, {}
        return 200, {"content": [{"type": "text", "text": text}],
                     "usage": {"input_tokens": 200, "output_tokens": 100},
                     "stop_reason": "end_turn"}, {}


@pytest.fixture
def fake_llm(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    for name in ("LLM_PROVIDER", "LLM_API_KEY", "LLM_MODEL", "LLM_MODEL_STRONG", "LLM_MODEL_CHEAP",
                 "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "LLM_BASE_URL", "GEMINI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(client, "_sleep", lambda s: None)
    cost.reset_run_spend()
    fake = FakeLLM()
    monkeypatch.setattr(client, "_post_json", fake)
    return fake
