import pytest

from epub_scraper.novelupdates import (ChallengeExpired, NUSearchHit, fetch_series,
                                        list_series, looks_like_challenge_page, search)
from conftest import load_fixture
from fakes import FakeResponse, FakeSession
from html_builders import nu_listing_html, nu_search_html, nu_series_html

BASE_URL = "https://www.novelupdates.com"
LISTING_URL = f"{BASE_URL}/novelslisting/"


def test_looks_like_challenge_page_real_fixture():
    html = load_fixture("novelupdates_cloudflare_challenge.html")
    assert looks_like_challenge_page(html) is True


def test_looks_like_challenge_page_false_for_real_content():
    assert looks_like_challenge_page(nu_series_html(title="Reverend Insanity")) is False


# -- search ---------------------------------------------------------------------

AJAX_URL = f"{BASE_URL}/wp-admin/admin-ajax.php"


def test_search_parses_hits():
    html = nu_search_html([
        ("Reverend Insanity", "https://www.novelupdates.com/series/reverend-insanity/"),
        ("Some Other Novel", "https://www.novelupdates.com/series/some-other/"),
    ])
    session = FakeSession({AJAX_URL: FakeResponse(html, 200, AJAX_URL)})

    hits = search(session, "reverend insanity")

    assert hits == [
        NUSearchHit("Reverend Insanity", "https://www.novelupdates.com/series/reverend-insanity/"),
        NUSearchHit("Some Other Novel", "https://www.novelupdates.com/series/some-other/"),
    ]
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert kwargs["data"] == {
        "action": "nd_ajaxsearchmain", "strType": "desktop",
        "strOne": "reverend insanity", "strSearchType": "series",
    }


def test_search_dedupes_by_url_keeping_longest_title():
    # Confirmed against a real multi-hit response (2026-08-02): the same
    # series URL can appear more than once, with shorter/partial link text
    # on some entries -- e.g. matching just an alternate name fragment.
    html = nu_search_html([
        ("omniscient reader", "https://www.novelupdates.com/series/orv/"),
        ("Omniscient Reader's Viewpoint", "https://www.novelupdates.com/series/orv/"),
    ])
    session = FakeSession({AJAX_URL: FakeResponse(html, 200, AJAX_URL)})

    hits = search(session, "omniscient reader")

    assert hits == [NUSearchHit("Omniscient Reader's Viewpoint",
                                 "https://www.novelupdates.com/series/orv/")]


def test_search_no_results_real_fixture():
    html = load_fixture("novelupdates_search_no_results.html")
    session = FakeSession({AJAX_URL: FakeResponse(html, 200, AJAX_URL)})
    assert search(session, "zzzznoresultxyz123") == []


def test_search_real_fixture_reverend_insanity():
    # Captured 2026-08-02 via a real solved session against the live
    # nd_ajaxsearchmain endpoint (see search()'s docstring).
    html = load_fixture("novelupdates_search_reverend_insanity.html")
    session = FakeSession({AJAX_URL: FakeResponse(html, 200, AJAX_URL)})

    hits = search(session, "reverend insanity")

    assert hits == [NUSearchHit("reverend insanity",
                                 "https://www.novelupdates.com/series/reverend-insanity/")]


def test_search_real_fixture_omniscient_reader_multi_hit_dedupe():
    # Captured 2026-08-02: a query matching several series/alt-names,
    # including duplicate <li>s for the same URL with shorter link text.
    html = load_fixture("novelupdates_search_omniscient_reader.html")
    session = FakeSession({AJAX_URL: FakeResponse(html, 200, AJAX_URL)})

    hits = search(session, "omniscient reader")

    assert hits == [
        NUSearchHit("omniscient reader's Viewpoint",
                    "https://www.novelupdates.com/series/omniscient-readers-viewpoint/"),
        NUSearchHit("omniscient reader's Viewpoint – Side Story",
                    "https://www.novelupdates.com/series/omniscient-readers-viewpoint-side-story/"),
    ]


def test_search_respects_limit():
    html = nu_search_html([(f"Novel {i}", f"https://www.novelupdates.com/series/n{i}/")
                            for i in range(5)])
    session = FakeSession({AJAX_URL: FakeResponse(html, 200, AJAX_URL)})
    assert len(search(session, "novel", limit=2)) == 2


def test_search_raises_challenge_expired_when_blocked():
    html = load_fixture("novelupdates_cloudflare_challenge.html")
    session = FakeSession({AJAX_URL: FakeResponse(html, 200, AJAX_URL)})
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


def test_fetch_series_parses_synopsis_from_multiple_paragraphs():
    url = "https://www.novelupdates.com/series/x/"
    html = nu_series_html(title="X", synopsis_paragraphs=["First paragraph.", "Second paragraph."])
    session = FakeSession({url: FakeResponse(html, 200, url)})

    result = fetch_series(session, url)

    assert result.synopsis == "First paragraph.\n\nSecond paragraph."


def test_fetch_series_synopsis_is_none_when_absent():
    url = "https://www.novelupdates.com/series/x/"
    html = nu_series_html(title="X")
    session = FakeSession({url: FakeResponse(html, 200, url)})

    result = fetch_series(session, url)

    assert result.synopsis is None


def test_fetch_series_real_fixture_reverend_insanity_synopsis():
    # Same real captured fixture the other real_fixture test above uses --
    # confirms the synopsis extraction against real markup, not just the
    # synthetic builder.
    url = "https://www.novelupdates.com/series/reverend-insanity/"
    html = load_fixture("novelupdates_series_reverend_insanity.html")
    session = FakeSession({url: FakeResponse(html, 200, url)})

    result = fetch_series(session, url)

    assert result.synopsis is not None
    assert "Fang Yuan" in result.synopsis or "Gu" in result.synopsis


# -- list_series (Novel Updates' own bulk catalog listing) ------------------------

def test_list_series_parses_hits_and_has_next_true():
    html = nu_listing_html([
        ("Novel A", "https://www.novelupdates.com/series/a/"),
        ("Novel B", "https://www.novelupdates.com/series/b/"),
    ], has_next=True)
    session = FakeSession({LISTING_URL: FakeResponse(html, 200, LISTING_URL)})

    hits, has_next = list_series(session, page=1)

    assert hits == [
        NUSearchHit("Novel A", "https://www.novelupdates.com/series/a/"),
        NUSearchHit("Novel B", "https://www.novelupdates.com/series/b/"),
    ]
    assert has_next is True
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert kwargs["params"] == {"st": 1, "pg": 1}


def test_list_series_has_next_false_on_last_page():
    # Confirmed live (2026-08-03): the real last page's pagination widget has
    # no a.next_page link -- NOT a 404 or an empty page (a page past the real
    # boundary silently clamps/repeats the last page's own content instead).
    html = nu_listing_html([("Novel A", "https://www.novelupdates.com/series/a/")],
                            has_next=False)
    session = FakeSession({LISTING_URL: FakeResponse(html, 200, LISTING_URL)})

    hits, has_next = list_series(session, page=99)

    assert len(hits) == 1
    assert has_next is False


def test_list_series_raises_challenge_expired_when_blocked():
    html = load_fixture("novelupdates_cloudflare_challenge.html")
    session = FakeSession({LISTING_URL: FakeResponse(html, 200, LISTING_URL)})
    with pytest.raises(ChallengeExpired):
        list_series(session, page=1)
