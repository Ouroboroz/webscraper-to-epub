import pytest

from epub_scraper.profile import CatalogEntry
from epub_scraper.sites import PROFILES, resolve_profile
from epub_scraper.sites.fanmtl import PROFILE, parse_fanmtl_catalog_page
from conftest import load_fixture


def test_profiles_registry_contains_fanmtl():
    assert PROFILES["fanmtl"] is PROFILE


def test_resolve_profile_by_domain_bare():
    assert resolve_profile("https://fanmtl.com/novel/x.html") is PROFILE


def test_resolve_profile_by_domain_www():
    assert resolve_profile("https://www.fanmtl.com/novel/x.html") is PROFILE


def test_resolve_profile_explicit_site_key_overrides_domain():
    assert resolve_profile("https://some-other-site.com/novel/x.html", site_key="fanmtl") is PROFILE


def test_resolve_profile_unknown_site_key_raises_systemexit():
    with pytest.raises(SystemExit):
        resolve_profile("https://fanmtl.com/novel/x.html", site_key="nope")


def test_resolve_profile_unrecognized_domain_raises_systemexit():
    with pytest.raises(SystemExit):
        resolve_profile("https://totally-unknown-site.example/novel/x.html")


def test_fanmtl_profile_chapter_url_template_and_skip_phrases_spot_check():
    assert PROFILE.chapter_url_template == "{base_url}/novel/{chapter_id}_{n}.html"
    assert "Bookmark this page" in PROFILE.skip_phrases
    assert PROFILE.paragraph_selector == "div.chapter-content p"


# -- parse_fanmtl_catalog_page -------------------------------------------------

def test_parse_fanmtl_catalog_page_real_fixture():
    html = load_fixture("fanmtl_catalog_page0.html")
    entries = parse_fanmtl_catalog_page(html)

    assert len(entries) == 30
    assert entries[0] == CatalogEntry(
        title="Naruto: My Chat Group Spans History.",
        url="https://www.fanmtl.com/novel/qb9211.html",
        chapter_id="qb9211", chapters=268, status="Completed",
        updated_text="8 hours ago")


def test_parse_fanmtl_catalog_page_skips_cards_without_href():
    html = '<ul><li class="novel-item"><h4>No link here</h4></li></ul>'
    assert parse_fanmtl_catalog_page(html) == []


def test_parse_fanmtl_catalog_page_missing_stats_yields_none_fields():
    html = ('<li class="novel-item"><a href="/novel/abc.html" title="T">'
            '<h4 class="novel-title">T</h4></a></li>')
    entries = parse_fanmtl_catalog_page(html)
    assert entries == [CatalogEntry(
        title="T", url="https://www.fanmtl.com/novel/abc.html",
        chapter_id="abc", chapters=None, status=None, updated_text=None)]
