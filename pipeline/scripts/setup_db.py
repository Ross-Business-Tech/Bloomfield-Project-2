"""
setup_db.py - create the DuckDB database and every table.

Run from the pipeline/ folder:   python scripts/setup_db.py
Safe to run again: existing tables and data are left alone.
"""
import argparse
import sys
from pathlib import Path

# Lets this script find the "collectors" package no matter where it is run from.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.common import config, db  # noqa: E402
from scout_reader_schema import create_scout_reader_tables  # noqa: E402  (Part B's tables)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the Bloomfield database tables.")
    parser.add_argument("--db", help="database file (default: data/bloomfield.duckdb)")
    args = parser.parse_args()

    con = db.connect(args.db)
    create_scout_reader_tables(con)   # Part B (scout + reader) tables
    for folder in (config.raw_dir(), config.inbox_dir(), config.logs_dir()):
        folder.mkdir(parents=True, exist_ok=True)
    tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
    print(f"Database ready: {args.db or config.db_path()}")
    for t in sorted(tables):
        n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        print(f"  {t:<26} {n} rows")


if __name__ == "__main__":
    main()
