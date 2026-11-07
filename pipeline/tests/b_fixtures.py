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
