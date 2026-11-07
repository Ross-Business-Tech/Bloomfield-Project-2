"""Tests for the AI wrapper and cost tracking. No network and no API key needed."""
import pytest
import requests

from llm import client, cost
from tests.b_fixtures import bcon, make_cfg  # noqa: F401  (bcon is a pytest fixture)


class FakeAPI:
    """Stands in for the AI service. `replies` are returned one at a time."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append((url, headers, payload))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def ok_anthropic(text='{"a": 1}', i=100, o=20, stop="end_turn"):
    return 200, {"content": [{"type": "text", "text": text}], "usage": {"input_tokens": i, "output_tokens": o},
                 "stop_reason": stop}, {}


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-secret-123")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_MODEL_STRONG", raising=False)
    monkeypatch.delenv("LLM_MODEL_CHEAP", raising=False)
    monkeypatch.setattr(client, "_sleep", lambda s: None)
    cost.reset_run_spend()


def use_api(monkeypatch, replies):
    api = FakeAPI(replies)
    monkeypatch.setattr(client, "_post_json", api)
    return api


def test_returns_text_and_token_counts(bcon, monkeypatch):
    use_api(monkeypatch, [ok_anthropic(i=1000, o=200)])
    text, i, o = client.call_llm("extract", "sys", "doc text", 500, con=bcon, file_hash="h1", prompt_version="v2")
    assert (text, i, o) == ('{"a": 1}', 1000, 200)


def test_same_document_twice_makes_one_call(bcon, monkeypatch):
    api = use_api(monkeypatch, [ok_anthropic()])
    first = client.call_llm_detailed("extract", "sys", "doc", 500, con=bcon, file_hash="h1", prompt_version="v2")
    second = client.call_llm_detailed("extract", "sys", "doc", 500, con=bcon, file_hash="h1", prompt_version="v2")
    assert len(api.calls) == 1
    assert not first.cached and second.cached and second.cost_usd == 0 and second.text == first.text
    # different prompt version or retry variant = a new question = a new call
    client.call_llm("extract", "sys", "doc", 500, con=bcon, file_hash="h1", prompt_version="v3")
    client.call_llm("extract", "sys", "doc", 500, con=bcon, file_hash="h1", prompt_version="v2", variant="retry1")
    assert len(api.calls) == 3


def test_task_picks_cheap_or_strong_model(bcon, monkeypatch):
    api = use_api(monkeypatch, [ok_anthropic()])
    cfg = make_cfg()
    client.call_llm("relevance", "s", "t", 100, con=bcon, cfg=cfg)
    client.call_llm("extract", "s", "t", 100, con=bcon, cfg=cfg)
    assert api.calls[0][2]["model"] == cfg["models"]["cheap"]
    assert api.calls[1][2]["model"] == cfg["models"]["strong"]


def test_env_can_override_models_and_provider(bcon, monkeypatch):
    monkeypatch.setenv("LLM_MODEL_STRONG", "my-strong-model")
    api = use_api(monkeypatch, [ok_anthropic()])
    client.call_llm("extract", "s", "t", 100, con=bcon)
    assert api.calls[0][2]["model"] == "my-strong-model"


def test_retries_rate_limits_then_succeeds(bcon, monkeypatch):
    api = use_api(monkeypatch, [(429, {"error": {"message": "slow down"}}, {"retry-after": "1"}),
                                (503, {}, {}), requests.Timeout("t"), ok_anthropic()])
    text, *_ = client.call_llm("extract", "s", "t", 100, con=bcon)
    assert text == '{"a": 1}' and len(api.calls) == 4


def test_gives_up_after_five_retries(bcon, monkeypatch):
    api = use_api(monkeypatch, [(500, {}, {})])
    with pytest.raises(client.LLMError, match="5 retries"):
        client.call_llm("extract", "s", "t", 100, con=bcon)
    assert len(api.calls) == 6          # first try + 5 retries


def test_bad_key_is_not_retried_and_key_never_shown(bcon, monkeypatch):
    api = use_api(monkeypatch, [(401, {"error": {"message": "invalid key sk-test-secret-123"}}, {})])
    with pytest.raises(client.LLMError) as err:
        client.call_llm("extract", "s", "t", 100, con=bcon)
    assert len(api.calls) == 1
    assert "sk-test-secret-123" not in str(err.value)


def test_missing_key_gives_clear_error(bcon, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(client.LLMConfigError, match="ANTHROPIC_API_KEY"):
        client.call_llm("extract", "s", "t", 100, con=bcon)


def test_openai_compatible_provider(bcon, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_API_KEY", "k-abc")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example.org/v1/")
    api = use_api(monkeypatch, [(200, {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                                       "usage": {"prompt_tokens": 12, "completion_tokens": 3}}, {})])
    assert client.call_llm("extract", "sys", "user", 50, con=bcon) == ("hello", 12, 3)
    url, headers, payload = api.calls[0]
    assert url == "https://llm.example.org/v1/chat/completions"
    assert headers["Authorization"] == "Bearer k-abc"
    assert payload["messages"][0] == {"role": "system", "content": "sys"} and payload["max_tokens"] == 50


def test_truncated_answers_are_flagged_and_not_cached(bcon, monkeypatch):
    api = use_api(monkeypatch, [ok_anthropic(text='{"a": ', stop="max_tokens"), ok_anthropic()])
    r1 = client.call_llm_detailed("extract", "s", "t", 100, con=bcon, file_hash="h")
    r2 = client.call_llm_detailed("extract", "s", "t", 100, con=bcon, file_hash="h")
    assert r1.truncated and not r2.cached and len(api.calls) == 2


def test_trepp_and_bloomfield_material_is_never_sent_by_default(bcon, monkeypatch):
    api = use_api(monkeypatch, [ok_anthropic()])
    for kind in ("trepp", "bloomfield"):
        with pytest.raises(client.RestrictedMaterialError):
            client.call_llm("extract", "s", "t", 100, con=bcon, restricted=kind)
    assert api.calls == []
    # only an explicit config switch lets it through
    client.call_llm("extract", "s", "t", 100, con=bcon, restricted="trepp", cfg=make_cfg(trepp_ai_allowed=True))
    assert len(api.calls) == 1


# ---- cost ----
def test_cost_math_and_usage_log(bcon, monkeypatch):
    use_api(monkeypatch, [ok_anthropic(i=1_000_000, o=1_000_000)])
    cfg = make_cfg(prices_per_million_tokens={"default": {"input": 2.0, "output": 10.0}})
    r = client.call_llm_detailed("extract", "s", "t", 100, con=bcon, cfg=cfg)
    assert r.cost_usd == pytest.approx(12.0)
    assert cost.weekly_spend(bcon) == pytest.approx(12.0)
    assert cost.cost_summary(bcon)[0]["calls"] == 1


def test_estimate_tokens_is_chars_over_four():
    assert cost.estimate_tokens("a" * 400, make_cfg()) == 100


def test_weekly_cap_stops_before_calling(bcon, monkeypatch):
    api = use_api(monkeypatch, [ok_anthropic(i=500_000, o=100_000)])
    cfg = make_cfg(weekly_cost_cap_usd=0.01, run_cost_cap_usd=100)
    with pytest.raises(cost.CostCapExceeded, match="weekly AI cost cap"):
        client.call_llm("extract", "s", "t", 1000, con=bcon, cfg=cfg)
    assert api.calls == []


def test_cap_counts_money_already_spent_this_week(bcon, monkeypatch):
    use_api(monkeypatch, [ok_anthropic(i=1_000_000, o=0)])          # costs $3 at the default sonnet price
    cfg = make_cfg(weekly_cost_cap_usd=3.5, run_cost_cap_usd=100)
    client.call_llm("extract", "s", "t", 10, con=bcon, cfg=cfg, file_hash="a")
    with pytest.raises(cost.CostCapExceeded):
        client.call_llm("extract", "s", "t", 100_000, con=bcon, cfg=cfg, file_hash="b")


def test_run_cap_is_separate_from_weekly_cap(bcon, monkeypatch):
    use_api(monkeypatch, [ok_anthropic(i=1_000_000, o=0)])
    cfg = make_cfg(weekly_cost_cap_usd=100, run_cost_cap_usd=3.5)
    client.call_llm("extract", "s", "t", 10, con=bcon, cfg=cfg, file_hash="a")
    with pytest.raises(cost.CostCapExceeded, match="per-run"):
        client.call_llm("extract", "s", "t", 100_000, con=bcon, cfg=cfg, file_hash="b")
    cost.reset_run_spend()                       # a new run starts fresh
    client.call_llm("extract", "s", "t", 10, con=bcon, cfg=cfg, file_hash="c")
