"""
check_no_data_staged.py - stops private data from being committed to this PUBLIC repo.

It fails (exit code 1) if any file about to be committed is:
  * a database (.duckdb) or spreadsheet (.xlsx / .xls)
  * anything under pipeline/data/ (raw files, the inbox, logs)
  * a .env file (only .env.example is allowed)
  * anything with "trepp" in its path (except our own Trepp code and settings)
  * anything under pipeline/samples/ or pipeline/bloomfield_samples/

Install it as a git pre-commit hook with:   python pipeline/scripts/install_hooks.py
Or run it by hand:                          python pipeline/scripts/check_no_data_staged.py
"""
import re
import subprocess
import sys

# Our own Trepp code and settings contain no data, so they are allowed.
ALLOWED = {
    "pipeline/collectors/trepp.py",
    "pipeline/config/trepp_columns.yaml",
    "pipeline/tests/test_trepp.py",
}


def problem_with(path: str) -> str | None:
    """Return a reason if this path must not be committed, else None."""
    p = path.replace("\\", "/")
    low = p.lower()
    if p in ALLOWED:
        return None
    name = low.rsplit("/", 1)[-1]
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return "secrets file"
    if low.endswith((".duckdb", ".duckdb.wal")):
        return "database file"
    if low.endswith((".xlsx", ".xls")):
        return "spreadsheet"
    if re.search(r"(^|/)pipeline/data(/|$)", low):
        return "data/inbox/raw-files folder"
    if re.search(r"(^|/)pipeline/(samples|bloomfield_samples)(/|$)", low):
        return "Bloomfield sample data"
    if "trepp" in low:
        return "Trepp data"
    return None


def check(paths: list[str]) -> list[tuple[str, str]]:
    return [(p, why) for p in paths if (why := problem_with(p))]


def git_lines(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def main() -> int:
    bad = check(git_lines("diff", "--cached", "--name-only", "--diff-filter=ACMR"))
    if bad:
        print("COMMIT BLOCKED - private or bulky files are staged:")
        for path, why in bad:
            print(f"  {path}   ({why})")
        print("Unstage them with:  git restore --staged <file>")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
