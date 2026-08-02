from epub_scraper import dataspine
from epub_scraper.dataspine_db import get_next_page, get_novel, init_db
from fakes import FakeResponse, FakeSession
from conftest import load_fixture
from html_builders import fanmtl_catalog_html, nu_search_html, nu_series_html

NU_BASE_URL = "https://www.novelupdates.com"
NU_AJAX_URL = f"{NU_BASE_URL}/wp-admin/admin-ajax.php"

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


def test_cmd_crawl_resumes_automatically_without_start_page(monkeypatch, db_path, tmp_path):
    page0 = fanmtl_catalog_html([("Novel A", "a1", 100, "Ongoing")])
    session0 = FakeSession({page_url(0): FakeResponse(page0, 200, page_url(0))})
    run_cli(monkeypatch, ["crawl", "--pages", "1", "--pacing-file",
                          str(tmp_path / "pacing.json"), "--db", db_path], session0)

    # Second run stubs ONLY page 1 -- if it didn't resume automatically and
    # instead restarted at page 0, FakeSession's strict mode would raise.
    page1 = fanmtl_catalog_html([("Novel B", "b1", 100, "Ongoing")])
    session1 = FakeSession({page_url(1): FakeResponse(page1, 200, page_url(1))})
    run_cli(monkeypatch, ["crawl", "--pages", "1", "--pacing-file",
                          str(tmp_path / "pacing.json"), "--db", db_path], session1)

    conn = init_db(db_path)
    assert get_novel(conn, "fanmtl", f"{BASE_URL}/novel/a1.html") is not None
    assert get_novel(conn, "fanmtl", f"{BASE_URL}/novel/b1.html") is not None


def test_cmd_crawl_start_page_overrides_persisted_resume_point(monkeypatch, db_path, tmp_path):
    page0 = fanmtl_catalog_html([("Novel A", "a1", 100, "Ongoing")])
    session0 = FakeSession({page_url(0): FakeResponse(page0, 200, page_url(0))})
    run_cli(monkeypatch, ["crawl", "--pages", "1", "--pacing-file",
                          str(tmp_path / "pacing.json"), "--db", db_path], session0)

    # Explicit --start-page 0 overrides the persisted resume point (which is
    # now 1) -- refetches page 0 instead of continuing at page 1.
    page0_again = fanmtl_catalog_html([("Novel A", "a1", 100, "Ongoing"),
                                        ("Novel C", "c1", 100, "Ongoing")])
    session1 = FakeSession({page_url(0): FakeResponse(page0_again, 200, page_url(0))})
    run_cli(monkeypatch, ["crawl", "--start-page", "0", "--pages", "1", "--refresh",
                          "--pacing-file", str(tmp_path / "pacing.json"), "--db", db_path],
            session1)

    conn = init_db(db_path)
    assert get_novel(conn, "fanmtl", f"{BASE_URL}/novel/c1.html") is not None


def test_cmd_crawl_retries_transient_failure_then_succeeds(monkeypatch, db_path, tmp_path):
    monkeypatch.setattr(dataspine.time, "sleep", lambda secs: None)
    page0 = fanmtl_catalog_html([("Novel A", "a1", 100, "Ongoing")])
    attempts = {"n": 0}

    def flaky_page0():
        attempts["n"] += 1
        if attempts["n"] == 1:
            return RuntimeError("connection reset")
        return FakeResponse(page0, 200, page_url(0))

    session = FakeSession({page_url(0): flaky_page0,
                           page_url(1): FakeResponse(fanmtl_catalog_html([]), 200, page_url(1))})
    run_cli(monkeypatch, ["crawl", "--pacing-file", str(tmp_path / "pacing.json"),
                          "--db", db_path], session)

    assert attempts["n"] == 2  # one failure, one successful retry
    conn = init_db(db_path)
    assert get_novel(conn, "fanmtl", f"{BASE_URL}/novel/a1.html") is not None


def test_cmd_crawl_gives_up_after_max_retries_and_persists_resume_point(
        monkeypatch, db_path, tmp_path, capsys):
    monkeypatch.setattr(dataspine.time, "sleep", lambda secs: None)
    session = FakeSession({page_url(0): RuntimeError("always fails")})
    run_cli(monkeypatch, ["crawl", "--pacing-file", str(tmp_path / "pacing.json"),
                          "--db", db_path], session)

    assert "giving up after" in capsys.readouterr().out
    conn = init_db(db_path)
    assert get_next_page(conn, "fanmtl") == 0  # resumes at the same failed page next time


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


def _crawl_one(monkeypatch, db_path, chapter_id, title="A Novel"):
    page0 = fanmtl_catalog_html([(title, chapter_id, 300, "Ongoing")])
    session = FakeSession({
        page_url(0): FakeResponse(page0, 200, page_url(0)),
        page_url(1): FakeResponse(fanmtl_catalog_html([]), 200, page_url(1)),
    })
    run_cli(monkeypatch, ["crawl", "--db", db_path], session)


def test_cmd_enrich_resolves_auto_match(monkeypatch, db_path):
    _crawl_one(monkeypatch, db_path, "ri1", title="Reverend Insanity")

    series_url = f"{NU_BASE_URL}/series/reverend-insanity/"
    search_html = nu_search_html([("Reverend Insanity", series_url)])
    series_html = nu_series_html(title="Reverend Insanity", associated_names=["Reverend Insanity"],
                                  genres=["Action"], tags=["Cultivation"])
    nu_session = FakeSession({
        NU_AJAX_URL: FakeResponse(search_html, 200, NU_AJAX_URL),
        series_url: FakeResponse(series_html, 200, series_url),
    })
    monkeypatch.setattr(dataspine.novelupdates, "solve_challenge_session", lambda *a, **kw: nu_session)

    run_cli(monkeypatch, ["enrich", "--db", db_path], FakeSession({}))

    conn = init_db(db_path)
    novel = get_novel(conn, "fanmtl", f"{BASE_URL}/novel/ri1.html")
    assert novel["nu_resolution"] == "auto"
    assert novel["nu_url"] == series_url
    assert novel["nu_title"] == "Reverend Insanity"


def test_cmd_enrich_no_search_results_records_no_candidates(monkeypatch, db_path):
    _crawl_one(monkeypatch, db_path, "obs1", title="Obscure Novel")

    nu_session = FakeSession({
        NU_AJAX_URL: FakeResponse(nu_search_html([]), 200, NU_AJAX_URL),
    })
    monkeypatch.setattr(dataspine.novelupdates, "solve_challenge_session", lambda *a, **kw: nu_session)

    run_cli(monkeypatch, ["enrich", "--db", db_path], FakeSession({}))

    conn = init_db(db_path)
    novel = get_novel(conn, "fanmtl", f"{BASE_URL}/novel/obs1.html")
    assert novel["nu_resolution"] == "no_candidates"
    assert novel["nu_url"] is None


def test_cmd_enrich_resolves_session_expiry_mid_run(monkeypatch, db_path):
    _crawl_one(monkeypatch, db_path, "ri1", title="Reverend Insanity")

    series_url = f"{NU_BASE_URL}/series/reverend-insanity/"
    challenge_html = load_fixture("novelupdates_cloudflare_challenge.html")
    search_html = nu_search_html([("Reverend Insanity", series_url)])
    series_html = nu_series_html(title="Reverend Insanity", associated_names=["Reverend Insanity"])

    call_count = {"n": 0}

    def search_response():
        call_count["n"] += 1
        if call_count["n"] == 1:
            return FakeResponse(challenge_html, 200, NU_AJAX_URL)
        return FakeResponse(search_html, 200, NU_AJAX_URL)

    nu_session = FakeSession({
        NU_AJAX_URL: search_response,
        series_url: FakeResponse(series_html, 200, series_url),
    })

    solve_calls = {"n": 0}

    def fake_solve(*a, **kw):
        solve_calls["n"] += 1
        return nu_session

    monkeypatch.setattr(dataspine.novelupdates, "solve_challenge_session", fake_solve)

    run_cli(monkeypatch, ["enrich", "--db", db_path], FakeSession({}))

    assert solve_calls["n"] == 2  # initial solve + one re-solve after the challenge came back
    conn = init_db(db_path)
    novel = get_novel(conn, "fanmtl", f"{BASE_URL}/novel/ri1.html")
    assert novel["nu_resolution"] == "auto"


def test_cmd_enrich_no_pending_candidates_prints_message(monkeypatch, db_path, capsys):
    run_cli(monkeypatch, ["enrich", "--db", db_path], FakeSession({}))
    assert "No candidates pending Novel Updates enrichment" in capsys.readouterr().out


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
