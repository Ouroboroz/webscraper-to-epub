import requests
import pytest

from epub_scraper.fetcher import ChallengeDetected, fetch
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


def test_fetch_raises_challenge_detected_on_challenge_body():
    url = "https://example.com/blocked.html"
    body = ("<html><head><title>Just a moment...</title></head>"
            "<body>Checking your browser before accessing example.com</body></html>")
    session = FakeSession({url: FakeResponse(body, 200, url)})
    with pytest.raises(ChallengeDetected):
        fetch(url, session)


def test_fetch_normal_200_body_is_unaffected_by_challenge_check():
    url = "https://example.com/page.html"
    session = FakeSession({url: FakeResponse("hello", 200, url)})
    assert fetch(url, session) == "hello"


def test_fetch_challenge_check_is_case_insensitive():
    url = "https://example.com/blocked.html"
    body = "<title>JUST A MOMENT...</title>"
    session = FakeSession({url: FakeResponse(body, 200, url)})
    with pytest.raises(ChallengeDetected):
        fetch(url, session)


def test_fetch_machine_token_in_body_is_a_challenge_without_any_title():
    # Vendor tokens never occur in prose, so they're trusted anywhere in the body.
    url = "https://example.com/blocked.html"
    body = '<html><body><div class="cf-browser-verification"></div></body></html>'
    session = FakeSession({url: FakeResponse(body, 200, url)})
    with pytest.raises(ChallengeDetected):
        fetch(url, session)


def test_fetch_prose_containing_just_a_moment_is_not_a_challenge():
    # Regression: "just a moment" is ordinary English and turns up constantly in
    # translated webnovel prose. Matching it in the body would silently discard a
    # real chapter, double the site's persisted pacing interval, and -- after five
    # such checks -- auto-disable the novel. Only <title> is trusted for it.
    url = "https://example.com/chapter_42.html"
    body = ("<html><head><title>Chapter 42 - The Return</title></head>"
            "<body><div class=\"chapter-content\">"
            "<p>Just a moment later, he turned around.</p>"
            "</div></body></html>")
    session = FakeSession({url: FakeResponse(body, 200, url)})
    assert fetch(url, session) == body


def test_fetch_prose_containing_checking_your_browser_is_not_a_challenge():
    url = "https://example.com/chapter_43.html"
    body = ("<html><head><title>Chapter 43</title></head>"
            "<body><p>She kept checking your browser history, he complained.</p>"
            "</body></html>")
    session = FakeSession({url: FakeResponse(body, 200, url)})
    assert fetch(url, session) == body
