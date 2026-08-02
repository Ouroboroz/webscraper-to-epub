"""
epub_scraper.dataspine — Stage 0 of the classification data spine: crawl
FanMTL's catalog, filter to real candidates, fetch their full metadata, and
enrich them against Novel Updates -- all into a local SQLite DB (dataspine.db).

Usage:
  python -m epub_scraper.dataspine crawl [--start-page N] [--pages N]
                                          [--min-chapters N] [--delay SECS]
                                          [--pacing-file FILE] [--refresh] [--db FILE]
  python -m epub_scraper.dataspine metadata [--limit N] [--delay SECS]
                                             [--pacing-file FILE] [--db FILE]
  python -m epub_scraper.dataspine enrich [--limit N] [--search-candidates N]
                                           [--delay SECS] [--pacing-file FILE] [--db FILE]
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
file and epub_scraper/novelupdates.py's docstring).

All three long-running commands share the same Pacer (epub_scraper.pacing --
originally built for the chapter scraper) via --pacing-file: a persisted,
jittered per-site interval that widens on a 429 or a detected challenge page
and never resets on its own. `crawl`/`metadata` route fetch failures through
it via fetcher.note_throttle(); `enrich`'s Novel Updates calls don't go
through epub_scraper.fetcher at all (curl_cffi, not requests, and
ChallengeExpired rather than ChallengeDetected/HTTPError) so they keep their
own existing re-solve logic, just using the Pacer for its paced sleep too --
one persisted pacing.json across the whole pipeline instead of a second,
disconnected mechanism.
"""

import argparse
import time

import requests

from . import entity_resolution, novelupdates
from .dataspine_db import (DEFAULT_DB_PATH, get_next_page, get_novel, init_db,
                            iter_candidates_missing_metadata,
                            iter_candidates_missing_nu_resolution, recompute_candidates,
                            set_next_page, stats, upsert_catalog_entry, upsert_metadata,
                            upsert_nu_metadata)
from .fetcher import HEADERS, fetch, note_throttle
from .pacing import DEFAULT_PACING_PATH, Pacer
from .sites.fanmtl import CATALOG_URL_TEMPLATE, parse_fanmtl_catalog_page, parse_fanmtl_metadata

SITE_KEY = "fanmtl"
NU_SITE_KEY = "novelupdates"
# Bounded re-solves per `enrich` run if the Cloudflare session expires
# mid-run -- protects against looping forever if solving itself is broken.
MAX_CHALLENGE_RESOLVES = 2
# Bounded retries per catalog page in `crawl` before giving up the whole run --
# a transient error/429/challenge shouldn't kill a multi-hour unattended crawl,
# but a truly dead site/URL shouldn't retry forever either.
MAX_PAGE_RETRIES = 5


def _session():
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def _fetch_page_with_retry(url, session, pacer, site_key, max_retries=MAX_PAGE_RETRIES):
    """fetch() a catalog page, widening the pacer and retrying (bounded) on
    failure instead of letting one transient error/429/challenge kill an
    entire multi-hour unattended crawl. Re-raises after max_retries."""
    attempt = 0
    while True:
        try:
            return fetch(url, session)
        except Exception as e:
            note_throttle(pacer, site_key, e)
            attempt += 1
            if attempt > max_retries:
                raise
            gap = pacer.gap(site_key)
            print(f"  fetch failed ({e}); retry {attempt}/{max_retries} after {gap:.1f}s...")
            time.sleep(gap)


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
        except Exception as e:
            print(f"Error fetching page {page} (giving up after {MAX_PAGE_RETRIES} retries): {e}")
            set_next_page(conn, SITE_KEY, page)
            conn.commit()
            break

        entries = parse_fanmtl_catalog_page(html)
        if not entries:
            print(f"Page {page} had no novels -- reached the end of the catalog.")
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
              f"{elapsed / 60:.1f}m elapsed, ~{rate:.1f} pages/min)")

        if args.pages is None or pages_fetched < args.pages:
            time.sleep(pacer.gap(SITE_KEY))

    recompute_candidates(conn, min_chapters=args.min_chapters, site_key=SITE_KEY)
    conn.commit()

    summary = stats(conn, site_key=SITE_KEY)
    print("-" * 56)
    print(f"Next run will resume at page {page} automatically (or pass --start-page to override)")
    print(f"Candidates (>= {args.min_chapters} chapters): "
          f"{summary['candidates']} / {summary['total']} catalogued")


def cmd_metadata(args):
    conn = init_db(args.db)
    session = _session()
    pacer = Pacer.load(args.pacing_file, default_interval=args.delay)

    rows = iter_candidates_missing_metadata(conn, SITE_KEY, limit=args.limit)
    if not rows:
        print("No candidates pending a metadata fetch.")
        return

    started_at = time.monotonic()
    for i, row in enumerate(rows):
        try:
            html = fetch(row["url"], session)
            metadata = parse_fanmtl_metadata(html)
            upsert_metadata(conn, SITE_KEY, row["url"], metadata)
            conn.commit()
            synopsis_status = "ok" if metadata.synopsis else "MISSING"
            elapsed = time.monotonic() - started_at
            print(f"[{i + 1}/{len(rows)}] {row['title']!r}  "
                  f"({len(metadata.genres)} genres, synopsis {synopsis_status}, "
                  f"{elapsed / 60:.1f}m elapsed)")
        except Exception as e:
            note_throttle(pacer, SITE_KEY, e)
            print(f"[{i + 1}/{len(rows)}] error on {row['title']!r}: {e}")

        if i < len(rows) - 1:
            time.sleep(pacer.gap(SITE_KEY))


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
            try:
                resolution, matched = _resolve_one(session, row, args.search_candidates)
            except novelupdates.ChallengeExpired:
                print(f"[{i + 1}/{len(rows)}] still blocked after re-solve on "
                      f"{row['title']!r}, skipping for this run")
                continue
        except Exception as e:
            print(f"[{i + 1}/{len(rows)}] error resolving {row['title']!r}: {e}")
            continue

        upsert_nu_metadata(conn, SITE_KEY, row["url"], resolution.decision, matched)
        conn.commit()

        detail = f" -- {matched.title!r}" if matched is not None else ""
        elapsed = time.monotonic() - started_at
        print(f"[{i + 1}/{len(rows)}] {row['title']!r} -> {resolution.decision}{detail}  "
              f"({elapsed / 60:.1f}m elapsed)")

        if i < len(rows) - 1:
            time.sleep(pacer.gap(NU_SITE_KEY))


def cmd_stats(args):
    conn = init_db(args.db)
    summary = stats(conn, site_key=SITE_KEY)
    print(f"Total catalogued : {summary['total']}")
    print(f"Candidates       : {summary['candidates']}")
    print(f"  with metadata  : {summary['candidates_with_metadata']}")
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

    p_stats = sub.add_parser("stats", help="Show crawl/candidate progress")
    p_stats.add_argument("--db", default=DEFAULT_DB_PATH, metavar="FILE")
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
