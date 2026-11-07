"""
files.py - saving downloaded files to disk, fingerprinting them, and logging
them in the raw_files table.

In plain English: every document we download is saved unchanged (so it can be
shown as evidence later), given a unique ID, and fingerprinted with SHA-256.
If we have already stored a file with the same fingerprint, we skip it.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import config
from .db import (VALID_DOC_CATEGORIES, VALID_SOURCES, find_file_by_hash,
                 insert_row, utc_now)

# A short word used inside file IDs, e.g. "washtenaw-lien-2026-00123".
KIND_BY_CATEGORY = {
    "foreclosure_notice": "foreclosure", "receivership": "receivership",
    "lender_lawsuit": "lawsuit", "lis_pendens": "lispendens",
    "recorded_loan": "recorded", "mechanics_lien": "lien",
    "tax_delinquency": "taxdelinq", "building_permit": "permit",
    "certificate_of_occupancy": "co", "license_action": "license",
    "business_record": "bizrecord", "other": "doc",
    # added by Part B (the scout):
    "project_website": "projectsite", "news_article": "news",
    "auction_listing": "auction", "ucc_sale_notice": "uccsale",
}

EXT_TO_TYPE = {
    "pdf": "pdf", "htm": "html", "html": "html", "xml": "xml", "csv": "csv",
    "txt": "txt", "json": "json", "xlsx": "xlsx", "xls": "xlsx",
    "png": "image", "jpg": "image", "jpeg": "image", "gif": "image",
    "tif": "image", "tiff": "image",
}
CONTENT_TYPE_TO_EXT = {
    "application/pdf": "pdf", "text/html": "html", "application/xml": "xml",
    "text/xml": "xml", "text/csv": "csv", "text/plain": "txt",
    "application/json": "json", "image/png": "png", "image/jpeg": "jpg",
    "image/tiff": "tif",
}


# ---- small helpers -------------------------------------------------------------

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_path(path: Path) -> str:
    """Fingerprint a file on disk without loading it all into memory."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def slugify(text: str) -> str:
    """'St. Clair County' -> 'st-clair-county'. Safe for use in file names."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "unknown"


_COUNTY_SUFFIX = re.compile(r"\s+(county|parish|borough|census area|municipality)$", re.I)


def county_slug(county_name: str) -> str:
    """
    Folder/ID-friendly county name: 'Washtenaw County' -> 'washtenaw'.
    Independent cities keep their word, so 'Baltimore city' stays 'baltimore-city'
    and does not clash with Baltimore County.
    """
    return slugify(_COUNTY_SUFFIX.sub("", (county_name or "").strip()))


def resolve_county_name(con, state: str | None, folder_name: str) -> str:
    """
    Turn a folder name like 'washtenaw' into the official registry name
    'Washtenaw County'. If the registry doesn't know it, keep the folder text.
    """
    slug = county_slug(folder_name)
    if state:
        rows = con.execute("SELECT county FROM county_registry WHERE state = ?", [state]).fetchall()
        for (name,) in rows:
            if county_slug(name) == slug or slugify(name) == slugify(folder_name):
                return name
    return folder_name.replace("-", " ").replace("_", " ").strip()


def clean_ext(ext: str | None) -> str:
    return (ext or "").lower().lstrip(".")


def ext_from_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    return CONTENT_TYPE_TO_EXT.get(content_type.split(";")[0].strip().lower())


def guess_file_type(ext: str) -> str:
    return EXT_TO_TYPE.get(clean_ext(ext), "other")


# ---- IDs and paths ---------------------------------------------------------------

def new_file_id(con, scope: str, kind: str, year: int | None = None) -> str:
    """Make an ID like 'washtenaw-lien-2026-00123' that is not used yet."""
    year = year or utc_now().year
    prefix = f"{slugify(scope)}-{slugify(kind)}-{year}-"
    n = con.execute("SELECT count(*) FROM raw_files WHERE file_id LIKE ?", [prefix + "%"]).fetchone()[0] + 1
    while con.execute("SELECT 1 FROM raw_files WHERE file_id = ?", [f"{prefix}{n:05d}"]).fetchone():
        n += 1
    return f"{prefix}{n:05d}"


def build_raw_path(state: str | None, county: str | None, source: str,
                   file_id: str, ext: str) -> Path:
    """
    Where a file lives on disk, e.g. data/raw/MI/washtenaw/recorder/<file_id>.pdf
    Statewide sources use '_statewide'; national ones use US/_national.
    """
    state_dir = (state or "US").upper()
    if county:
        county_dir = county_slug(county)
    else:
        county_dir = "_statewide" if state else "_national"
    name = f"{file_id}.{clean_ext(ext)}" if clean_ext(ext) else file_id
    return config.raw_dir() / state_dir / county_dir / source / name


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Write via a temp file, so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


# ---- the main function every collector uses ---------------------------------------

@dataclass
class StoreResult:
    status: str                 # "new" or "duplicate"
    file_id: str                # the new ID, or the ID of the identical file we already had
    file_path: str | None       # path stored in the database
    file_hash: str


def store_file(con, data: bytes, *, source: str, doc_category: str, collector_name: str,
               state: str | None = None, county: str | None = None, city: str | None = None,
               url: str | None = None, source_record_id: str | None = None,
               notes: str | None = None, ext: str | None = None,
               file_type: str | None = None, scope: str | None = None,
               file_id: str | None = None) -> StoreResult:
    """
    Save raw bytes unchanged to data/raw/..., fingerprint them, and add a
    raw_files row. If a file with the same fingerprint is already stored,
    nothing is written and status is "duplicate".

    scope  = the first word of the generated file_id (defaults to county/state/source).
    file_id = give one explicitly only for special cases (like fake data).
    """
    if source not in VALID_SOURCES:
        raise ValueError(f"source must be one of {sorted(VALID_SOURCES)}, got {source!r}")
    if doc_category not in VALID_DOC_CATEGORIES:
        raise ValueError(f"doc_category must be one of {sorted(VALID_DOC_CATEGORIES)}, got {doc_category!r}")
    if state is not None:
        state = state.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", state):
            raise ValueError(f"state must be a 2-letter code, got {state!r}")

    digest = sha256_bytes(data)
    existing = find_file_by_hash(con, digest)
    if existing:
        return StoreResult("duplicate", existing[0], existing[1], digest)

    ext = clean_ext(ext)
    if file_id is None:
        scope_word = scope or (county_slug(county) if county else (state or source))
        file_id = new_file_id(con, scope_word, KIND_BY_CATEGORY[doc_category])
    elif con.execute("SELECT 1 FROM raw_files WHERE file_id = ?", [file_id]).fetchone():
        raise ValueError(f"file_id already exists: {file_id}")

    path = build_raw_path(state, county, source, file_id, ext)
    write_bytes_atomic(path, data)
    stored = config.to_stored_path(path)
    try:
        insert_row(con, "raw_files", {
            "file_id": file_id, "source": source, "doc_category": doc_category,
            "state": state, "county": county, "city": city, "url": url,
            "file_path": stored, "file_type": file_type or guess_file_type(ext),
            "file_hash": digest, "source_record_id": source_record_id,
            "collected_at": utc_now(), "collector_name": collector_name, "notes": notes,
        })
    except Exception:
        path.unlink(missing_ok=True)  # don't leave a file the database doesn't know about
        raise
    return StoreResult("new", file_id, stored, digest)
