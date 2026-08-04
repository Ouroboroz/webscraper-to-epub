from epub_scraper import dataspine
from epub_scraper.dataspine_db import (get_next_page, get_novel, init_db,
                                        iter_candidates_missing_chapters,
                                        iter_nu_novels_missing_details, upsert_nu_novel_details,
                                        upsert_nu_novel_listing)
from epub_scraper.novelupdates import NUSeriesMetadata
from fakes import FakeResponse, FakeSession
from conftest import load_fixture
from html_builders import (fanmtl_catalog_html, fanmtl_chapter_html, nu_listing_html,
                            nu_search_html, nu_series_html)

NU_BASE_URL = "https://www.novelupdates.com"
NU_AJAX_URL = f"{NU_BASE_URL}/wp-admin/admin-ajax.php"
NU_LISTING_URL = f"{NU_BASE_URL}/novelslisting/"

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


def test_cmd_crawl_recovers_from_transient_empty_page(monkeypatch, db_path, tmp_path):
    # Confirmed live (2026-08-03): a catalog page can fetch fine (HTTP 200)
    # but parse to zero novels as a one-off (soft block/rate-limit), then
    # recover on the very next request -- treating the first empty parse as
    # final would silently truncate the crawl here.
    monkeypatch.setattr(dataspine.time, "sleep", lambda secs: None)
    page0 = fanmtl_catalog_html([("Novel A", "a1", 100, "Ongoing")])
    attempts = {"n": 0}

    def flaky_page1():
        attempts["n"] += 1
        if attempts["n"] == 1:
            return FakeResponse(fanmtl_catalog_html([]), 200, page_url(1))
        return FakeResponse(fanmtl_catalog_html([("Novel B", "b1", 100, "Ongoing")]),
                             200, page_url(1))

    session = FakeSession({
        page_url(0): FakeResponse(page0, 200, page_url(0)),
        page_url(1): flaky_page1,
        page_url(2): FakeResponse(fanmtl_catalog_html([]), 200, page_url(2)),
    })
    run_cli(monkeypatch, ["crawl", "--pacing-file", str(tmp_path / "pacing.json"),
                          "--db", db_path], session)

    assert attempts["n"] == 2  # one empty parse, one recovered retry
    conn = init_db(db_path)
    assert get_novel(conn, "fanmtl", f"{BASE_URL}/novel/a1.html") is not None
    assert get_novel(conn, "fanmtl", f"{BASE_URL}/novel/b1.html") is not None


def test_cmd_crawl_stops_without_falsely_marking_end_after_persistent_empty_page(
        monkeypatch, db_path, tmp_path, capsys):
    # 2026-08-03 incident: page 3909 parsed to zero once, got taken as "the
    # end", and the run stopped ~1,400 pages (~42,000 novels) short of the
    # catalog's real end (confirmed separately to be page 5323, signaled by
    # a real 404 -- see test_cmd_crawl_stops_cleanly_on_404_without_retrying).
    # A page that STAYS empty through every retry is still not proof of the
    # real end, just of something wrong right now -- so this must not print
    # the old "reached the end" claim, must not silently advance past the
    # page, and must leave a concrete artifact instead of guessing.
    monkeypatch.setattr(dataspine.time, "sleep", lambda secs: None)
    monkeypatch.chdir(tmp_path)
    page0 = fanmtl_catalog_html([("Novel A", "a1", 100, "Ongoing")])
    empty_page1 = fanmtl_catalog_html([])
    session = FakeSession({
        page_url(0): FakeResponse(page0, 200, page_url(0)),
        page_url(1): FakeResponse(empty_page1, 200, page_url(1)),
    })
    run_cli(monkeypatch, ["crawl", "--pacing-file", str(tmp_path / "pacing.json"),
                          "--db", db_path], session)

    out = capsys.readouterr().out
    assert "reached the end of the catalog" not in out
    assert "NOT treating this as the end" in out
    conn = init_db(db_path)
    assert get_next_page(conn, "fanmtl") == 1  # re-checked next run, not marked final
    debug_file = tmp_path / "dataspine_crawl_debug.html"
    assert debug_file.exists()
    assert "novel-list" in debug_file.read_text()


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


def _fanmtl_index_with_synopsis(synopsis):
    return (f'<html><body><div class="summary"><div class="content">'
            f'<p>{synopsis}</p></div></div></body></html>')


def test_cmd_metadata_workers_processes_all_candidates_without_crosstalk(monkeypatch, db_path):
    # --workers > 1 routes each fetch through a thread pool -- the thing
    # most likely to break is a race that assigns novel A's parsed metadata
    # to novel B's DB row (or drops one entirely). 8 candidates through 4
    # workers, each with a distinguishable synopsis, so any crosstalk shows
    # up as a wrong/missing synopsis rather than needing to inspect timing.
    entries = [(f"Novel {i}", f"n{i}", 100, "Ongoing") for i in range(8)]
    page0 = fanmtl_catalog_html(entries)
    crawl_session = FakeSession({
        page_url(0): FakeResponse(page0, 200, page_url(0)),
        page_url(1): FakeResponse(fanmtl_catalog_html([]), 200, page_url(1)),
    })
    run_cli(monkeypatch, ["crawl", "--db", db_path], crawl_session)

    metadata_session = FakeSession({
        f"{BASE_URL}/novel/n{i}.html": FakeResponse(
            _fanmtl_index_with_synopsis(f"Synopsis for novel {i}."),
            200, f"{BASE_URL}/novel/n{i}.html")
        for i in range(8)
    })
    run_cli(monkeypatch, ["metadata", "--workers", "4", "--db", db_path], metadata_session)

    conn = init_db(db_path)
    for i in range(8):
        novel = get_novel(conn, "fanmtl", f"{BASE_URL}/novel/n{i}.html")
        assert novel["synopsis"] == f"Synopsis for novel {i}."


def _crawl_one(monkeypatch, db_path, chapter_id, title="A Novel"):
    page0 = fanmtl_catalog_html([(title, chapter_id, 300, "Ongoing")])
    session = FakeSession({
        page_url(0): FakeResponse(page0, 200, page_url(0)),
        page_url(1): FakeResponse(fanmtl_catalog_html([]), 200, page_url(1)),
    })
    run_cli(monkeypatch, ["crawl", "--db", db_path], session)


# -- nu-crawl (Novel Updates' own bulk catalog listing) -------------------------

def test_cmd_nu_crawl_paginates_until_has_next_false_and_stores_listings(monkeypatch, db_path):
    # FakeSession keys by URL only (query params aren't part of the dict key
    # -- list_series() passes page via `params=`, not baked into the URL), so
    # successive pages are simulated with a call-counting callable, same
    # pattern the crawl-retry tests above use.
    page1 = nu_listing_html([("Novel A", f"{NU_BASE_URL}/series/a/")], has_next=True)
    page2 = nu_listing_html([("Novel B", f"{NU_BASE_URL}/series/b/")], has_next=False)
    calls = {"n": 0}

    def listing_response():
        calls["n"] += 1
        return FakeResponse(page1 if calls["n"] == 1 else page2, 200, NU_LISTING_URL)

    nu_session = FakeSession({NU_LISTING_URL: listing_response})
    monkeypatch.setattr(dataspine.novelupdates, "solve_challenge_session", lambda *a, **kw: nu_session)

    run_cli(monkeypatch, ["nu-crawl", "--db", db_path], FakeSession({}))

    assert calls["n"] == 2  # stopped once has_next was False, not a fixed page count
    conn = init_db(db_path)
    rows = conn.execute("SELECT url, title FROM nu_novels ORDER BY id").fetchall()
    assert [(r["url"], r["title"]) for r in rows] == [
        (f"{NU_BASE_URL}/series/a/", "Novel A"), (f"{NU_BASE_URL}/series/b/", "Novel B")]


def test_cmd_nu_crawl_resumes_automatically_without_start_page(monkeypatch, db_path, tmp_path):
    page1 = nu_listing_html([("Novel A", f"{NU_BASE_URL}/series/a/")], has_next=True)
    session0 = FakeSession({NU_LISTING_URL: FakeResponse(page1, 200, NU_LISTING_URL)})
    monkeypatch.setattr(dataspine.novelupdates, "solve_challenge_session", lambda *a, **kw: session0)
    run_cli(monkeypatch, ["nu-crawl", "--pages", "1", "--pacing-file",
                          str(tmp_path / "pacing.json"), "--db", db_path], FakeSession({}))

    # Second run only stubs one response -- if it restarted at page 1 instead
    # of resuming at page 2, it'd still "work" against this stub (same URL
    # key), so the real proof is the recorded call's params below.
    page2 = nu_listing_html([("Novel B", f"{NU_BASE_URL}/series/b/")], has_next=False)
    session1 = FakeSession({NU_LISTING_URL: FakeResponse(page2, 200, NU_LISTING_URL)})
    monkeypatch.setattr(dataspine.novelupdates, "solve_challenge_session", lambda *a, **kw: session1)
    run_cli(monkeypatch, ["nu-crawl", "--pages", "1", "--pacing-file",
                          str(tmp_path / "pacing.json"), "--db", db_path], FakeSession({}))

    assert session1.calls[0][2]["params"] == {"st": 1, "pg": 2}
    conn = init_db(db_path)
    urls = {r["url"] for r in conn.execute("SELECT url FROM nu_novels")}
    assert urls == {f"{NU_BASE_URL}/series/a/", f"{NU_BASE_URL}/series/b/"}
    assert get_next_page(conn, "novelupdates") == 3


def test_cmd_nu_crawl_first_run_starts_at_page_1_not_0(monkeypatch, db_path):
    # get_next_page()'s generic "never crawled" sentinel is 0, but NU's own
    # pagination starts at 1 -- confirmed live (2026-08-03).
    page1 = nu_listing_html([("Novel A", f"{NU_BASE_URL}/series/a/")], has_next=False)
    nu_session = FakeSession({NU_LISTING_URL: FakeResponse(page1, 200, NU_LISTING_URL)})
    monkeypatch.setattr(dataspine.novelupdates, "solve_challenge_session", lambda *a, **kw: nu_session)

    run_cli(monkeypatch, ["nu-crawl", "--db", db_path], FakeSession({}))

    assert nu_session.calls[0][2]["params"] == {"st": 1, "pg": 1}


def test_cmd_nu_crawl_resolves_session_expiry_mid_run(monkeypatch, db_path):
    challenge_html = load_fixture("novelupdates_cloudflare_challenge.html")
    listing_html = nu_listing_html([("Novel A", f"{NU_BASE_URL}/series/a/")], has_next=False)

    call_count = {"n": 0}

    def listing_response():
        call_count["n"] += 1
        if call_count["n"] == 1:
            return FakeResponse(challenge_html, 200, NU_LISTING_URL)
        return FakeResponse(listing_html, 200, NU_LISTING_URL)

    nu_session = FakeSession({NU_LISTING_URL: listing_response})

    solve_calls = {"n": 0}

    def fake_solve(*a, **kw):
        solve_calls["n"] += 1
        return nu_session

    monkeypatch.setattr(dataspine.novelupdates, "solve_challenge_session", fake_solve)

    run_cli(monkeypatch, ["nu-crawl", "--db", db_path], FakeSession({}))

    assert solve_calls["n"] == 2  # initial solve + one re-solve after the challenge came back
    conn = init_db(db_path)
    assert conn.execute("SELECT COUNT(*) FROM nu_novels").fetchone()[0] == 1


# -- nu-metadata (per-series Novel Updates detail fetch) -------------------------

def _seed_nu_listing(db_path, url, title):
    conn = init_db(db_path)
    upsert_nu_novel_listing(conn, url, title)
    conn.commit()


def test_cmd_nu_metadata_fills_in_details_for_pending_novel(monkeypatch, db_path):
    nu_url = f"{NU_BASE_URL}/series/reverend-insanity/"
    _seed_nu_listing(db_path, nu_url, "Reverend Insanity")

    series_html = nu_series_html(title="Reverend Insanity", associated_names=["Reverend Insanity"],
                                  genres=["Action"], tags=["Cultivation"],
                                  synopsis_paragraphs=["A great story."])
    nu_session = FakeSession({nu_url: FakeResponse(series_html, 200, nu_url)})
    monkeypatch.setattr(dataspine.novelupdates, "solve_challenge_session", lambda *a, **kw: nu_session)

    run_cli(monkeypatch, ["nu-metadata", "--db", db_path], FakeSession({}))

    conn = init_db(db_path)
    row = conn.execute("SELECT * FROM nu_novels WHERE url = ?", (nu_url,)).fetchone()
    assert row["fetched_at"] is not None
    assert row["genres"] == "Action"
    assert row["tags"] == "Cultivation"
    assert row["synopsis"] == "A great story."
    assert iter_nu_novels_missing_details(conn) == []  # resumable: no longer pending


def test_cmd_nu_metadata_no_pending_prints_message(monkeypatch, db_path, capsys):
    run_cli(monkeypatch, ["nu-metadata", "--db", db_path], FakeSession({}))
    assert "No Novel Updates novels pending a detail fetch" in capsys.readouterr().out


def test_cmd_nu_metadata_resolves_session_expiry_mid_run(monkeypatch, db_path):
    nu_url = f"{NU_BASE_URL}/series/reverend-insanity/"
    _seed_nu_listing(db_path, nu_url, "Reverend Insanity")

    challenge_html = load_fixture("novelupdates_cloudflare_challenge.html")
    series_html = nu_series_html(title="Reverend Insanity", associated_names=["Reverend Insanity"])

    call_count = {"n": 0}

    def series_response():
        call_count["n"] += 1
        if call_count["n"] == 1:
            return FakeResponse(challenge_html, 200, nu_url)
        return FakeResponse(series_html, 200, nu_url)

    nu_session = FakeSession({nu_url: series_response})

    solve_calls = {"n": 0}

    def fake_solve(*a, **kw):
        solve_calls["n"] += 1
        return nu_session

    monkeypatch.setattr(dataspine.novelupdates, "solve_challenge_session", fake_solve)

    run_cli(monkeypatch, ["nu-metadata", "--db", db_path], FakeSession({}))

    assert solve_calls["n"] == 2  # initial solve + one re-solve after the challenge came back
    conn = init_db(db_path)
    assert conn.execute(
        "SELECT fetched_at FROM nu_novels WHERE url = ?", (nu_url,)
    ).fetchone()["fetched_at"] is not None


def test_cmd_nu_metadata_widens_pacer_on_429(monkeypatch, db_path, tmp_path):
    # Same underlying issue as the old cmd_enrich test this replaces
    # (confirmed live 2026-08-03: curl_cffi's HTTPError isn't a
    # requests.HTTPError subclass, so fetcher.note_throttle() silently never
    # matches it) -- nu-metadata is now the command doing real per-item NU
    # network calls, so it's the one that needs this covered.
    nu_url = f"{NU_BASE_URL}/series/reverend-insanity/"
    _seed_nu_listing(db_path, nu_url, "Reverend Insanity")

    nu_session = FakeSession({nu_url: FakeResponse("", 429, nu_url)})
    monkeypatch.setattr(dataspine.novelupdates, "solve_challenge_session", lambda *a, **kw: nu_session)

    pacing_file = tmp_path / "pacing.json"
    run_cli(monkeypatch, ["nu-metadata", "--pacing-file", str(pacing_file), "--db", db_path],
            FakeSession({}))

    import json
    pacing = json.loads(pacing_file.read_text())
    assert pacing["novelupdates"] > 2.5  # default nu-metadata --delay, widened past it


def test_cmd_nu_metadata_resolves_after_sustained_429s_by_getting_a_fresh_session(monkeypatch, db_path):
    # 3 back-to-back 429s (MAX_CONSECUTIVE_429S) should force a fresh
    # session, not just keep widening the pacer forever inside a session
    # that's evidently now suspect/rate-limited.
    urls = [f"{NU_BASE_URL}/series/n{i}/" for i in range(4)]
    conn = init_db(db_path)
    for i, url in enumerate(urls):
        upsert_nu_novel_listing(conn, url, f"Novel {i}")
    conn.commit()

    responses = {url: FakeResponse("", 429, url) for url in urls[:3]}
    responses[urls[3]] = FakeResponse(nu_series_html(title="Novel 3"), 200, urls[3])

    sessions_created = []

    def fake_solve(*a, **kw):
        s = FakeSession(responses)
        sessions_created.append(s)
        return s

    monkeypatch.setattr(dataspine.novelupdates, "solve_challenge_session", fake_solve)

    run_cli(monkeypatch, ["nu-metadata", "--db", db_path], FakeSession({}))

    assert len(sessions_created) == 2  # initial session + one forced re-solve after 3 sustained 429s
    conn = init_db(db_path)
    fetched = {r["url"] for r in
               conn.execute("SELECT url FROM nu_novels WHERE fetched_at IS NOT NULL")}
    assert fetched == {urls[3]}  # only the row fetched after the fresh session landed


# -- enrich (now a pure local computation against the crawled nu_novels table) ---

def test_cmd_enrich_resolves_auto_match_purely_locally_no_network(monkeypatch, db_path):
    # The whole point of this rewrite: no Novel Updates network call anywhere
    # in `enrich` anymore. FakeSession({}) below has zero stubbed responses,
    # so if enrich tried to make ANY request through it, FakeSession's strict
    # mode would raise AssertionError -- this test would fail loudly instead
    # of silently passing if that regressed.
    _crawl_one(monkeypatch, db_path, "ri1", title="Reverend Insanity")

    nu_url = f"{NU_BASE_URL}/series/reverend-insanity/"
    conn = init_db(db_path)
    upsert_nu_novel_listing(conn, nu_url, "Reverend Insanity")
    conn.commit()
    upsert_nu_novel_details(conn, nu_url, NUSeriesMetadata(
        url=nu_url, title="Reverend Insanity", associated_names=["Reverend Insanity"],
        genres=["Action"], tags=["Cultivation"], author="Gu Zhen Ren",
        translation_status="Ongoing", translation_groups=[], release_frequency=None,
        rating=None, votes=None, synopsis="Some synopsis."))
    conn.commit()

    run_cli(monkeypatch, ["enrich", "--db", db_path], FakeSession({}))

    conn = init_db(db_path)
    novel = get_novel(conn, "fanmtl", f"{BASE_URL}/novel/ri1.html")
    assert novel["nu_resolution"] == "auto"
    assert novel["nu_url"] == nu_url
    assert novel["nu_title"] == "Reverend Insanity"
    assert novel["nu_author"] == "Gu Zhen Ren"
    tag_names = {row["name"] for row in conn.execute(
        "SELECT t.name FROM tags t JOIN novel_tags nt ON nt.tag_id = t.id "
        "WHERE nt.novel_id = ?", (novel["id"],))}
    assert "Cultivation" in tag_names


def test_cmd_enrich_no_local_match_records_no_candidates(monkeypatch, db_path):
    _crawl_one(monkeypatch, db_path, "obs1", title="Obscure Novel")

    nu_url = f"{NU_BASE_URL}/series/totally-unrelated/"
    conn = init_db(db_path)
    upsert_nu_novel_listing(conn, nu_url, "Totally Unrelated")
    conn.commit()
    upsert_nu_novel_details(conn, nu_url, NUSeriesMetadata(
        url=nu_url, title="Totally Unrelated", associated_names=["Totally Unrelated"],
        genres=[], tags=[], author=None, translation_status=None, translation_groups=[],
        release_frequency=None, rating=None, votes=None, synopsis=None))
    conn.commit()

    run_cli(monkeypatch, ["enrich", "--db", db_path], FakeSession({}))

    conn = init_db(db_path)
    novel = get_novel(conn, "fanmtl", f"{BASE_URL}/novel/obs1.html")
    assert novel["nu_resolution"] == "no_candidates"
    assert novel["nu_url"] is None


def test_cmd_enrich_no_resolved_nu_novels_prints_message(monkeypatch, db_path, capsys):
    _crawl_one(monkeypatch, db_path, "ri1", title="Reverend Insanity")
    # nu_novels is completely empty -- neither nu-crawl nor nu-metadata has run.
    run_cli(monkeypatch, ["enrich", "--db", db_path], FakeSession({}))
    out = capsys.readouterr().out
    assert "nu-crawl" in out and "nu-metadata" in out


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


def test_cmd_chapters_workers_processes_all_candidates_without_crosstalk(
        monkeypatch, db_path, tmp_path):
    # Same crosstalk concern as the metadata --workers test, but exercising
    # scrape_chapters()'s own per-chapter loop from inside a worker thread
    # (see cmd_chapters's --workers branch comment on why that's tolerated
    # unlocked) -- each of 4 novels' 3 sampled chapters must land under the
    # right novel_id, not a sibling's.
    entries = [(f"Novel {i}", f"n{i}", 100, "Ongoing") for i in range(4)]
    page0 = fanmtl_catalog_html(entries)
    crawl_session = FakeSession({
        page_url(0): FakeResponse(page0, 200, page_url(0)),
        page_url(1): FakeResponse(fanmtl_catalog_html([]), 200, page_url(1)),
    })
    run_cli(monkeypatch, ["crawl", "--db", db_path], crawl_session)

    responses = {
        chapter_url(f"n{i}", n): FakeResponse(
            fanmtl_chapter_html([f"Prose for novel {i} chapter {n}, long enough to keep."],
                                 title=f"N{i}Ch{n}"),
            200, chapter_url(f"n{i}", n))
        for i in range(4) for n in range(1, 4)
    }
    session = FakeSession(responses)
    run_cli(monkeypatch, ["chapters", "--count", "3", "--workers", "4",
                          "--cache-dir", str(tmp_path / ".cache"), "--db", db_path], session)

    conn = init_db(db_path)
    for i in range(4):
        novel = get_novel(conn, "fanmtl", f"{BASE_URL}/novel/n{i}.html")
        assert novel["chapters_sampled_at"] is not None
        rows = conn.execute(
            "SELECT * FROM chapters WHERE novel_id = ? ORDER BY chapter_number", (novel["id"],)
        ).fetchall()
        assert [r["chapter_number"] for r in rows] == [1, 2, 3]
        for r in rows:
            assert r["body"] == f"Prose for novel {i} chapter {r['chapter_number']}, long enough to keep."


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
