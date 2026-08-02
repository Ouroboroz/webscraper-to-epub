# Fetcher Hardening (Pacing, Challenge Detection, Safe Links) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port four cheap, dependency-free scraping-resilience techniques (learned from reviewing `lncrawl/scraper`, Apache-2.0) into `epub_scraper`: response diagnosis for challenge/interstitial pages, a jittered+learned request pacer, and honeypot-link filtering.

**Architecture:** One new module, `epub_scraper/pacing.py`, holds a `Pacer` class that is the single source of "how long to wait" for every site, backed by one `pacing.json` file (site_key → interval). `fetcher.py` gains a body-sniffing check that raises a new `ChallengeDetected` exception on soft-blocked responses. `engine.py` gains a link-safety filter used only in `parse_index()`'s anchor scans. `scrape.py` is the only place that wires diagnosis output into the pacer (a 429 or a `ChallengeDetected` both widen the pacer's interval for that site); `cli.py` and `update.py` construct one `Pacer` per invocation and pass it down. All changes to `scrape_chapters()`, `_check_one()`, and `_send_batch()` are additive (`pacer=None` default) so the ~40 existing tests that never pass `pacer` are unaffected.

**Tech Stack:** Python 3, `requests`, `beautifulsoup4`, `pytest` — no new dependencies.

## Global Constraints

- No new third-party dependencies (stdlib `random`/`json`/`os` only for `pacing.py`).
- All new/changed behavior must stay covered by the offline `FakeSession`/`FakeResponse` test doubles in `tests/fakes.py` — no real network or real `time.sleep` in tests (the autouse `_no_real_sleep` fixture in `tests/conftest.py` already neutralizes `time.sleep` globally).
- `pacing.json` follows the same convention as `library.json`: gitignored, single file, JSON, lives at the repo root by default.
- Every change to `scrape_chapters()`, `_check_one()`, `_send_batch()` must be backward compatible for callers that don't pass `pacer` — this is what keeps the existing test suites (`test_update.py`, `test_scrape.py`) passing without modification, except where a task explicitly says otherwise.
- `--delay` becomes the *mean* of a jittered distribution wherever a `Pacer` is wired in (cli.py, update.py's `check`/`mail`); its default value (`2.5`) and flag name are unchanged.

---

### Task 1: `Pacer` class in a new `pacing.py` module

**Files:**
- Create: `epub_scraper/pacing.py`
- Test: `tests/test_pacing.py`

**Interfaces:**
- Produces: `epub_scraper.pacing.DEFAULT_PACING_PATH` (str, `"pacing.json"`), `epub_scraper.pacing.Pacer` with:
  - `Pacer.load(path=DEFAULT_PACING_PATH, default_interval=2.5) -> Pacer` (classmethod)
  - `Pacer.current_interval(site_key) -> float`
  - `Pacer.gap(site_key) -> float` (jittered draw, mean == `current_interval(site_key)`, clamped to `[0.2*mean, 3.0*mean]`)
  - `Pacer.throttled(site_key, retry_after=None) -> float` (widens and persists the interval for `site_key`; never shrinks it; returns the new interval)
  - `Pacer.save()` (writes `self.intervals` to `self.path` as JSON)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pacing.py`:

```python
import json

from epub_scraper.pacing import Pacer


def test_load_with_no_existing_file_uses_default_interval(tmp_path):
    path = str(tmp_path / "pacing.json")
    pacer = Pacer.load(path, default_interval=3.0)
    assert pacer.current_interval("fanmtl") == 3.0


def test_load_reads_persisted_interval_for_known_site(tmp_path):
    path = str(tmp_path / "pacing.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"fanmtl": 9.0}, f)

    pacer = Pacer.load(path, default_interval=3.0)

    assert pacer.current_interval("fanmtl") == 9.0
    assert pacer.current_interval("other_site") == 3.0


def test_gap_varies_across_calls(tmp_path):
    pacer = Pacer.load(str(tmp_path / "pacing.json"), default_interval=4.0)
    gaps = [pacer.gap("fanmtl") for _ in range(20)]
    assert len(set(gaps)) > 1


def test_gap_stays_within_floor_and_ceiling(tmp_path):
    pacer = Pacer.load(str(tmp_path / "pacing.json"), default_interval=4.0)
    gaps = [pacer.gap("fanmtl") for _ in range(200)]
    assert all(0.8 <= g <= 12.0 for g in gaps)  # 0.2x .. 3x of mean=4.0


def test_throttled_with_retry_after_sets_interval(tmp_path):
    pacer = Pacer.load(str(tmp_path / "pacing.json"), default_interval=2.5)
    widened = pacer.throttled("fanmtl", retry_after="30")
    assert widened == 30.0
    assert pacer.current_interval("fanmtl") == 30.0


def test_throttled_without_retry_after_widens_by_backoff_factor(tmp_path):
    pacer = Pacer.load(str(tmp_path / "pacing.json"), default_interval=2.5)
    widened = pacer.throttled("fanmtl")
    assert widened == 5.0  # 2.5 * BACKOFF_FACTOR (2.0)


def test_throttled_never_shrinks_below_current_interval(tmp_path):
    pacer = Pacer.load(str(tmp_path / "pacing.json"), default_interval=30.0)
    widened = pacer.throttled("fanmtl", retry_after="5")  # server asks for less than we're already at
    assert widened == 30.0


def test_throttled_caps_at_max_interval(tmp_path):
    pacer = Pacer.load(str(tmp_path / "pacing.json"), default_interval=2.5)
    widened = pacer.throttled("fanmtl", retry_after="99999")
    assert widened == 120.0  # MAX_INTERVAL


def test_throttled_with_unparseable_retry_after_widens_by_backoff_factor(tmp_path):
    pacer = Pacer.load(str(tmp_path / "pacing.json"), default_interval=2.5)
    widened = pacer.throttled("fanmtl", retry_after="not-a-number")
    assert widened == 5.0


def test_throttled_persists_immediately(tmp_path):
    path = str(tmp_path / "pacing.json")
    pacer = Pacer.load(path, default_interval=2.5)
    pacer.throttled("fanmtl", retry_after="30")

    reloaded = Pacer.load(path, default_interval=2.5)
    assert reloaded.current_interval("fanmtl") == 30.0


def test_two_sites_stay_independent_in_same_file(tmp_path):
    pacer = Pacer.load(str(tmp_path / "pacing.json"), default_interval=2.5)
    pacer.throttled("fanmtl", retry_after="30")
    assert pacer.current_interval("fanmtl") == 30.0
    assert pacer.current_interval("other_site") == 2.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_pacing.py -v`
Expected: FAIL/ERROR — `ModuleNotFoundError: No module named 'epub_scraper.pacing'`

- [ ] **Step 3: Write the implementation**

Create `epub_scraper/pacing.py`:

```python
import json
import os
import random

DEFAULT_PACING_PATH = "pacing.json"

BACKOFF_FACTOR = 2.0
MAX_INTERVAL = 120.0
GAMMA_SHAPE = 2.5


class Pacer:
    """Per-site request pacing: jittered gaps around a learned interval that
    widens (and persists) when a site signals it's being asked for too much.
    One Pacer covers every site_key for the process's run -- pacing.json
    holds {site_key: interval} for all of them, never just one site."""

    def __init__(self, path, default_interval, intervals=None):
        self.path = path
        self.default_interval = default_interval
        self.intervals = dict(intervals or {})

    @classmethod
    def load(cls, path=DEFAULT_PACING_PATH, default_interval=2.5):
        intervals = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                intervals = json.load(f)
        return cls(path, default_interval, intervals)

    def current_interval(self, site_key):
        return self.intervals.get(site_key, self.default_interval)

    def gap(self, site_key):
        mean = self.current_interval(site_key)
        draw = random.gammavariate(GAMMA_SHAPE, mean / GAMMA_SHAPE)
        return min(max(draw, mean * 0.2), mean * 3.0)

    def throttled(self, site_key, retry_after=None):
        current = self.current_interval(site_key)
        widened = current * BACKOFF_FACTOR
        if retry_after is not None:
            try:
                widened = float(retry_after)
            except (TypeError, ValueError):
                pass
        widened = min(max(widened, current), MAX_INTERVAL)
        self.intervals[site_key] = widened
        self.save()
        return widened

    def save(self):
        dirname = os.path.dirname(self.path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.intervals, f, indent=2)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_pacing.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add epub_scraper/pacing.py tests/test_pacing.py
git commit -m "Add Pacer: jittered, learned, persisted per-site request pacing"
```

---

### Task 2: Challenge-page detection in `fetcher.py`

**Files:**
- Modify: `epub_scraper/fetcher.py`
- Test: `tests/test_fetcher.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `epub_scraper.fetcher.ChallengeDetected` (Exception subclass), raised by `fetch()` when a response body looks like a bot-challenge/interstitial page. `fetch()`'s signature and success-path return type (`str`) are unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fetcher.py` (add `ChallengeDetected` to the existing `from epub_scraper.fetcher import fetch` import line, making it `from epub_scraper.fetcher import ChallengeDetected, fetch`):

```python
def test_fetch_raises_challenge_detected_on_challenge_body():
    url = "https://example.com/blocked.html"
    body = "<html><body>Checking your browser before accessing example.com</body></html>"
    session = FakeSession({url: FakeResponse(body, 200, url)})
    with pytest.raises(ChallengeDetected):
        fetch(url, session)


def test_fetch_normal_200_body_is_unaffected_by_challenge_check():
    url = "https://example.com/page.html"
    session = FakeSession({url: FakeResponse("hello", 200, url)})
    assert fetch(url, session) == "hello"


def test_fetch_challenge_check_is_case_insensitive():
    url = "https://example.com/blocked.html"
    body = "<title>JUST A MOMENT...</title>"
    session = FakeSession({url: FakeResponse(body, 200, url)})
    with pytest.raises(ChallengeDetected):
        fetch(url, session)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_fetcher.py -v`
Expected: FAIL — `ImportError: cannot import name 'ChallengeDetected'`

- [ ] **Step 3: Write the implementation**

Replace the full contents of `epub_scraper/fetcher.py` with:

```python
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_CHALLENGE_MARKERS = (
    "checking your browser",
    "cf-browser-verification",
    "just a moment",
    "ddos-guard",
    "attention required! | cloudflare",
    "captcha-delivery",
)


class ChallengeDetected(Exception):
    """Response body looks like a bot-challenge/interstitial page, not real content."""


def _looks_like_challenge(html):
    lowered = html.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


def fetch(url, session):
    r = session.get(url, timeout=15)
    r.raise_for_status()
    if _looks_like_challenge(r.text):
        raise ChallengeDetected(f"challenge page at {url}")
    return r.text
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_fetcher.py -v`
Expected: PASS (all existing + 3 new tests)

- [ ] **Step 5: Commit**

```bash
git add epub_scraper/fetcher.py tests/test_fetcher.py
git commit -m "Detect challenge/interstitial pages in fetch() instead of treating them as real content"
```

---

### Task 3: Safe-link filtering in `engine.py`'s `parse_index()`

**Files:**
- Modify: `epub_scraper/engine.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Produces: `epub_scraper.engine._safe_anchors(soup)` (internal helper, returns `list[Tag]`). `parse_index()`'s signature and return type (`IndexResult`) are unchanged. `search_novels()` is untouched.

- [ ] **Step 1: Write the failing tests**

Add to the `-- parse_index --` section of `tests/test_engine.py`:

```python
def test_parse_index_skips_nofollow_hidden_decoy_for_chapter_id():
    html = ('<html><body><h1>Test Novel</h1>'
            '<a href="/novel/decoy_999.html" rel="nofollow" style="display:none">Ch 999</a>'
            '<a href="/novel/abc_1.html">Chapter 1</a>'
            '</body></html>')
    result = engine.parse_index(PROFILE, html, "https://www.fanmtl.com/novel/abc.html")
    assert result.chapter_id == "abc"


def test_parse_index_skips_aria_hidden_decoy_for_chapter_id():
    html = ('<html><body><h1>Test Novel</h1>'
            '<a href="/novel/decoy_999.html" aria-hidden="true">Ch 999</a>'
            '<a href="/novel/abc_1.html">Chapter 1</a>'
            '</body></html>')
    result = engine.parse_index(PROFILE, html, "https://www.fanmtl.com/novel/abc.html")
    assert result.chapter_id == "abc"


def test_parse_index_skips_decoy_link_in_fallback_max_scan():
    html = ('<html><body><h1>Test Novel</h1>'
            '<a href="/novel/abc_1.html">Chapter 1</a>'
            '<a href="/novel/abc_999.html" rel="nofollow">Decoy</a>'
            '</body></html>')
    result = engine.parse_index(PROFILE, html, "https://www.fanmtl.com/novel/abc.html")
    assert result.total == 1


def test_parse_index_normal_links_without_decoys_are_unaffected():
    html = fanmtl_index_html(chapter_id="abc", total=5, with_count_text=False)
    result = engine.parse_index(PROFILE, html, "https://www.fanmtl.com/novel/abc.html")
    assert result.chapter_id == "abc"
    assert result.total == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_engine.py -v -k parse_index`
Expected: FAIL — the two decoy-skipping tests fail (`chapter_id == "decoy"` / `total == 999`) since filtering doesn't exist yet; the other two pass already (they're regression coverage).

- [ ] **Step 3: Write the implementation**

In `epub_scraper/engine.py`, add the helper near the top (after `_escape_xhtml`):

```python
_HIDDEN_STYLE_MARKERS = ("display:none", "visibility:hidden", "opacity:0")


def _is_safe_link(a):
    rel = a.get("rel") or []
    if isinstance(rel, str):
        rel = rel.split()
    if "nofollow" in rel:
        return False
    if a.has_attr("hidden"):
        return False
    if (a.get("aria-hidden") or "").strip().lower() == "true":
        return False
    style = (a.get("style") or "").lower().replace(" ", "")
    if any(marker in style for marker in _HIDDEN_STYLE_MARKERS):
        return False
    return True


def _safe_anchors(soup):
    return [a for a in soup.find_all("a", href=True) if _is_safe_link(a)]
```

Then in `parse_index()`, replace both occurrences of `soup.find_all("a", href=True)` with `_safe_anchors(soup)`:

```python
    # Extract slug/id from chapter links
    chapter_id = None
    for a in _safe_anchors(soup):
        m = re.search(profile.chapter_link_pattern, a["href"])
        if m:
            chapter_id = m.group(1)
            break
```

and

```python
    # Fallback total: scan all chapter hrefs for the highest number
    if total is None:
        nums = []
        for a in _safe_anchors(soup):
            m = re.search(profile.chapter_number_fallback_pattern, a["href"])
            if m:
                nums.append(int(m.group(1)))
        if nums:
            total = max(nums)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_engine.py -v`
Expected: PASS (all existing + 4 new tests)

- [ ] **Step 5: Commit**

```bash
git add epub_scraper/engine.py tests/test_engine.py
git commit -m "Skip nofollow/hidden decoy links when scanning for chapter_id in parse_index()"
```

---

### Task 4: Wire `Pacer` and `ChallengeDetected` into `scrape.py`

**Files:**
- Modify: `tests/fakes.py` (add `headers` to `FakeResponse`)
- Modify: `epub_scraper/scrape.py`
- Test: `tests/test_scrape.py`

**Interfaces:**
- Consumes: `epub_scraper.pacing.Pacer` (Task 1), `epub_scraper.fetcher.ChallengeDetected` (Task 2).
- Produces: `scrape_chapters(profile, session, base_url, chapter_id, chapter_range, cache_dir=".cache", no_cache=False, delay=2.5, pacer=None, progress_cb=None, max_consecutive_failures=None)` — same return type `(chapters, failed_ns, stopped_at)` as before. When `pacer` is `None`, behavior is byte-for-byte identical to today.

- [ ] **Step 1: Add `headers` support to the `FakeResponse` test double**

In `tests/fakes.py`, change `FakeResponse.__init__`:

```python
class FakeResponse:
    def __init__(self, text="", status_code=200, url="", headers=None):
        self.text = text
        self.status_code = status_code
        self.url = url
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} Error for url: {self.url}", response=self)
```

This is additive (`headers` defaults to `{}`), so every existing `FakeResponse(...)` construction across the test suite keeps working unchanged.

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_scrape.py` (add imports `from epub_scraper.fetcher import ChallengeDetected` and `from epub_scraper.pacing import Pacer` at the top):

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_scrape.py -v`
Expected: FAIL — `TypeError: scrape_chapters() got an unexpected keyword argument 'pacer'`

- [ ] **Step 4: Write the implementation**

Replace the full contents of `epub_scraper/scrape.py` with:

```python
import time

import requests

from . import engine
from .cache import load_cached, save_cache
from .fetcher import ChallengeDetected, fetch


def scrape_chapters(profile, session, base_url, chapter_id, chapter_range,
                     cache_dir=".cache", no_cache=False, delay=2.5, pacer=None,
                     progress_cb=None, max_consecutive_failures=None):
    """Fetch+cache+parse each n in chapter_range (cache checked before network,
    per-chapter). progress_cb(i, total, n, flag, label), if given, is called once
    per chapter: flag is "cache"/"web" (label = chapter title) or "skip"
    (label = error message).

    max_consecutive_failures: if set, stop attempting further chapters once this
    many real fetch/parse attempts have failed BACK TO BACK (any success resets
    the streak to 0). None preserves the original behavior of always attempting
    every chapter in chapter_range regardless of failures.

    pacer: an epub_scraper.pacing.Pacer, or None. When given, it replaces the
    fixed `delay` sleep with a jittered gap, and a 429 or ChallengeDetected
    widens its learned interval for profile.site_key. When None, behavior is
    unchanged from before pacer support existed (fixed time.sleep(delay)).

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
            if isinstance(e, requests.HTTPError):
                label = f"HTTP {e.response.status_code}"
                if pacer is not None and e.response.status_code == 429:
                    pacer.throttled(profile.site_key, retry_after=e.response.headers.get("Retry-After"))
            elif isinstance(e, ChallengeDetected):
                label = "challenge page"
                if pacer is not None:
                    pacer.throttled(profile.site_key)
            else:
                label = str(e)
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
            if pacer is not None:
                time.sleep(pacer.gap(profile.site_key))
            else:
                time.sleep(delay)

    return chapters, failed_ns, stopped_at
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_scrape.py tests/test_fetcher.py -v`
Expected: PASS (all existing + 5 new tests)

- [ ] **Step 6: Run the full suite to check for regressions**

Run: `python -m pytest`
Expected: PASS — this touches a shared test double (`FakeResponse`) and a module several other files import from, so confirm nothing else broke.

- [ ] **Step 7: Commit**

```bash
git add tests/fakes.py epub_scraper/scrape.py tests/test_scrape.py
git commit -m "Wire Pacer and ChallengeDetected into scrape_chapters(), additive via pacer=None"
```

---

### Task 5: Wire `Pacer` into `cli.py`

**Files:**
- Modify: `epub_scraper/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `epub_scraper.pacing.Pacer`, `epub_scraper.pacing.DEFAULT_PACING_PATH` (Task 1).
- Produces: new `--pacing-file FILE` CLI flag (default `pacing.json`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli.py` (add `import json` at the top):

```python
def test_main_pacing_file_flag_persists_widened_interval_on_429(monkeypatch, tmp_path):
    pacing_file = str(tmp_path / "custom_pacing.json")
    session = FakeSession({
        INDEX_URL: FakeResponse(index_page(total=1), 200, INDEX_URL),
        chapter_url(1): FakeResponse("", 429, chapter_url(1), headers={"Retry-After": "20"}),
    })

    with pytest.raises(SystemExit):  # no chapters fetched (only one, and it 429s) -> exits 1
        run_cli(monkeypatch,
                [INDEX_URL, "--cache-dir", str(tmp_path / ".cache"), "--pacing-file", pacing_file],
                session)

    with open(pacing_file, encoding="utf-8") as f:
        data = json.load(f)
    assert data["fanmtl"] == 20.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli.py -v -k pacing_file`
Expected: FAIL — `error: unrecognized arguments: --pacing-file ...` (argparse `SystemExit(2)`, caught by `pytest.raises(SystemExit)` but for the wrong reason — confirm by temporarily checking `exc_info.value.code == 2` if the test passes for a reason other than the one intended; the flag simply doesn't exist yet, and `pacing_file` is never written to at all).

- [ ] **Step 3: Write the implementation**

In `epub_scraper/cli.py`:

1. Add to the imports:
```python
from .pacing import DEFAULT_PACING_PATH, Pacer
```

2. Add to the module docstring's Options list (after the `--delay` line):
```
  --pacing-file FILE  Where to persist learned per-site pacing (default: pacing.json)
```

3. Add the argparse flag (after the `--site` argument):
```python
    parser.add_argument("--pacing-file", default=DEFAULT_PACING_PATH, metavar="FILE",
                        help="Where to persist learned per-site request pacing (default: pacing.json)")
```

4. Construct the pacer right after `session` is built:
```python
    session = requests.Session()
    session.headers.update(HEADERS)
    session.headers["Referer"] = get_base_url(args.url)

    pacer = Pacer.load(args.pacing_file, default_interval=args.delay)
```

5. Pass it to `scrape_chapters()`:
```python
    chapters, failed_ns, _ = scrape_chapters(
        profile, session, base_url, chapter_id, chapter_range,
        cache_dir=args.cache_dir, no_cache=args.no_cache,
        delay=args.delay, pacer=pacer, progress_cb=_progress)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cli.py -v`
Expected: PASS (all existing + 1 new test)

- [ ] **Step 5: Run the full suite to check for regressions**

Run: `python -m pytest`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add epub_scraper/cli.py tests/test_cli.py
git commit -m "Wire Pacer into the one-shot CLI via a new --pacing-file flag"
```

---

### Task 6: Wire `Pacer` into `update.py` (`check` and `mail`)

**Files:**
- Modify: `epub_scraper/update.py`
- Modify: `tests/test_update.py`

**Interfaces:**
- Consumes: `epub_scraper.pacing.Pacer`, `epub_scraper.pacing.DEFAULT_PACING_PATH` (Task 1).
- Produces: new `--pacing-file FILE` flag on the `check` and `mail` subcommands (default `pacing.json`). `_check_one(...)` and `_send_batch(...)` both gain a `pacer=None` keyword argument, threaded down to their `scrape_chapters()` calls.

- [ ] **Step 1: Update the 7 existing `argparse.Namespace(...)` test fixtures that call `cmd_check`/`cmd_mail` directly**

`cmd_check` and `cmd_mail` are going to read `args.pacing_file`, so every test that builds an `argparse.Namespace` by hand (bypassing `build_parser()`) needs that field or it'll hit `AttributeError`. In `tests/test_update.py`, update these 7 call sites by adding `pacing_file=str(tmp_path / "pacing.json")` to each:

Line ~651-652 (`test_cmd_mail_...` sends batch):
```python
    args = argparse.Namespace(site_key="fanmtl", chapter_id=CHAPTER_ID, cache_dir=cache_dir,
                               delay=0, library=library_path, mail_config="unused",
                               pacing_file=str(tmp_path / "pacing.json"))
```

Line ~671-672 (`test_cmd_mail_nothing_pending_...`):
```python
    args = argparse.Namespace(site_key="fanmtl", chapter_id=CHAPTER_ID, cache_dir=".cache",
                               delay=0, library=library_path, mail_config="unused",
                               pacing_file=str(tmp_path / "pacing.json"))
```

Line ~679-680 (`test_cmd_mail_untracked_entry_exits_1`):
```python
    args = argparse.Namespace(site_key="fanmtl", chapter_id="nope", cache_dir=".cache",
                               delay=0, library=library_path, mail_config="unused",
                               pacing_file=str(tmp_path / "pacing.json"))
```

Line ~697-698 (`test_cmd_mail_bad_config_exits_1`):
```python
    args = argparse.Namespace(site_key="fanmtl", chapter_id=CHAPTER_ID, cache_dir=".cache",
                               delay=0, library=library_path, mail_config="unused",
                               pacing_file=str(tmp_path / "pacing.json"))
```

Line ~715-716 (`test_cmd_mail_send_batch_failure_exits_1`):
```python
    args = argparse.Namespace(site_key="fanmtl", chapter_id=CHAPTER_ID, cache_dir=".cache",
                               delay=0, library=library_path, mail_config="unused",
                               pacing_file=str(tmp_path / "pacing.json"))
```

Line ~894-896 (`test_cmd_check_..._email` happy path):
```python
    args = argparse.Namespace(library=library_path, cache_dir=cache_dir, delay=0,
                               novel_delay=0, only=None, dry_run=False,
                               email=True, email_threshold=100, mail_config="unused",
                               pacing_file=str(tmp_path / "pacing.json"))
```

Line ~919-921 (`test_cmd_check_missing_mail_config_exits_1_...`):
```python
    args = argparse.Namespace(library=library_path, cache_dir=".cache", delay=0,
                               novel_delay=0, only=None, dry_run=False,
                               email=True, email_threshold=100, mail_config="unused",
                               pacing_file=str(tmp_path / "pacing.json"))
```

(All of these tests already receive `tmp_path` as a fixture parameter — confirm each test signature includes it; every one of the 7 does, per current `test_update.py`.)

- [ ] **Step 2: Write the new failing tests**

Add near the `cmd_mail`/`cmd_check` test sections in `tests/test_update.py` (add `from epub_scraper.pacing import Pacer` and `import json` to the top imports):

```python
def test_cmd_mail_pacing_file_flag_persists_widened_interval_on_429(monkeypatch, tmp_path):
    library_path = str(tmp_path / "library.json")
    pacing_file = str(tmp_path / "pacing.json")
    lib = {"novels": []}
    add_novel(lib, site_key="fanmtl", chapter_id=CHAPTER_ID, index_url=INDEX_URL,
              title="T", output_file="epubs/x.epub", last_known_chapter=1)
    save_library(lib, library_path)

    session = FakeSession({chapter_url(1): FakeResponse("", 429, chapter_url(1),
                                                          headers={"Retry-After": "15"})})
    monkeypatch.setattr(update, "_session_for", lambda url: session)
    monkeypatch.setattr(update, "load_mail_config", lambda path: mail_config())

    args = argparse.Namespace(site_key="fanmtl", chapter_id=CHAPTER_ID, cache_dir=".cache",
                               delay=0, library=library_path, mail_config="unused",
                               pacing_file=pacing_file)
    with pytest.raises(SystemExit):  # the only chapter fails to send -> exits 1
        update.cmd_mail(args)

    with open(pacing_file, encoding="utf-8") as f:
        data = json.load(f)
    assert data["fanmtl"] == 15.0


def test_cmd_check_pacing_file_flag_persists_widened_interval_on_429(monkeypatch, tmp_path):
    library_path = str(tmp_path / "library.json")
    pacing_file = str(tmp_path / "pacing.json")
    lib = {"novels": []}
    add_novel(lib, site_key="fanmtl", chapter_id=CHAPTER_ID, index_url=INDEX_URL,
              title="T", output_file="epubs/x.epub", last_known_chapter=0)
    save_library(lib, library_path)

    session = FakeSession({
        INDEX_URL: FakeResponse(index_page(total=1), 200, INDEX_URL),
        chapter_url(1): FakeResponse("", 429, chapter_url(1), headers={"Retry-After": "12"}),
    })
    monkeypatch.setattr(update, "_session_for", lambda url: session)

    args = argparse.Namespace(library=library_path, cache_dir=str(tmp_path / ".cache"), delay=0,
                               novel_delay=0, only=None, dry_run=False,
                               email=False, email_threshold=100, mail_config="unused",
                               pacing_file=pacing_file)
    update.cmd_check(args)

    with open(pacing_file, encoding="utf-8") as f:
        data = json.load(f)
    assert data["fanmtl"] == 12.0
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_update.py -v -k pacing_file`
Expected: FAIL — `AttributeError: 'Namespace' object has no attribute 'pacing_file'` for the two new tests, and (before Step 1's edits are in place) for the 7 pre-existing tests too once `cmd_check`/`cmd_mail` start reading `args.pacing_file` in Step 4.

- [ ] **Step 4: Write the implementation**

In `epub_scraper/update.py`:

1. Add to the imports:
```python
from .pacing import DEFAULT_PACING_PATH, Pacer
```

2. Update `_send_batch`'s signature and its `scrape_chapters()` call:
```python
def _send_batch(entry, profile, session, base_url, cache_dir, delay, config,
                 library, library_path, *, force=False, threshold=EMAIL_CHAPTER_THRESHOLD,
                 pacer=None):
    """...(existing docstring unchanged)..."""
    start = entry.get("last_emailed_chapter", 0) + 1
    end = entry["last_known_chapter"]
    if end < start:
        return "nothing-new"
    if not force and (end - start + 1) < threshold:
        return "below-threshold"

    batch_chapters, _, _ = scrape_chapters(
        profile, session, base_url, entry["chapter_id"], range(start, end + 1),
        cache_dir=cache_dir, delay=delay, pacer=pacer)
```
(rest of `_send_batch` unchanged)

3. Update `_check_one`'s signature and its three `scrape_chapters()` calls:
```python
def _check_one(entry, cache_dir, delay, dry_run, library=None, library_path=None,
                mail_config=None, email_threshold=EMAIL_CHAPTER_THRESHOLD, pacer=None):
    """...(existing docstring unchanged)..."""
```
In the `delta <= 0` branch:
```python
        retried, still_failed, _ = scrape_chapters(
            profile, session, base_url, entry["chapter_id"], sorted(entry["failed_chapters"]),
            cache_dir=cache_dir, delay=delay, pacer=pacer,
            max_consecutive_failures=CIRCUIT_BREAKER_THRESHOLD)
        fetched_count = len(retried)
        entry["failed_chapters"] = still_failed
        if retried:
            all_chapters, _, _ = scrape_chapters(
                profile, session, base_url, entry["chapter_id"],
                range(1, entry["last_known_chapter"] + 1), cache_dir=cache_dir, delay=delay,
                pacer=pacer)
```
In the `delta > 0` branch:
```python
        all_chapters, failed_ns, stopped_at = scrape_chapters(
            profile, session, base_url, entry["chapter_id"], range(1, total + 1),
            cache_dir=cache_dir, delay=delay, pacer=pacer,
            max_consecutive_failures=CIRCUIT_BREAKER_THRESHOLD)
```
And its call to `_send_batch`:
```python
        if mail_config is not None:
            _send_batch(entry, profile, session, base_url, cache_dir, delay, mail_config,
                        library, library_path, threshold=email_threshold, pacer=pacer)
```

4. Add the `--pacing-file` flag to both the `check` and `mail` subparsers:
```python
    p_check.add_argument("--pacing-file", default=DEFAULT_PACING_PATH, metavar="FILE")
```
(placed alongside `p_check`'s other arguments)
```python
    p_mail.add_argument("--pacing-file", default=DEFAULT_PACING_PATH, metavar="FILE")
```
(placed alongside `p_mail`'s other arguments)

5. Update the module docstring's Usage block to mention the new flag on both `check` and `mail` lines (append `[--pacing-file FILE]` to each).

6. Construct one `Pacer` in `cmd_check()`, before the loop, and pass it into `_check_one()`:
```python
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

    pacer = Pacer.load(args.pacing_file, default_interval=args.delay)

    tally = {}
    for i, entry in enumerate(targets):
        label = f"{entry['site_key']}:{entry['chapter_id']}"
        try:
            status = _check_one(entry, cache_dir=args.cache_dir, delay=args.delay, dry_run=args.dry_run,
                                 library=library, library_path=args.library,
                                 mail_config=mail_config, email_threshold=args.email_threshold,
                                 pacer=pacer)
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
```

7. Construct one `Pacer` in `cmd_mail()` and pass it into `_send_batch()`:
```python
def cmd_mail(args):
    """...(existing docstring unchanged)..."""
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
    pacer = Pacer.load(args.pacing_file, default_interval=args.delay)

    status = _send_batch(entry, profile, session, base_url, args.cache_dir, args.delay,
                          mail_config, library, args.library, force=True, pacer=pacer)

    if status == "nothing-new":
        print(f"Nothing new to send since chapter {entry.get('last_emailed_chapter', 0)}.")
    elif status == "failed":
        sys.exit(1)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_update.py -v`
Expected: PASS (all existing + 2 new tests)

- [ ] **Step 6: Run the full suite to check for regressions**

Run: `python -m pytest`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add epub_scraper/update.py tests/test_update.py
git commit -m "Wire Pacer into update check/mail via a new --pacing-file flag"
```

---

### Task 7: Documentation and `.gitignore`

**Files:**
- Modify: `README.md`
- Modify: `.gitignore`

**Interfaces:** None (docs/config only, no code).

- [ ] **Step 1: Update `.gitignore`**

Add `pacing.json` next to `library.json` in the "Scheduled-update-check runtime state" block:

```
# Scheduled-update-check runtime state (mutates every run; not committed config)
library.json
pacing.json
.update_check.lock
logs/
```

- [ ] **Step 2: Update the one-shot scrape options table in `README.md`**

Change the `--delay` row and add a `--pacing-file` row:

```
| `--delay SECS` | `2.5` | Mean delay between chapter requests (actual gap is jittered around this) |
| `--pacing-file FILE` | `pacing.json` | Where to persist learned per-site request pacing |
```

- [ ] **Step 3: Add a short "Request pacing & resilience" section to `README.md`**, right after the "Tracked library" examples block and before "Auto-email to Kindle":

```markdown
### Request pacing & resilience

Every fetch goes through a few cheap defenses adapted from reviewing
[lncrawl/scraper](https://github.com/lncrawl/scraper) (Apache-2.0):

- **Jittered pacing.** `--delay` is the *mean* of a randomized gap between
  chapter requests, not a fixed sleep — perfectly regular request timing is
  itself a signal that whatever's making the requests isn't a person.
- **Learned backoff.** A `429` (or a server's own `Retry-After` header, when
  present) widens the delay for that site and persists it to `pacing.json`
  (`--pacing-file` to override the path) so the next run — including the
  next `check` under cron — starts already slowed down instead of relearning
  the same limit from scratch. The interval only ever widens, never shrinks
  back down automatically.
- **Challenge-page detection.** A `200` response whose body looks like a
  bot-challenge/interstitial page (rather than real chapter content) is
  treated as a failure and triggers the same backoff as a `429`, instead of
  silently being cached and shipped into the EPUB as garbage.
- **Honeypot-link filtering.** When scanning a novel's index page for its
  chapter ID, links that are `rel=nofollow`, `hidden`, `aria-hidden="true"`,
  or hidden via inline `display:none`/`visibility:hidden`/`opacity:0` are
  ignored — a defense against decoy links some sites plant specifically to
  catch scrapers walking every `<a href>` on the page.
```

- [ ] **Step 4: Update the "Project layout" section in `README.md`**

Add a line for `pacing.json` next to `library.json`:

```
library.json      tracked-novel state (gitignored)
pacing.json       learned per-site request pacing (gitignored)
```

- [ ] **Step 5: Commit**

```bash
git add README.md .gitignore
git commit -m "Document pacing/challenge-detection/safe-link behavior and pacing.json"
```

---

## Self-Review Notes

- **Spec coverage:** all four techniques from the approved design are covered — pacing/backoff (Task 1, wired in Task 4/5/6), challenge diagnosis (Task 2, wired in Task 4), safe-link filtering (Task 3). The dropped fifth technique (null-safe soup wrapper) is intentionally absent — already covered by existing `_select_text()`.
- **Backward compatibility:** verified the exact 7 existing `argparse.Namespace(...)` test call sites in `test_update.py` that need updating (Task 6, Step 1) so this doesn't surface as a surprise mid-implementation.
- **Type/signature consistency:** `Pacer.current_interval`/`gap`/`throttled` names are used identically across Tasks 1, 4, 5, 6. `scrape_chapters(..., pacer=None)` signature from Task 4 is what Tasks 5 and 6 call against.
