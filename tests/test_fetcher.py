import requests
import pytest

from epub_scraper.fetcher import fetch
from fakes import FakeResponse, FakeSession


def test_fetch_returns_text_on_200():
    url = "https://example.com/page.html"
    session = FakeSession({url: FakeResponse("hello", 200, url)})
    assert fetch(url, session) == "hello"


def test_fetch_uses_15s_timeout():
    url = "https://example.com/page.html"
    session = FakeSession({url: FakeResponse("hello", 200, url)})
    fetch(url, session)
    assert session.calls == [("GET", url, {"timeout": 15, "params": None})]


def test_fetch_raises_httperror_on_4xx():
    url = "https://example.com/missing.html"
    session = FakeSession({url: FakeResponse("", 404, url)})
    with pytest.raises(requests.HTTPError) as exc_info:
        fetch(url, session)
    assert exc_info.value.response.status_code == 404


def test_fetch_raises_httperror_on_5xx():
    url = "https://example.com/broken.html"
    session = FakeSession({url: FakeResponse("", 500, url)})
    with pytest.raises(requests.HTTPError) as exc_info:
        fetch(url, session)
    assert exc_info.value.response.status_code == 500


def test_fetch_propagates_generic_exceptions_unchanged():
    url = "https://example.com/timeout.html"
    session = FakeSession({url: requests.exceptions.ConnectionError("refused")})
    with pytest.raises(requests.exceptions.ConnectionError, match="refused"):
        fetch(url, session)
