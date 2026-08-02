"""
epub_scraper.dataspine — Stage 0 of the classification data spine: crawl
FanMTL's catalog, filter to real candidates, and fetch their full metadata
into a local SQLite DB (dataspine.db).

Usage:
  python -m epub_scraper.dataspine crawl [--start-page N] [--pages N]
                                          [--min-chapters N] [--delay SECS]
                                          [--refresh] [--db FILE]
  python -m epub_scraper.dataspine metadata [--limit N] [--delay SECS] [--db FILE]
  python -m epub_scraper.dataspine stats [--db FILE]

`crawl` paginates the catalog browse listing (30 novels/page, thousands of
pages) and marks candidate=1 for every novel with at least --min-chapters
chapters -- entirely from the listing card itself, no per-novel fetch needed.
`metadata` then fetches the full index page for each candidate still missing
a synopsis (i.e. not yet processed), so it's safe to re-run repeatedly as the
candidate set grows.
"""

import argparse
import time

import requests

from .dataspine_db import (DEFAULT_DB_PATH, get_novel, init_db,
                            iter_candidates_missing_metadata, recompute_candidates,
                            stats, upsert_catalog_entry, upsert_metadata)
from .fetcher import HEADERS, fetch
from .sites.fanmtl import CATALOG_URL_TEMPLATE, parse_fanmtl_catalog_page, parse_fanmtl_metadata

SITE_KEY = "fanmtl"


def _session():
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def cmd_crawl(args):
    conn = init_db(args.db)
    session = _session()

    page = args.start_page
    pages_fetched = 0
    new_or_updated = 0
    skipped = 0

    while args.pages is None or pages_fetched < args.pages:
        url = CATALOG_URL_TEMPLATE.format(page=page)
        try:
            html = fetch(url, session)
        except Exception as e:
            print(f"Error fetching page {page}: {e}")
            break

        entries = parse_fanmtl_catalog_page(html)
        if not entries:
            print(f"Page {page} had no novels -- reached the end of the catalog.")
            break

        for entry in entries:
            existing = get_novel(conn, SITE_KEY, entry.url)
            if existing is not None and existing["chapter_count"] is not None and not args.refresh:
                skipped += 1
                continue
            upsert_catalog_entry(conn, entry, site_key=SITE_KEY)
            new_or_updated += 1
        conn.commit()

        print(f"[page {page}] {len(entries)} novels  "
              f"({new_or_updated} new/updated, {skipped} skipped so far)")

        pages_fetched += 1
        page += 1
        if args.pages is None or pages_fetched < args.pages:
            time.sleep(args.delay)

    recompute_candidates(conn, min_chapters=args.min_chapters, site_key=SITE_KEY)
    conn.commit()

    summary = stats(conn, site_key=SITE_KEY)
    print("-" * 56)
    print(f"Next run: --start-page {page}")
    print(f"Candidates (>= {args.min_chapters} chapters): "
          f"{summary['candidates']} / {summary['total']} catalogued")


def cmd_metadata(args):
    conn = init_db(args.db)
    session = _session()

    rows = iter_candidates_missing_metadata(conn, SITE_KEY, limit=args.limit)
    if not rows:
        print("No candidates pending a metadata fetch.")
        return

    for i, row in enumerate(rows):
        try:
            html = fetch(row["url"], session)
            metadata = parse_fanmtl_metadata(html)
            upsert_metadata(conn, SITE_KEY, row["url"], metadata)
            conn.commit()
            synopsis_status = "ok" if metadata.synopsis else "MISSING"
            print(f"[{i + 1}/{len(rows)}] {row['title']!r}  "
                  f"({len(metadata.genres)} genres, synopsis {synopsis_status})")
        except Exception as e:
            print(f"[{i + 1}/{len(rows)}] error on {row['title']!r}: {e}")

        if i < len(rows) - 1:
            time.sleep(args.delay)


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


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m epub_scraper.dataspine",
        description="Build the Stage 0 classification data spine from FanMTL's catalog.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_crawl = sub.add_parser("crawl", help="Crawl the FanMTL catalog and mark candidates")
    p_crawl.add_argument("--start-page", type=int, default=0, metavar="N")
    p_crawl.add_argument("--pages", type=int, default=10, metavar="N",
                          help="Pages to fetch this run (default: 10). The catalog has "
                               "thousands of pages -- ramp this up deliberately.")
    p_crawl.add_argument("--min-chapters", type=int, default=80, metavar="N")
    p_crawl.add_argument("--delay", type=float, default=2.5, metavar="SECS")
    p_crawl.add_argument("--refresh", action="store_true",
                          help="Re-upsert novels already in the DB instead of skipping them")
    p_crawl.add_argument("--db", default=DEFAULT_DB_PATH, metavar="FILE")
    p_crawl.set_defaults(func=cmd_crawl)

    p_metadata = sub.add_parser("metadata", help="Fetch full metadata for candidates missing it")
    p_metadata.add_argument("--limit", type=int, default=50, metavar="N")
    p_metadata.add_argument("--delay", type=float, default=2.5, metavar="SECS")
    p_metadata.add_argument("--db", default=DEFAULT_DB_PATH, metavar="FILE")
    p_metadata.set_defaults(func=cmd_metadata)

    p_stats = sub.add_parser("stats", help="Show crawl/candidate progress")
    p_stats.add_argument("--db", default=DEFAULT_DB_PATH, metavar="FILE")
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
