import os

import pytest

from epub_scraper import cache, cli
from fakes import FakeResponse, FakeSession
from html_builders import fanmtl_chapter_html, fanmtl_index_html, fanmtl_index_html_no_links

BASE_URL = "https://www.fanmtl.com"
CHAPTER_ID = "abc"
INDEX_URL = f"{BASE_URL}/novel/{CHAPTER_ID}.html"


def chapter_url(n):
    return f"{BASE_URL}/novel/{CHAPTER_ID}_{n}.html"


def chapter_page(n):
    return fanmtl_chapter_html([f"Real prose for chapter {n}, long enough to keep around."],
                                title=f"Chapter {n}")


def index_page(total, title="Test Novel"):
    return fanmtl_index_html(chapter_id=CHAPTER_ID, total=total, title=title)


def run_cli(monkeypatch, argv, session):
    monkeypatch.setattr("sys.argv", ["epub_scraper"] + argv)
    monkeypatch.setattr(cli.requests, "Session", lambda: session)
    cli.main()


def test_main_happy_path_builds_epub(monkeypatch, tmp_path):
    out = str(tmp_path / "book.epub")
    session = FakeSession({
        INDEX_URL: FakeResponse(index_page(total=2), 200, INDEX_URL),
        chapter_url(1): FakeResponse(chapter_page(1), 200, chapter_url(1)),
        chapter_url(2): FakeResponse(chapter_page(2), 200, chapter_url(2)),
    })

    run_cli(monkeypatch, [INDEX_URL, "--output", out, "--cache-dir", str(tmp_path / ".cache")],
            session)

    assert os.path.exists(out)


def test_main_index_fetch_error_exits_1(monkeypatch, tmp_path):
    session = FakeSession({INDEX_URL: FakeResponse("", 500, INDEX_URL)})
    with pytest.raises(SystemExit) as exc_info:
        run_cli(monkeypatch, [INDEX_URL, "--cache-dir", str(tmp_path / ".cache")], session)
    assert exc_info.value.code == 1


def test_main_missing_chapter_id_exits_1(monkeypatch, tmp_path):
    url = "https://www.fanmtl.com/not-a-novel-path"
    session = FakeSession({url: FakeResponse(fanmtl_index_html_no_links(), 200, url)})
    with pytest.raises(SystemExit) as exc_info:
        run_cli(monkeypatch, [url, "--cache-dir", str(tmp_path / ".cache")], session)
    assert exc_info.value.code == 1


def test_main_no_total_and_no_end_flag_exits_1(monkeypatch, tmp_path):
    session = FakeSession({INDEX_URL: FakeResponse(fanmtl_index_html_no_links(), 200, INDEX_URL)})
    with pytest.raises(SystemExit) as exc_info:
        run_cli(monkeypatch, [INDEX_URL, "--cache-dir", str(tmp_path / ".cache")], session)
    assert exc_info.value.code == 1


def test_main_no_chapters_fetched_exits_1(monkeypatch, tmp_path):
    session = FakeSession({
        INDEX_URL: FakeResponse(index_page(total=1), 200, INDEX_URL),
        chapter_url(1): FakeResponse("", 500, chapter_url(1)),
    })
    with pytest.raises(SystemExit) as exc_info:
        run_cli(monkeypatch, [INDEX_URL, "--cache-dir", str(tmp_path / ".cache")], session)
    assert exc_info.value.code == 1


def test_main_no_cache_flag_forces_fresh_fetch_despite_existing_cache(monkeypatch, tmp_path):
    cache_dir = str(tmp_path / ".cache")
    cache.save_cache(cache_dir, CHAPTER_ID, 1, "<html><body><h2>Stale</h2></body></html>")
    out = str(tmp_path / "book.epub")
    session = FakeSession({
        INDEX_URL: FakeResponse(index_page(total=1), 200, INDEX_URL),
        chapter_url(1): FakeResponse(chapter_page(1), 200, chapter_url(1)),
    })

    run_cli(monkeypatch,
            [INDEX_URL, "--output", out, "--cache-dir", cache_dir, "--no-cache"], session)

    assert len(session.calls) == 2  # index GET + chapter 1 GET (cache bypassed)
    assert cache.load_cached(cache_dir, CHAPTER_ID, 1) == chapter_page(1)
    assert os.path.exists(out)
