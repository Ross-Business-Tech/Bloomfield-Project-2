"""Loads scripts/check_no_data_staged.py as a module (scripts/ is not a package)."""
import importlib.util
from pathlib import Path


def load_checker():
    path = Path(__file__).resolve().parents[1] / "scripts" / "check_no_data_staged.py"
    spec = importlib.util.spec_from_file_location("check_no_data_staged", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
