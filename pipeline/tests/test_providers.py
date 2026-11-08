"""
Tests that the AI wrapper and the Reader work with Google Gemini (and still with Anthropic / OpenAI-style).
The web service is faked, so these check OUR side: what we send, how we read the answer, and the edge cases.
Run llm/check.py with your own key for the real thing.
"""
import json
import re
from pathlib import Path

import pytest

from llm import check as llm_check
from llm import client, cost
from llm.settings import load_reader_config
from reader import batch
from tests.b_fixtures import bcon, fake_llm, make_cfg  # noqa: F401
from tests.test_reader_steps import add_raw, lien_reply, rows

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"


@pytest.fixture
def gemini(monkeypatch, fake_llm):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-secret-key")
    return fake_llm


def gemini_ok(text='{"a": 1}', prompt=100, out=20, thoughts=0, finish="STOP", parts=None):
    usage = {"promptTokenCount": prompt, "candidatesTokenCount": out, "totalTokenCount": prompt + out + thoughts}
    if thoughts:
        usage["thoughtsTokenCount"] = thoughts
    return 200, {"candidates": [{"content": {"parts": parts or [{"text": text}]}, "finishReason": finish}],
                 "usageMetadata": usage}, {}


class Api:
    """A scripted pretend Gemini: returns the given (status, body, headers) tuples in turn."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append((url, headers, payload))
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def use(monkeypatch, *replies):
    api = Api(*replies)
    monkeypatch.setattr(client, "_post_json", api)
    return api


# ---- what we send --------------------------------------------------------------------------------------
def test_gemini_request_format(gemini, bcon):
    client.call_llm("extract", "SYSTEM RULES", "DOC TEXT", 777, con=bcon, cfg=make_cfg())
    url, headers, payload = gemini.requests[0]
    assert url == GEMINI_URL.format("gemini-3.5-flash")
    assert headers["x-goog-api-key"] == "AIza-test-secret-key" and "AIza" not in url      # key in a header only
    assert "AIza" not in json.dumps(payload)
    assert payload["systemInstruction"] == {"parts": [{"text": "SYSTEM RULES"}]}
    assert payload["contents"] == [{"role": "user", "parts": [{"text": "DOC TEXT"}]}]
    gen = payload["generationConfig"]
    assert gen["maxOutputTokens"] == 777
    assert gen["thinkingConfig"] == {"thinkingBudget": 0}                                 # thinking off by default
    assert gen["responseMimeType"] == "application/json"                                  # extraction must be JSON


def test_json_mode_is_not_used_for_plain_text_summaries(gemini, bcon):
    client.call_llm("summary", "s", "t", 100, con=bcon, cfg=make_cfg())
    assert "responseMimeType" not in gemini.requests[0][2]["generationConfig"]


def test_cheap_and_strong_models_come_from_the_gemini_list(gemini, bcon):
    client.call_llm("relevance", "s", "t", 50, con=bcon, cfg=make_cfg())
    client.call_llm("extract", "s", "t", 50, con=bcon, cfg=make_cfg())
    assert [r[0] for r in gemini.requests] == [GEMINI_URL.format("gemini-3.5-flash-lite"),
                                               GEMINI_URL.format("gemini-3.5-flash")]
    # the two models want different "thinking" settings (found by testing against the real API)
    assert gemini.requests[0][2]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "minimal"}
    assert gemini.requests[1][2]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}


def test_model_names_can_be_overridden_and_a_models_prefix_is_accepted(gemini, bcon, monkeypatch):
    monkeypatch.setenv("LLM_MODEL_STRONG", "models/gemini-2.5-pro")
    client.call_llm("extract", "s", "t", 50, con=bcon, cfg=make_cfg())
    assert gemini.requests[0][0] == GEMINI_URL.format("gemini-2.5-pro")


def test_base_url_can_be_overridden(gemini, bcon, monkeypatch):
    monkeypatch.setenv("GEMINI_BASE_URL", "https://proxy.example.org/v1beta/")
    client.call_llm("extract", "s", "t", 50, con=bcon, cfg=make_cfg())
    assert gemini.requests[0][0].startswith("https://proxy.example.org/v1beta/models/")


def test_switching_provider_never_sends_another_companys_model_name(monkeypatch, bcon, fake_llm):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    client.call_llm("extract", "s", "t", 50, con=bcon, cfg=make_cfg())
    assert fake_llm.requests[0][2]["model"] == "claude-sonnet-5-5"
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    client.call_llm("extract", "s2", "t", 50, con=bcon, cfg=make_cfg())
    assert "gemini" in fake_llm.requests[1][0]


# ---- how we read the answer -------------------------------------------------------------------------------
def test_gemini_answer_and_token_counts_including_thinking(gemini, monkeypatch, bcon):
    use(monkeypatch, gemini_ok("hello", prompt=1_000_000, out=0, thoughts=0))
    r = client.call_llm_detailed("extract", "s", "t", 100, con=bcon, cfg=make_cfg())
    assert (r.text, r.input_tokens, r.output_tokens) == ("hello", 1_000_000, 0)
    assert r.cost_usd == pytest.approx(3.00)                                  # no price listed: the default $3 / million in
    use(monkeypatch, gemini_ok("x", prompt=10, out=20, thoughts=500))         # thinking tokens are billed as output
    r = client.call_llm_detailed("extract", "s", "t2", 100, con=bcon, cfg=make_cfg())
    assert r.output_tokens == 520


def test_thought_summary_parts_are_not_part_of_the_answer(gemini, monkeypatch, bcon):
    use(monkeypatch, gemini_ok(parts=[{"text": "I am thinking...", "thought": True}, {"text": '{"ok": '},
                                      {"text": "true}"}]))
    assert client.call_llm("extract", "s", "t", 100, con=bcon, cfg=make_cfg())[0] == '{"ok": true}'


def test_cut_off_answers_are_flagged_and_not_cached(gemini, monkeypatch, bcon):
    api = use(monkeypatch, gemini_ok('{"a": ', finish="MAX_TOKENS"), gemini_ok())
    r1 = client.call_llm_detailed("extract", "s", "t", 100, con=bcon, file_hash="h", cfg=make_cfg())
    r2 = client.call_llm_detailed("extract", "s", "t", 100, con=bcon, file_hash="h", cfg=make_cfg())
    assert r1.truncated and not r2.truncated and not r2.cached and len(api.calls) == 2


def test_thinking_that_uses_the_whole_budget_shows_up_as_cut_off_not_a_crash(gemini, monkeypatch, bcon):
    use(monkeypatch, (200, {"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}],
                            "usageMetadata": {"promptTokenCount": 50, "thoughtsTokenCount": 2500}}, {}))
    r = client.call_llm_detailed("extract", "s", "t", 2500, con=bcon, cfg=make_cfg())
    assert r.text == "" and r.truncated and r.output_tokens == 2500


def test_a_blocked_prompt_is_a_clear_error_and_is_not_retried(gemini, monkeypatch, bcon):
    api = use(monkeypatch, (200, {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}, {}))
    with pytest.raises(client.LLMError, match="PROHIBITED_CONTENT"):
        client.call_llm("extract", "s", "t", 100, con=bcon, cfg=make_cfg())
    assert len(api.calls) == 1


def test_a_safety_stop_with_no_text_is_a_clear_error(gemini, monkeypatch, bcon):
    api = use(monkeypatch, (200, {"candidates": [{"finishReason": "SAFETY"}]}, {}))
    with pytest.raises(client.LLMError, match="SAFETY"):
        client.call_llm("extract", "s", "t", 100, con=bcon, cfg=make_cfg())
    assert len(api.calls) == 1


# ---- errors and retries -----------------------------------------------------------------------------------------
def test_rate_limit_hint_in_the_error_body_is_honoured(gemini, monkeypatch, bcon):
    waits = []
    monkeypatch.setattr(client, "_sleep", waits.append)
    body = {"error": {"code": 429, "message": "quota", "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                                                      "retryDelay": "34s"}]}}
    api = use(monkeypatch, (429, body, {}), gemini_ok())
    assert client.call_llm("extract", "s", "t", 100, con=bcon, cfg=make_cfg())[0] == '{"a": 1}'
    assert len(api.calls) == 2 and waits and waits[0] >= 34


def test_server_errors_are_retried_up_to_five_times(gemini, monkeypatch, bcon):
    api = use(monkeypatch, (503, {"error": {"message": "overloaded"}}, {}))
    with pytest.raises(client.LLMError, match="5 retries"):
        client.call_llm("extract", "s", "t", 100, con=bcon, cfg=make_cfg())
    assert len(api.calls) == 6


def test_a_bad_key_is_a_clear_error_that_never_shows_the_key(gemini, monkeypatch, bcon):
    api = use(monkeypatch, (400, {"error": {"message": "API key not valid: AIza-test-secret-key. Pass a valid key."}}, {}))
    with pytest.raises(client.LLMError) as err:
        client.call_llm("extract", "s", "t", 100, con=bcon, cfg=make_cfg())
    assert "API key not valid" in str(err.value) and "AIza-test-secret-key" not in str(err.value)
    assert len(api.calls) == 1


@pytest.mark.parametrize("var", ["GEMINI_API_KEY", "GOOGLE_API_KEY", "LLM_API_KEY"])
def test_any_of_the_usual_key_names_works(var, monkeypatch, bcon, fake_llm):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv(var, "k")
    client.call_llm("extract", "s", "t", 50, con=bcon, cfg=make_cfg())
    assert fake_llm.requests[0][1]["x-goog-api-key"] == "k"


def test_missing_gemini_key_names_the_variables_to_set(monkeypatch, bcon, fake_llm):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    with pytest.raises(client.LLMConfigError, match="GEMINI_API_KEY"):
        client.call_llm("extract", "s", "t", 50, con=bcon, cfg=make_cfg())


def test_private_material_is_still_refused_with_gemini(gemini, bcon):
    with pytest.raises(client.RestrictedMaterialError):
        client.call_llm("extract", "s", "t", 50, con=bcon, restricted="trepp", cfg=make_cfg())
    assert gemini.requests == []


def test_the_cost_cap_still_applies_with_gemini(gemini, bcon):
    with pytest.raises(cost.CostCapExceeded):
        client.call_llm("extract", "s", "t" * 100, 1000, con=bcon, cfg=make_cfg(weekly_cost_cap_usd=0.0000001))
    assert gemini.requests == []


# ---- the whole Reader, on every provider ----------------------------------------------------------------------------
@pytest.mark.parametrize("provider,key_var,model,marker", [
    ("anthropic", "ANTHROPIC_API_KEY", "claude-sonnet-5-5", "api.anthropic.com"),
    ("gemini", "GEMINI_API_KEY", "gemini-3.5-flash", "generateContent"),
    ("openai_compatible", "OPENAI_API_KEY", "gpt-4o", "chat/completions"),
])
def test_the_reader_works_end_to_end_on_each_provider(provider, key_var, model, marker, fake_llm, bcon, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", provider)
    monkeypatch.setenv(key_var, "test-key")
    fid = add_raw(bcon)
    fake_llm.reply = lien_reply()
    s = batch.run(con=bcon, progress=False, quiet=True)
    assert s["ok"] == 1 and s["cost_usd"] > 0 and marker in fake_llm.requests[0][0]
    row = bcon.execute("SELECT status, lien_claimant, lien_amount, model_name FROM filing_fields WHERE file_id=?",
                       [fid]).fetchone()
    assert row == ("ok", "Acme HVAC Inc.", 5000, model)
    # the same file read again costs nothing: the cache works the same on every provider
    bcon.execute("DELETE FROM filing_fields")
    s2 = batch.run(con=bcon, progress=False, quiet=True)
    assert s2["cached_calls"] == 1 and len(fake_llm.calls) == 1


def test_bad_json_retry_works_on_gemini(gemini, bcon):
    add_raw(bcon)
    gemini.queue = ["Sorry, here you go:", lien_reply()]
    s = batch.run(con=bcon, progress=False, quiet=True)
    assert s["ok"] == 1 and len(gemini.calls) == 2 and "could not be used" in gemini.calls[1]


# ---- the self-test script ----------------------------------------------------------------------------------------------
def lien_aware_reply(user_text):
    if "CLAIM OF LIEN" in user_text:
        return {"filing_type": "mechanics_lien", "lien_claimant": "Acme Heating & Air FAKE Inc.",
                "lien_amount": 1240000, "lien_work": "HVAC installation", "property_address":
                "1000 Example Campus Way, Ann Arbor, MI 48104", "confidence": "high",
                "quotes": {"lien_claimant": "Acme Heating & Air FAKE Inc.", "lien_amount": "$1,240,000",
                           "lien_work": "HVAC installation",
                           "property_address": "1000 Example Campus Way, Ann Arbor, MI 48104"}}
    return {"ok": True}


def test_self_test_passes_when_everything_works(gemini, capsys):
    gemini.reply = lien_aware_reply
    assert llm_check.main() == 0
    out = capsys.readouterr().out
    assert "Provider: gemini" in out and "gemini-3.5-flash" in out and "All good" in out
    assert "AIza" not in out


def test_self_test_reports_unusable_answers(gemini, capsys):
    gemini.reply = "not json at all"
    assert llm_check.main() == 1
    assert "FAILED" in capsys.readouterr().out


def test_self_test_reports_a_missing_key(monkeypatch, fake_llm, capsys):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    assert llm_check.main() == 1
    assert "GEMINI_API_KEY" in capsys.readouterr().out


# ---- Parts C and D ------------------------------------------------------------------------------------------------------
def test_parts_c_and_d_never_call_an_ai():
    """The matcher and scorer are plain code, so they work the same whichever AI company is chosen."""
    root = Path(__file__).resolve().parents[1]
    for pkg in ("matcher", "scorer"):
        for path in (root / pkg).glob("*.py"):
            text = path.read_text()
            assert not re.search(r"^\s*(from|import)\s+llm\b", text, re.M), path
            assert "call_llm" not in text, path


def test_an_unpriced_model_warns_only_once(gemini, bcon, caplog):
    cost._warned_models.clear()
    with caplog.at_level("WARNING"):
        for i in range(3):
            client.call_llm("extract", "s", f"t{i}", 50, con=bcon, cfg=make_cfg())
    assert sum("no price listed" in m for m in caplog.messages) == 1


def test_every_provider_has_models_and_prices_in_the_shipped_config():
    cfg = load_reader_config()
    for provider in client.PROVIDERS:
        assert set(cfg["models"][provider]) == {"cheap", "strong"}
    assert "default" in cfg["prices_per_million_tokens"]          # models without their own price use this one
