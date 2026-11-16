"""
client.py - the ONE place that talks to an AI service.

In plain English: every other file that needs the AI calls call_llm(). This file
  * picks the cheap or strong model for the job (reader.yaml),
  * checks the cache first, so we never pay to read the same thing twice,
  * refuses to run if it would break the spending cap,
  * sends the request, retrying politely if the service is busy,
  * records what it cost and saves the answer in the cache.

To switch AI companies, change LLM_PROVIDER in .env (openai_responses for Codex, openai_compatible, anthropic or gemini), or add a
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
    """We refused to send private (Bloomfield) material to an outside service."""


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


KEY_NAMES = {"anthropic": ["ANTHROPIC_API_KEY"], "openai_compatible": ["OPENAI_API_KEY"], "openai_responses": ["OPENAI_API_KEY"],
             "gemini": ["GEMINI_API_KEY", "GOOGLE_API_KEY"]}


def get_api_key(provider: str) -> str:
    """Find the API key in .env. LLM_API_KEY works for any provider."""
    config.load_env()
    names = ["LLM_API_KEY"] + KEY_NAMES.get(provider, [])
    for name in KEY_NAMES.get(provider, []) + ["LLM_API_KEY"]:       # the provider's own variable first (OPENAI_API_KEY)
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


def _build_openai_responses(model, system_prompt, user_text, max_tokens, cfg, key, task=None):
    """
    OpenAI's Responses API (POST /v1/responses). The Codex models are served through this endpoint rather than
    chat/completions. Same key (OPENAI_API_KEY) and same base address as the other OpenAI provider.
    """
    oc = (cfg.get("llm") or {}).get("openai_responses") or (cfg.get("llm") or {}).get("openai_compatible") or {}
    base = (os.environ.get(oc.get("base_url_env", "LLM_BASE_URL"), "").strip()
            or oc.get("default_base_url", "https://api.openai.com/v1")).rstrip("/")
    payload = {"model": model, "instructions": system_prompt, "input": user_text, "max_output_tokens": max_tokens}
    payload.update((cfg.get("llm") or {}).get("request_params") or {})
    headers = {"Authorization": f"Bearer {key}", "content-type": "application/json"}
    return f"{base}/responses", headers, payload


def _parse_openai_responses(body):
    text = body.get("output_text") or "".join(
        part.get("text", "") for item in body.get("output") or [] if item.get("type") == "message"
        for part in item.get("content") or [] if part.get("type") in ("output_text", "text"))
    usage = body.get("usage") or {}
    cut = (body.get("incomplete_details") or {}).get("reason") == "max_output_tokens"
    return text, usage.get("input_tokens"), usage.get("output_tokens"), cut


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
    "openai_responses": (_build_openai_responses, _parse_openai_responses),
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


def _log_unknown_usage(con, task, provider, model, worst_in, max_tokens, cfg) -> float:
    """A request was SENT but we cannot prove what it used: the answer never came back
    (timeout / connection drop) or came back without usage numbers. The provider may
    still bill it, so charge the FULL WORST CASE (estimated prompt + max output) -
    the spending ledger never under-counts. Rows are marked unknown-usage-worst-case.
    Returns the dollars charged."""
    try:
        charged = cost.record_usage(con, task, provider, model, worst_in, max_tokens,
                                    "unknown-usage-worst-case", cfg)
        log.warning("usage unknown for task=%s; charged the worst-case estimate", task)
        return charged
    except Exception:
        log.exception("could not record unknown-usage worst case")
        return 0.0


def _send_with_retries(provider, model, system_prompt, user_text, max_tokens, cfg, key, task=None,
                       con=None):
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
            _log_unknown_usage(con, task, provider, model,
                               cost.estimate_tokens(system_prompt + user_text, cfg), max_tokens, cfg)
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
    restricted      "bloomfield" if the text is private material; we refuse unless
                    reader.yaml allows it (the switch defaults to false)
    """
    cfg = cfg or load_reader_config()

    # Privacy guard: private material never leaves this computer unless config allows it.
    if restricted:
        flag = {"bloomfield": "bloomfield_material_ai_allowed"}.get(restricted)
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
        provider, model, system_prompt, user_text, max_output_tokens, cfg, api_key, task, con=con)

    # 4. Record what it cost; cache the answer unless it was cut off. An answered call
    #    that did NOT report usage is charged at full worst case, never at a guess.
    if in_tok is None or out_tok is None:
        spent = _log_unknown_usage(con, task, provider, model, est_in, max_output_tokens, cfg)
        in_tok = int(in_tok) if in_tok is not None else est_in
        out_tok = int(out_tok) if out_tok is not None else max_output_tokens
    else:
        in_tok, out_tok = int(in_tok), int(out_tok)
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


# ---------------------------------------------------------------------------
# Tool calling (used by the sourcing agent in agent/)
# ---------------------------------------------------------------------------

def _parse_tool_calls(message: dict) -> list[dict]:
    """Normalize the assistant's tool calls to [{id, name, arguments(dict)}]."""
    out = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (TypeError, json.JSONDecodeError):
            args = {"_unparseable_arguments": fn.get("arguments")}
        out.append({"id": tc.get("id") or "", "name": fn.get("name") or "", "arguments": args})
    return out


def call_with_tools(task: str, system_prompt: str, messages: list[dict], tools: list[dict],
                    max_output_tokens: int, *, con=None, cfg: dict | None = None,
                    restricted: str | None = None) -> dict:
    """
    ONE step of a tool-calling conversation, through the OpenAI-compatible chat API (the U-M GPT
    Toolkit default). `messages` is the running conversation WITHOUT the system message; this
    function prepends it. Returns {"message": <assistant message dict>, "tool_calls": [...],
    "text": <content or "">, "input_tokens": int, "output_tokens": int, "truncated": bool,
    "cost_usd": float}.

    Unlike call_llm() there is no cache (every step depends on the tool results before it), but
    the spending cap and the usage log still apply, so an agent run can never break the budget.
    Only openai_compatible is wired up: the Toolkit speaks that API. Other providers raise a
    clear error instead of silently doing the wrong thing.
    """
    cfg = cfg or load_reader_config()
    if restricted:
        flag = {"bloomfield": "bloomfield_material_ai_allowed"}.get(restricted)
        if flag is None or not cfg.get(flag, False):
            raise RestrictedMaterialError(
                f"Refusing to send {restricted} material to an outside AI service "
                f"({flag or 'unknown category'} is false in config/reader.yaml).")
    provider = provider_name(cfg)
    if provider != "openai_compatible":
        raise LLMConfigError(
            f"Tool calling is only wired for the 'openai_compatible' provider (the U-M Toolkit); "
            f"LLM_PROVIDER is '{provider}'. The plain reader still works with every provider.")
    model = model_for_task(task, cfg)
    api_key = get_api_key(provider)

    oc = (cfg.get("llm") or {}).get("openai_compatible") or {}
    base = (os.environ.get(oc.get("base_url_env", "LLM_BASE_URL"), "").strip()
            or oc.get("default_base_url", "https://api.openai.com/v1")).rstrip("/")
    payload = {"model": model,
               "messages": [{"role": "system", "content": system_prompt}] + list(messages),
               oc.get("max_tokens_field", "max_tokens"): max_output_tokens}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    payload.update((cfg.get("llm") or {}).get("request_params") or {})
    headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}

    est_in = cost.estimate_tokens(system_prompt + json.dumps(messages), cfg)
    cost.check_budget(con, model, est_in, max_output_tokens, cfg)

    llm_cfg = cfg.get("llm") or {}
    max_retries = int(llm_cfg.get("max_retries", 5))
    base_delay = float(llm_cfg.get("backoff_base_seconds", 2))
    timeout = float(llm_cfg.get("timeout_seconds", 120))
    problem = ""
    for attempt in range(max_retries + 1):
        retry_after = 0.0
        try:
            status, body, resp_headers = _post_json(f"{base}/chat/completions", headers, payload, timeout)
        except (requests.Timeout, requests.ConnectionError) as e:
            problem = f"network problem ({type(e).__name__})"
            _log_unknown_usage(con, task, provider, model, est_in, max_output_tokens, cfg)
        else:
            if status == 200:
                choice = (body.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                usage = body.get("usage") or {}
                truncated = choice.get("finish_reason") == "length"
                if usage.get("prompt_tokens") is None or usage.get("completion_tokens") is None:
                    # answered but unmetered: charge the full worst case, never a guess
                    spent = _log_unknown_usage(con, task, provider, model, est_in, max_output_tokens, cfg)
                    in_tok, out_tok = est_in, max_output_tokens
                else:
                    in_tok = int(usage["prompt_tokens"])
                    out_tok = int(usage["completion_tokens"])
                    spent = cost.record_usage(con, task, provider, model, in_tok, out_tok,
                                              hashlib.sha256(json.dumps(messages).encode()).hexdigest(), cfg)
                if truncated:
                    log.warning("agent step for task=%s was cut off at max_output_tokens=%d", task, max_output_tokens)
                return {"message": message, "tool_calls": _parse_tool_calls(message),
                        "text": message.get("content") or "", "input_tokens": in_tok,
                        "output_tokens": out_tok, "truncated": truncated, "cost_usd": spent}
            if status == 429 or status >= 500:
                problem = f"HTTP {status}"
                ra = str(resp_headers.get("retry-after", resp_headers.get("Retry-After", "")))
                retry_after = min(float(ra), 60) if ra.replace(".", "", 1).isdigit() else _retry_delay_from_body(body)
            else:
                raise LLMError(f"{provider} rejected the request (HTTP {status}): "
                               f"{_error_message(body, api_key)}")
        if attempt < max_retries:
            wait = max(retry_after, base_delay * (2 ** attempt)) + random.uniform(0, 0.5)
            log.warning("%s from %s; retry %d/%d in %.0fs", problem, provider, attempt + 1, max_retries, wait)
            _sleep(wait)
    raise LLMError(f"{provider} still failing after {max_retries} retries ({problem}). Try again later.")
