"""The project default for every AI step is U-M Toolkit, with the key read from OPENAI_API_KEY."""
import re
from pathlib import Path

import pytest

from llm import client, settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for n in ("LLM_PROVIDER", "LLM_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(n, raising=False)


def test_the_default_provider_is_umich_compatible():
    assert settings.provider_name(settings.load_reader_config()) == "openai_compatible"


def test_the_key_comes_from_openai_api_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-placeholder-from-env")
    assert client.get_api_key("openai_compatible") == "sk-placeholder-from-env"


def test_the_providers_own_variable_wins_over_the_generic_one(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "generic")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-specific")
    assert client.get_api_key("openai_compatible") == "openai-specific"


def test_a_missing_key_names_openai_api_key():
    with pytest.raises(client.LLMConfigError, match="OPENAI_API_KEY"):
        client.get_api_key("openai_compatible")


def test_no_real_key_is_written_anywhere_in_the_project():
    pattern = re.compile(r"sk-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{20,}")
    for path in ROOT.rglob("*"):
        if path.suffix in {".py", ".yaml", ".md", ".html", ".json", ".example", ".cjs"} and ".venv" not in path.parts \
                and "tests" not in path.parts and path.is_file():
            assert not pattern.search(path.read_text(encoding="utf-8", errors="ignore")), path
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=replace-with-your-um-toolkit-key" in example


def test_codex_requests_use_the_responses_endpoint_and_parse_the_answer():
    cfg = settings.load_reader_config()
    url, headers, payload = client._build_openai_responses("gpt-5-codex", "be brief", "hello", 500, cfg, "sk-x")
    assert url == "https://api.openai.com/v1/responses" and headers["Authorization"] == "Bearer sk-x"
    assert payload == {"model": "gpt-5-codex", "instructions": "be brief", "input": "hello", "max_output_tokens": 500}
    body = {"output": [{"type": "reasoning"}, {"type": "message", "content": [{"type": "output_text", "text": '{"a": 1}'}]}],
            "usage": {"input_tokens": 12, "output_tokens": 4}}
    assert client._parse_openai_responses(body) == ('{"a": 1}', 12, 4, False)
    cut = {"output_text": "par", "usage": {}, "incomplete_details": {"reason": "max_output_tokens"}}
    assert client._parse_openai_responses(cut)[3] is True
    assert "openai_responses" in client.PROVIDERS
