"""
fetch.py - the ONE polite way every collector downloads anything from the web.

In plain English, before touching a website this code:
  1. checks the site's robots.txt (its "please don't visit these pages" list),
  2. waits so we never go faster than the configured rate (default 1 request / 3 s),
  3. identifies itself with a User-Agent that includes our contact email,
  4. retries a few times if the site is briefly overloaded (429 / 5xx errors),
  5. gives up IMMEDIATELY on 401/403 (access denied) and on CAPTCHA or sign-in
     pages. We never try to get around those. Instead the website is flagged
     "use the manual inbox" and the reason is logged.
"""
from __future__ import annotations

import time
from urllib import robotparser
from urllib.parse import urlparse

import requests

from . import config
from .files import StoreResult, ext_from_content_type, store_file
from .log import get_logger

log = get_logger("fetch")

ROBOTS_CACHE_SECONDS = 24 * 3600

# Words that mean "a person must prove they're human or log in". We stop if we see them.
CAPTCHA_PHRASES = ["captcha", "verify you are human", "are you a robot",
                   "unusual traffic", "i'm not a robot", "human verification"]
LOGIN_PHRASES = ["sign in", "log in", "login required", "please log in"]

# ---- module memory (reset between tests with reset_state) -------------------------
_last_request_at: dict[str, float] = {}                     # domain -> time of last request
_robots_cache: dict[str, tuple[float, robotparser.RobotFileParser]] = {}
_manual_inbox_flags: dict[str, str] = {}                    # domain -> why we gave up
_robots_skipped: dict[str, set[str]] = {}                   # domain -> pages robots.txt told us to skip

# Swappable so tests don't really wait.
_sleep = time.sleep
_clock = time.monotonic


def reset_state() -> None:
    _last_request_at.clear()
    _robots_cache.clear()
    _manual_inbox_flags.clear()
    _robots_skipped.clear()


def get_robots_skips() -> dict[str, set[str]]:
    """Pages we skipped because robots.txt disallows them (the rest of the site is still used)."""
    return {d: set(p) for d, p in _robots_skipped.items()}


def get_fallbacks() -> dict[str, str]:
    """Websites we stopped using this run (domain -> reason). Use the manual inbox for them."""
    return dict(_manual_inbox_flags)


def needs_manual_inbox(domain_or_url: str) -> bool:
    d = urlparse(domain_or_url).netloc.lower() if "://" in domain_or_url else domain_or_url.lower()
    return d in _manual_inbox_flags


def _flag_manual_inbox(domain: str, reason: str) -> None:
    _manual_inbox_flags[domain] = reason
    log.warning("%s: switching to MANUAL INBOX mode - %s", domain, reason)


# ---- rate limiting ------------------------------------------------------------------

def _wait_for_turn(domain: str, seconds_between: float) -> None:
    """Sleep just long enough that requests to one website stay `seconds_between` apart."""
    last = _last_request_at.get(domain)
    if last is not None:
        wait = seconds_between - (_clock() - last)
        if wait > 0:
            _sleep(wait)
    _last_request_at[domain] = _clock()


def _request(url, settings, ua, session, params=None, headers=None):
    """One rate-limited request. Returns a Response, or raises requests exceptions."""
    domain = urlparse(url).netloc.lower()
    _wait_for_turn(domain, float(settings.get("seconds_between_requests", 3)))
    hdrs = {"User-Agent": ua, "Accept": "*/*"}
    hdrs.update(headers or {})
    return session.get(url, params=params, headers=hdrs,
                       timeout=float(settings.get("timeout_seconds", 30)))


# ---- robots.txt -----------------------------------------------------------------------

def _get_robots(scheme, domain, settings, ua, session):
    """Return the site's parsed robots.txt (cached 24 hours), or None if unusable."""
    cached = _robots_cache.get(domain)
    if cached and _clock() - cached[0] < ROBOTS_CACHE_SECONDS:
        return cached[1]

    rp = robotparser.RobotFileParser()
    url = f"{scheme}://{domain}/robots.txt"
    try:
        resp = _request(url, settings, ua, session)
    except requests.RequestException as e:
        log.warning("could not read %s (%s); skipping this site for now to be safe", url, e)
        return None  # not cached, so we try again next time

    if resp.status_code == 200:
        rp.parse(resp.text.splitlines())
    elif resp.status_code in (401, 403):
        rp.parse(["User-agent: *", "Disallow: /"])
        _flag_manual_inbox(domain, f"robots.txt returned {resp.status_code} (automated access refused)")
    elif 400 <= resp.status_code < 500:
        rp.parse([])  # no robots.txt = no restrictions
    else:
        log.warning("%s returned %s; skipping this site for now to be safe", url, resp.status_code)
        return None
    _robots_cache[domain] = (_clock(), rp)
    return rp


# ---- block-page detection ----------------------------------------------------------------

def looks_like_block_page(resp) -> str | None:
    """
    If the page is a CAPTCHA or a login wall, return a short reason; otherwise None.
    Only HTML pages are checked (PDFs, XML, JSON, CSV are left alone).
    Sign-in words only count when the page also has a password box, so an ordinary
    page with a "Sign in" link in its menu is not mistaken for a login wall.
    """
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if ctype and "html" not in ctype:
        return None
    text = (resp.text or "")[:300_000].lower()
    for phrase in CAPTCHA_PHRASES:
        if phrase in text:
            return f"CAPTCHA / human check detected ('{phrase}')"
    path = urlparse(resp.url or "").path.lower()
    if any(w in path for w in ("/login", "/signin", "/sign-in")):
        return "redirected to a login page"
    if 'type="password"' in text or "type='password'" in text:
        if any(p in text for p in LOGIN_PHRASES):
            return "login required"
    return None


# ---- the main function ----------------------------------------------------------------------

def polite_get(url: str, domain_settings: dict | None = None,
               session: requests.Session | None = None,
               params: dict | None = None, headers: dict | None = None):
    """
    Fetch a URL politely. Returns the response, or None if we skipped it
    (disallowed by robots.txt, access denied, CAPTCHA/login, or repeated failure).
    The reason is logged; websites we must stop using are listed by get_fallbacks().
    """
    settings = domain_settings if domain_settings is not None else {}
    ua = settings.get("user_agent") or config.get_user_agent()
    if not ua:
        log.error("CONTACT_EMAIL is not set in .env - refusing to fetch anonymously")
        return None

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        log.error("not a web address: %s", url)
        return None
    domain = parsed.netloc.lower()
    session = session or requests.Session()

    if domain in _manual_inbox_flags:
        log.info("skipping %s: %s is in manual inbox mode", url, domain)
        return None

    # 1. robots.txt
    rp = _get_robots(parsed.scheme, domain, settings, ua, session)
    if rp is None:
        return None
    if not rp.can_fetch(ua, url):
        # Skip just this page; other pages on the site may still be allowed.
        _robots_skipped.setdefault(domain, set()).add(parsed.path or "/")
        log.warning("skipping %s: robots.txt disallows it (use the manual inbox for these)", url)
        return None

    # 2. the request, with retries for temporary problems
    max_retries = int(settings.get("max_retries", 3))
    base = float(settings.get("backoff_base_seconds", 2))
    for attempt in range(max_retries + 1):
        try:
            resp = _request(url, settings, ua, session, params, headers)
        except requests.RequestException as e:
            problem = f"network error: {e}"
        else:
            code = resp.status_code
            if code in (401, 403):
                _flag_manual_inbox(domain, f"HTTP {code} access denied at {url}")
                return None
            if code == 429 or code >= 500:
                problem = f"HTTP {code}"
                retry_after = resp.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    _sleep(min(int(retry_after), 60))
            elif code >= 400:
                log.warning("%s returned HTTP %s; not retrying", url, code)
                return None
            else:
                reason = looks_like_block_page(resp)
                if reason:
                    _flag_manual_inbox(domain, f"{reason} at {url}")
                    return None
                return resp

        if attempt < max_retries:
            wait = base * (2 ** attempt)   # 2s, 4s, 8s
            log.info("%s for %s - retry %d/%d in %.0fs", problem, url, attempt + 1, max_retries, wait)
            _sleep(wait)
        else:
            log.error("giving up on %s after %d tries (%s)", url, max_retries + 1, problem)
    return None


def fetch_and_store(con, url: str, domain_settings: dict | None = None, *,
                    source: str, doc_category: str, collector_name: str,
                    session: requests.Session | None = None,
                    **meta) -> StoreResult | None:
    """
    polite_get + save the unchanged bytes + log in raw_files.
    `meta` can include state, county, city, source_record_id, notes, ext, scope.
    Returns None if the page was skipped.
    """
    resp = polite_get(url, domain_settings, session=session)
    if resp is None:
        return None
    ext = meta.pop("ext", None) or ext_from_content_type(resp.headers.get("Content-Type")) \
        or (url.rsplit(".", 1)[-1][:5] if "." in url.rsplit("/", 1)[-1] else "html")
    return store_file(con, resp.content, source=source, doc_category=doc_category,
                      collector_name=collector_name, url=url, ext=ext, **meta)
