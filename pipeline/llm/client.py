"""
client.py - the ONE place that talks to an AI service.

In plain English: every other file that needs the AI calls call_llm(). This file
  * picks the cheap or strong model for the job (reader.yaml),
  * checks the cache first, so we never pay to read the same thing twice,
  * refuses to run if it would break the spending cap,
  * sends the request, retrying politely if the service is busy,
  * records what it cost and saves the answer in the cache.

To switch AI companies, change LLM_PROVIDER in .env (anthropic, gemini or openai_compatible), or add a
new builder/parser pair below and list it in PROVIDERS. Nothing else in the project needs to change.

The AI here does ONE narrow job per call: it reads the text we give it and returns text.
It never browses, runs tools, or decides what to do next. API keys come from .env and
are never printed or logged.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import time
from dataclasses import dataclass

import requests

from collectors.common import config
from collectors.common.db import utc_now
from collectors.common.log import get_logger

from . import cost
from .settings import load_reader_config, model_for_task, provider_name

log = get_logger("llm")

# Swappable so tests don't really wait.
_sleep = time.sleep


class LLMError(RuntimeError):
    """The AI service failed in a way retrying will not fix (or we ran out of retries)."""


class LLMConfigError(LLMError):
    """Something is missing in the setup (for example the API key)."""


class RestrictedMaterialError(LLMError):
    """We refused to send private (Trepp / Bloomfield) material to an outside service."""


@dataclass
class LLMResult:
    text: str
    input_tokens: int          # 0 when the answer came from the cache
    output_tokens: int
    cached: bool
    model: str
    cost_usd: float            # 0 when cached
    truncated: bool = False    # the answer was cut off by max_output_tokens
    cache_key: str = ""


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def make_cache_key(file_hash: str, prompt_version: str, model: str, task: str, variant: str = "") -> str:
    """
    One key per (document fingerprint, prompt version, model, job). `variant` is only
    used for the "retry with the error message" call, so it doesn't reuse the first answer.
    """
    parts = [file_hash, prompt_version, model, task] + ([variant] if variant else [])
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _cache_get(con, key: str):
    row = con.execute("SELECT response_json FROM llm_cache WHERE cache_key = ?", [key]).fetchone()
    return json.loads(row[0]) if row else None


def _cache_put(con, key: str, task: str, text: str, in_tok: int, out_tok: int) -> None:
    con.execute("INSERT OR REPLACE INTO llm_cache (cache_key, task, response_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                [key, task, json.dumps({"text": text, "input_tokens": in_tok, "output_tokens": out_tok}),
                 utc_now()])


# ---------------------------------------------------------------------------
# Provider adapters. Each takes the request and returns (text, in_tokens, out_tokens, truncated).
# ---------------------------------------------------------------------------

def _post_json(url: str, headers: dict, payload: dict, timeout: float):
    """Send one request. Returns (status_code, parsed_json_or_{}, response_headers)."""
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text[:500]}
    return resp.status_code, body, resp.headers


KEY_NAMES = {"anthropic": ["ANTHROPIC_API_KEY"], "openai_compatible": ["OPENAI_API_KEY"],
             "gemini": ["GEMINI_API_KEY", "GOOGLE_API_KEY"]}


def get_api_key(provider: str) -> str:
    """Find the API key in .env. LLM_API_KEY works for any provider."""
    config.load_env()
    names = ["LLM_API_KEY"] + KEY_NAMES.get(provider, [])
    for name in names:
        if os.environ.get(name, "").strip():
            return os.environ[name].strip()
    raise LLMConfigError(
        f"No API key found for provider '{provider}'. Add {' (or '.join(names[1:] or names)}"
        f"{')' if len(names) > 2 else ''} or LLM_API_KEY to pipeline/.env. See .env.example.")


def _build_anthropic(model, system_prompt, user_text, max_tokens, cfg, key, task=None):
    payload = {"model": model, "max_tokens": max_tokens, "system": system_prompt,
               "messages": [{"role": "user", "content": user_text}]}
    payload.update((cfg.get("llm") or {}).get("request_params") or {})
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    return "https://api.anthropic.com/v1/messages", headers, payload


def _parse_anthropic(body):
    text = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
    usage = body.get("usage") or {}
    return (text, usage.get("input_tokens"), usage.get("output_tokens"),
            body.get("stop_reason") == "max_tokens")


def _build_openai_compatible(model, system_prompt, user_text, max_tokens, cfg, key, task=None):
    oc = (cfg.get("llm") or {}).get("openai_compatible") or {}
    base = (os.environ.get(oc.get("base_url_env", "LLM_BASE_URL"), "").strip()
            or oc.get("default_base_url", "https://api.openai.com/v1")).rstrip("/")
    payload = {"model": model,
               "messages": [{"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_text}],
               oc.get("max_tokens_field", "max_tokens"): max_tokens}
    payload.update((cfg.get("llm") or {}).get("request_params") or {})
    headers = {"Authorization": f"Bearer {key}", "content-type": "application/json"}
    return f"{base}/chat/completions", headers, payload


def _parse_openai_compatible(body):
    choice = (body.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    usage = body.get("usage") or {}
    return (text, usage.get("prompt_tokens"), usage.get("completion_tokens"),
            choice.get("finish_reason") == "length")


def _build_gemini(model, system_prompt, user_text, max_tokens, cfg, key, task=None):
    """
    Google's Gemini API (generateContent). The key travels in a header (never in the web address, so it can
    not end up in logs). For jobs that must return JSON we ask Gemini for JSON output. Gemini's "thinking"
    uses up the answer budget, so reader.yaml turns it down per model (model_generation_config).
    """
    g = (cfg.get("llm") or {}).get("gemini") or {}
    base = (os.environ.get("GEMINI_BASE_URL", "").strip()
            or g.get("base_url", "https://generativelanguage.googleapis.com/v1beta")).rstrip("/")
    name = model[len("models/"):] if model.startswith("models/") else model
    generation = {"maxOutputTokens": max_tokens}
    generation.update(g.get("generation_config") or {})
    generation.update((g.get("model_generation_config") or {}).get(name) or {})   # per-model thinking setting
    if task in (g.get("json_tasks") or []):
        generation["responseMimeType"] = "application/json"
    payload = {"systemInstruction": {"parts": [{"text": system_prompt}]},
               "contents": [{"role": "user", "parts": [{"text": user_text}]}],
               "generationConfig": generation}
    headers = {"x-goog-api-key": key, "content-type": "application/json"}
    return f"{base}/models/{name}:generateContent", headers, payload


def _parse_gemini(body):
    candidates = body.get("candidates") or []
    if not candidates:                                    # the prompt itself was refused
        reason = (body.get("promptFeedback") or {}).get("blockReason")
        raise LLMError("Gemini returned no answer" + (f" (the request was blocked: {reason})" if reason else "")
                       + ". Nothing was retried; check the document text and Gemini's safety settings.")
    cand = candidates[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))   # skip "thought summary" parts
    finish = cand.get("finishReason")
    if not text and finish not in (None, "STOP", "MAX_TOKENS"):                # SAFETY, RECITATION, ...
        raise LLMError(f"Gemini stopped without an answer (finishReason={finish}). Nothing was retried.")
    usage = body.get("usageMetadata") or {}
    out = (usage.get("candidatesTokenCount") or 0) + (usage.get("thoughtsTokenCount") or 0)   # thinking is billed
    return text, usage.get("promptTokenCount"), (out if usage else None), finish == "MAX_TOKENS"


# provider name -> (request builder, response parser). Add a new provider here.
PROVIDERS = {
    "anthropic": (_build_anthropic, _parse_anthropic),
    "openai_compatible": (_build_openai_compatible, _parse_openai_compatible),
    "gemini": (_build_gemini, _parse_gemini),
}


def _scrub(text: str, key: str) -> str:
    """Make sure the API key can never appear in an error message."""
    return text.replace(key, "***") if key else text


def _error_message(body: dict, key: str) -> str:
    err = body.get("error")
    msg = err.get("message") if isinstance(err, dict) else (err or body.get("raw") or "")
    return _scrub(str(msg)[:300], key)


def _retry_delay_from_body(body: dict) -> float:
    """Gemini says how long to wait inside the 429 error ("retryDelay": "34s"). Seconds, capped at 60."""
    try:
        for detail in (body.get("error") or {}).get("details") or []:
            delay = str(detail.get("retryDelay") or "")
            if delay.endswith("s"):
                return min(float(delay[:-1]), 60.0)
    except (ValueError, AttributeError, TypeError):
        pass
    return 0.0


def _send_with_retries(provider, model, system_prompt, user_text, max_tokens, cfg, key, task=None):
    """Send the request; retry on rate limits / server errors / timeouts with growing waits."""
    build, parse = PROVIDERS[provider]
    url, headers, payload = build(model, system_prompt, user_text, max_tokens, cfg, key, task)
    llm_cfg = cfg.get("llm") or {}
    max_retries = int(llm_cfg.get("max_retries", 5))
    base = float(llm_cfg.get("backoff_base_seconds", 2))
    timeout = float(llm_cfg.get("timeout_seconds", 120))

    problem = ""
    for attempt in range(max_retries + 1):
        retry_after = 0.0
        try:
            status, body, resp_headers = _post_json(url, headers, payload, timeout)
        except (requests.Timeout, requests.ConnectionError) as e:
            problem = f"network problem ({type(e).__name__})"
        else:
            if status == 200:
                return parse(body)
            if status == 429 or status >= 500:           # busy or broken on their end: retry
                problem = f"HTTP {status}"
                ra = str(resp_headers.get("retry-after", resp_headers.get("Retry-After", "")))
                retry_after = min(float(ra), 60) if ra.replace(".", "", 1).isdigit() else _retry_delay_from_body(body)
            else:                                         # our mistake (bad key, bad request): stop
                raise LLMError(f"{provider} rejected the request (HTTP {status}): "
                               f"{_error_message(body, key)}")
        if attempt < max_retries:
            wait = max(retry_after, base * (2 ** attempt)) + random.uniform(0, 0.5)
            log.warning("%s from %s; retry %d/%d in %.0fs", problem, provider, attempt + 1, max_retries, wait)
            _sleep(wait)
    raise LLMError(f"{provider} still failing after {max_retries} retries ({problem}). Try again later.")


# ---------------------------------------------------------------------------
# The main functions
# ---------------------------------------------------------------------------

def call_llm_detailed(task: str, system_prompt: str, user_text: str, max_output_tokens: int, *,
                      con=None, file_hash: str | None = None, prompt_version: str | None = None,
                      variant: str = "", restricted: str | None = None, use_cache: bool = True,
                      cfg: dict | None = None) -> LLMResult:
    """
    Do one AI job and return everything we know about the call.

    task            "extract", "relevance", "summary", "opening_phrase", ... (see reader.yaml)
    con             database connection; needed for the cache, the cost cap and the usage log
    file_hash       fingerprint of the document (for the cache key); if omitted we hash the text
    prompt_version  e.g. "v2" (for the cache key); if omitted we hash the system prompt
    variant         only for a "retry with the error message" call
    restricted      "trepp" or "bloomfield" if the text is private material; we refuse unless
                    reader.yaml allows it (both switches default to false)
    """
    cfg = cfg or load_reader_config()

    # Privacy guard: private material never leaves this computer unless config allows it.
    if restricted:
        flag = {"trepp": "trepp_ai_allowed", "bloomfield": "bloomfield_material_ai_allowed"}.get(restricted)
        if flag is None or not cfg.get(flag, False):
            raise RestrictedMaterialError(
                f"Refusing to send {restricted} material to an outside AI service "
                f"({flag or 'unknown category'} is false in config/reader.yaml).")

    provider = provider_name(cfg)
    if provider not in PROVIDERS:
        raise LLMConfigError(f"Unknown LLM provider '{provider}'. Choose one of: {', '.join(PROVIDERS)}.")
    model = model_for_task(task, cfg)
    file_hash = file_hash or hashlib.sha256((system_prompt + "\n" + user_text).encode("utf-8")).hexdigest()
    prompt_version = prompt_version or hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:8]
    key_id = make_cache_key(file_hash, prompt_version, model, task, variant)

    # 1. Cache: an answer we already paid for is free.
    if con is not None and use_cache:
        hit = _cache_get(con, key_id)
        if hit is not None:
            log.info("cache hit for task=%s", task)
            return LLMResult(hit["text"], 0, 0, True, model, 0.0, False, key_id)

    api_key = get_api_key(provider)

    # 2. Cost cap: refuse BEFORE spending if the worst case would go over.
    est_in = cost.estimate_tokens(system_prompt + user_text, cfg)
    cost.check_budget(con, model, est_in, max_output_tokens, cfg)

    # 3. The real call.
    text, in_tok, out_tok, truncated = _send_with_retries(
        provider, model, system_prompt, user_text, max_output_tokens, cfg, api_key, task)
    in_tok = int(in_tok) if in_tok is not None else est_in
    out_tok = int(out_tok) if out_tok is not None else cost.estimate_tokens(text, cfg)

    # 4. Record what it cost; cache the answer unless it was cut off.
    spent = cost.record_usage(con, task, provider, model, in_tok, out_tok, file_hash, cfg)
    if con is not None and use_cache and not truncated:
        _cache_put(con, key_id, task, text, in_tok, out_tok)
    if truncated:
        log.warning("answer for task=%s was cut off at max_output_tokens=%d", task, max_output_tokens)
    return LLMResult(text, in_tok, out_tok, False, model, spent, truncated, key_id)


def call_llm(task: str, system_prompt: str, user_text: str, max_output_tokens: int, **kwargs):
    """Simple form: returns (text, input_tokens, output_tokens). Same options as call_llm_detailed."""
    r = call_llm_detailed(task, system_prompt, user_text, max_output_tokens, **kwargs)
    return r.text, r.input_tokens, r.output_tokens
