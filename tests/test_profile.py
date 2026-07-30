import dataclasses

import pytest

from epub_scraper.profile import SearchResult, SiteProfile


def _minimal_profile(**overrides):
    kwargs = dict(
        site_key="test",
        domains=["example.com"],
        chapter_link_pattern=r"/novel/([^/]+?)_(\d+)\.html",
        index_url_id_pattern=r"/novel/([^/]+?)\.html",
        chapter_number_fallback_pattern=r"_(\d+)\.html",
        chapter_count_pattern=r"(\d+)\s+Chapters?",
        chapter_url_template="{base_url}/novel/{chapter_id}_{n}.html",
    )
    kwargs.update(overrides)
    return SiteProfile(**kwargs)


def test_siteprofile_is_frozen():
    profile = _minimal_profile()
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.site_key = "other"


def test_siteprofile_default_field_values():
    profile = _minimal_profile()
    assert profile.index_title_selector == "h1"
    assert profile.chapter_title_selector == "h2"
    assert profile.chapter_title_fallback == "Chapter {n}"
    assert profile.paragraph_selector == "p"
    assert profile.min_paragraph_length == 4
    assert profile.link_paragraph_max_length == 80
    assert profile.search_method == "get"
    assert profile.search_url is None
    assert profile.search_fn is None
    assert profile.parse_index_fn is None
    assert profile.parse_chapter_fn is None


def test_siteprofile_mutable_defaults_independent_per_instance():
    a = _minimal_profile()
    b = _minimal_profile()
    a.skip_phrases.append("only on a")
    assert b.skip_phrases == []
    a.search_extra_params["x"] = "1"
    assert b.search_extra_params == {}


def test_search_result_fields():
    r = SearchResult(title="T", url="https://x", chapters=5)
    assert (r.title, r.url, r.chapters) == ("T", "https://x", 5)
