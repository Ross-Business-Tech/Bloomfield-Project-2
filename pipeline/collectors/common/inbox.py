"""
inbox.py - the manual inbox, our fallback for every source.

In plain English: many county and court sites block robots. For those, a person
downloads the documents by hand and drops them in:

    data/inbox/<source>/<state>/<county>/            e.g. data/inbox/recorder/MI/washtenaw/mortgage.pdf
    data/inbox/<source>/<state>/_statewide/          (statewide sources)
    data/inbox/<source>/                              (national sources)

  <source> is one of: county_court, recorder, public_notice, permit, state_license,
           cms, sec_edgar, ffiec, manual_upload   (trepp has its own loader, trepp.py)

Optional extras:
  * Put a category folder one level deeper (e.g. .../washtenaw/mechanics_lien/lien1.pdf)
    to set doc_category.
  * manual_upload/bloomfield/ is for private Bloomfield material (for example redacted sample
    deals). Files there still load normally (you may put <state>/<county> folders inside it),
    but every one is marked "[CONFIDENTIAL]" in raw_files.notes so Part B never sends it to an
    outside AI service. A sidecar with "confidential": true does the same for any file.
  * Add a "sidecar" file with the same name plus .json (lien1.pdf.json) to give details:
        {"url": "...", "source_record_id": "2026-00123", "doc_category": "mechanics_lien",
         "city": "Ann Arbor", "doc_type": "Claim of Lien", "notes": "..."}

load_inbox() copies each file into data/raw/, fingerprints it, adds it to raw_files,
then removes it from the inbox. Duplicates are moved to data/inbox/_duplicates/.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from . import config
from .db import VALID_DOC_CATEGORIES, VALID_SOURCES, utc_now
from .files import resolve_county_name, store_file
from .log import get_logger

log = get_logger("inbox")

# Folders inside the inbox that the loader itself manages or that other code handles.
SKIP_TOP_FOLDERS = {"_duplicates", "_rejected", "trepp"}
NATIONAL_STATE_NAMES = {"US", "_NATIONAL", "_ALL"}

# Private Bloomfield material: its raw_files.notes start with this marker (Part B checks for it).
CONFIDENTIAL_MARKER = "[CONFIDENTIAL]"
CONFIDENTIAL_FOLDER = "bloomfield"        # data/inbox/manual_upload/bloomfield/...


def _is_sidecar(path: Path) -> bool:
    """A .json file is a sidecar if it describes another file sitting next to it."""
    if path.suffix.lower() != ".json":
        return False
    sibling_full = path.with_suffix("")                    # lien1.pdf.json -> lien1.pdf
    if sibling_full.suffix and sibling_full.exists():
        return True
    return any(p != path and p.stem == path.stem and p.suffix.lower() != ".json"
               for p in path.parent.glob(path.stem + ".*"))


def _find_sidecar(path: Path) -> Path | None:
    for cand in (path.with_name(path.name + ".json"), path.with_suffix(".json")):
        if cand != path and cand.exists():
            return cand
    return None


def _read_sidecar(sidecar: Path | None) -> dict:
    if not sidecar:
        return {}
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as e:
        log.warning("could not read sidecar %s: %s", sidecar, e)
        return {}


def _unique_dest(dest: Path) -> Path:
    """Add a counter if a file with this name already exists."""
    if not dest.exists():
        return dest
    i = 1
    while True:
        cand = dest.with_name(f"{dest.stem}.{i}{dest.suffix}")
        if not cand.exists():
            return cand
        i += 1


def _move_aside(path: Path, sidecar: Path | None, inbox_root: Path, folder: str) -> None:
    """Move a duplicate (and its sidecar) to inbox/<folder>/ keeping its relative location."""
    dest = _unique_dest(inbox_root / folder / path.relative_to(inbox_root))
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), dest)
    if sidecar and sidecar.exists():
        shutil.move(str(sidecar), dest.with_name(dest.name + ".json"))


def load_inbox(con, run=None, only_source: str | None = None, only_state: str | None = None,
               only_county: str | None = None, inbox_root: Path | None = None,
               collector_name: str = "inbox.py") -> dict:
    """
    Load every file waiting in the inbox. Returns {"new": n, "skipped": n, "errors": n}.
    `run` is an optional RunTracker so counts flow into the run log.
    Filters (only_source / only_state / only_county) limit what is loaded.
    """
    root = Path(inbox_root) if inbox_root else config.inbox_dir()
    totals = {"new": 0, "skipped": 0, "errors": 0}
    if not root.exists():
        log.info("no inbox folder yet at %s", root)
        return totals

    only_county_slug = only_county.lower().replace("_", "-") if only_county else None
    defaults = (config.load_sources_config().get("inbox") or {}).get("default_doc_category") or {}

    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if not path.exists():                  # a sidecar we already moved along with its file
            continue
        rel = path.relative_to(root)
        if rel.name.startswith(".") or rel.name.lower() in ("readme.md", "readme.txt"):
            continue
        parts = rel.parts
        if len(parts) < 2:
            continue                                  # needs at least <source>/<file>
        source = parts[0]
        if source in SKIP_TOP_FOLDERS:
            continue
        if source not in VALID_SOURCES:
            msg = f"inbox folder '{source}' is not a known source; skipped {rel}"
            log.warning(msg)
            totals["errors"] += 1
            if run:
                run.error(msg)
            continue
        if only_source and source != only_source:
            continue
        if _is_sidecar(path):
            continue

        # Work out state / county / category from the folder names.
        dirs = parts[1:-1]
        confidential = False
        if source == "manual_upload" and dirs and dirs[0].lower() == CONFIDENTIAL_FOLDER:
            confidential = True
            dirs = dirs[1:]                       # what is left may still be <state>/<county>/...
        state = None
        county_folder = None
        category_folder = None
        if len(dirs) >= 1 and dirs[0].upper() not in NATIONAL_STATE_NAMES:
            if not re.fullmatch(r"[A-Za-z]{2}", dirs[0]):
                msg = f"'{dirs[0]}' is not a 2-letter state code; skipped {rel}"
                log.warning(msg)
                totals["errors"] += 1
                if run:
                    run.error(msg)
                continue
            state = dirs[0].upper()
        if len(dirs) >= 2 and dirs[1] != "_statewide":
            county_folder = dirs[1]
        if len(dirs) >= 3:
            category_folder = dirs[2]

        if only_state and (state or "").upper() != only_state.upper():
            continue
        if only_county_slug and (county_folder or "").lower().replace("_", "-") != only_county_slug:
            continue

        sidecar = _find_sidecar(path)
        meta = _read_sidecar(sidecar)

        # doc_category: sidecar wins, then the category folder, then the source's default.
        category = meta.get("doc_category") or category_folder or defaults.get(source) or "other"
        if category not in VALID_DOC_CATEGORIES:
            log.warning("unknown doc_category %r for %s; using 'other'", category, rel)
            category = "other"

        county = resolve_county_name(con, state, county_folder) if county_folder else None
        notes = meta.get("notes")
        if meta.get("doc_type"):
            notes = f"doc type: {meta['doc_type']}" + (f"; {notes}" if notes else "")
        if meta.get("confidential") is True:
            confidential = True
        if confidential:
            notes = CONFIDENTIAL_MARKER + (f" {notes}" if notes else "")
        record_id = (meta.get("source_record_id") or meta.get("case_number")
                     or meta.get("instrument_number") or meta.get("permit_number"))

        try:
            result = store_file(
                con, path.read_bytes(), source=source, doc_category=category,
                collector_name=f"{collector_name}:{source}", state=state, county=county,
                city=meta.get("city"), url=meta.get("url"),
                source_record_id=str(record_id) if record_id else None,
                notes=notes, ext=path.suffix,
            )
        except Exception as e:                       # one bad file must not stop the rest
            msg = f"could not load {rel}: {e}"
            log.error(msg)
            totals["errors"] += 1
            if run:
                run.error(msg)
            continue

        if result.status == "duplicate":
            totals["skipped"] += 1
            if run:
                run.skipped()
            _move_aside(path, sidecar, root, "_duplicates")
            log.info("duplicate of %s: %s", result.file_id, rel)
        else:
            totals["new"] += 1
            if run:
                run.new_file()
            # Keep the sidecar next to the saved evidence file, then clear the inbox.
            if sidecar and sidecar.exists():
                saved = config.from_stored_path(result.file_path)
                shutil.move(str(sidecar), saved.with_name(saved.name + ".meta.json"))
            path.unlink()
            log.info("loaded %s -> %s", rel, result.file_id)

    return totals
