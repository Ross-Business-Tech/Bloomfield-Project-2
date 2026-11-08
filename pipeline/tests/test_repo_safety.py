"""Fails if private data (databases, spreadsheets, inbox files, Trepp data, secrets) is tracked by git."""
import subprocess
from pathlib import Path

from tests.scripts_loader import load_checker

check = load_checker()


def test_blocks_private_paths():
    bad = [
        "pipeline/data/bloomfield.duckdb", "x/y/file.duckdb", "pipeline/data/inbox/recorder/a.pdf",
        "pipeline/inbox_export.xlsx", "pipeline/config/Trepp_screen.csv", "pipeline/.env",
        "pipeline/samples/deal.pdf",
    ]
    for p in bad:
        assert check.problem_with(p), p


def test_allows_our_own_code():
    ok = ["pipeline/collectors/trepp.py", "pipeline/config/trepp_columns.yaml",
          "pipeline/.env.example", "pipeline/collectors/county.py", "pipeline/README.md"]
    for p in ok:
        assert check.problem_with(p) is None, p


def test_nothing_private_is_tracked_by_git():
    root = Path(__file__).resolve().parents[2]
    tracked = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True,
                             text=True).stdout.splitlines()
    assert check.check(tracked) == []
