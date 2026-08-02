import requests

from epub_scraper import cache
from epub_scraper.fetcher import ChallengeDetected
from epub_scraper.pacing import Pacer
from epub_scraper.scrape import scrape_chapters
from epub_scraper.sites.fanmtl import PROFILE
from fakes import FakeResponse, FakeSession
from html_builders import fanmtl_chapter_html

BASE_URL = "https://www.fanmtl.com"
CHAPTER_ID = "abc"


def url_for(n):
    return f"{BASE_URL}/novel/{CHAPTER_ID}_{n}.html"


def chapter_page(n, title=None):
    return fanmtl_chapter_html(
        [f"This is the real prose content of chapter {n}, long enough to keep."],
        title=title or f"Chapter {n}")


class ProgressRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, i, total, n, flag, label):
        self.calls.append((i, total, n, flag, label))


# -- cache vs. web dispatch -----------------------------------------------------

def test_cache_hit_skips_network(cache_dir):
    cache.save_cache(cache_dir, CHAPTER_ID, 1, chapter_page(1))
    session = FakeSession(strict=True)  # no stubs: any network call fails the test
    progress = ProgressRecorder()

    chapters, failed_ns, stopped_at = scrape_chapters(
        PROFILE, session, BASE_URL, CHAPTER_ID, [1],
        cache_dir=cache_dir, progress_cb=progress)

    assert chapters == [("Chapter 1", '<p>This is the real prose content of chapter 1, long enough to keep.</p>')]
    assert failed_ns == []
    assert stopped_at is None
    assert session.calls == []
    assert progress.calls[0][3] == "cache"


def test_web_fetch_success_saves_cache(cache_dir):
    session = FakeSession({url_for(1): FakeResponse(chapter_page(1), 200, url_for(1))})
    progress = ProgressRecorder()

    chapters, failed_ns, stopped_at = scrape_chapters(
        PROFILE, session, BASE_URL, CHAPTER_ID, [1],
        cache_dir=cache_dir, progress_cb=progress)

    assert chapters == [("Chapter 1", '<p>This is the real prose content of chapter 1, long enough to keep.</p>')]
    assert cache.load_cached(cache_dir, CHAPTER_ID, 1) == chapter_page(1)
    assert progress.calls[0][3] == "web"


def test_no_cache_bypasses_existing_cache_and_overwrites_it(cache_dir):
    cache.save_cache(cache_dir, CHAPTER_ID, 1, "<html><body><h2>Stale</h2></body></html>")
    session = FakeSession({url_for(1): FakeResponse(chapter_page(1, title="Fresh"), 200, url_for(1))})

    chapters, _, _ = scrape_chapters(
        PROFILE, session, BASE_URL, CHAPTER_ID, [1],
        cache_dir=cache_dir, no_cache=True)

    assert chapters[0][0] == "Fresh"
    assert len(session.calls) == 1
    assert cache.load_cached(cache_dir, CHAPTER_ID, 1) == chapter_page(1, title="Fresh")


# -- failure labeling -------------------------------------------------------------

def test_http_error_failure_labeled_with_status_code(cache_dir):
    session = FakeSession({url_for(1): FakeResponse("", 500, url_for(1))})
    progress = ProgressRecorder()

    chapters, failed_ns, stopped_at = scrape_chapters(
        PROFILE, session, BASE_URL, CHAPTER_ID, [1],
        cache_dir=cache_dir, progress_cb=progress)

    assert chapters == []
    assert failed_ns == [1]
    assert stopped_at is None
    assert progress.calls[0][3] == "skip"
    assert progress.calls[0][4] == "HTTP 500"


def test_generic_exception_failure_labeled_with_str(cache_dir):
    session = FakeSession({url_for(1): requests.exceptions.ConnectionError("refused")})
    progress = ProgressRecorder()

    chapters, failed_ns, _ = scrape_chapters(
        PROFILE, session, BASE_URL, CHAPTER_ID, [1],
        cache_dir=cache_dir, progress_cb=progress)

    assert failed_ns == [1]
    assert progress.calls[0][4] == "refused"


# -- circuit breaker -------------------------------------------------------------

def test_consecutive_failures_trip_breaker_at_threshold_streak_start(cache_dir):
    session = FakeSession({
        url_for(1): FakeResponse(chapter_page(1), 200, url_for(1)),
        url_for(2): FakeResponse("", 500, url_for(2)),
        url_for(3): FakeResponse("", 500, url_for(3)),
        url_for(4): FakeResponse("", 500, url_for(4)),
        # url_for(5) deliberately unstubbed: must never be reached
    })

    chapters, failed_ns, stopped_at = scrape_chapters(
        PROFILE, session, BASE_URL, CHAPTER_ID, [1, 2, 3, 4, 5],
        cache_dir=cache_dir, max_consecutive_failures=3)

    assert failed_ns == [2, 3, 4]
    assert stopped_at == 2  # streak START, not the trip point (4)
    assert len(chapters) == 1
    assert url_for(5) not in [c[1] for c in session.calls]


def test_failures_below_threshold_do_not_trip_breaker(cache_dir):
    session = FakeSession({
        url_for(1): FakeResponse("", 500, url_for(1)),
        url_for(2): FakeResponse("", 500, url_for(2)),
        url_for(3): FakeResponse(chapter_page(3), 200, url_for(3)),
    })

    chapters, failed_ns, stopped_at = scrape_chapters(
        PROFILE, session, BASE_URL, CHAPTER_ID, [1, 2, 3],
        cache_dir=cache_dir, max_consecutive_failures=3)

    assert stopped_at is None
    assert failed_ns == [1, 2]
    assert len(chapters) == 1
    assert len(session.calls) == 3  # every chapter in range was attempted


def test_success_resets_consecutive_failure_streak(cache_dir):
    session = FakeSession({
        url_for(1): FakeResponse("", 500, url_for(1)),
        url_for(2): FakeResponse("", 500, url_for(2)),
        url_for(3): FakeResponse(chapter_page(3), 200, url_for(3)),
        url_for(4): FakeResponse("", 500, url_for(4)),
        url_for(5): FakeResponse("", 500, url_for(5)),
    })

    chapters, failed_ns, stopped_at = scrape_chapters(
        PROFILE, session, BASE_URL, CHAPTER_ID, [1, 2, 3, 4, 5],
        cache_dir=cache_dir, max_consecutive_failures=3)

    assert stopped_at is None  # never 3-in-a-row, even though 4 total failures
    assert failed_ns == [1, 2, 4, 5]
    assert len(chapters) == 1


def test_breaker_trips_mid_range_streak_start_n_correct(cache_dir):
    responses = {}
    for n in range(1, 10):
        responses[url_for(n)] = FakeResponse(chapter_page(n), 200, url_for(n))
    for n in (10, 11, 12):
        responses[url_for(n)] = FakeResponse("", 500, url_for(n))
    session = FakeSession(responses)  # 13, 14 deliberately unstubbed

    chapters, failed_ns, stopped_at = scrape_chapters(
        PROFILE, session, BASE_URL, CHAPTER_ID, list(range(1, 15)),
        cache_dir=cache_dir, max_consecutive_failures=3)

    assert stopped_at == 10
    assert failed_ns == [10, 11, 12]
    assert len(chapters) == 9
    assert url_for(13) not in [c[1] for c in session.calls]


def test_max_consecutive_failures_none_never_trips(cache_dir):
    session = FakeSession({url_for(n): FakeResponse("", 500, url_for(n)) for n in range(1, 6)})

    chapters, failed_ns, stopped_at = scrape_chapters(
        PROFILE, session, BASE_URL, CHAPTER_ID, [1, 2, 3, 4, 5],
        cache_dir=cache_dir, max_consecutive_failures=None)

    assert stopped_at is None
    assert failed_ns == [1, 2, 3, 4, 5]
    assert chapters == []
    assert len(session.calls) == 5


# -- delay / progress_cb / ordering -----------------------------------------------

def test_delay_only_sleeps_after_a_non_last_web_fetch(monkeypatch, cache_dir):
    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

    cache.save_cache(cache_dir, CHAPTER_ID, 1, chapter_page(1))
    cache.save_cache(cache_dir, CHAPTER_ID, 3, chapter_page(3))
    session = FakeSession({
        url_for(2): FakeResponse(chapter_page(2), 200, url_for(2)),
        url_for(4): FakeResponse(chapter_page(4), 200, url_for(4)),
    })

    scrape_chapters(PROFILE, session, BASE_URL, CHAPTER_ID, [1, 2, 3, 4],
                     cache_dir=cache_dir, delay=1.23)

    # chapter 2 is a non-last web fetch -> sleeps; chapter 4 is web but LAST -> no sleep;
    # chapters 1 and 3 are cache hits -> never sleep.
    assert sleep_calls == [1.23]


def test_progress_cb_receives_expected_positional_args(cache_dir):
    session = FakeSession({
        url_for(1): FakeResponse(chapter_page(1), 200, url_for(1)),
        url_for(2): FakeResponse(chapter_page(2), 200, url_for(2)),
    })
    progress = ProgressRecorder()

    scrape_chapters(PROFILE, session, BASE_URL, CHAPTER_ID, [1, 2],
                     cache_dir=cache_dir, progress_cb=progress)

    assert progress.calls == [
        (0, 2, 1, "web", "Chapter 1"),
        (1, 2, 2, "web", "Chapter 2"),
    ]


def test_chapters_list_preserves_iteration_order_of_chapter_range(cache_dir):
    session = FakeSession({
        url_for(5): FakeResponse(chapter_page(5), 200, url_for(5)),
        url_for(3): FakeResponse(chapter_page(3), 200, url_for(3)),
        url_for(1): FakeResponse(chapter_page(1), 200, url_for(1)),
    })

    chapters, _, _ = scrape_chapters(PROFILE, session, BASE_URL, CHAPTER_ID, [5, 3, 1],
                                      cache_dir=cache_dir)

    assert [title for title, _ in chapters] == ["Chapter 5", "Chapter 3", "Chapter 1"]


# -- pacer wiring -----------------------------------------------------------

def test_429_response_throttles_pacer(cache_dir, tmp_path):
    pacer = Pacer.load(str(tmp_path / "pacing.json"), default_interval=2.5)
    session = FakeSession({url_for(1): FakeResponse("", 429, url_for(1), headers={"Retry-After": "30"})})

    scrape_chapters(PROFILE, session, BASE_URL, CHAPTER_ID, [1],
                     cache_dir=cache_dir, pacer=pacer)

    assert pacer.current_interval(PROFILE.site_key) == 30.0


def test_challenge_detected_throttles_pacer(cache_dir, tmp_path):
    pacer = Pacer.load(str(tmp_path / "pacing.json"), default_interval=2.5)
    session = FakeSession({url_for(1): FakeResponse("Just a moment...", 200, url_for(1))})

    scrape_chapters(PROFILE, session, BASE_URL, CHAPTER_ID, [1],
                     cache_dir=cache_dir, pacer=pacer)

    assert pacer.current_interval(PROFILE.site_key) == 5.0  # 2.5 * BACKOFF_FACTOR


def test_challenge_detected_is_labeled_distinctly_in_progress_cb(cache_dir, tmp_path):
    pacer = Pacer.load(str(tmp_path / "pacing.json"), default_interval=2.5)
    session = FakeSession({url_for(1): FakeResponse("Just a moment...", 200, url_for(1))})
    progress = ProgressRecorder()

    scrape_chapters(PROFILE, session, BASE_URL, CHAPTER_ID, [1],
                     cache_dir=cache_dir, pacer=pacer, progress_cb=progress)

    assert progress.calls[0][3] == "skip"
    assert progress.calls[0][4] == "challenge page"


def test_sleep_uses_pacer_gap_when_pacer_provided(monkeypatch, cache_dir, tmp_path):
    pacer = Pacer.load(str(tmp_path / "pacing.json"), default_interval=2.5)
    monkeypatch.setattr(pacer, "gap", lambda site_key: 9.87)
    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

    session = FakeSession({
        url_for(1): FakeResponse(chapter_page(1), 200, url_for(1)),
        url_for(2): FakeResponse(chapter_page(2), 200, url_for(2)),
    })

    scrape_chapters(PROFILE, session, BASE_URL, CHAPTER_ID, [1, 2],
                     cache_dir=cache_dir, pacer=pacer)

    assert sleep_calls == [9.87]


def test_no_pacer_falls_back_to_fixed_delay(monkeypatch, cache_dir):
    # Regression: unchanged behavior when pacer isn't passed at all.
    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))
    session = FakeSession({
        url_for(1): FakeResponse(chapter_page(1), 200, url_for(1)),
        url_for(2): FakeResponse(chapter_page(2), 200, url_for(2)),
    })

    scrape_chapters(PROFILE, session, BASE_URL, CHAPTER_ID, [1, 2],
                     cache_dir=cache_dir, delay=1.23)

    assert sleep_calls == [1.23]
