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
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")      # the project default is OpenAI; these tests use pretend Anthropic answers
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
    assert api.calls[0][2]["model"] == cfg["models"]["anthropic"]["cheap"]
    assert api.calls[1][2]["model"] == cfg["models"]["anthropic"]["strong"]


def test_env_can_override_models_and_provider(bcon, monkeypatch):
    monkeypatch.setenv("LLM_MODEL_STRONG", "my-strong-model")
    api = use_api(monkeypatch, [ok_anthropic()])
    cfg = client.load_reader_config()   # an overridden model needs an exact (test) price, like any model
    cfg["prices_per_million_tokens"]["my-strong-model"] = {"input": 1.0, "output": 5.0}
    client.call_llm("extract", "s", "t", 100, con=bcon, cfg=cfg)
    assert api.calls[0][2]["model"] == "my-strong-model"


def _retry_cfg(n):
    cfg = client.load_reader_config()
    cfg.setdefault("llm", {})["max_retries"] = n       # exercise the mechanism, not the shipped default
    return cfg


def test_retries_rate_limits_then_succeeds(bcon, monkeypatch):
    api = use_api(monkeypatch, [(429, {"error": {"message": "slow down"}}, {"retry-after": "1"}),
                                (503, {}, {}), requests.Timeout("t"), ok_anthropic()])
    text, *_ = client.call_llm("extract", "s", "t", 100, con=bcon, cfg=_retry_cfg(5))
    assert text == '{"a": 1}' and len(api.calls) == 4


def test_gives_up_after_five_retries(bcon, monkeypatch):
    api = use_api(monkeypatch, [(500, {}, {})])
    with pytest.raises(client.LLMError, match="5 retries"):
        client.call_llm("extract", "s", "t", 100, con=bcon, cfg=_retry_cfg(5))
    assert len(api.calls) == 6          # first try + 5 retries


def test_shipped_retry_default_is_zero_for_money_safety():
    cfg = client.load_reader_config()
    assert int((cfg.get("llm") or {}).get("max_retries", 5)) == 0   # no unaccounted retry attempts
    assert float(cfg.get("weekly_cost_cap_usd", 10)) == 9.0         # user's "under $10" ceiling minus margin


def test_a_sent_request_with_no_answer_is_charged_at_worst_case(bcon, monkeypatch):
    api = use_api(monkeypatch, [requests.Timeout("t")])
    cfg = client.load_reader_config()
    with pytest.raises(client.LLMError):
        client.call_llm("extract", "s", "t" * 400, 1000, con=bcon, cfg=cfg)
    rows = bcon.execute("SELECT input_tokens, output_tokens, file_hash FROM llm_usage").fetchall()
    assert rows, "an answered-or-not request must never vanish from the ledger"
    assert rows[0][2] == "unknown-usage-worst-case"
    assert rows[0][1] == 1000                       # full max output charged


def test_an_answered_call_without_usage_numbers_is_charged_at_worst_case(bcon, monkeypatch):
    # The service answered 200 but its body carried NO usage block: we cannot prove what
    # it used, so it must be charged at the full worst case, never at a guess.
    use_api(monkeypatch, [(200, {"content": [{"type": "text", "text": '{"a": 1}'}],
                                 "stop_reason": "end_turn"}, {})])
    text, i, o = client.call_llm("extract", "sys", "t" * 400, 1000, con=bcon,
                                 file_hash="h1", prompt_version="v9")
    assert text == '{"a": 1}'
    rows = bcon.execute("SELECT input_tokens, output_tokens, file_hash FROM llm_usage").fetchall()
    assert len(rows) == 1 and rows[0][2] == "unknown-usage-worst-case"
    assert rows[0][0] == cost.estimate_tokens("sys" + "t" * 400) and rows[0][1] == 1000
    assert (i, o) == (rows[0][0], 1000)


def test_unknown_model_is_refused_until_it_has_an_exact_price():
    # fail-closed: with dollars at stake an unpriced model is rejected, never guessed
    cfg = client.load_reader_config()
    with pytest.raises(ValueError, match="no exact price"):
        cost.get_prices("some-model-not-in-the-table", cfg)
    # a listed model still gets its own price (the verified U-M rate here)
    gpin, gpout = cost.get_prices("gpt-4o-mini", cfg)
    assert (gpin, gpout) == (0.15 / 1_000_000, 0.60 / 1_000_000)


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


def test_bloomfield_material_is_never_sent_by_default(bcon, monkeypatch):
    api = use_api(monkeypatch, [ok_anthropic()])
    for kind in ("bloomfield",):
        with pytest.raises(client.RestrictedMaterialError):
            client.call_llm("extract", "s", "t", 100, con=bcon, restricted=kind)
    assert api.calls == []
    # only an explicit config switch lets it through
    client.call_llm("extract", "s", "t", 100, con=bcon, restricted="bloomfield", cfg=make_cfg(bloomfield_material_ai_allowed=True))
    assert len(api.calls) == 1


# ---- cost ----
def test_cost_math_and_usage_log(bcon, monkeypatch):
    use_api(monkeypatch, [ok_anthropic(i=1_000_000, o=1_000_000)])
    cfg = make_cfg(prices_per_million_tokens={"claude-haiku-5-5": {"input": 2.0, "output": 10.0},
                                              "claude-sonnet-5-5": {"input": 2.0, "output": 10.0}})
    r = client.call_llm_detailed("extract", "s", "t", 100, con=bcon, cfg=cfg)
    assert r.cost_usd == pytest.approx(12.0)
    assert cost.weekly_spend(bcon) == pytest.approx(12.0)
    assert cost.cost_summary(bcon)[0]["calls"] == 1


def test_estimate_tokens_is_chars_over_four():
    assert cost.estimate_tokens("a" * 400, make_cfg()) == 100


def test_weekly_advisory_cap_does_not_stop_the_call(bcon, monkeypatch):
    # The provider account enforces its own dollar limit; the code-level cap is advisory only.
    api = use_api(monkeypatch, [ok_anthropic(i=500_000, o=100_000)])
    cfg = make_cfg(weekly_cost_cap_usd=0.01, run_cost_cap_usd=100)
    client.call_llm("extract", "s", "t", 1000, con=bcon, cfg=cfg)
    assert len(api.calls) == 1


def test_advisory_cap_counts_money_already_spent_this_week(bcon, monkeypatch):
    api = use_api(monkeypatch, [ok_anthropic(i=1_000_000, o=0), ok_anthropic(i=1_000, o=10)])
    cfg = make_cfg(weekly_cost_cap_usd=3.5, run_cost_cap_usd=100)
    client.call_llm("extract", "s", "t", 10, con=bcon, cfg=cfg, file_hash="a")
    client.call_llm("extract", "s", "t", 100_000, con=bcon, cfg=cfg, file_hash="b")
    assert len(api.calls) == 2   # spend past the advisory cap is warned about, not blocked


def test_run_advisory_cap_is_separate_from_weekly_cap(bcon, monkeypatch):
    api = use_api(monkeypatch, [ok_anthropic(i=1_000_000, o=0), ok_anthropic(i=1_000, o=10), ok_anthropic(i=1_000, o=10)])
    cfg = make_cfg(weekly_cost_cap_usd=100, run_cost_cap_usd=3.5)
    client.call_llm("extract", "s", "t", 10, con=bcon, cfg=cfg, file_hash="a")
    client.call_llm("extract", "s", "t", 100_000, con=bcon, cfg=cfg, file_hash="b")
    cost.reset_run_spend()                       # a new run starts fresh
    client.call_llm("extract", "s", "t", 10, con=bcon, cfg=cfg, file_hash="c")
    assert len(api.calls) == 3
