from epub_scraper import dataspine
from epub_scraper.dataspine_db import get_novel, init_db
from fakes import FakeResponse, FakeSession
from conftest import load_fixture
from html_builders import fanmtl_catalog_html

BASE_URL = "https://www.fanmtl.com"


def page_url(n):
    return f"{BASE_URL}/list/all/all-newstime-{n}.html"


def run_cli(monkeypatch, argv, session):
    monkeypatch.setattr("sys.argv", ["epub_scraper.dataspine"] + argv)
    monkeypatch.setattr(dataspine.requests, "Session", lambda: session)
    dataspine.main()


def test_cmd_crawl_paginates_until_empty_page_and_marks_candidates(monkeypatch, db_path):
    page0 = fanmtl_catalog_html([
        ("Novel A", "kks30150", 100, "Ongoing"),
        ("Novel B", "b2", 10, "Ongoing"),
    ])
    session = FakeSession({
        page_url(0): FakeResponse(page0, 200, page_url(0)),
        page_url(1): FakeResponse(fanmtl_catalog_html([]), 200, page_url(1)),
    })

    run_cli(monkeypatch, ["crawl", "--db", db_path], session)

    conn = init_db(db_path)
    a = get_novel(conn, "fanmtl", f"{BASE_URL}/novel/kks30150.html")
    b = get_novel(conn, "fanmtl", f"{BASE_URL}/novel/b2.html")
    assert a["chapter_count"] == 100 and a["candidate"] == 1
    assert b["chapter_count"] == 10 and b["candidate"] == 0


def test_cmd_crawl_respects_pages_limit(monkeypatch, db_path):
    # Only page 0 is stubbed -- if --pages 1 didn't stop the loop, the
    # FakeSession's strict mode would raise on the unstubbed page-1 request.
    page0 = fanmtl_catalog_html([("Novel A", "a1", 100, "Ongoing")])
    session = FakeSession({page_url(0): FakeResponse(page0, 200, page_url(0))})

    run_cli(monkeypatch, ["crawl", "--pages", "1", "--db", db_path], session)

    conn = init_db(db_path)
    assert get_novel(conn, "fanmtl", f"{BASE_URL}/novel/a1.html") is not None


def test_cmd_metadata_fills_in_synopsis_for_pending_candidate(monkeypatch, db_path):
    page0 = fanmtl_catalog_html([("Porter's", "kks30150", 300, "Ongoing")])
    crawl_session = FakeSession({
        page_url(0): FakeResponse(page0, 200, page_url(0)),
        page_url(1): FakeResponse(fanmtl_catalog_html([]), 200, page_url(1)),
    })
    run_cli(monkeypatch, ["crawl", "--db", db_path], crawl_session)

    novel_url = f"{BASE_URL}/novel/kks30150.html"
    metadata_html = load_fixture("fanmtl_index_kks30150.html")
    metadata_session = FakeSession({novel_url: FakeResponse(metadata_html, 200, novel_url)})
    run_cli(monkeypatch, ["metadata", "--db", db_path], metadata_session)

    conn = init_db(db_path)
    novel = get_novel(conn, "fanmtl", novel_url)
    assert novel["synopsis"] and novel["synopsis"].startswith("Awakening to the mystery")
    assert novel["alt_title"] == "挑夫修仙：我有5级满铭文"


def test_cmd_metadata_no_pending_candidates_prints_message(monkeypatch, db_path, capsys):
    run_cli(monkeypatch, ["metadata", "--db", db_path], FakeSession({}))
    assert "No candidates pending" in capsys.readouterr().out


def test_cmd_stats_prints_summary(monkeypatch, db_path, capsys):
    page0 = fanmtl_catalog_html([("Novel A", "a1", 100, "Ongoing")])
    session = FakeSession({
        page_url(0): FakeResponse(page0, 200, page_url(0)),
        page_url(1): FakeResponse(fanmtl_catalog_html([]), 200, page_url(1)),
    })
    run_cli(monkeypatch, ["crawl", "--db", db_path], session)

    run_cli(monkeypatch, ["stats", "--db", db_path], FakeSession({}))
    out = capsys.readouterr().out
    assert "Total catalogued : 1" in out
    assert "Candidates       : 1" in out
