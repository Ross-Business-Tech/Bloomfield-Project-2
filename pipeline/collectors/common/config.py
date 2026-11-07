"""
config.py - where settings and folder locations come from.

In plain English: this file answers questions like "where is the database?",
"what is our contact email?" and "how fast may we hit this website?".
Settings live in config/sources.yaml and in the .env file, never in the code.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

# The "pipeline" folder (two levels above this file).
PIPELINE_DIR = Path(__file__).resolve().parents[2]
CONFIG_DIR = PIPELINE_DIR / "config"

_env_loaded = False


def load_env() -> None:
    """Read pipeline/.env (secrets like the contact email) once."""
    global _env_loaded
    if not _env_loaded:
        load_dotenv(PIPELINE_DIR / ".env")
        _env_loaded = True


def load_sources_config(path: str | Path | None = None) -> dict:
    """Load config/sources.yaml (or another file, which is handy for tests)."""
    load_env()
    p = Path(path) if path else CONFIG_DIR / "sources.yaml"
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---- folders -----------------------------------------------------------------

def data_dir() -> Path:
    """Folder holding the database, downloaded files, inbox and logs."""
    load_env()
    override = os.environ.get("BLOOMFIELD_DATA_DIR")
    return Path(override).expanduser().resolve() if override else PIPELINE_DIR / "data"


def db_path() -> Path:
    return data_dir() / "bloomfield.duckdb"


def raw_dir() -> Path:
    return data_dir() / "raw"


def inbox_dir() -> Path:
    return data_dir() / "inbox"


def logs_dir() -> Path:
    return data_dir() / "logs"


def to_stored_path(path: Path) -> str:
    """
    The path we write into the database. Inside the pipeline folder we store it
    relative (e.g. "data/raw/MI/washtenaw/recorder/x.pdf") so it still works
    on a teammate's computer. Outside, we store the full path.
    """
    path = Path(path).resolve()
    try:
        return path.relative_to(PIPELINE_DIR).as_posix()
    except ValueError:
        return str(path)


def from_stored_path(stored: str) -> Path:
    """Reverse of to_stored_path: turn a database path back into a real path."""
    p = Path(stored)
    return p if p.is_absolute() else PIPELINE_DIR / p


# ---- web request settings ------------------------------------------------------

def domain_settings(domain: str, cfg: dict | None = None) -> dict:
    """Default web settings, with any per-website overrides from sources.yaml."""
    cfg = cfg if cfg is not None else load_sources_config()
    merged = dict(cfg.get("defaults", {}))
    merged.update((cfg.get("domains") or {}).get(domain, {}))
    return merged


def get_user_agent(cfg: dict | None = None) -> str | None:
    """
    Build the User-Agent text, including our contact email from .env.
    Returns None if the email is missing (we refuse to scrape anonymously).
    """
    cfg = cfg if cfg is not None else load_sources_config()
    load_env()
    email = os.environ.get(cfg.get("contact_email_env", "CONTACT_EMAIL"), "").strip()
    if not email:
        return None
    template = cfg.get(
        "user_agent_template",
        "BloomfieldSourcingResearch/0.1 (contact: {email})",
    )
    return template.format(email=email)
