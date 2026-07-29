import time

import requests

from . import engine
from .cache import load_cached, save_cache
from .fetcher import fetch


def scrape_chapters(profile, session, base_url, chapter_id, chapter_range,
                     cache_dir=".cache", no_cache=False, delay=2.5, progress_cb=None,
                     max_consecutive_failures=None):
    """Fetch+cache+parse each n in chapter_range (cache checked before network,
    per-chapter). progress_cb(i, total, n, flag, label), if given, is called once
    per chapter: flag is "cache"/"web" (label = chapter title) or "skip"
    (label = error message).

    max_consecutive_failures: if set, stop attempting further chapters once this
    many real fetch/parse attempts have failed BACK TO BACK (any success resets
    the streak to 0). None preserves the original behavior of always attempting
    every chapter in chapter_range regardless of failures.

    Returns (chapters, failed_ns, stopped_at):
      chapters:   list[(title, body_html)] for every chapter that succeeded, in order
      failed_ns:  list[int], chapter numbers that failed, in order
      stopped_at: the first chapter number of the streak that tripped the
                  breaker, or None if it never tripped
    """
    chapters = []
    failed_ns = []
    stopped_at = None
    consecutive_failures = 0
    streak_start_n = None

    total = len(chapter_range)
    for i, n in enumerate(chapter_range):
        url = engine.chapter_url(profile, base_url, chapter_id, n)
        src = None
        try:
            cached = None if no_cache else load_cached(cache_dir, chapter_id, n)
            if cached:
                html = cached
                src = "cache"
            else:
                html = fetch(url, session)
                save_cache(cache_dir, chapter_id, n, html)
                src = "web"

            ch_title, body = engine.parse_chapter(profile, html, n)
            chapters.append((ch_title, body))
            consecutive_failures = 0
            if progress_cb:
                progress_cb(i, total, n, src, ch_title)
        except Exception as e:
            label = f"HTTP {e.response.status_code}" if isinstance(e, requests.HTTPError) else str(e)
            failed_ns.append(n)
            if progress_cb:
                progress_cb(i, total, n, "skip", label)
            if consecutive_failures == 0:
                streak_start_n = n
            consecutive_failures += 1

        if max_consecutive_failures is not None and consecutive_failures >= max_consecutive_failures:
            stopped_at = streak_start_n
            break

        if i < total - 1 and src == "web":
            time.sleep(delay)

    return chapters, failed_ns, stopped_at
