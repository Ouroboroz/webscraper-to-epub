import os

from epub_scraper.cache import cache_path, load_cached, save_cache


def test_cache_path_format(cache_dir):
    assert cache_path(cache_dir, "abc", 7) == os.path.join(cache_dir, "abc_7.html")


def test_load_cached_returns_none_when_missing(cache_dir):
    assert load_cached(cache_dir, "abc", 1) is None


def test_save_then_load_roundtrip_unicode(cache_dir):
    save_cache(cache_dir, "abc", 1, "<p>héllo — 世界</p>")
    assert load_cached(cache_dir, "abc", 1) == "<p>héllo — 世界</p>"


def test_save_cache_creates_missing_directory(cache_dir):
    assert not os.path.exists(cache_dir)
    save_cache(cache_dir, "abc", 1, "<p>x</p>")
    assert os.path.isdir(cache_dir)


def test_save_cache_overwrites_existing_file(cache_dir):
    save_cache(cache_dir, "abc", 1, "<p>old</p>")
    save_cache(cache_dir, "abc", 1, "<p>new</p>")
    assert load_cached(cache_dir, "abc", 1) == "<p>new</p>"
