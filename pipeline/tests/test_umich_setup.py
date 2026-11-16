from llm.check import setup_problem


def test_umich_correct():
    assert setup_problem("openai_compatible", "https://api.toolkit.umgpt.umich.edu/v1") is None
    assert setup_problem("openai_compatible", "https://api.toolkit.umgpt.umich.edu/v1/") is None


def test_umich_wrong_protocol():
    assert "openai_compatible" in setup_problem("openai_responses", "https://api.toolkit.umgpt.umich.edu/v1")


def test_umich_wrong_path():
    for path in ("", "/", "/v1/chat/completions", "/v1/v1"):
        assert setup_problem("openai_compatible", "https://api.toolkit.umgpt.umich.edu" + path)


def test_other_providers_unchanged():
    assert setup_problem("openai_responses", "https://api.openai.com/v1") is None
    assert setup_problem("anthropic", "") is None


def test_project_defaults_target_umich_without_env(monkeypatch):
    from llm.settings import load_reader_config, provider_name, model_for_task
    from llm.client import _build_openai_compatible
    for key in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL", "LLM_MODEL_CHEAP", "LLM_MODEL_STRONG"):
        monkeypatch.delenv(key, raising=False)
    cfg = load_reader_config()
    assert provider_name(cfg) == "openai_compatible"
    assert model_for_task("relevance", cfg) == "gpt-4o-mini"
    assert model_for_task("extract", cfg) == "gpt-4o"
    url, headers, payload = _build_openai_compatible("gpt-4o-mini", "test", "FAKE", 10, cfg, "test-not-a-key")
    assert url == "https://api.toolkit.umgpt.umich.edu/v1/chat/completions"
    assert headers["Authorization"] == "Bearer test-not-a-key"


def test_other_provider_explicit_override(monkeypatch):
    from llm.settings import load_reader_config, provider_name
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    assert provider_name(load_reader_config()) == "anthropic"
