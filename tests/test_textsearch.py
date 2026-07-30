from epub_scraper.epub_writer import build_epub
from epub_scraper.textsearch import search_epub_text


def _build(tmp_path, chapters):
    out = str(tmp_path / "book.epub")
    build_epub("Title", "site", "id", chapters, out)
    return out


def test_search_epub_text_case_insensitive_by_default(tmp_path):
    path = _build(tmp_path, [("Ch 1", "<p>The Dragon roared loudly.</p>")])
    hits = search_epub_text(path, "dragon")
    assert len(hits) == 1
    assert "Dragon" in hits[0].snippet


def test_search_epub_text_case_sensitive_flag_respected(tmp_path):
    path = _build(tmp_path, [("Ch 1", "<p>The Dragon roared loudly.</p>")])
    hits = search_epub_text(path, "dragon", ignore_case=False)
    assert hits == []
    hits = search_epub_text(path, "Dragon", ignore_case=False)
    assert len(hits) == 1


def test_search_epub_text_context_window_length_respected(tmp_path):
    # no space adjacent to NEEDLE, so the context window's edge characters
    # are unambiguously x's/y's, not a get_text() separator space.
    path = _build(tmp_path, [("Ch 1", "<p>" + "x" * 100 + "NEEDLE" + "y" * 100 + "</p>")])
    hits = search_epub_text(path, "NEEDLE", context=10)
    assert len(hits) == 1
    snippet = hits[0].snippet
    assert snippet.count("x") == 10
    assert snippet.count("y") == 10


def test_search_epub_text_multiple_matches_in_one_chapter(tmp_path):
    path = _build(tmp_path, [("Ch 1", "<p>needle here and needle there and needle everywhere.</p>")])
    hits = search_epub_text(path, "needle")
    assert len(hits) == 3


def test_search_epub_text_no_matches_returns_empty_list(tmp_path):
    path = _build(tmp_path, [("Ch 1", "<p>Nothing relevant here.</p>")])
    assert search_epub_text(path, "zzz-not-present") == []


def test_search_epub_text_excludes_nav_document(tmp_path):
    # regression: nav.xhtml (ebooklib's own TOC doc) used to be returned as a
    # spurious extra "chapter" whenever the query matched any chapter title.
    path = _build(tmp_path, [
        ("Unique Chapter Title", "<p>Some prose.</p>"),
        ("Another Unique Title", "<p>More prose.</p>"),
    ])
    hits = search_epub_text(path, "Title")
    assert all(h.chapter_title != "nav.xhtml" for h in hits)
    assert {h.chapter_title for h in hits} == {"Unique Chapter Title", "Another Unique Title"}
