"""
epub_scraper.dataspine — Stage 0 of the classification data spine: crawl
FanMTL's catalog, filter to real candidates, fetch their full metadata, and
enrich them against Novel Updates -- all into a local SQLite DB (dataspine.db).

Usage:
  python -m epub_scraper.dataspine crawl [--start-page N] [--pages N]
                                          [--min-chapters N] [--delay SECS]
                                          [--pacing-file FILE] [--refresh] [--db FILE]
  python -m epub_scraper.dataspine metadata [--limit N] [--delay SECS] [--workers N]
                                             [--pacing-file FILE] [--db FILE]
  python -m epub_scraper.dataspine enrich [--limit N] [--search-candidates N]
                                           [--delay SECS] [--pacing-file FILE] [--db FILE]
  python -m epub_scraper.dataspine chapters [--count N] [--limit N] [--delay SECS] [--workers N]
                                             [--pacing-file FILE] [--db FILE]
  python -m epub_scraper.dataspine embed [--limit N] [--model NAME] [--db FILE]
  python -m epub_scraper.dataspine cluster [--umap-dims N] [--min-cluster-size N] [--db FILE]
  python -m epub_scraper.dataspine tag-communities [--db FILE]
  python -m epub_scraper.dataspine stats [--db FILE]

`crawl` paginates the catalog browse listing (30 novels/page, thousands of
pages) and marks candidate=1 for every novel with at least --min-chapters
chapters -- entirely from the listing card itself, no per-novel fetch needed.
Resumable across runs with no flags needed: the next page to fetch is
persisted in the DB itself (crawl_state table) after every page, so a killed
or interrupted run picks back up on its own -- pass --start-page explicitly
only to override that. `metadata` then fetches the full index page for each
candidate still missing a synopsis (i.e. not yet processed), so it's safe to
re-run repeatedly as the candidate set grows. `enrich` resolves candidates
against Novel Updates (requires requirements-novelupdates.txt -- see that
file and epub_scraper/novelupdates.py's docstring). `chapters` samples the
first --count chapters (default 5 -- most novels can be judged on their
opening) of each candidate's actual prose, for signal synopsis/tags/metadata
alone can't capture (pacing, prose quality, whether the hook lands) --
reuses epub_scraper.scrape.scrape_chapters() (the same engine the interactive
EPUB-download pipeline uses, including its on-disk .cache/, so a chapter
sampled here is already warm if the novel later gets a full download)
instead of a second chapter fetcher. `embed`/`cluster`/`tag-communities` are
Stage 1 (corpus structure) -- pure local computation, no network at all, see
epub_scraper/corpus_structure.py's docstring. They only need `synopsis` +
tags (already available once `metadata`/`enrich` have run on a candidate),
not `chapters` or a fully-finished `enrich`. Unlike the other subcommands,
`cluster`/`tag-communities` are full recomputes over the whole corpus each
time, not incremental -- a cluster boundary can shift for every novel as the
corpus grows.

All three long-running commands share the same Pacer (epub_scraper.pacing --
originally built for the chapter scraper) via --pacing-file: a persisted,
jittered per-site interval that widens on a 429 or a detected challenge page
and never resets on its own. `crawl`/`metadata` route fetch failures through
fetcher.note_throttle(); `enrich`'s Novel Updates calls go through curl_cffi,
not requests, so they use a separate duck-typed _note_nu_throttle() for the
same 429-widening behavior (curl_cffi's HTTPError isn't a requests.HTTPError
subclass, confirmed live -- note_throttle() would silently never match it)
and keep their own existing ChallengeExpired re-solve logic on top -- one
persisted pacing.json across the whole pipeline either way.

`metadata`/`chapters` accept --workers to fetch several novels concurrently
(each request still individually paced through the shared Pacer -- N workers
approximates Nx one stream's throughput, not one stream firing Nx faster).
Deliberately NOT offered on `enrich`: Novel Updates already showed sustained
429s under plain sequential pacing (see _note_nu_throttle's docstring) and
sits behind an actual Cloudflare challenge, unlike FanMTL -- concurrent
requests there are far more likely to trip a fresh block (and force another
slow browser-based re-solve) than to net any real speedup.
"""

import argparse
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

from . import corpus_structure, entity_resolution, novelupdates
from .dataspine_db import (DEFAULT_DB_PATH, get_next_page, get_novel, init_db,
                            iter_candidates_missing_chapters, iter_candidates_missing_metadata,
                            iter_candidates_missing_nu_resolution, recompute_candidates,
                            set_next_page, stats, upsert_catalog_entry, upsert_chapters,
                            upsert_metadata, upsert_nu_metadata)
from .fetcher import HEADERS, fetch, note_throttle
from .pacing import DEFAULT_PACING_PATH, Pacer
from .scrape import scrape_chapters
from .sites.fanmtl import BASE_URL as FANMTL_BASE_URL
from .sites.fanmtl import CATALOG_URL_TEMPLATE, PROFILE, parse_fanmtl_catalog_page, parse_fanmtl_metadata

SITE_KEY = "fanmtl"
NU_SITE_KEY = "novelupdates"
DEFAULT_CHAPTER_SAMPLE_SIZE = 5
# Bounded re-solves per `enrich` run if the Cloudflare session expires
# mid-run -- protects against looping forever if solving itself is broken.
MAX_CHALLENGE_RESOLVES = 2
# Consecutive 429s (not ChallengeExpired -- a real 429 response, still
# "solved" as far as the session looks) before proactively re-solving.
# Confirmed live (2026-08-03): a real enrich run kept getting sustained 429s
# even after the pacer had already widened all the way to its own
# MAX_INTERVAL ceiling (pacing.py) -- since the pacer literally cannot back
# off any further, that's evidence the block is tied to the solved
# session/cookie's cumulative volume, not just request rate, and no amount
# of extra waiting at the same interval was ever going to clear it. Shares
# MAX_CHALLENGE_RESOLVES's budget rather than its own -- both are "give up
# on this session, get a fresh one" for the same underlying reason.
MAX_CONSECUTIVE_429S = 5
# Bounded retries per catalog page in `crawl` before giving up the whole run --
# a transient error/429/challenge shouldn't kill a multi-hour unattended crawl,
# but a truly dead site/URL shouldn't retry forever either.
MAX_PAGE_RETRIES = 5
# Bounded retries for a catalog page that fetched fine (HTTP 200) but parsed
# to zero novels, before treating that as real signal rather than a fluke.
# Confirmed live (2026-08-03): a genuine end-of-catalog page 404s (see
# _is_permanent_404 above) -- a 200 with zero parsed novels has never been
# observed as the real end, only as a transient soft-block/rate-limit. Not
# retrying this cost ~1,400 pages (~42,000 novels) of a real crawl, twice:
# page ~3810 and then page 3909 both parsed to zero once, got taken at face
# value as "the end", and the run stopped there even though the catalog
# actually continues to page 5323.
MAX_EMPTY_PAGE_RETRIES = 3


def _session():
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def _format_elapsed(seconds):
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes:02d}m{secs:02d}s"


def _is_permanent_404(e):
    return (isinstance(e, requests.HTTPError) and e.response is not None
            and e.response.status_code == 404)


def _note_nu_throttle(pacer, e):
    """fetcher.note_throttle() only recognizes requests.HTTPError -- Novel
    Updates calls go through curl_cffi instead (see novelupdates.py), whose
    HTTPError is a structurally similar but genuinely unrelated exception
    type (confirmed: not a requests.HTTPError subclass), so note_throttle()
    silently never matches it. Found live (2026-08-03): a real enrich run hit
    sustained 429s in its last ~14 candidates and the pacer never widened,
    meaning every one of those requests fired at the same unthrottled
    interval that had already started failing. Duck-typed equivalent for the
    one thing that matters here, since curl_cffi's Response still exposes
    the same .status_code/.headers shape requests does."""
    response = getattr(e, "response", None)
    if getattr(response, "status_code", None) != 429:
        return False
    headers = getattr(response, "headers", None)
    retry_after = headers.get("Retry-After") if headers is not None else None
    pacer.throttled(NU_SITE_KEY, retry_after=retry_after)
    return True


def _fetch_page_with_retry(url, session, pacer, site_key, max_retries=MAX_PAGE_RETRIES):
    """fetch() a catalog page, widening the pacer and retrying (bounded) on
    failure instead of letting one transient error/429/challenge kill an
    entire multi-hour unattended crawl. Re-raises after max_retries.

    A 404 is NOT retried -- confirmed live (2026-08-03) that FanMTL returns a
    stable 404, not an empty 200, once a page number is past its current
    catalog boundary, so retrying it just burns 5 rounds of backoff on a
    condition that will never change within this run."""
    attempt = 0
    while True:
        try:
            return fetch(url, session)
        except Exception as e:
            if _is_permanent_404(e):
                raise
            note_throttle(pacer, site_key, e)
            attempt += 1
            if attempt > max_retries:
                raise
            gap = pacer.gap(site_key)
            print(f"  fetch failed ({e}); retry {attempt}/{max_retries} after {gap:.1f}s...")
            time.sleep(gap)


def _dump_crawl_debug(html):
    """Best-effort page-source dump when a catalog page keeps parsing to
    zero novels after retrying -- since the real end-of-catalog signal is a
    404, not this, that means either a soft block/rate-limit or a broken
    catalog-page selector, and a concrete artifact beats guessing which.
    Written to the current directory -- gitignored, not meant to be
    committed."""
    try:
        with open("dataspine_crawl_debug.html", "w", encoding="utf-8") as f:
            f.write(html or "")
        print("  wrote dataspine_crawl_debug.html for inspection")
    except OSError as e:
        print(f"  (failed to write dataspine_crawl_debug.html: {e})")


def _retry_empty_page(url, session, pacer, page, max_retries=MAX_EMPTY_PAGE_RETRIES):
    """Re-fetch and re-parse a catalog page that just parsed to zero novels,
    up to max_retries times (with the pacer's normal backoff gap between
    attempts). Returns (entries, last_html_seen) -- entries is [] if it
    never recovered. Can raise (a permanent 404 mid-retry propagates
    straight to the caller, same as a first-attempt 404 would)."""
    html = None
    for attempt in range(1, max_retries + 1):
        gap = pacer.gap(SITE_KEY)
        print(f"  page {page} parsed to zero novels; retry {attempt}/{max_retries} "
              f"after {gap:.1f}s...")
        time.sleep(gap)
        html = _fetch_page_with_retry(url, session, pacer, SITE_KEY)
        entries = parse_fanmtl_catalog_page(html)
        if entries:
            return entries, html
    return [], html


def _run_concurrent(rows, worker_fn, workers, pacer, site_key):
    """Call `worker_fn(row)` for each row using `workers` threads, each call
    preceded by a locked pacer.gap() sleep -- Pacer.gap() draws from the
    shared `random` module and (via throttled(), called back on the main
    thread below, not here) mutates self.intervals/rewrites pacing.json, so
    concurrent unlocked callers could race. `workers` streams at a per-
    stream `gap()` pace approximates `workers`x the throughput of one
    sequential stream while every individual request is still paced the
    same as before -- not one stream firing `workers`x faster.

    worker_fn must do network I/O + pure-Python parsing ONLY, no DB access:
    sqlite3 connections are thread-affine (same reasoning as labeling.py's
    build_app() docstring), so all DB writes happen back on the caller,
    single-threaded, as it consumes what this yields.

    Yields (row, result) in COMPLETION order (not input order) -- result is
    whatever worker_fn returned, or the exception it raised (never re-raised
    here, so the caller can handle it exactly like the sequential path's
    try/except does).
    """
    lock = threading.Lock()

    def _paced_call(row):
        with lock:
            gap = pacer.gap(site_key)
        time.sleep(gap)
        try:
            return worker_fn(row)
        except Exception as e:
            return e

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_paced_call, row): row for row in rows}
        for future in as_completed(futures):
            yield futures[future], future.result()


def cmd_crawl(args):
    conn = init_db(args.db)
    session = _session()
    pacer = Pacer.load(args.pacing_file, default_interval=args.delay)

    page = args.start_page if args.start_page is not None else get_next_page(conn, SITE_KEY)
    pages_fetched = 0
    new_or_updated = 0
    skipped = 0
    started_at = time.monotonic()

    while args.pages is None or pages_fetched < args.pages:
        url = CATALOG_URL_TEMPLATE.format(page=page)
        try:
            html = _fetch_page_with_retry(url, session, pacer, SITE_KEY)
            entries = parse_fanmtl_catalog_page(html)
            if not entries:
                entries, html = _retry_empty_page(url, session, pacer, page)
        except Exception as e:
            if _is_permanent_404(e):
                print(f"Page {page} returned 404 -- reached the current end of the catalog. "
                      f"(This boundary moves as new novels get added -- a future crawl may "
                      f"find more here, so it's re-checked rather than remembered as final.)")
            else:
                print(f"Error fetching page {page} (giving up after {MAX_PAGE_RETRIES} retries): {e}")
            set_next_page(conn, SITE_KEY, page)
            conn.commit()
            break

        if not entries:
            print(f"Page {page} still parsed to zero novels after {MAX_EMPTY_PAGE_RETRIES} "
                  f"retries -- NOT treating this as the end (a real end-of-catalog page 404s "
                  f"here, not this). Looks like a soft block/rate-limit or a broken selector. "
                  f"Stopping without advancing past page {page} so the next run retries here.")
            _dump_crawl_debug(html)
            set_next_page(conn, SITE_KEY, page)
            conn.commit()
            break

        for entry in entries:
            existing = get_novel(conn, SITE_KEY, entry.url)
            if existing is not None and existing["chapter_count"] is not None and not args.refresh:
                skipped += 1
                continue
            upsert_catalog_entry(conn, entry, site_key=SITE_KEY)
            new_or_updated += 1
        conn.commit()

        pages_fetched += 1
        page += 1
        set_next_page(conn, SITE_KEY, page)
        conn.commit()

        elapsed = time.monotonic() - started_at
        rate = pages_fetched / elapsed * 60 if elapsed > 0 else 0.0
        print(f"[page {page - 1}] {len(entries)} novels  "
              f"({new_or_updated} new/updated, {skipped} skipped so far, "
              f"{_format_elapsed(elapsed)} elapsed, ~{rate:.1f} pages/min)")

        if args.pages is None or pages_fetched < args.pages:
            time.sleep(pacer.gap(SITE_KEY))

    recompute_candidates(conn, min_chapters=args.min_chapters, site_key=SITE_KEY)
    conn.commit()

    summary = stats(conn, site_key=SITE_KEY)
    print("-" * 56)
    print(f"Next run will resume at page {page} automatically (or pass --start-page to override)")
    print(f"Candidates (>= {args.min_chapters} chapters): "
          f"{summary['candidates']} / {summary['total']} catalogued")


def _handle_metadata_result(conn, pacer, i, total, started_at, row, result):
    elapsed = time.monotonic() - started_at
    if isinstance(result, Exception):
        note_throttle(pacer, SITE_KEY, result)
        print(f"[{i}/{total}] error on {row['title']!r}: {result}")
        return
    metadata = parse_fanmtl_metadata(result)
    upsert_metadata(conn, SITE_KEY, row["url"], metadata)
    conn.commit()
    synopsis_status = "ok" if metadata.synopsis else "MISSING"
    print(f"[{i}/{total}] {row['title']!r}  "
          f"({len(metadata.genres)} genres, synopsis {synopsis_status}, "
          f"{_format_elapsed(elapsed)} elapsed)")


def cmd_metadata(args):
    conn = init_db(args.db)
    session = _session()
    pacer = Pacer.load(args.pacing_file, default_interval=args.delay)

    rows = iter_candidates_missing_metadata(conn, SITE_KEY, limit=args.limit)
    if not rows:
        print("No candidates pending a metadata fetch.")
        return

    total = len(rows)
    started_at = time.monotonic()

    if args.workers <= 1:
        for i, row in enumerate(rows, start=1):
            try:
                result = fetch(row["url"], session)
            except Exception as e:
                result = e
            _handle_metadata_result(conn, pacer, i, total, started_at, row, result)
            if i < total:
                time.sleep(pacer.gap(SITE_KEY))
    else:
        worker_fn = lambda row: fetch(row["url"], session)  # noqa: E731 -- closes over `session`
        for i, (row, result) in enumerate(
                _run_concurrent(rows, worker_fn, args.workers, pacer, SITE_KEY), start=1):
            _handle_metadata_result(conn, pacer, i, total, started_at, row, result)


def _resolve_one(session, row, search_candidates):
    """search() + fetch_series() + entity_resolution.resolve() for one
    candidate. Returns (resolution, matched_metadata_or_None)."""
    metadatas = novelupdates.find_nu_candidates(session, row["title"], limit=search_candidates)
    nu_candidates = [entity_resolution.NUCandidate(m.title, m.url, m.associated_names)
                      for m in metadatas]
    resolution = entity_resolution.resolve(row["title"], row["alt_title"], nu_candidates)

    matched = None
    if resolution.decision == "auto":
        matched = next(m for m in metadatas if m.url == resolution.best.url)
    return resolution, matched


def cmd_enrich(args):
    conn = init_db(args.db)
    pacer = Pacer.load(args.pacing_file, default_interval=args.delay)
    rows = iter_candidates_missing_nu_resolution(conn, SITE_KEY, limit=args.limit)
    if not rows:
        print("No candidates pending Novel Updates enrichment.")
        return

    try:
        session = novelupdates.solve_challenge_session()
    except ImportError as e:
        print(f"Could not start a Novel Updates session: {e}")
        print("Install requirements-novelupdates.txt (needs a real Chrome + Xvfb on Linux) "
              "and try `python -m epub_scraper.novelupdates check` first.")
        return
    except novelupdates.ChallengeExpired as e:
        print(f"Could not solve the Novel Updates challenge: {e}")
        print("Try `python -m epub_scraper.novelupdates check` on its own to debug.")
        return

    resolves_left = MAX_CHALLENGE_RESOLVES
    consecutive_429s = 0
    started_at = time.monotonic()
    for i, row in enumerate(rows):
        try:
            resolution, matched = _resolve_one(session, row, args.search_candidates)
        except novelupdates.ChallengeExpired:
            if resolves_left <= 0:
                print("Challenge re-solve budget exhausted -- stopping this run.")
                break
            print("  session expired -- re-solving the challenge...")
            session = novelupdates.solve_challenge_session()
            resolves_left -= 1
            consecutive_429s = 0
            try:
                resolution, matched = _resolve_one(session, row, args.search_candidates)
            except novelupdates.ChallengeExpired:
                print(f"[{i + 1}/{len(rows)}] still blocked after re-solve on "
                      f"{row['title']!r}, skipping for this run")
                continue
        except Exception as e:
            was_429 = _note_nu_throttle(pacer, e)
            print(f"[{i + 1}/{len(rows)}] error resolving {row['title']!r}: {e}")
            if not was_429:
                continue
            consecutive_429s += 1
            if consecutive_429s < MAX_CONSECUTIVE_429S:
                continue
            if resolves_left <= 0:
                print("Sustained 429s and challenge re-solve budget exhausted -- "
                      "stopping this run.")
                break
            print(f"  {consecutive_429s} consecutive 429s despite max pacing -- the session "
                  f"looks exhausted, not just rate-limited. Re-solving with a fresh one...")
            session = novelupdates.solve_challenge_session()
            resolves_left -= 1
            consecutive_429s = 0
            continue

        consecutive_429s = 0
        upsert_nu_metadata(conn, SITE_KEY, row["url"], resolution.decision, matched)
        conn.commit()

        detail = f" -- {matched.title!r}" if matched is not None else ""
        elapsed = time.monotonic() - started_at
        print(f"[{i + 1}/{len(rows)}] {row['title']!r} -> {resolution.decision}{detail}  "
              f"({_format_elapsed(elapsed)} elapsed)")

        if i < len(rows) - 1:
            time.sleep(pacer.gap(NU_SITE_KEY))


def _plain_text_from_chapter_body(body_html):
    """scrape_chapters() returns each chapter's body as XHTML-escaped
    '<p>...</p>' fragments (meant for splicing straight into an EPUB) -- undo
    that here so what lands in the DB is clean prose, not markup, for
    whatever reads it next (embeddings, a labeling-review UI, an LLM)."""
    paragraphs = [p.get_text() for p in BeautifulSoup(body_html, "html.parser").find_all("p")]
    return "\n\n".join(paragraphs)


def _handle_chapters_result(conn, pacer, i, total, started_at, args, row, result):
    elapsed = time.monotonic() - started_at
    if isinstance(result, Exception):
        note_throttle(pacer, SITE_KEY, result)
        print(f"[{i}/{total}] error on {row['title']!r}: {result}")
        return
    fetched, failed_ns = result
    chapter_range = range(1, args.count + 1)
    successful_ns = [n for n in chapter_range if n not in failed_ns]
    upsert_chapters(conn, row["id"], [
        (n, title, _plain_text_from_chapter_body(body))
        for n, (title, body) in zip(successful_ns, fetched)
    ])
    conn.commit()
    print(f"[{i}/{total}] {row['title']!r}  "
          f"({len(fetched)}/{args.count} chapters, {_format_elapsed(elapsed)} elapsed)")


def cmd_chapters(args):
    conn = init_db(args.db)
    session = _session()
    pacer = Pacer.load(args.pacing_file, default_interval=args.delay)

    rows = iter_candidates_missing_chapters(conn, SITE_KEY, limit=args.limit)
    if not rows:
        print("No candidates pending a chapter sample.")
        return

    chapter_range = range(1, args.count + 1)

    def _sample_one(row):
        fetched, failed_ns, _ = scrape_chapters(
            PROFILE, session, FANMTL_BASE_URL, row["chapter_id"], chapter_range,
            cache_dir=args.cache_dir, delay=args.delay, pacer=pacer)
        return fetched, failed_ns

    total = len(rows)
    started_at = time.monotonic()

    if args.workers <= 1:
        for i, row in enumerate(rows, start=1):
            try:
                result = _sample_one(row)
            except Exception as e:
                result = e
            _handle_chapters_result(conn, pacer, i, total, started_at, args, row, result)
            if i < total:
                time.sleep(pacer.gap(SITE_KEY))
    else:
        # scrape_chapters() paces its own OWN inner per-chapter loop via this
        # same `pacer` without a lock (unchanged, shared code with the
        # interactive EPUB-download CLI -- not touching its internals here).
        # Only the per-NOVEL stagger below (when each worker's scrape_chapters
        # call *starts*) goes through _run_concurrent's locked gap() -- the
        # occasional unlocked race inside one novel's own 5-chapter fetch is
        # the same "corrupt pacing.json is never fatal, just re-learned"
        # tolerance Pacer.load() already documents, not a new risk class.
        for i, (row, result) in enumerate(
                _run_concurrent(rows, _sample_one, args.workers, pacer, SITE_KEY), start=1):
            _handle_chapters_result(conn, pacer, i, total, started_at, args, row, result)


def cmd_embed(args):
    conn = init_db(args.db)
    try:
        n = corpus_structure.embed_synopses(conn, SITE_KEY, model_name=args.model, limit=args.limit)
    except ImportError as e:
        print(f"Could not embed: {e}")
        print("Install requirements-ml.txt (sentence-transformers + torch) first.")
        return
    if n == 0:
        print("No candidates pending an embedding (need a synopsis first -- run `metadata`).")
    else:
        print(f"Embedded {n} synopses with {args.model!r}.")


def cmd_cluster(args):
    conn = init_db(args.db)
    try:
        n, n_clusters, n_outliers = corpus_structure.cluster_corpus(
            conn, SITE_KEY, umap_dims=args.umap_dims, min_cluster_size=args.min_cluster_size)
    except ImportError as e:
        print(f"Could not cluster: {e}")
        print("Install requirements-ml.txt (umap-learn + hdbscan) first.")
        return
    if n == 0:
        print("No candidates with an embedding yet -- run `embed` first.")
    else:
        print(f"Clustered {n} novels into {n_clusters} clusters ({n_outliers} outliers).")


def cmd_tag_communities(args):
    conn = init_db(args.db)
    try:
        n_tags, n_communities = corpus_structure.build_tag_communities(conn)
    except ImportError as e:
        print(f"Could not build tag communities: {e}")
        print("Install requirements-ml.txt (python-igraph + leidenalg) first.")
        return
    if n_tags == 0:
        print("No tags yet -- run `metadata`/`enrich` first.")
    else:
        print(f"Grouped {n_tags} tags into {n_communities} communities.")


def cmd_stats(args):
    conn = init_db(args.db)
    summary = stats(conn, site_key=SITE_KEY)
    print(f"Total catalogued : {summary['total']}")
    print(f"Candidates       : {summary['candidates']}")
    print(f"  with metadata  : {summary['candidates_with_metadata']}")
    print(f"  with chapters  : {summary['candidates_with_chapters']}")
    print(f"  with embedding : {summary['candidates_with_embedding']}")
    print("  by status:")
    for status, count in sorted(summary["candidates_by_status"].items(),
                                 key=lambda kv: -kv[1]):
        print(f"    {status or '(unknown)'}: {count}")
    print("  by NU resolution:")
    for resolution, count in sorted(summary["candidates_by_nu_resolution"].items(),
                                     key=lambda kv: -kv[1]):
        print(f"    {resolution}: {count}")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m epub_scraper.dataspine",
        description="Build the Stage 0 classification data spine from FanMTL's catalog.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_crawl = sub.add_parser("crawl", help="Crawl the FanMTL catalog and mark candidates")
    p_crawl.add_argument("--start-page", type=int, default=None, metavar="N",
                          help="Page to start from (default: resume automatically from "
                               "wherever the last run left off)")
    p_crawl.add_argument("--pages", type=int, default=10, metavar="N",
                          help="Pages to fetch this run (default: 10). The catalog has "
                               "thousands of pages -- ramp this up deliberately.")
    p_crawl.add_argument("--min-chapters", type=int, default=80, metavar="N")
    p_crawl.add_argument("--delay", type=float, default=2.5, metavar="SECS")
    p_crawl.add_argument("--pacing-file", default=DEFAULT_PACING_PATH, metavar="FILE",
                          help="Where to persist learned per-site request pacing "
                               "(default: pacing.json)")
    p_crawl.add_argument("--refresh", action="store_true",
                          help="Re-upsert novels already in the DB instead of skipping them")
    p_crawl.add_argument("--db", default=DEFAULT_DB_PATH, metavar="FILE")
    p_crawl.set_defaults(func=cmd_crawl)

    p_metadata = sub.add_parser("metadata", help="Fetch full metadata for candidates missing it")
    p_metadata.add_argument("--limit", type=int, default=50, metavar="N")
    p_metadata.add_argument("--delay", type=float, default=2.5, metavar="SECS")
    p_metadata.add_argument("--workers", type=int, default=1, metavar="N",
                             help="Concurrent fetches (default: 1, sequential). FanMTL has no "
                                  "known Cloudflare-style challenge, unlike Novel Updates, so "
                                  "this is reasonable to raise (e.g. 5-10) here.")
    p_metadata.add_argument("--pacing-file", default=DEFAULT_PACING_PATH, metavar="FILE")
    p_metadata.add_argument("--db", default=DEFAULT_DB_PATH, metavar="FILE")
    p_metadata.set_defaults(func=cmd_metadata)

    p_enrich = sub.add_parser("enrich", help="Resolve candidates against Novel Updates")
    p_enrich.add_argument("--limit", type=int, default=20, metavar="N")
    p_enrich.add_argument("--search-candidates", type=int, default=5, metavar="N",
                           help="Max Novel Updates search results to fetch per novel (default: 5)")
    p_enrich.add_argument("--delay", type=float, default=2.5, metavar="SECS")
    p_enrich.add_argument("--pacing-file", default=DEFAULT_PACING_PATH, metavar="FILE")
    p_enrich.add_argument("--db", default=DEFAULT_DB_PATH, metavar="FILE")
    p_enrich.set_defaults(func=cmd_enrich)

    p_chapters = sub.add_parser("chapters", help="Sample each candidate's first N chapters")
    p_chapters.add_argument("--count", type=int, default=DEFAULT_CHAPTER_SAMPLE_SIZE, metavar="N",
                             help=f"Chapters to sample per novel (default: "
                                  f"{DEFAULT_CHAPTER_SAMPLE_SIZE})")
    p_chapters.add_argument("--limit", type=int, default=20, metavar="N")
    p_chapters.add_argument("--delay", type=float, default=2.5, metavar="SECS")
    p_chapters.add_argument("--workers", type=int, default=1, metavar="N",
                             help="Concurrent novels sampled at once (default: 1, sequential). "
                                  "Same FanMTL-only reasoning as `metadata --workers`.")
    p_chapters.add_argument("--pacing-file", default=DEFAULT_PACING_PATH, metavar="FILE")
    p_chapters.add_argument("--cache-dir", default=".cache", metavar="DIR",
                             help="Shared with the interactive EPUB-download pipeline's chapter "
                                  "cache (default: .cache) -- a sampled chapter is already warm "
                                  "if the novel later gets a full download")
    p_chapters.add_argument("--db", default=DEFAULT_DB_PATH, metavar="FILE")
    p_chapters.set_defaults(func=cmd_chapters)

    p_embed = sub.add_parser("embed", help="Embed each candidate's synopsis (Stage 1)")
    p_embed.add_argument("--limit", type=int, default=200, metavar="N")
    p_embed.add_argument("--model", default=corpus_structure.DEFAULT_EMBEDDING_MODEL, metavar="NAME")
    p_embed.add_argument("--db", default=DEFAULT_DB_PATH, metavar="FILE")
    p_embed.set_defaults(func=cmd_embed)

    p_cluster = sub.add_parser(
        "cluster", help="UMAP+HDBSCAN cluster every embedded candidate (Stage 1, full recompute)")
    p_cluster.add_argument("--umap-dims", type=int, default=8, metavar="N")
    p_cluster.add_argument("--min-cluster-size", type=int, default=10, metavar="N")
    p_cluster.add_argument("--db", default=DEFAULT_DB_PATH, metavar="FILE")
    p_cluster.set_defaults(func=cmd_cluster)

    p_tag_communities = sub.add_parser(
        "tag-communities", help="Leiden-cluster the tag co-occurrence graph (Stage 1, full recompute)")
    p_tag_communities.add_argument("--db", default=DEFAULT_DB_PATH, metavar="FILE")
    p_tag_communities.set_defaults(func=cmd_tag_communities)

    p_stats = sub.add_parser("stats", help="Show crawl/candidate progress")
    p_stats.add_argument("--db", default=DEFAULT_DB_PATH, metavar="FILE")
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
