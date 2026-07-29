"""
epub_scraper.update — track novels and check them for new chapters.

Usage:
  python -m epub_scraper.update add <index-url> [--site KEY] [--output FILE] [--last-known N] [--library FILE]
  python -m epub_scraper.update remove <site_key> <chapter_id> [--library FILE]
  python -m epub_scraper.update list [--library FILE]
  python -m epub_scraper.update check [--library FILE] [--cache-dir DIR] [--delay SECS]
                                       [--novel-delay SECS] [--only SITE:CHAPTER_ID ...] [--dry-run]
"""

import argparse
import sys
import time

import requests

from . import engine
from .epub_writer import build_epub
from .fetcher import HEADERS, fetch
from .library import (DEFAULT_LIBRARY_PATH, add_novel, load_library, record_check,
                       remove_novel, save_library)
from .scrape import scrape_chapters
from .sites import PROFILES, resolve_profile
from .util import get_base_url, slugify

# Consecutive real chapter-fetch failures that abort a novel's fetch for this run.
CIRCUIT_BREAKER_THRESHOLD = 3
# Consecutive checks with zero progress before a novel is auto-disabled.
AUTO_DISABLE_AFTER = 5


def _session_for(url):
    session = requests.Session()
    session.headers.update(HEADERS)
    session.headers["Referer"] = get_base_url(url)
    return session


# -- add / remove / list -------------------------------------------------------

def cmd_add(args):
    profile = resolve_profile(args.url, args.site)
    session = _session_for(args.url)

    print(f"Fetching index: {args.url}")
    try:
        index_html = fetch(args.url, session)
    except Exception as e:
        print(f"Error fetching index: {e}")
        sys.exit(1)

    title, chapter_id, total, base_url = engine.parse_index(profile, index_html, args.url)
    if not chapter_id:
        print("Could not determine chapter ID from index page.")
        sys.exit(1)

    library = load_library(args.library)
    output_file = args.output or slugify(title)
    try:
        entry = add_novel(library, site_key=profile.site_key, chapter_id=chapter_id,
                           index_url=args.url, title=title, output_file=output_file,
                           last_known_chapter=args.last_known if args.last_known is not None else 0)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
    save_library(library, args.library)

    print(f"Added: {title!r} ({profile.site_key}:{chapter_id}) -> {output_file}")
    print(f"  last_known_chapter={entry['last_known_chapter']}  "
          f"(site currently reports {total if total else 'unknown'} chapters)")


def cmd_remove(args):
    library = load_library(args.library)
    if remove_novel(library, args.site_key, args.chapter_id):
        save_library(library, args.library)
        print(f"Removed {args.site_key}:{args.chapter_id}")
    else:
        print(f"Not tracked: {args.site_key}:{args.chapter_id}")
        sys.exit(1)


def cmd_list(args):
    library = load_library(args.library)
    if not library["novels"]:
        print("No tracked novels.")
        return
    for entry in library["novels"]:
        status = "enabled" if entry["enabled"] else "DISABLED"
        failed = f"  failed={entry['failed_chapters']}" if entry["failed_chapters"] else ""
        err = f"  error={entry['last_error']!r}" if entry["last_error"] else ""
        print(f"{entry['site_key']}:{entry['chapter_id']}  [{status}]  "
              f"ch {entry['last_known_chapter']}  {entry['title']!r}{failed}{err}")


# -- check ----------------------------------------------------------------------

def _parse_only(only_args):
    if not only_args:
        return None
    out = set()
    for item in only_args:
        site_key, sep, chapter_id = item.partition(":")
        if not sep:
            raise SystemExit(f"--only expects SITE:CHAPTER_ID, got {item!r}")
        out.add((site_key, chapter_id))
    return out


def _check_one(entry, cache_dir, delay, dry_run):
    """Check and, if needed, update a single tracked novel in place.
    Returns a short status string for the summary tally."""
    profile = PROFILES.get(entry["site_key"])
    if profile is None:
        record_check(entry, error=f"unknown site_key {entry['site_key']!r}")
        return "error"

    session = _session_for(entry["index_url"])
    index_html = fetch(entry["index_url"], session)
    title, parsed_id, total, base_url = engine.parse_index(profile, index_html, entry["index_url"])

    if parsed_id and parsed_id != entry["chapter_id"]:
        print(f"  warning: chapter_id drifted ({entry['chapter_id']} -> {parsed_id}); keeping stored id")

    if total is None:
        record_check(entry, error="could not determine chapter count")
        return "error"

    delta = total - entry["last_known_chapter"]
    has_failed = bool(entry["failed_chapters"])

    if delta <= 0 and not has_failed:
        record_check(entry, title=title)
        return "unchanged"

    if dry_run:
        if delta > 0:
            print(f"  [dry-run] {delta} new chapter(s) available "
                  f"({entry['last_known_chapter'] + 1}..{total})")
        if has_failed:
            print(f"  [dry-run] {len(entry['failed_chapters'])} previously-failed chapter(s) would be retried")
        return "dry-run"

    fetched_count = 0

    if delta <= 0:
        # Nothing new per the index, but retry any chapters that failed on a
        # past run — cheap (a handful of numbers, not a full re-scan).
        retried, still_failed, _ = scrape_chapters(
            profile, session, base_url, entry["chapter_id"], sorted(entry["failed_chapters"]),
            cache_dir=cache_dir, delay=delay, max_consecutive_failures=CIRCUIT_BREAKER_THRESHOLD)
        fetched_count = len(retried)
        entry["failed_chapters"] = still_failed
        if retried:
            all_chapters, _, _ = scrape_chapters(
                profile, session, base_url, entry["chapter_id"],
                range(1, entry["last_known_chapter"] + 1), cache_dir=cache_dir, delay=delay)
            build_epub(title, profile.site_key, entry["chapter_id"], all_chapters, entry["output_file"])
        record_check(entry, title=title)
    else:
        all_chapters, failed_ns, stopped_at = scrape_chapters(
            profile, session, base_url, entry["chapter_id"], range(1, total + 1),
            cache_dir=cache_dir, delay=delay, max_consecutive_failures=CIRCUIT_BREAKER_THRESHOLD)
        # 1..last_known are cache hits (free); only last_known+1..total are real
        # fetches, so a single full-range call does both the delta fetch and the
        # full-rebuild reconstruction at once.
        fetched_count = max(0, len(all_chapters) - entry["last_known_chapter"])

        new_last_known = (stopped_at - 1) if stopped_at else total
        entry["failed_chapters"] = failed_ns
        build_epub(title, profile.site_key, entry["chapter_id"], all_chapters, entry["output_file"])
        record_check(entry, total=new_last_known, title=title,
                     updated=(new_last_known > entry["last_known_chapter"]))
        if stopped_at:
            entry["last_error"] = (f"circuit breaker: {CIRCUIT_BREAKER_THRESHOLD} consecutive "
                                    f"failures starting at chapter {stopped_at}")

    if fetched_count > 0:
        entry["consecutive_failed_checks"] = 0
    else:
        entry["consecutive_failed_checks"] += 1

    if entry["consecutive_failed_checks"] >= AUTO_DISABLE_AFTER:
        entry["enabled"] = False
        entry["last_error"] = (f"auto-disabled after {AUTO_DISABLE_AFTER} consecutive checks with "
                                f"no progress — investigate and re-enable manually")
        return "disabled"

    return "updated" if fetched_count > 0 else "no-progress"


def cmd_check(args):
    library = load_library(args.library)
    only = _parse_only(args.only)
    targets = [e for e in library["novels"]
               if (only is not None and (e["site_key"], e["chapter_id"]) in only)
               or (only is None and e["enabled"])]

    if not targets:
        print("No matching novels to check." if only else "No enabled tracked novels to check.")
        return

    tally = {}
    for i, entry in enumerate(targets):
        label = f"{entry['site_key']}:{entry['chapter_id']}"
        try:
            status = _check_one(entry, cache_dir=args.cache_dir, delay=args.delay, dry_run=args.dry_run)
        except (SystemExit, Exception) as e:
            record_check(entry, error=str(e))
            status = "error"
        finally:
            save_library(library, args.library)

        tally[status] = tally.get(status, 0) + 1
        print(f"[{status}] {label}  {entry['title']}")

        if i < len(targets) - 1:
            time.sleep(args.novel_delay)

    print("-" * 40)
    print("  ".join(f"{k}={v}" for k, v in sorted(tally.items())))


# -- CLI --------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m epub_scraper.update",
        description="Track novels and check them for new chapters.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Start tracking a novel")
    p_add.add_argument("url", help="Novel index page URL")
    p_add.add_argument("--site", choices=sorted(PROFILES), default=None, metavar="KEY")
    p_add.add_argument("--output", default=None, metavar="FILE")
    p_add.add_argument("--last-known", type=int, default=None, metavar="N",
                        help="Chapter number already downloaded (default: 0)")
    p_add.add_argument("--library", default=DEFAULT_LIBRARY_PATH, metavar="FILE")
    p_add.set_defaults(func=cmd_add)

    p_remove = sub.add_parser("remove", help="Stop tracking a novel")
    p_remove.add_argument("site_key")
    p_remove.add_argument("chapter_id")
    p_remove.add_argument("--library", default=DEFAULT_LIBRARY_PATH, metavar="FILE")
    p_remove.set_defaults(func=cmd_remove)

    p_list = sub.add_parser("list", help="List tracked novels")
    p_list.add_argument("--library", default=DEFAULT_LIBRARY_PATH, metavar="FILE")
    p_list.set_defaults(func=cmd_list)

    p_check = sub.add_parser("check", help="Check all tracked novels for new chapters")
    p_check.add_argument("--library", default=DEFAULT_LIBRARY_PATH, metavar="FILE")
    p_check.add_argument("--cache-dir", default=".cache", metavar="DIR")
    p_check.add_argument("--delay", type=float, default=2.5, metavar="SECS")
    p_check.add_argument("--novel-delay", type=float, default=5.0, metavar="SECS")
    p_check.add_argument("--only", action="append", default=None, metavar="SITE:CHAPTER_ID")
    p_check.add_argument("--dry-run", action="store_true")
    p_check.set_defaults(func=cmd_check)

    return parser


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
