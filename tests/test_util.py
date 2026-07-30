import re

from epub_scraper.util import (EPUB_DIR, epub_filename, epub_path,
                                get_base_url, now_iso, sanitize_title)


def test_sanitize_title_colon_becomes_dash_space():
    assert sanitize_title("Global Lord: One more talent") == "Global Lord - One more talent"


def test_sanitize_title_strips_illegal_chars():
    assert sanitize_title('a\\b/c*d?e"f<g>h|i') == "abcdefghi"


def test_sanitize_title_collapses_whitespace():
    assert sanitize_title("  a   b\t\tc  ") == "a b c"


def test_sanitize_title_question_mark_and_bang():
    assert sanitize_title("What treasure chest monster? Call me!") == "What treasure chest monster Call me!"


def test_epub_filename_format():
    assert epub_filename("My Title", 1, 50) == "[Ch 1 - Ch 50] My Title.epub"


def test_epub_filename_sanitizes_title():
    assert epub_filename("A: B", 1, 2) == "[Ch 1 - Ch 2] A - B.epub"


def test_epub_path_joins_directory_and_defaults_to_epubs_dir():
    assert epub_path("Title", 1, 5) == f"{EPUB_DIR}/[Ch 1 - Ch 5] Title.epub"
    assert epub_path("Title", 1, 5, directory="somewhere") == "somewhere/[Ch 1 - Ch 5] Title.epub"


def test_get_base_url_extracts_scheme_and_host():
    assert get_base_url("https://www.fanmtl.com/novel/abc.html") == "https://www.fanmtl.com"


def test_get_base_url_no_match_returns_empty_string():
    assert get_base_url("not-a-url") == ""


def test_now_iso_parseable_utc():
    stamp = now_iso()
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", stamp)
    assert "+00:00" in stamp
