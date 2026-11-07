"""Shared test helpers: a throwaway database + data folder, and a pretend web."""
import pytest

from collectors.common import config, db, fetch


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point the pipeline at a temporary folder so tests never touch real data."""
    monkeypatch.setenv("BLOOMFIELD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CONTACT_EMAIL", "tester@example.com")
    config._env_loaded = True
    fetch.reset_state()
    # Tests must not really wait for the rate limiter or retry backoff.
    monkeypatch.setattr(fetch, "_sleep", lambda s: None)
    return tmp_path


@pytest.fixture
def con(env):
    connection = db.connect(":memory:")
    yield connection
    connection.close()


class FakeResponse:
    def __init__(self, text="", status=200, content_type="text/html", url="https://example.org/x"):
        self.status_code = status
        self.text = text
        self.content = text.encode()
        self.headers = {"Content-Type": content_type}
        self.url = url


class FakeSession:
    """Pretend website. `pages` maps a URL to a FakeResponse (or a list, used one at a time)."""

    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.requested.append(url)
        page = self.pages.get(url)
        if page is None:
            return FakeResponse("not found", 404, url=url)
        if isinstance(page, list):
            page = page.pop(0) if len(page) > 1 else page[0]
        page.url = url
        return page
