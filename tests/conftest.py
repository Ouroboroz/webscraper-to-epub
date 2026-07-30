import os

import pytest

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES_DIR, name), "r", encoding="utf-8") as f:
        return f.read()


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """scrape_chapters defaults to delay=2.5s and cmd_check to --novel-delay
    5.0s -- without this, a FakeSession-backed suite would still be slow.
    Individual tests that need to assert on sleep timing itself locally
    override this with a call-recording stub instead."""
    monkeypatch.setattr("time.sleep", lambda s: None)


@pytest.fixture
def cache_dir(tmp_path):
    return str(tmp_path / ".cache")


@pytest.fixture
def library_path(tmp_path):
    return str(tmp_path / "library.json")


@pytest.fixture
def epubs_dir(tmp_path):
    d = tmp_path / "epubs"
    d.mkdir()
    return str(d)
