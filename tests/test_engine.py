import pytest

from epub_scraper import engine
from epub_scraper.profile import (ChapterResult, IndexResult, MetadataResult,
                                   SearchResult, SiteProfile)
from epub_scraper.sites.fanmtl import PROFILE
from conftest import load_fixture
from fakes import FakeResponse, FakeSession
from html_builders import fanmtl_chapter_html, fanmtl_index_html, fanmtl_index_html_no_links


# -- parse_chapter ------------------------------------------------------------

def test_parse_chapter_real_fixture_normal():
    html = load_fixture("fanmtl_chapter_kks30150_001.html")
    result = engine.parse_chapter(PROFILE, html, 1)
    assert result.title == "Chapter 1 3-Legged Ding"
    assert "<p>" in result.body_html


def test_parse_chapter_real_fixture_ad_splice_drops_whole_paragraph():
    # ch266: the ad text is appended onto the END of a paragraph that starts
    # with real content, in the same <p> -- skip_phrases drops the whole
    # paragraph, real sentence included, not just the ad sentence.
    html = load_fixture("fanmtl_chapter_kks30150_266.html")
    result = engine.parse_chapter(PROFILE, html, 266)
    assert "Bookmark this page" not in result.body_html
    assert "possessing its own complete memories" not in result.body_html


def test_parse_chapter_real_fixture_ad_paragraph_standalone_variant():
    # ch313: here the ad text is its OWN standalone paragraph (different from
    # ch266's mid-sentence splice), different exact wording ("to continue
    # reading" vs. "and continue reading") -- still caught by skip_phrases.
    html = load_fixture("fanmtl_chapter_kks30150_313.html")
    result = engine.parse_chapter(PROFILE, html, 313)
    assert "Bookmark this page" not in result.body_html


def test_parse_chapter_drops_short_paragraphs():
    html = fanmtl_chapter_html(["Ok.", "This one is long enough to keep around."])
    result = engine.parse_chapter(PROFILE, html, 1)
    assert "Ok." not in result.body_html
    assert "long enough to keep" in result.body_html


def test_parse_chapter_drops_short_link_paragraphs():
    html = fanmtl_chapter_html(['<a href="/x">short link</a>',
                                 "A real paragraph with plenty of actual prose in it."])
    result = engine.parse_chapter(PROFILE, html, 1)
    assert "short link" not in result.body_html
    assert "real paragraph" in result.body_html


@pytest.mark.parametrize("phrase", PROFILE.skip_phrases)
def test_parse_chapter_drops_all_configured_skip_phrases(phrase):
    html = fanmtl_chapter_html([f"Some text containing {phrase} inside it, padded to be long."])
    result = engine.parse_chapter(PROFILE, html, 1)
    assert phrase not in result.body_html


def test_parse_chapter_escapes_xhtml_special_chars():
    # &lt;/&gt; here so BeautifulSoup's get_text() decodes them back to literal
    # "<fighting>" text (not an actual tag) -- exercising _escape_xhtml's
    # re-escaping of that text for the output XHTML.
    html = fanmtl_chapter_html(["Cats &amp; dogs &lt;fighting&gt; in the yard, long enough."])
    result = engine.parse_chapter(PROFILE, html, 1)
    assert "&amp;" in result.body_html
    assert "&lt;fighting&gt;" in result.body_html
    assert "<fighting>" not in result.body_html


def test_parse_chapter_missing_h2_uses_fallback_title():
    html = fanmtl_chapter_html(["A perfectly ordinary paragraph of prose."], include_h2=False)
    result = engine.parse_chapter(PROFILE, html, 42)
    assert result.title == "Chapter 42"


def test_parse_chapter_fn_escape_hatch_bypasses_declarative_fields():
    profile = SiteProfile(
        site_key="x", domains=[], chapter_link_pattern="", index_url_id_pattern="",
        chapter_number_fallback_pattern="", chapter_count_pattern="",
        chapter_url_template="", parse_chapter_fn=lambda html, n: ChapterResult("X", "Y"))
    result = engine.parse_chapter(profile, "<html></html>", 1)
    assert result == ChapterResult("X", "Y")


# -- parse_index ----------------------------------------------------------------

def test_parse_index_real_fixture_fanmtl():
    html = load_fixture("fanmtl_index_kks30150.html")
    result = engine.parse_index(PROFILE, html, "https://www.fanmtl.com/novel/kks30150.html")
    assert result.chapter_id == "kks30150"
    assert result.total >= 300  # loose bound: real, ongoing novel, fixture frozen at capture time
    assert result.title
    assert result.base_url == "https://www.fanmtl.com"


def test_parse_index_via_fallback_max_link_scan():
    html = fanmtl_index_html(chapter_id="abc", total=42, with_count_text=False)
    result = engine.parse_index(PROFILE, html, "https://www.fanmtl.com/novel/abc.html")
    assert result.total == 42
    assert result.chapter_id == "abc"


def test_parse_index_primary_count_pattern_wins_when_present():
    html = fanmtl_index_html(chapter_id="abc", total=10, with_count_text=True, count_text_total=99)
    result = engine.parse_index(PROFILE, html, "https://www.fanmtl.com/novel/abc.html")
    assert result.total == 99


def test_parse_index_falls_back_to_url_id_pattern_when_no_links():
    html = fanmtl_index_html_no_links()
    result = engine.parse_index(PROFILE, html, "https://www.fanmtl.com/novel/xyz123.html")
    assert result.chapter_id == "xyz123"


def test_parse_index_missing_h1_uses_unknown_novel_fallback():
    result = engine.parse_index(PROFILE, "<html><body>no title here</body></html>",
                                 "https://www.fanmtl.com/novel/abc.html")
    assert result.title == "Unknown Novel"


def test_parse_index_no_chapter_id_found_anywhere():
    result = engine.parse_index(PROFILE, "<html><body><h1>T</h1></body></html>",
                                 "https://www.fanmtl.com/not-a-novel-url")
    assert result.chapter_id is None


def test_parse_index_fn_escape_hatch():
    profile = SiteProfile(
        site_key="x", domains=[], chapter_link_pattern="", index_url_id_pattern="",
        chapter_number_fallback_pattern="", chapter_count_pattern="",
        chapter_url_template="",
        parse_index_fn=lambda html, url: IndexResult("T", "id", 3, "https://x"))
    result = engine.parse_index(profile, "<html></html>", "https://x/novel/id.html")
    assert result == IndexResult("T", "id", 3, "https://x")


def test_parse_metadata_real_fixture_fanmtl():
    html = load_fixture("fanmtl_index_kks30150.html")
    result = engine.parse_metadata(PROFILE, html)
    assert result.alt_title == "挑夫修仙：我有5级满铭文"
    assert result.author == "佚名"
    assert result.status == "Ongoing"
    assert result.genres == ["Wuxia Xianxia"]
    assert result.rating == ""
    assert result.synopsis and result.synopsis.startswith("Awakening to the mystery of his birth")


def test_parse_metadata_fn_dispatches():
    expected = MetadataResult(synopsis="S", genres=["G"], author="A",
                               alt_title="Alt", status="Ongoing", rating="4.5")
    profile = SiteProfile(
        site_key="x", domains=[], chapter_link_pattern="", index_url_id_pattern="",
        chapter_number_fallback_pattern="", chapter_count_pattern="",
        chapter_url_template="", parse_metadata_fn=lambda html: expected)
    result = engine.parse_metadata(profile, "<html></html>")
    assert result == expected


def test_parse_metadata_raises_notimplementederror_when_unconfigured():
    profile = SiteProfile(
        site_key="x", domains=[], chapter_link_pattern="", index_url_id_pattern="",
        chapter_number_fallback_pattern="", chapter_count_pattern="", chapter_url_template="")
    with pytest.raises(NotImplementedError):
        engine.parse_metadata(profile, "<html></html>")


def test_parse_index_skips_nofollow_hidden_decoy_for_chapter_id():
    html = ('<html><body><h1>Test Novel</h1>'
            '<a href="/novel/decoy_999.html" rel="nofollow" style="display:none">Ch 999</a>'
            '<a href="/novel/abc_1.html">Chapter 1</a>'
            '</body></html>')
    result = engine.parse_index(PROFILE, html, "https://www.fanmtl.com/novel/abc.html")
    assert result.chapter_id == "abc"


def test_parse_index_skips_aria_hidden_decoy_for_chapter_id():
    html = ('<html><body><h1>Test Novel</h1>'
            '<a href="/novel/decoy_999.html" aria-hidden="true">Ch 999</a>'
            '<a href="/novel/abc_1.html">Chapter 1</a>'
            '</body></html>')
    result = engine.parse_index(PROFILE, html, "https://www.fanmtl.com/novel/abc.html")
    assert result.chapter_id == "abc"


def test_parse_index_skips_decoy_link_in_fallback_max_scan():
    html = ('<html><body><h1>Test Novel</h1>'
            '<a href="/novel/abc_1.html">Chapter 1</a>'
            '<a href="/novel/abc_999.html" rel="nofollow">Decoy</a>'
            '</body></html>')
    result = engine.parse_index(PROFILE, html, "https://www.fanmtl.com/novel/abc.html")
    assert result.total == 1


def test_parse_index_skips_fully_transparent_decoy_link():
    html = ('<html><body><h1>Test Novel</h1>'
            '<a href="/novel/decoy_999.html" style="opacity: 0">Ch 999</a>'
            '<a href="/novel/abc_1.html">Chapter 1</a>'
            '</body></html>')
    result = engine.parse_index(PROFILE, html, "https://www.fanmtl.com/novel/abc.html")
    assert result.chapter_id == "abc"
    assert result.total == 1  # 999 would leak through if the decoy weren't skipped


def test_parse_index_keeps_partially_transparent_link():
    # opacity:0.85 is a perfectly visible link -- prefix-matching "opacity:0"
    # silently dropped it. The index URL here deliberately does NOT match the
    # chapter_id fallback pattern, so the link is the only possible source.
    html = ('<html><body><h1>Test Novel</h1>'
            '<a href="/novel/abc_1.html" style="opacity:0.85">Chapter 1</a>'
            '</body></html>')
    result = engine.parse_index(PROFILE, html, "https://www.fanmtl.com/not-a-novel-url")
    assert result.chapter_id == "abc"
    assert result.total == 1


def test_parse_index_keeps_link_with_opacity_one():
    html = ('<html><body><h1>Test Novel</h1>'
            '<a href="/novel/abc_1.html" style="opacity:1;color:red">Chapter 1</a>'
            '</body></html>')
    result = engine.parse_index(PROFILE, html, "https://www.fanmtl.com/not-a-novel-url")
    assert result.chapter_id == "abc"


def test_parse_index_skips_zero_opacity_followed_by_another_declaration():
    html = ('<html><body><h1>Test Novel</h1>'
            '<a href="/novel/decoy_999.html" style="opacity:0;color:red">Ch 999</a>'
            '<a href="/novel/abc_1.html">Chapter 1</a>'
            '</body></html>')
    result = engine.parse_index(PROFILE, html, "https://www.fanmtl.com/novel/abc.html")
    assert result.total == 1


def test_parse_index_skips_mixed_case_nofollow_decoy():
    # HTML link types are ASCII case-insensitive per spec.
    html = ('<html><body><h1>Test Novel</h1>'
            '<a href="/novel/abc_1.html">Chapter 1</a>'
            '<a href="/novel/abc_999.html" rel="NoFollow">Decoy</a>'
            '</body></html>')
    result = engine.parse_index(PROFILE, html, "https://www.fanmtl.com/novel/abc.html")
    assert result.total == 1


def test_parse_index_normal_links_without_decoys_are_unaffected():
    html = fanmtl_index_html(chapter_id="abc", total=5, with_count_text=False)
    result = engine.parse_index(PROFILE, html, "https://www.fanmtl.com/novel/abc.html")
    assert result.chapter_id == "abc"
    assert result.total == 5


def test_chapter_url_formats_template():
    url = engine.chapter_url(PROFILE, "https://www.fanmtl.com", "abc", 7)
    assert url == "https://www.fanmtl.com/novel/abc_7.html"


# -- search_novels ----------------------------------------------------------------

def test_search_novels_real_fixture_fanmtl():
    html = load_fixture("fanmtl_search_results.html")
    session = FakeSession({PROFILE.search_url: FakeResponse(html, 200, PROFILE.search_url)})
    results = engine.search_novels(PROFILE, session, "cult")

    assert len(results) == 20
    assert results[0] == SearchResult(
        "Douluo Continent: The insane cultivation speed left Tang San speechless.",
        "https://www.fanmtl.com/novel/kks39751.html", 25)

    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert kwargs["data"]["keyboard"] == "cult"
    assert kwargs["data"]["show"] == "title"
    assert kwargs["data"]["tempid"] == "1"
    assert kwargs["data"]["tbname"] == "news"


def test_search_novels_skips_items_without_href():
    html = '<ul><li class="novel-item"><h4>No link here</h4></li></ul>'
    session = FakeSession({PROFILE.search_url: FakeResponse(html, 200, PROFILE.search_url)})
    results = engine.search_novels(PROFILE, session, "x")
    assert results == []


def test_search_novels_missing_chapter_count_yields_none():
    html = '<ul><li class="novel-item"><a href="/novel/abc.html" title="T"></a></li></ul>'
    session = FakeSession({PROFILE.search_url: FakeResponse(html, 200, PROFILE.search_url)})
    results = engine.search_novels(PROFILE, session, "x")
    assert results == [SearchResult("T", "https://www.fanmtl.com/novel/abc.html", None)]


def test_search_novels_raises_notimplementederror_when_unconfigured():
    profile = SiteProfile(
        site_key="x", domains=[], chapter_link_pattern="", index_url_id_pattern="",
        chapter_number_fallback_pattern="", chapter_count_pattern="", chapter_url_template="")
    with pytest.raises(NotImplementedError):
        engine.search_novels(profile, FakeSession(), "x")


def test_search_novels_get_method_used_when_configured():
    profile = SiteProfile(
        site_key="x", domains=[], chapter_link_pattern="", index_url_id_pattern="",
        chapter_number_fallback_pattern="", chapter_count_pattern="", chapter_url_template="",
        search_base_url="https://x", search_url="https://x/search",
        search_method="get", search_query_param="q", search_result_selector="li")
    session = FakeSession({"https://x/search": FakeResponse("<ul></ul>", 200, "https://x/search")})
    engine.search_novels(profile, session, "hello")
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert kwargs["params"] == {"q": "hello"}


def test_search_novels_fn_escape_hatch():
    profile = SiteProfile(
        site_key="x", domains=[], chapter_link_pattern="", index_url_id_pattern="",
        chapter_number_fallback_pattern="", chapter_count_pattern="", chapter_url_template="",
        search_fn=lambda session, query: [SearchResult("X", "https://x", 1)])
    results = engine.search_novels(profile, FakeSession(), "x")
    assert results == [SearchResult("X", "https://x", 1)]
