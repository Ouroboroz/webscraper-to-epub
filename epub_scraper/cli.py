"""
epub_scraper — webnovel aggregator site → EPUB

Usage:
  python -m epub_scraper <novel-index-url> [options]

Options:
  --start N       First chapter (default: 1)
  --end N         Last chapter inclusive (default: auto-detect)
  --delay N       Seconds between requests (default: 2.5)
  --output FILE   Output filename (default: auto from title)
  --site KEY      Force a specific site profile (default: auto-detect from URL domain)

Examples:
  python -m epub_scraper https://www.fanmtl.com/novel/some-novel.html
  python -m epub_scraper https://www.fanmtl.com/novel/some-novel.html --start 50 --end 100
  python -m epub_scraper https://www.fanmtl.com/novel/some-novel.html --delay 3 --output mybook.epub

Requirements:
  pip install requests beautifulsoup4 ebooklib
"""

import argparse
import sys
import time

import requests

from . import engine
from .cache import load_cached, save_cache
from .epub_writer import build_epub
from .fetcher import HEADERS, fetch
from .sites import PROFILES, resolve_profile
from .util import get_base_url, slugify


def main():
    parser = argparse.ArgumentParser(
        description="Scrape a webnovel from a supported aggregator site and save it as an EPUB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Requirements:")[0].strip(),
    )
    parser.add_argument("url", help="Novel index page URL")
    parser.add_argument("--start", type=int, default=1, metavar="N",
                        help="First chapter to fetch (default: 1)")
    parser.add_argument("--end", type=int, default=None, metavar="N",
                        help="Last chapter to fetch inclusive (default: auto)")
    parser.add_argument("--delay", type=float, default=2.5, metavar="SECS",
                        help="Delay between requests in seconds (default: 2.5)")
    parser.add_argument("--output", default=None, metavar="FILE",
                        help="Output .epub filename (default: auto from title)")
    parser.add_argument("--cache-dir", default=".cache", metavar="DIR",
                        help="Directory to cache raw chapter HTML (default: .cache)")
    parser.add_argument("--no-cache", action="store_true",
                        help="Ignore and overwrite any existing cache")
    parser.add_argument("--site", choices=sorted(PROFILES), default=None, metavar="KEY",
                        help="Force a specific site profile (default: auto-detect from URL domain)")
    args = parser.parse_args()

    profile = resolve_profile(args.url, args.site)

    session = requests.Session()
    session.headers.update(HEADERS)
    session.headers["Referer"] = get_base_url(args.url)

    # -- Index ----------------------------------------------------------------
    print(f"Fetching index: {args.url}")
    try:
        index_html = fetch(args.url, session)
    except Exception as e:
        print(f"Error fetching index: {e}")
        sys.exit(1)

    novel_title, chapter_id, total, base_url = engine.parse_index(profile, index_html, args.url)

    if not chapter_id:
        print("Could not determine chapter ID from index page. Exiting.")
        sys.exit(1)

    print(f"Site   : {profile.site_key}")
    print(f"Title  : {novel_title}")
    print(f"ID     : {chapter_id}")
    print(f"Total  : {total if total else 'unknown'} chapters")

    end = args.end if args.end is not None else total
    if end is None:
        print("Could not auto-detect chapter count. Use --end N to set it manually.")
        sys.exit(1)

    output_file = args.output or slugify(novel_title)
    chapter_range = range(args.start, end + 1)

    print(f"Range  : {args.start}–{end}  ({len(chapter_range)} chapters)")
    print(f"Delay  : {args.delay}s")
    print(f"Cache  : {args.cache_dir}{'  (disabled)' if args.no_cache else ''}")
    print(f"Output : {output_file}")
    print("-" * 56)

    # -- Fetch chapters -------------------------------------------------------
    chapters = []
    skipped = 0

    for i, n in enumerate(chapter_range):
        url = engine.chapter_url(profile, base_url, chapter_id, n)
        src = None
        try:
            # Try cache first
            cached = None if args.no_cache else load_cached(args.cache_dir, chapter_id, n)
            if cached:
                html = cached
                src = "cache"
            else:
                html = fetch(url, session)
                save_cache(args.cache_dir, chapter_id, n, html)
                src = "web"

            ch_title, body = engine.parse_chapter(profile, html, n)
            chapters.append((ch_title, body))
            pct = int((i + 1) / len(chapter_range) * 100)
            flag = "·" if src == "cache" else "↓"
            print(f"  [{pct:3d}%] {flag} Ch {n:>4d}  {ch_title[:50]}")
        except requests.HTTPError as e:
            print(f"  [SKIP] Ch {n:>4d}  HTTP {e.response.status_code}")
            skipped += 1
        except Exception as e:
            print(f"  [SKIP] Ch {n:>4d}  {e}")
            skipped += 1

        if i < len(chapter_range) - 1 and src == "web":
            time.sleep(args.delay)

    print("-" * 56)

    if not chapters:
        print("No chapters fetched. Exiting.")
        sys.exit(1)

    # -- Build EPUB -----------------------------------------------------------
    print(f"Building EPUB  ({len(chapters)} chapters, {skipped} skipped)…")
    try:
        build_epub(novel_title, profile.site_key, chapter_id, chapters, output_file)
        print(f"✓  Saved: {output_file}")
    except Exception as e:
        print(f"Error building EPUB: {e}")
        sys.exit(1)
