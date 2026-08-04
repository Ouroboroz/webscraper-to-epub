"""
epub_scraper.novelupdates — Novel Updates (NU) enrichment: crawl NU's own
bulk catalog listing, fetch a series page's associated names/tags/author/
status/synopsis, and (still available, but no longer used by dataspine.py's
`enrich` -- see below) search by title.

**2026-08-03 finding that changed how this module is used**: NU's entire
catalog turned out to be only ~2,475 series total (see list_series() below)
-- tiny next to the FanMTL candidate pool (up to 105,049) that dataspine.py's
old `enrich` used to live-search against it one candidate at a time, meaning
>95% of those searches were guaranteed to come back empty against a site 40x
smaller than what was being searched. Fixed by crawling NU's own catalog
into `nu_novels` once (`nu-crawl` + `nu-metadata` in dataspine.py) and
matching locally instead (`enrich`, now pure computation, no network). search()
is kept working and tested since it's still a reasonable ad-hoc lookup (see
`novelupdates check`), just no longer on the hot path of the main pipeline.

NU sits behind a live Cloudflare Managed Challenge. Series-page selectors
were originally grounded only in two independent open-source NU scrapers'
source (jckli/novelupdates.py, GetRektByMe/Raitonoberu) since NU couldn't be
reached from the environment that first wrote this module -- confirmed
working (2026-08-02, real-hardware testing) and several were corrected
against an actual captured page since the reference scrapers turned out to
be wrong or outdated on some fields (translation_status, release_frequency,
rating/votes -- see fetch_series()'s docstring). search()'s original
reference-scraper endpoint was confirmed dead and has since been replaced
with NU's real live-search AJAX endpoint (found by reading the site's own
`ajax_search_post.js` and confirmed against real queries -- see search()'s
docstring).

`seleniumbase` and `curl_cffi` are NOT base dependencies (see
requirements-novelupdates.txt) -- most users of this package (FanMTL scraping
only) don't need them. solve_challenge_session() imports both locally so the
rest of this module stays importable without them.

**2026-08-02 finding**: a solved challenge's cookies replayed through a plain
`requests.Session` still got a live 403 (confirmed by manual testing on real
hardware -- the browser-based solve itself worked fine). Cloudflare Managed
Challenges evidently also check the TLS handshake fingerprint, not just the
`cf_clearance` cookie -- `requests`/urllib3's TLS stack doesn't look like real
Chrome even with the right cookie and User-Agent header. Fixed by replaying
through `curl_cffi` (`impersonate="chrome124"`, matching TLS fingerprint)
instead of plain `requests` -- both expose the same `.get()`/`.post()`/
`.cookies.set()`/response `.text`/`.raise_for_status()` shape, so search()/
fetch_series() below don't need to know which one they were handed.
"""

import argparse
import re
import time
from typing import NamedTuple, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

BASE_URL = "https://www.novelupdates.com"


class NUSearchHit(NamedTuple):
    title: str
    url: str


class NUSeriesMetadata(NamedTuple):
    url: str
    title: Optional[str]
    associated_names: list
    genres: list
    tags: list
    author: Optional[str]
    translation_status: Optional[str]
    translation_groups: list
    release_frequency: Optional[str]
    rating: Optional[str]
    votes: Optional[str]
    # Added after the initial fields shipped -- default keeps every existing
    # keyword-args construction site (tests included) working unchanged.
    # Not wired into the FanMTL `novels` row (see dataspine.py's cmd_enrich
    # docstring) -- Stage 8 reads it straight off nu_novels.synopsis instead.
    synopsis: Optional[str] = None


class ChallengeExpired(Exception):
    """Raised when a request that should have used a solved session instead
    got the Cloudflare interstitial back -- the session's cookies expired (or
    never solved), caller should re-solve and retry."""


def _challenge_page_reason(html):
    """Which marker (if any) makes `html` look like a Cloudflare interstitial
    -- exposed separately from looks_like_challenge_page so a failed solve
    can report *why* it thinks it's still blocked, instead of just that it is."""
    if "<title>Just a moment" in html:
        return "title is 'Just a moment...'"
    if "cf_chl_opt" in html:
        return "cf_chl_opt marker present"
    return None


def looks_like_challenge_page(html):
    """True if `html` is a Cloudflare interstitial rather than real content --
    checked before parsing so a stale/expired session fails loudly instead of
    silently producing an empty/garbage NUSeriesMetadata."""
    return _challenge_page_reason(html) is not None


def _dump_debug(sb):
    """Best-effort page-source + screenshot dump when a solve attempt fails,
    so the next real-hardware run gives a concrete artifact to diagnose from
    instead of another blind guess. Written to the current directory --
    gitignored, not meant to be committed."""
    try:
        with open("nu_challenge_debug.html", "w", encoding="utf-8") as f:
            f.write(sb.cdp.get_page_source())
    except Exception as e:
        print(f"  (failed to write nu_challenge_debug.html: {e})")
    try:
        sb.cdp.save_screenshot("nu_challenge_debug.png")
    except Exception as e:
        print(f"  (failed to write nu_challenge_debug.png: {e})")
    print("  wrote nu_challenge_debug.html / nu_challenge_debug.png for inspection")


def solve_challenge_session(url=BASE_URL, max_solve_attempts=4, poll_delay=2.0):
    """Solve NU's Cloudflare challenge once with a real (SeleniumBase CDP Mode)
    browser, then hand back a curl_cffi session (Chrome TLS fingerprint --
    see module docstring for why plain requests doesn't work here) carrying
    its cookies. Cloudflare ties the solved challenge to the session/IP, so
    this is meant to be called once and reused for many search()/
    fetch_series() calls, not per-lookup.

    **2026-08-02 finding, superseding earlier attempts in this function's
    history**: SeleniumBase's PyAutoGUI-based click (`uc_gui_click_captcha` /
    `uc_gui_handle_captcha`) never actually clicks the Turnstile checkbox in
    this environment -- confirmed by direct testing that PyAutoGUI's setup
    (`get_configured_pyautogui`) crashes on every call with a `TypeError:
    'type' object does not support item assignment`, a known unresolved
    python-xlib 0.33 bug in its RandR extension init (asweigart/pyautogui#202)
    that's specific to this Xvfb/X-server's RandR opcode -- silently swallowed
    somewhere upstream, which is why it just never clicked with no visible
    error. **CDP Mode sidesteps this entirely**: `activate_cdp_mode()` +
    `click_captcha()` simulate the click through Chrome's DevTools Protocol
    directly, no PyAutoGUI/Xlib involved -- confirmed working end-to-end by
    direct testing (solves in ~10s, real series-page HTML afterward)."""
    from curl_cffi import requests as cf_requests  # local import -- see module docstring
    from seleniumbase import SB  # local import -- see module docstring

    # Deliberately not raising inside the `with SB(...)` block: confirmed by
    # real-hardware testing that SB(test=True)'s pytest-integration machinery
    # catches/reports an exception raised inside the block (prints a
    # "failed" line) but does NOT re-raise it when not actually running under
    # pytest -- execution then continues past the `with` block as if nothing
    # happened. So: only ever set local flags/values inside the block, decide
    # whether to raise after it has closed.
    solved = False
    cookies = None
    user_agent = None

    last_html = None
    with SB(uc=True, test=True) as sb:
        sb.activate_cdp_mode(url)

        for _ in range(max_solve_attempts):
            last_html = sb.cdp.get_page_source()
            if not looks_like_challenge_page(last_html):
                solved = True
                break
            sb.click_captcha()
            time.sleep(poll_delay)

        if solved:
            cookies = sb.cdp.get_all_cookies()
            user_agent = sb.cdp.evaluate("navigator.userAgent")
        else:
            reason = _challenge_page_reason(last_html) if last_html else "no page loaded"
            print(f"  still blocked after {max_solve_attempts} checks ({reason})")
            _dump_debug(sb)

    if not solved:
        raise ChallengeExpired(
            f"Still looked like a challenge page after {max_solve_attempts} checks -- "
            "see nu_challenge_debug.html/.png")

    session = cf_requests.Session(impersonate="chrome124")
    session.headers["User-Agent"] = user_agent
    for cookie in cookies:
        # CDP Mode's get_all_cookies() returns Cookie objects (.name/.value/
        # .domain attributes), not the dicts driver.get_cookies() used to.
        session.cookies.set(cookie.name, cookie.value, domain=cookie.domain)
    return session


def search(session, query, limit=10):
    """Query Novel Updates' real live-search widget and return up to `limit`
    deduped hits; each still needs fetch_series() for associated names.

    **Fixed 2026-08-02**, replacing the reference scraper's dead endpoint
    (GET / with s=<query>&post_type=seriesplan(s) -- 404/403, see git history
    for that investigation). The real endpoint was found by fetching NU's own
    `cdn.novelupdates.com/js/ajax_search_post.js` (loaded on every page, no
    challenge in the way) and reading its `showSearch()` function -- the
    handler behind the homepage search box's `delayedSearch()` -- directly,
    then confirmed live against real queries:

        POST {BASE_URL}/wp-admin/admin-ajax.php
        data={"action": "nd_ajaxsearchmain", "strType": "desktop",
              "strOne": <query>, "strSearchType": "series"}

    Response is an HTML fragment (`<ul><li class="search_li_results">
    <a class="a_search" href="...">...</a></li>...</ul>`) with a trailing
    literal "0" (WordPress admin-ajax's convention for an action that
    doesn't call wp_die() -- the site's own JS strips it with `.slice(0,
    -1)` before use; harmless here since we only select() specific tags).
    No results looks like `<ul></ul><span>No Results found.</span>0`.

    The same series URL can appear in multiple <li>s (once per field that
    matched -- title vs. an alternate name), each with different link text
    (sometimes just the matched fragment, e.g. "omniscient reader" rather
    than the full title) -- confirmed against a real multi-hit response.
    Deduped by URL below, keeping the longest (most complete) title seen
    per URL rather than just the first."""
    r = session.post(f"{BASE_URL}/wp-admin/admin-ajax.php", data={
        "action": "nd_ajaxsearchmain",
        "strType": "desktop",
        "strOne": query,
        "strSearchType": "series",
    }, timeout=20)
    r.raise_for_status()
    if looks_like_challenge_page(r.text):
        raise ChallengeExpired(f"Challenge page returned for search {query!r}")

    soup = BeautifulSoup(r.text, "html.parser")
    order = []
    best_title = {}
    for link in soup.select("li.search_li_results a.a_search"):
        href = link.get("href")
        title = link.get_text(strip=True)
        if not href or not title:
            continue
        href = urljoin(BASE_URL, href)
        if href not in best_title:
            order.append(href)
        if href not in best_title or len(title) > len(best_title[href]):
            best_title[href] = title
    hits = [NUSearchHit(title=best_title[href], url=href) for href in order]
    return hits[:limit]


def _names_from_div(div):
    """#editassociated-style divs are a handful of names separated by <br>,
    not separate tags -- split on newlines from get_text() rather than
    expecting one name per child element."""
    if div is None:
        return []
    return [line for line in div.get_text("\n", strip=True).split("\n") if line]


def _links_text(soup, selector):
    return [a.get_text(strip=True) for a in soup.select(selector) if a.get_text(strip=True)]


def _release_frequency(soup):
    """Release Frequency's value is unusual among the seriesother sidebar
    fields: a bare text node directly after its <h5>, not wrapped in any
    element -- find_next_sibling() (tag-only) skips right past it to the
    *next* <h5> instead, silently grabbing the wrong field. Confirmed against
    a real captured page (2026-08-02) -- <h5 ...>Release Frequency</h5>Every
    13.1 Day(s)."""
    for h5 in soup.select("h5.seriesother"):
        if "release frequency" in h5.get_text(strip=True).lower():
            sibling = h5.next_sibling
            if sibling and str(sibling).strip():
                return str(sibling).strip()
    return None


_RATING_VOTES_PATTERN = re.compile(r"\(([\d.]+)\s*/\s*5\.0,\s*([\d,]+)\s*votes?\)", re.I)


def _rating_and_votes(soup):
    """Rating + vote count live together inside the Rating <h5>'s own nested
    span.uvotes (e.g. "Rating<span class="uvotes">(4.3 / 5.0, 1700 votes)
    </span>"), not in a sibling table -- confirmed against a real captured
    page (2026-08-02); table#myrates is the per-star vote breakdown, a
    different (and uglier to parse) thing entirely."""
    for h5 in soup.select("h5.seriesother"):
        if h5.get_text(strip=True).lower().startswith("rating"):
            uvotes = h5.select_one("span.uvotes")
            if uvotes is not None:
                m = _RATING_VOTES_PATTERN.search(uvotes.get_text(strip=True))
                if m:
                    return m.group(1), m.group(2)
    return None, None


def _synopsis_from_div(div):
    """Mirrors epub_scraper/sites/fanmtl.py's parse_fanmtl_metadata() synopsis
    extraction, for consistency between the two sites' scrapers: join every
    non-empty <p>'s text with a blank line between paragraphs, falling back
    to the div's own text if it has no <p> children at all."""
    if div is None:
        return None
    paragraphs = [p.get_text(strip=True) for p in div.select("p")]
    paragraphs = [p for p in paragraphs if p]
    if paragraphs:
        return "\n\n".join(paragraphs)
    text = div.get_text(strip=True)
    return text or None


def fetch_series(session, url):
    """Fetch and parse one NU series page.

    Confirmed against a real captured page (2026-08-02): title,
    associated_names, genres, tags, author, translation_status,
    release_frequency, rating, votes. Still unverified: translation_groups
    (the one real page checked -- Reverend Insanity -- is an officially
    licensed release with no fan-translation groups to test against;
    selector kept as the reference scraper had it, low confidence).

    synopsis (added 2026-08-03, confirmed live against a real captured page
    -- Reverend Insanity again): div#editdescription holds one <p> per
    synopsis paragraph -- same shape as FanMTL's own synopsis div, see
    _synopsis_from_div() above. Not extracted before this -- a real gap,
    since Stage 8 (tag/synopsis classification) will want it.

    translation_status reads div#editstatus ("Status in Country of Origin"
    -- e.g. "2334 Chapters (Cancelled/Banned)"), not div#showtranslated
    (which is actually a "Completely Translated" Yes/No flag, a different,
    less useful field both reference scrapers happened to expose under a
    similarly-named selector)."""
    r = session.get(url, timeout=20)
    r.raise_for_status()
    if looks_like_challenge_page(r.text):
        raise ChallengeExpired(f"Challenge page returned for {url}")

    soup = BeautifulSoup(r.text, "html.parser")

    title = None
    title_tag = soup.select_one(".seriestitlenu")
    if title_tag is not None:
        title = title_tag.get_text(strip=True) or None

    associated_names = _names_from_div(soup.select_one("div#editassociated"))
    genres = _links_text(soup, "div#seriesgenre a")
    tags = _links_text(soup, "div#showtags a")

    author = None
    authors = _links_text(soup, "div#showauthors a") or _links_text(soup, "a#authtag")
    if authors:
        author = ", ".join(authors)

    status_tag = soup.select_one("div#editstatus")
    translation_status = status_tag.get_text(strip=True) if status_tag else None

    translation_groups = [
        span.get_text(strip=True)
        for span in soup.select('ol.sp_grouptable li span[style="padding-left:20px;"]')
        if span.get_text(strip=True)
    ]

    rating, votes = _rating_and_votes(soup)
    synopsis = _synopsis_from_div(soup.select_one("div#editdescription"))

    return NUSeriesMetadata(
        url=url, title=title, associated_names=associated_names, genres=genres, tags=tags,
        author=author, translation_status=translation_status,
        translation_groups=translation_groups,
        release_frequency=_release_frequency(soup),
        rating=rating,
        votes=votes,
        synopsis=synopsis,
    )


def list_series(session, page):
    """Fetch one page of Novel Updates' own bulk catalog listing -- ALL of
    NU's series, independent of any search query -- for the local `nu-crawl`
    pipeline (see dataspine.py's cmd_nu_crawl docstring for why this exists:
    NU's entire catalog turned out to be only ~2,475 series, far smaller than
    the FanMTL candidate pool that used to be searched against it one at a
    time). Returns (hits, has_next).

    URL: {BASE_URL}/novelslisting/?st=1&pg=<page> -- page starts at 1, not 0.
    Confirmed live (2026-08-03): 25 entries/page, each at
    `div.search_main_box_nu div.search_title a` (title text + series URL --
    nothing else needed at listing time; full detail is fetch_series()'s job,
    called later per-series by `nu-metadata`).

    Real end-of-list signal (NOT a 404): also confirmed live (2026-08-03)
    that fetching past the catalog's real last page silently clamps/repeats
    that last page's content rather than erroring -- pages 100 through 10000
    all returned page 99's identical entries on the day this was checked (the
    real last-page number grows over time as NU adds series -- never
    hardcode it). The only reliable stop signal is the pagination widget
    itself: `div.digg_pagination` contains `<a class="next_page" ...
    rel="next">` only when a genuine next page exists (present on page 99,
    absent on the clamped page 100 that same day). So: has_next=False means
    stop, regardless of how many hits this page's soup contained."""
    r = session.get(f"{BASE_URL}/novelslisting/", params={"st": 1, "pg": page}, timeout=20)
    r.raise_for_status()
    if looks_like_challenge_page(r.text):
        raise ChallengeExpired(f"Challenge page returned for novelslisting page {page}")

    soup = BeautifulSoup(r.text, "html.parser")
    hits = []
    for link in soup.select("div.search_main_box_nu div.search_title a"):
        href = link.get("href")
        title = link.get_text(strip=True)
        if not href or not title:
            continue
        hits.append(NUSearchHit(title=title, url=urljoin(BASE_URL, href)))

    has_next = soup.select_one("div.digg_pagination a.next_page") is not None
    return hits, has_next


def find_nu_candidates(session, query, limit=10):
    """search() then fetch_series() per hit -- the richer NUSeriesMetadata
    each candidate needs for entity resolution (associated_names) as well as
    for storage if it turns out to be the match, without a second fetch."""
    return [fetch_series(session, hit.url) for hit in search(session, query, limit=limit)]


def _print_series(metadata):
    print(f"Title             : {metadata.title}")
    print(f"URL               : {metadata.url}")
    print(f"Associated names  : {metadata.associated_names}")
    print(f"Genres            : {metadata.genres}")
    print(f"Tags              : {metadata.tags}")
    print(f"Author            : {metadata.author}")
    print(f"Translation status: {metadata.translation_status}")
    print(f"Translation groups: {metadata.translation_groups}")
    print(f"Release frequency : {metadata.release_frequency}")
    print(f"Rating / votes    : {metadata.rating} / {metadata.votes}")
    print(f"Synopsis          : {(metadata.synopsis or '')[:200]!r}")


def cmd_check(args):
    """Manual verification entry point -- solves the Cloudflare challenge and
    fetches/parses one series page in isolation, so a selector mismatch or a
    failed challenge-solve shows up immediately rather than buried inside the
    full `dataspine.py enrich` pipeline."""
    print("Solving Novel Updates' Cloudflare challenge "
          "(needs a real Chrome + Xvfb on Linux -- see requirements-novelupdates.txt)...")
    session = solve_challenge_session()
    print("Challenge solved. Fetching a series page...")
    metadata = fetch_series(session, args.url)
    _print_series(metadata)


def main():
    parser = argparse.ArgumentParser(
        prog="python -m epub_scraper.novelupdates",
        description="Manual verification for the Novel Updates enrichment scraper.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="Solve the challenge and fetch/parse one series page")
    p_check.add_argument("--url", default=f"{BASE_URL}/series/reverend-insanity/", metavar="URL",
                          help="Series page to fetch (default: a known real NU series)")
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
