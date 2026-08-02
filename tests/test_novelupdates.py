import pytest

from epub_scraper.novelupdates import (ChallengeExpired, NUSearchHit, fetch_series,
                                        looks_like_challenge_page, search)
from conftest import load_fixture
from fakes import FakeResponse, FakeSession
from html_builders import nu_search_html, nu_series_html

BASE_URL = "https://www.novelupdates.com"


def test_looks_like_challenge_page_real_fixture():
    html = load_fixture("novelupdates_cloudflare_challenge.html")
    assert looks_like_challenge_page(html) is True


def test_looks_like_challenge_page_false_for_real_content():
    assert looks_like_challenge_page(nu_series_html(title="Reverend Insanity")) is False


# -- search ---------------------------------------------------------------------

def test_search_parses_hits():
    html = nu_search_html([
        ("Reverend Insanity", "https://www.novelupdates.com/series/reverend-insanity/"),
        ("Some Other Novel", "https://www.novelupdates.com/series/some-other/"),
    ])
    session = FakeSession({f"{BASE_URL}/": FakeResponse(html, 200, f"{BASE_URL}/")})

    hits = search(session, "reverend insanity")

    assert hits == [
        NUSearchHit("Reverend Insanity", "https://www.novelupdates.com/series/reverend-insanity/"),
        NUSearchHit("Some Other Novel", "https://www.novelupdates.com/series/some-other/"),
    ]
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert kwargs["params"] == {"s": "reverend insanity", "post_type": "seriesplan"}


def test_search_respects_limit():
    html = nu_search_html([(f"Novel {i}", f"https://www.novelupdates.com/series/n{i}/")
                            for i in range(5)])
    session = FakeSession({f"{BASE_URL}/": FakeResponse(html, 200, f"{BASE_URL}/")})
    assert len(search(session, "novel", limit=2)) == 2


def test_search_raises_challenge_expired_when_blocked():
    html = load_fixture("novelupdates_cloudflare_challenge.html")
    session = FakeSession({f"{BASE_URL}/": FakeResponse(html, 200, f"{BASE_URL}/")})
    with pytest.raises(ChallengeExpired):
        search(session, "anything")


# -- fetch_series -----------------------------------------------------------------

def test_fetch_series_parses_high_confidence_fields():
    url = "https://www.novelupdates.com/series/reverend-insanity/"
    html = nu_series_html(
        title="Reverend Insanity",
        associated_names=["Xin Xi Lu", "脑抽的"],
        genres=["Action", "Fantasy"],
        tags=["Reincarnation", "Cultivation"],
        author="Gu Zhen Ren",
        translation_status="Ongoing",
    )
    session = FakeSession({url: FakeResponse(html, 200, url)})

    result = fetch_series(session, url)

    assert result.title == "Reverend Insanity"
    assert result.associated_names == ["Xin Xi Lu", "脑抽的"]
    assert result.genres == ["Action", "Fantasy"]
    assert result.tags == ["Reincarnation", "Cultivation"]
    assert result.author == "Gu Zhen Ren"
    assert result.translation_status == "Ongoing"


def test_fetch_series_parses_medium_confidence_fields():
    url = "https://www.novelupdates.com/series/x/"
    html = nu_series_html(translation_groups=["Group One", "Group Two"],
                           release_frequency="Every 13.1 Day(s)",
                           rating="4.3", votes="1700")
    session = FakeSession({url: FakeResponse(html, 200, url)})

    result = fetch_series(session, url)

    assert result.translation_groups == ["Group One", "Group Two"]
    assert result.release_frequency == "Every 13.1 Day(s)"
    assert result.rating == "4.3"
    assert result.votes == "1700"


def test_fetch_series_missing_optional_fields_are_none_or_empty():
    url = "https://www.novelupdates.com/series/x/"
    html = nu_series_html(title="X")
    session = FakeSession({url: FakeResponse(html, 200, url)})

    result = fetch_series(session, url)

    assert result.associated_names == []
    assert result.author is None
    assert result.release_frequency is None


def test_fetch_series_real_fixture_reverend_insanity():
    # Captured 2026-08-02 via a real solved session (see solve_challenge_session's
    # docstring) -- the only real (non-synthetic) NU series page fixture here.
    url = "https://www.novelupdates.com/series/reverend-insanity/"
    html = load_fixture("novelupdates_series_reverend_insanity.html")
    session = FakeSession({url: FakeResponse(html, 200, url)})

    result = fetch_series(session, url)

    assert result.title == "Reverend Insanity"
    assert "Gu Zhen Ren" in result.author
    assert "Action" in result.genres
    assert "Cultivation" in result.tags
    assert result.translation_status == "2334 Chapters (Cancelled/Banned)"
    assert result.release_frequency == "Every 13.1 Day(s)"
    assert result.rating == "4.3"
    assert result.votes == "1700"


def test_fetch_series_raises_challenge_expired_when_blocked():
    url = "https://www.novelupdates.com/series/x/"
    html = load_fixture("novelupdates_cloudflare_challenge.html")
    session = FakeSession({url: FakeResponse(html, 200, url)})
    with pytest.raises(ChallengeExpired):
        fetch_series(session, url)
