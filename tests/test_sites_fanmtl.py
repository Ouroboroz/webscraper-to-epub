import pytest

from epub_scraper.sites import PROFILES, resolve_profile
from epub_scraper.sites.fanmtl import PROFILE


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
