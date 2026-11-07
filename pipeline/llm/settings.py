"""
settings.py - loads config/reader.yaml and works out which model and provider to use.

In plain English: this answers "which AI company?", "which model for this job?" and
"what are the spending limits?". Everything comes from reader.yaml and .env.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from collectors.common import config


def load_reader_config(path: str | Path | None = None) -> dict:
    """Read config/reader.yaml (or another file, handy for tests)."""
    config.load_env()
    p = Path(path) if path else config.CONFIG_DIR / "reader.yaml"
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def provider_name(cfg: dict) -> str:
    """LLM_PROVIDER in .env wins over reader.yaml."""
    return (os.environ.get("LLM_PROVIDER") or cfg.get("provider") or "anthropic").strip().lower()


def model_for_task(task: str, cfg: dict) -> str:
    """Pick the cheap or strong model for a job (names come from reader.yaml / .env)."""
    tier = (cfg.get("task_models") or {}).get(task)
    if tier is None:
        raise ValueError(f"Unknown task {task!r}. Add it under task_models in reader.yaml.")
    if tier == "cheap":
        env_name = os.environ.get("LLM_MODEL_CHEAP")
    else:
        env_name = os.environ.get("LLM_MODEL_STRONG") or os.environ.get("LLM_MODEL")
    model = env_name or (cfg.get("models") or {}).get(tier)
    if not model:
        raise ValueError(f"No model configured for tier {tier!r} in reader.yaml")
    return model
