from epub_scraper import dataspine
from epub_scraper.dataspine_db import (get_next_page, get_novel, init_db,
                                        iter_candidates_missing_chapters)
from fakes import FakeResponse, FakeSession
from conftest import load_fixture
from html_builders import fanmtl_catalog_html, fanmtl_chapter_html, nu_search_html, nu_series_html

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


def test_cmd_crawl_stops_cleanly_on_404_without_retrying(monkeypatch, db_path, tmp_path, capsys):
    # Confirmed live (2026-08-03): FanMTL returns a stable 404, not an empty
    # 200, once a page number is past the catalog's current end -- retrying
    # that like a transient error just burns 5 rounds of backoff for nothing.
    monkeypatch.setattr(dataspine.time, "sleep", lambda secs: None)
    session = FakeSession({page_url(0): FakeResponse("", 404, page_url(0))})
    run_cli(monkeypatch, ["crawl", "--pacing-file", str(tmp_path / "pacing.json"),
                          "--db", db_path], session)

    out = capsys.readouterr().out
    assert "reached the current end of the catalog" in out
    assert "giving up after" not in out
    assert len(session.calls) == 1  # no retries -- a 404 is a permanent condition, not transient
    conn = init_db(db_path)
    assert get_next_page(conn, "fanmtl") == 0  # still re-checked next time -- the boundary moves


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


def test_cmd_enrich_widens_pacer_on_429(monkeypatch, db_path, tmp_path):
    # Confirmed live (2026-08-03): a real enrich run hit sustained 429s and
    # the pacer never widened, because curl_cffi's HTTPError isn't a
    # requests.HTTPError subclass, so fetcher.note_throttle() silently never
    # matched it. FakeResponse's raise_for_status() raises a real
    # requests.HTTPError here, but _note_nu_throttle() is duck-typed (checks
    # .response.status_code, not the exception class), so this exercises the
    # same code path a real curl_cffi 429 would.
    _crawl_one(monkeypatch, db_path, "ri1", title="Reverend Insanity")

    nu_session = FakeSession({
        NU_AJAX_URL: FakeResponse("", 429, NU_AJAX_URL),
    })
    monkeypatch.setattr(dataspine.novelupdates, "solve_challenge_session", lambda *a, **kw: nu_session)

    pacing_file = tmp_path / "pacing.json"
    run_cli(monkeypatch, ["enrich", "--pacing-file", str(pacing_file), "--db", db_path],
            FakeSession({}))

    import json
    pacing = json.loads(pacing_file.read_text())
    assert pacing["novelupdates"] > 2.5  # default enrich --delay, widened past it


def test_cmd_enrich_no_pending_candidates_prints_message(monkeypatch, db_path, capsys):
    run_cli(monkeypatch, ["enrich", "--db", db_path], FakeSession({}))
    assert "No candidates pending Novel Updates enrichment" in capsys.readouterr().out


def chapter_url(chapter_id, n):
    return f"{BASE_URL}/novel/{chapter_id}_{n}.html"


def test_cmd_chapters_samples_and_stores_plain_text(monkeypatch, db_path, tmp_path):
    _crawl_one(monkeypatch, db_path, "ri1", title="Reverend Insanity")

    session = FakeSession({
        chapter_url("ri1", n): FakeResponse(
            fanmtl_chapter_html([f"Real prose for chapter {n}, long enough to keep."],
                                 title=f"Chapter {n}"),
            200, chapter_url("ri1", n))
        for n in range(1, 6)
    })
    run_cli(monkeypatch, ["chapters", "--count", "5", "--cache-dir", str(tmp_path / ".cache"),
                          "--db", db_path], session)

    conn = init_db(db_path)
    novel = get_novel(conn, "fanmtl", f"{BASE_URL}/novel/ri1.html")
    assert novel["chapters_sampled_at"] is not None
    rows = conn.execute(
        "SELECT * FROM chapters WHERE novel_id = ? ORDER BY chapter_number", (novel["id"],)
    ).fetchall()
    assert [r["chapter_number"] for r in rows] == [1, 2, 3, 4, 5]
    assert rows[0]["title"] == "Chapter 1"
    assert rows[0]["body"] == "Real prose for chapter 1, long enough to keep."
    assert "<p>" not in rows[0]["body"]  # stored as clean plain text, not markup


def test_cmd_chapters_partial_failure_still_marks_processed(monkeypatch, db_path, tmp_path):
    _crawl_one(monkeypatch, db_path, "ri1", title="Reverend Insanity")

    responses = {
        chapter_url("ri1", n): FakeResponse(
            fanmtl_chapter_html([f"Real prose for chapter {n}, long enough to keep."],
                                 title=f"Chapter {n}"),
            200, chapter_url("ri1", n))
        for n in [1, 2, 4, 5]  # chapter 3 missing entirely -> a hard failure
    }
    responses[chapter_url("ri1", 3)] = FakeResponse("", 404, chapter_url("ri1", 3))
    session = FakeSession(responses)
    run_cli(monkeypatch, ["chapters", "--count", "5", "--cache-dir", str(tmp_path / ".cache"),
                          "--db", db_path], session)

    conn = init_db(db_path)
    novel = get_novel(conn, "fanmtl", f"{BASE_URL}/novel/ri1.html")
    assert novel["chapters_sampled_at"] is not None
    rows = conn.execute("SELECT chapter_number FROM chapters WHERE novel_id = ?",
                         (novel["id"],)).fetchall()
    assert {r["chapter_number"] for r in rows} == {1, 2, 4, 5}
    # Marked processed despite the gap -- must not be retried forever.
    assert iter_candidates_missing_chapters(conn, "fanmtl") == []


def test_cmd_chapters_no_pending_candidates_prints_message(monkeypatch, db_path, capsys):
    run_cli(monkeypatch, ["chapters", "--db", db_path], FakeSession({}))
    assert "No candidates pending a chapter sample" in capsys.readouterr().out


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
