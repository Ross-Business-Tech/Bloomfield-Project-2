"""Shared helpers for every collector.

* database connection (SQLite by default, Postgres when DB_URL says so)
* one download / register path that fingerprints every file with SHA-256 and
  never stores the same bytes twice
* polite HTTP: declared User-Agent, timeouts, per-host rate limits, retries
  with exponential backoff
* run statistics and logging in the same format for every collector
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, TypeVar
from urllib.parse import urlparse

import requests
from sqlalchemy import Engine, create_engine, event, text

try:  # optional: .env support
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]

NEEL_DIR = Path(__file__).resolve().parent.parent
SCHEMA_PATH = Path(__file__).resolve().parent / "schema_draft.sql"
CONFIG_DIR = NEEL_DIR / "config"

DEFAULT_TIMEOUT = 60  # seconds, for every HTTP call
log = logging.getLogger("collectors")
T = TypeVar("T")

if load_dotenv is not None:
    load_dotenv(NEEL_DIR / ".env")


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def data_dir() -> Path:
    """Folder for the SQLite file and downloaded files (git-ignored)."""
    path = Path(os.environ.get("DATA_DIR") or NEEL_DIR / "data")
    path.mkdir(parents=True, exist_ok=True)
    return path


def raw_dir() -> Path:
    """Folder where downloaded file bytes are stored."""
    path = Path(os.environ.get("RAW_FILES_DIR") or data_dir() / "raw_files")
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_agent() -> str:
    """The declared User-Agent. SEC requires a name and a contact email."""
    ua = (os.environ.get("USER_AGENT") or "").strip()
    if not ua:
        raise RuntimeError(
            "USER_AGENT is not set. Copy Neel/.env.example to Neel/.env and set "
            "USER_AGENT, e.g. 'Bloomfield AI Sprint Team yourname@umich.edu'."
        )
    return ua


def setup_logging(verbose: bool = False) -> None:
    """Configure console logging once for a CLI run."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def utc_now() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def get_engine(db_url: str | None = None) -> Engine:
    """Return a SQLAlchemy engine.

    Uses ``db_url``, else the DB_URL env var, else a SQLite file in DATA_DIR.
    Postgres works with e.g. ``postgresql+psycopg://user:pw@host/db``.
    """
    url = db_url or os.environ.get("DB_URL") or f"sqlite:///{data_dir() / 'bloomfield.sqlite'}"
    engine = create_engine(url, future=True)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    return engine


def _schema_statements(sql: str) -> list[str]:
    no_comments = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    return [stmt.strip() for stmt in no_comments.split(";") if stmt.strip()]


def init_db(engine: Engine) -> None:
    """Create the shared tables if they do not exist (safe to call every run)."""
    with engine.begin() as conn:
        for stmt in _schema_statements(SCHEMA_PATH.read_text()):
            conn.execute(text(stmt))


def upsert(
    engine: Engine,
    table: str,
    rows: Sequence[Mapping[str, Any]],
    key_cols: Sequence[str],
) -> int:
    """Insert rows, updating the existing row when the key already exists.

    Re-running with the same rows changes nothing and adds no duplicates.
    Returns the number of rows sent to the database.
    """
    if not rows:
        return 0
    cols = list(rows[0].keys())
    update_cols = [c for c in cols if c not in key_cols]
    for name in [table, *cols]:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"unsafe identifier {name!r}")
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) "
        f"VALUES ({', '.join(':' + c for c in cols)}) "
        f"ON CONFLICT ({', '.join(key_cols)}) "
    )
    sql += (
        "DO UPDATE SET " + ", ".join(f"{c} = excluded.{c}" for c in update_cols)
        if update_cols
        else "DO NOTHING"
    )
    with engine.begin() as conn:
        conn.execute(text(sql), [dict(r) for r in rows])
    return len(rows)


def count_rows(engine: Engine, table: str, where: str = "", params: Mapping[str, Any] | None = None) -> int:
    """Row count helper used by tests and run summaries."""
    sql = f"SELECT COUNT(*) FROM {table}" + (f" WHERE {where}" if where else "")
    with engine.connect() as conn:
        return int(conn.execute(text(sql), dict(params or {})).scalar_one())


# --------------------------------------------------------------------------- #
# Run statistics
# --------------------------------------------------------------------------- #
@dataclass
class RunStats:
    """What every collector reports at the end of a run."""

    name: str
    files_found: int = 0
    files_downloaded: int = 0
    files_skipped_duplicate: int = 0
    rows_written: int = 0
    errors: list[str] = field(default_factory=list)

    def error(self, where: str, exc: BaseException | str) -> None:
        """Record and log an error without stopping the run."""
        msg = f"{where}: {exc}"
        self.errors.append(msg)
        log.error("[%s] %s", self.name, msg)

    def summary(self) -> str:
        return (
            f"[{self.name}] files found={self.files_found} downloaded={self.files_downloaded} "
            f"skipped as duplicates={self.files_skipped_duplicate} rows written={self.rows_written} "
            f"errors={len(self.errors)}"
        )

    def log_summary(self) -> None:
        log.info(self.summary())
        for e in self.errors:
            log.info("[%s]   error: %s", self.name, e)


def run_isolated(stats: RunStats, label: str, fn: Callable[[], T]) -> T | None:
    """Run one source/county/file; log and continue if it fails."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - deliberate: one failure must not stop the rest
        log.debug("traceback for %s", label, exc_info=True)
        stats.error(label, exc)
        return None


# --------------------------------------------------------------------------- #
# HTTP: rate limits, retries, timeouts
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Allow at most ``per_second`` calls per second (thread-safe)."""

    def __init__(self, per_second: float) -> None:
        self.min_interval = 1.0 / per_second if per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.min_interval
        if delay > 0:
            time.sleep(delay)


class RetryableHTTPError(Exception):
    """A response worth retrying (429 or 5xx)."""


def retry(
    fn: Callable[[], T],
    attempts: int = 4,
    base_delay: float = 2.0,
    retry_on: tuple[type[BaseException], ...] = (requests.ConnectionError, requests.Timeout, RetryableHTTPError),
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn``; on a retryable error wait 2s, 4s, 8s ... and try again."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as exc:
            if attempt == attempts:
                raise
            delay = base_delay * 2 ** (attempt - 1)
            log.warning("attempt %d/%d failed (%s); retrying in %.0fs", attempt, attempts, exc, delay)
            sleep(delay)
    raise AssertionError("unreachable")


class HttpClient:
    """requests.Session with a declared User-Agent, timeouts, rate limits and retries.

    ``host_rates`` maps a host name to max requests per second; other hosts use
    ``default_rate``. SEC's limit is 10/s, so we default sec.gov hosts to 8/s.
    """

    def __init__(
        self,
        ua: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        default_rate: float = 2.0,
        host_rates: Mapping[str, float] | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = ua or user_agent()
        self.session.headers.setdefault("Accept-Encoding", "gzip, deflate")
        self.timeout = timeout
        self.default_rate = default_rate
        self.host_rates = {"sec.gov": 8.0, **(host_rates or {})}
        self._limiters: dict[str, RateLimiter] = {}

    def _limiter(self, host: str) -> RateLimiter:
        rate = self.default_rate
        for suffix, r in self.host_rates.items():
            if host == suffix or host.endswith("." + suffix):
                rate = r
        key = f"{host}@{rate}"
        if key not in self._limiters:
            self._limiters[key] = RateLimiter(rate)
        return self._limiters[key]

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """HTTP request with rate limit, timeout and retry. Raises on 4xx/5xx."""
        kwargs.setdefault("timeout", self.timeout)
        host = urlparse(url).hostname or ""

        def _once() -> requests.Response:
            self._limiter(host).wait()
            resp = self.session.request(method, url, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise RetryableHTTPError(f"HTTP {resp.status_code} for {url}")
            resp.raise_for_status()
            return resp

        return retry(_once)

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)


# --------------------------------------------------------------------------- #
# Files: fingerprint, dedupe, register
# --------------------------------------------------------------------------- #
def sha256_bytes(data: bytes) -> str:
    """SHA-256 hex digest of file bytes (the raw_files fingerprint)."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    """SHA-256 of a file on disk without loading it all into memory."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class FileResult:
    """Outcome of downloading / registering one file."""

    file_id: str
    status: str  # "new" | "duplicate" (same bytes already loaded) | "seen_url" (URL already loaded)
    local_path: str
    fingerprint: str | None = None

    @property
    def is_new(self) -> bool:
        return self.status == "new"


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "x"


def _guess_ext(url: str | None, content_type: str | None) -> str:
    if url:
        suffix = Path(urlparse(url).path).suffix.lower()
        if re.fullmatch(r"\.[a-z0-9]{1,5}", suffix):
            return suffix
    if content_type:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if ext:
            return ext
    return ".bin"


def find_file_by_fingerprint(engine: Engine, fingerprint: str) -> tuple[str, str] | None:
    """(file_id, local_path) of an already-loaded file with these bytes, if any."""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT file_id, local_path FROM raw_files WHERE fingerprint = :f"), {"f": fingerprint}
        ).first()
    return (row[0], row[1]) if row else None


def find_file_by_url(engine: Engine, url: str) -> tuple[str, str] | None:
    """(file_id, local_path) of a file already downloaded from this URL, if any."""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT file_id, local_path FROM raw_files WHERE source_url = :u ORDER BY downloaded_at LIMIT 1"),
            {"u": url},
        ).first()
    return (row[0], row[1]) if row else None


def _insert_raw_file(engine: Engine, row: Mapping[str, Any]) -> FileResult:
    """Insert a raw_files row unless the fingerprint already exists."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO raw_files (file_id, source, state, date, fingerprint, local_path, downloaded_at, "
                "source_url, content_type, size_bytes, needs_reading, restricted) VALUES (:file_id, :source, "
                ":state, :date, :fingerprint, :local_path, :downloaded_at, :source_url, :content_type, "
                ":size_bytes, :needs_reading, :restricted) ON CONFLICT (fingerprint) DO NOTHING"
            ),
            dict(row),
        )
        stored = conn.execute(
            text("SELECT file_id, local_path FROM raw_files WHERE fingerprint = :f"), {"f": row["fingerprint"]}
        ).one()
    status = "new" if stored[0] == row["file_id"] else "duplicate"
    return FileResult(stored[0], status, stored[1], row["fingerprint"])


def register_bytes(
    engine: Engine,
    data: bytes,
    *,
    source: str,
    state: str | None = None,
    date: str | None = None,
    source_url: str | None = None,
    content_type: str | None = None,
    ext: str | None = None,
    needs_reading: bool = False,
    restricted: bool = False,
) -> FileResult:
    """Save bytes under RAW_FILES_DIR and add a raw_files row, unless these exact
    bytes (same SHA-256) were already loaded, in which case nothing is written."""
    fingerprint = sha256_bytes(data)
    existing = find_file_by_fingerprint(engine, fingerprint)
    if existing:
        return FileResult(existing[0], "duplicate", existing[1], fingerprint)

    folder = raw_dir() / _safe_name(source.replace(":", "_"))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{fingerprint}{ext or _guess_ext(source_url, content_type)}"
    if not path.exists():
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)

    result = _insert_raw_file(
        engine,
        {
            "file_id": uuid.uuid4().hex,
            "source": source,
            "state": state,
            "date": date,
            "fingerprint": fingerprint,
            "local_path": str(path),
            "downloaded_at": utc_now(),
            "source_url": source_url,
            "content_type": content_type,
            "size_bytes": len(data),
            "needs_reading": int(needs_reading),
            "restricted": int(restricted),
        },
    )
    return result


def download_file(
    client: HttpClient,
    engine: Engine,
    url: str,
    *,
    source: str,
    state: str | None = None,
    date: str | None = None,
    needs_reading: bool = False,
    skip_if_url_seen: bool = True,
    ext: str | None = None,
    stats: RunStats | None = None,
    **request_kwargs: Any,
) -> FileResult:
    """THE way every collector downloads a file.

    * ``skip_if_url_seen``: for URLs whose content never changes (EDGAR archive
      files), skip the request entirely if this URL is already in raw_files.
    * Otherwise downloads, fingerprints, and stores the file only if its bytes
      are new. Updates ``stats`` (found / downloaded / skipped).
    """
    if stats:
        stats.files_found += 1
    if skip_if_url_seen:
        seen = find_file_by_url(engine, url)
        if seen:
            if stats:
                stats.files_skipped_duplicate += 1
            log.debug("skip (URL already loaded): %s", url)
            return FileResult(seen[0], "seen_url", seen[1])

    resp = client.get(url, **request_kwargs)
    result = register_bytes(
        engine,
        resp.content,
        source=source,
        state=state,
        date=date,
        source_url=url,
        content_type=resp.headers.get("Content-Type"),
        ext=ext,
        needs_reading=needs_reading,
    )
    if stats:
        if result.is_new:
            stats.files_downloaded += 1
        else:
            stats.files_skipped_duplicate += 1
    log.info("%s %s -> %s", "saved" if result.is_new else "duplicate", url, result.file_id)
    return result


def register_local_file(
    engine: Engine,
    path: str | Path,
    *,
    source: str,
    state: str | None = None,
    date: str | None = None,
    restricted: bool = False,
    stats: RunStats | None = None,
) -> FileResult:
    """Register a file that is already on this computer (e.g. an uploaded Trepp
    export or a manually downloaded FFIEC zip). The file is NOT copied; its own
    path is recorded. Same-bytes files are reported as duplicates."""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"file not found: {p}")
    if stats:
        stats.files_found += 1
    fingerprint = sha256_file(p)
    existing = find_file_by_fingerprint(engine, fingerprint)
    if existing:
        result = FileResult(existing[0], "duplicate", existing[1], fingerprint)
    else:
        result = _insert_raw_file(
            engine,
            {
                "file_id": uuid.uuid4().hex,
                "source": source,
                "state": state,
                "date": date,
                "fingerprint": fingerprint,
                "local_path": str(p),
                "downloaded_at": utc_now(),
                "source_url": None,
                "content_type": mimetypes.guess_type(p.name)[0],
                "size_bytes": p.stat().st_size,
                "needs_reading": 0,
                "restricted": int(restricted),
            },
        )
    if stats:
        if result.is_new:
            stats.files_downloaded += 1
        else:
            stats.files_skipped_duplicate += 1
    return result


# --------------------------------------------------------------------------- #
# Small parsing helpers shared by collectors
# --------------------------------------------------------------------------- #
def load_yaml(path: str | Path) -> Any:
    """Read a YAML config file."""
    import yaml

    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y/%m/%d", "%m/%d/%y", "%Y%m%d", "%d-%b-%Y", "%b %d, %Y")


def to_iso_date(value: Any) -> str | None:
    """Best-effort conversion of many date formats to 'YYYY-MM-DD'."""
    if value is None:
        return None
    if hasattr(value, "strftime"):
        try:
            if value != value:  # NaT
                return None
        except Exception:  # noqa: BLE001
            pass
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    if not s or s.lower() in {"nan", "nat", "none", "null"}:
        return None
    s = s.split("T")[0].split(" ")[0] if re.match(r"\d{4}-\d{2}-\d{2}[T ]", s) else s
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def to_float(value: Any) -> float | None:
    """Parse numbers like '1,234.5', '$1,000', '85%', '(12)' -> float or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return None if value != value else float(value)  # NaN check
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none", "null", "n/a", "na", "-"}:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[,$%()\s]", "", s)
    s = re.sub(r"[xX]$", "", s)  # DSCR written as "1.25x"
    try:
        num = float(s)
    except ValueError:
        return None
    return -num if negative else num


def to_flag(value: Any) -> int | None:
    """Parse Y/N, true/false, 1/0 style values into 1/0 (None if blank/unknown)."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in {"y", "yes", "true", "t", "1", "1.0"}:
        return 1
    if s in {"n", "no", "false", "f", "0", "0.0"}:
        return 0
    return None


def clean_str(value: Any) -> str | None:
    """Strip strings; turn blanks and NaN into None."""
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    s = str(value).strip()
    return s or None


def chunked(items: Iterable[T], size: int) -> Iterable[list[T]]:
    """Yield lists of up to ``size`` items."""
    batch: list[T] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
