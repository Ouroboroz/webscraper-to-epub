"""
epub_scraper.update — track novels and check them for new chapters.

Usage:
  python -m epub_scraper.update add <index-url> [--site KEY] [--output FILE] [--last-known N] [--library FILE]
  python -m epub_scraper.update remove <site_key> <chapter_id> [--library FILE]
  python -m epub_scraper.update list [--library FILE]
  python -m epub_scraper.update check [--library FILE] [--cache-dir DIR] [--delay SECS]
                                       [--novel-delay SECS] [--only SITE:CHAPTER_ID ...] [--dry-run]
  python -m epub_scraper.update search <query> [--library FILE]
  python -m epub_scraper.update find <query> [--site KEY] [--limit N]
  python -m epub_scraper.update grep <query> [--epubs-dir DIR] [--case-sensitive] [--context N]
  python -m epub_scraper.update check ... [--email] [--email-threshold N] [--mail-config FILE]
  python -m epub_scraper.update mail <site_key> <chapter_id> [--library FILE] [--mail-config FILE]
"""

import argparse
import glob
import os
import sys
import tempfile
import time
import uuid

import requests

from . import engine
from .epub_writer import build_epub
from .fetcher import HEADERS, fetch
from .library import (DEFAULT_LIBRARY_PATH, add_novel, find_novel, load_library,
                       record_check, record_email, remove_novel, save_library)
from .mailer import (DEFAULT_MAIL_CONFIG_PATH, MailConfigError, MailSendError,
                      SanityCheckError, load_mail_config, send_epub_to_kindle,
                      send_failure_alert)
from .scrape import scrape_chapters
from .sites import PROFILES, resolve_profile
from .textsearch import search_epub_text
from .util import EPUB_DIR, epub_filename, epub_path, get_base_url

# Consecutive real chapter-fetch failures that abort a novel's fetch for this run.
CIRCUIT_BREAKER_THRESHOLD = 3
# Consecutive checks with zero progress before a novel is auto-disabled.
AUTO_DISABLE_AFTER = 5
# Chapters accumulated (since the last successful Kindle send) before a batch
# fires -- Send-to-Kindle-by-email never merges/replaces, so each send is its
# own non-overlapping [last_emailed_chapter+1 .. last_known_chapter] slice
# rather than a resend of the whole growing book.
EMAIL_CHAPTER_THRESHOLD = 100


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
    last_known = args.last_known if args.last_known is not None else 0
    output_file = args.output or epub_path(title, 1, last_known)
    try:
        entry = add_novel(library, site_key=profile.site_key, chapter_id=chapter_id,
                           index_url=args.url, title=title, output_file=output_file,
                           last_known_chapter=last_known)
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


def _print_novels(entries):
    for entry in entries:
        status = "enabled" if entry["enabled"] else "DISABLED"
        failed = f"  failed={entry['failed_chapters']}" if entry["failed_chapters"] else ""
        err = f"  error={entry['last_error']!r}" if entry["last_error"] else ""
        print(f"{entry['site_key']}:{entry['chapter_id']}  [{status}]  "
              f"ch {entry['last_known_chapter']}  {entry['title']!r}{failed}{err}")


def cmd_list(args):
    library = load_library(args.library)
    if not library["novels"]:
        print("No tracked novels.")
        return
    _print_novels(library["novels"])


def cmd_search(args):
    library = load_library(args.library)
    query = args.query.lower()
    matches = [e for e in library["novels"] if query in e["title"].lower()]
    if not matches:
        print(f"No tracked novels matching {args.query!r}.")
        return
    _print_novels(matches)


def cmd_find(args):
    if args.site:
        profiles = [PROFILES[args.site]]
    else:
        profiles = list(PROFILES.values())

    session = requests.Session()
    session.headers.update(HEADERS)

    any_results = False
    for profile in profiles:
        try:
            results = engine.search_novels(profile, session, args.query)
        except NotImplementedError:
            continue
        except Exception as e:
            print(f"[{profile.site_key}] search failed: {e}")
            continue

        if not results:
            continue
        any_results = True
        print(f"\n{profile.site_key}:")
        for r in results[:args.limit]:
            chapters = f"{r.chapters} ch" if r.chapters is not None else "? ch"
            print(f"  {r.title}  [{chapters}]")
            print(f"    {r.url}")

    if not any_results:
        print("No results.")


def cmd_grep(args):
    if not os.path.isdir(args.epubs_dir):
        print(f"No epubs directory found at {args.epubs_dir!r}")
        sys.exit(1)

    paths = sorted(glob.glob(os.path.join(args.epubs_dir, "*.epub")))
    if not paths:
        print(f"No .epub files found in {args.epubs_dir!r}")
        return

    total_hits = 0
    for path in paths:
        hits = search_epub_text(path, args.query, ignore_case=not args.case_sensitive,
                                 context=args.context)
        if not hits:
            continue
        plural = "es" if len(hits) != 1 else ""
        print(f"\n{os.path.basename(path)}  ({len(hits)} match{plural})")
        for hit in hits:
            print(f"  {hit.chapter_title}: …{hit.snippet}…")
        total_hits += len(hits)

    print("-" * 40)
    print(f"{total_hits} match(es) across {len(paths)} epub(s)")


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


def _retarget_output(entry, title, end):
    """The output filename bakes in the title and the last chapter number, so
    it has to shift as a novel gets new chapters. Compute the current-correct
    path, drop the stale file if the name changed, and update the entry
    in place. Returns the path build_epub() should write to."""
    new_path = epub_path(title, 1, end)
    old_path = entry.get("output_file")
    if old_path and old_path != new_path and os.path.exists(old_path):
        os.remove(old_path)
    entry["output_file"] = new_path
    return new_path


def _send_batch(entry, profile, session, base_url, cache_dir, delay, config,
                 library, library_path, *, force=False, threshold=EMAIL_CHAPTER_THRESHOLD):
    """Build and send exactly the chapters not yet emailed
    (last_emailed_chapter+1 .. last_known_chapter) as their own small epub --
    never a resend of the whole growing book, since Send-to-Kindle-by-email
    can't merge/replace an existing document anyway. Those chapters are
    already cached from the normal check flow in the common case, so this
    costs no new network calls. force=True (manual `mail`) bypasses the
    threshold; force=False (semi-automatic `check --email`) only fires once
    the pending range is at least `threshold` chapters.

    Returns "nothing-new" / "below-threshold" / "sent" / "failed"."""
    start = entry.get("last_emailed_chapter", 0) + 1
    end = entry["last_known_chapter"]
    if end < start:
        return "nothing-new"
    if not force and (end - start + 1) < threshold:
        return "below-threshold"

    batch_chapters, _, _ = scrape_chapters(
        profile, session, base_url, entry["chapter_id"], range(start, end + 1),
        cache_dir=cache_dir, delay=delay)
    tmp_path = os.path.join(tempfile.gettempdir(), f"kindle-batch-{uuid.uuid4().hex}.epub")
    build_epub(entry["title"], profile.site_key, entry["chapter_id"], batch_chapters, tmp_path)
    attachment_name = epub_filename(entry["title"], start, end)

    try:
        send_epub_to_kindle(tmp_path, entry["title"], config, attachment_name=attachment_name)
    except (SanityCheckError, MailSendError) as e:
        kind = "refused" if isinstance(e, SanityCheckError) else "send error"
        print(f"  email failed ({kind}): {e}")
        record_email(entry, error=str(e))
        save_library(library, library_path)
        send_failure_alert(f"[epub_scraper] Kindle send failed: {entry['title']}",
                            f"Batch Ch {start}-{end} of {entry['title']} failed: {e}", config)
        return "failed"
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)  # transient -- library.json's last_emailed_* is the record

    record_email(entry, chapter=end)
    save_library(library, library_path)
    print(f"  emailed Ch {start}-{end} to Kindle")
    return "sent"


def _check_one(entry, cache_dir, delay, dry_run, library=None, library_path=None,
                mail_config=None, email_threshold=EMAIL_CHAPTER_THRESHOLD):
    """Check and, if needed, update a single tracked novel in place.
    Returns a short status string for the summary tally.

    library/library_path are only required when mail_config is given -- they
    let _send_batch persist its own email bookkeeping immediately, the same
    way cmd_check's outer loop persists check bookkeeping."""
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
            out_path = _retarget_output(entry, title, entry["last_known_chapter"])
            build_epub(title, profile.site_key, entry["chapter_id"], all_chapters, out_path)
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
        out_path = _retarget_output(entry, title, new_last_known)
        build_epub(title, profile.site_key, entry["chapter_id"], all_chapters, out_path)
        record_check(entry, total=new_last_known, title=title,
                     updated=(new_last_known > entry["last_known_chapter"]))
        if stopped_at:
            entry["last_error"] = (f"circuit breaker: {CIRCUIT_BREAKER_THRESHOLD} consecutive "
                                    f"failures starting at chapter {stopped_at}")

        # Batch-email gate lives here (delta>0 branch only): start/end are
        # computed from persistent entry fields, not this run's fetch count,
        # so any backlog left over from a prior failed/skipped send is
        # automatically included the next time this branch fires. A run
        # where the index reports no new chapters at all won't catch a
        # leftover backlog -- narrow, self-healing the moment new chapters
        # appear, not worth complicating the retry-only branch for.
        if mail_config is not None:
            _send_batch(entry, profile, session, base_url, cache_dir, delay, mail_config,
                        library, library_path, threshold=email_threshold)

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


def cmd_mail(args):
    """Email a tracked novel's not-yet-sent chapters to Kindle right now,
    bypassing the batch threshold (this is an explicit, deliberate request)."""
    library = load_library(args.library)
    entry = find_novel(library, args.site_key, args.chapter_id)
    if entry is None:
        print(f"Not tracked: {args.site_key}:{args.chapter_id}")
        sys.exit(1)

    profile = PROFILES.get(entry["site_key"])
    if profile is None:
        print(f"Unknown site_key {entry['site_key']!r}")
        sys.exit(1)

    try:
        mail_config = load_mail_config(args.mail_config)
    except MailConfigError as e:
        print(str(e))
        sys.exit(1)

    session = _session_for(entry["index_url"])
    base_url = get_base_url(entry["index_url"])

    status = _send_batch(entry, profile, session, base_url, args.cache_dir, args.delay,
                          mail_config, library, args.library, force=True)

    if status == "nothing-new":
        print(f"Nothing new to send since chapter {entry.get('last_emailed_chapter', 0)}.")
    elif status == "failed":
        sys.exit(1)


def cmd_check(args):
    library = load_library(args.library)
    only = _parse_only(args.only)
    targets = [e for e in library["novels"]
               if (only is not None and (e["site_key"], e["chapter_id"]) in only)
               or (only is None and e["enabled"])]

    if not targets:
        print("No matching novels to check." if only else "No enabled tracked novels to check.")
        return

    mail_config = None
    if args.email:
        try:
            mail_config = load_mail_config(args.mail_config)
        except MailConfigError as e:
            print(str(e))
            sys.exit(1)

    tally = {}
    for i, entry in enumerate(targets):
        label = f"{entry['site_key']}:{entry['chapter_id']}"
        try:
            status = _check_one(entry, cache_dir=args.cache_dir, delay=args.delay, dry_run=args.dry_run,
                                 library=library, library_path=args.library,
                                 mail_config=mail_config, email_threshold=args.email_threshold)
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
    p_check.add_argument("--email", action="store_true",
                          help="After checking, email tracked novels to Kindle once "
                               "--email-threshold new chapters have accumulated since the "
                               "last send (off by default)")
    p_check.add_argument("--email-threshold", type=int, default=EMAIL_CHAPTER_THRESHOLD, metavar="N")
    p_check.add_argument("--mail-config", default=DEFAULT_MAIL_CONFIG_PATH, metavar="FILE")
    p_check.set_defaults(func=cmd_check)

    p_search = sub.add_parser("search", help="Search tracked novels by title")
    p_search.add_argument("query")
    p_search.add_argument("--library", default=DEFAULT_LIBRARY_PATH, metavar="FILE")
    p_search.set_defaults(func=cmd_search)

    p_find = sub.add_parser("find", help="Search a site for a novel to add")
    p_find.add_argument("query")
    p_find.add_argument("--site", choices=sorted(PROFILES), default=None, metavar="KEY")
    p_find.add_argument("--limit", type=int, default=15, metavar="N")
    p_find.set_defaults(func=cmd_find)

    p_grep = sub.add_parser("grep", help="Full-text search inside downloaded epub chapters")
    p_grep.add_argument("query")
    p_grep.add_argument("--epubs-dir", default=EPUB_DIR, metavar="DIR")
    p_grep.add_argument("--case-sensitive", action="store_true")
    p_grep.add_argument("--context", type=int, default=60, metavar="N",
                         help="Characters of context around each match (default: 60)")
    p_grep.set_defaults(func=cmd_grep)

    p_mail = sub.add_parser("mail", help="Email a tracked novel's not-yet-sent chapters "
                                          "to Kindle now (bypasses the email threshold)")
    p_mail.add_argument("site_key")
    p_mail.add_argument("chapter_id")
    p_mail.add_argument("--cache-dir", default=".cache", metavar="DIR")
    p_mail.add_argument("--delay", type=float, default=2.5, metavar="SECS")
    p_mail.add_argument("--library", default=DEFAULT_LIBRARY_PATH, metavar="FILE")
    p_mail.add_argument("--mail-config", default=DEFAULT_MAIL_CONFIG_PATH, metavar="FILE")
    p_mail.set_defaults(func=cmd_mail)

    return parser


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
