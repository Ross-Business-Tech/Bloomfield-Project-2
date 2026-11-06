"""Test setup: every test gets its own empty SQLite database and data folder.
No test touches the network."""

import sys
from pathlib import Path

import pytest

NEEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(NEEL))
FIXTURES = NEEL / "fixtures"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("RAW_FILES_DIR", raising=False)
    monkeypatch.delenv("DB_URL", raising=False)
    monkeypatch.setenv("USER_AGENT", "Bloomfield AI Sprint test test@example.com")


@pytest.fixture
def engine(tmp_path):
    from collectors.common import get_engine, init_db

    eng = get_engine(f"sqlite:///{tmp_path / 'test.sqlite'}")
    init_db(eng)
    return eng


class FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200, content_type: str = "text/html"):
        self.content = content
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        self.text = content.decode("utf-8", errors="replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        import json

        return json.loads(self.content)


class FakeSession:
    """Stands in for requests.Session: serves bytes from a dict {url: bytes or FakeResponse}."""

    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.calls = []

    def request(self, method, url, **kwargs):
        assert "timeout" in kwargs, "every HTTP call must have a timeout"
        self.calls.append(url)
        if url not in self.routes:
            return FakeResponse(b"not found", 404)
        r = self.routes[url]
        return r if isinstance(r, FakeResponse) else FakeResponse(r)


@pytest.fixture
def fake_client():
    from collectors.common import HttpClient

    def make(routes):
        return HttpClient(ua="test test@example.com", session=FakeSession(routes), default_rate=1000)

    return make
