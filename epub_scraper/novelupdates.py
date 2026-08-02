"""
epub_scraper.novelupdates — Novel Updates (NU) enrichment: search by title,
fetch a series page's associated names/tags/author/status.

NU sits behind a live Cloudflare Managed Challenge. Series-page selectors
were originally grounded only in two independent open-source NU scrapers'
source (jckli/novelupdates.py, GetRektByMe/Raitonoberu) since NU couldn't be
reached from the environment that first wrote this module -- confirmed
working (2026-08-02, real-hardware testing) and several were corrected
against an actual captured page since the reference scrapers turned out to
be wrong or outdated on some fields (translation_status, release_frequency,
rating/votes -- see fetch_series()'s docstring). search()'s endpoint is
confirmed broken and still needs a fix (see its docstring) -- the site's
real search is JS-driven, not the simple GET the reference scraper used.

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
    """Query NU's own search. Returns up to `limit` hits; each still needs
    fetch_series() for associated names.

    **CONFIRMED BROKEN as of 2026-08-02, not yet fixed**: the reference
    scraper's approach (GET / with params s=<query>, post_type=seriesplan)
    now 404s -- NU's real search form uses post_type=**seriesplans** (plural).
    Fixing just that typo isn't enough either: a direct GET to that corrected
    URL gets a *different* Cloudflare block ("Attention Required!", a WAF
    rule, not the Managed Challenge this module solves for) even from a real
    solved browser session -- confirmed by testing both a curl_cffi replay
    AND direct browser navigation to that exact URL. The real search is
    JS-driven (an autocomplete widget, `delayedSearch()`/`keyenter_search()`
    handlers on `input[name="s"]`) -- typing into that input via CDP got 90
    real `/series/` links into the DOM, but not ones matching the typed
    query, so simulating the interaction isn't right either yet. This needs
    its own follow-up (most likely: capture the actual AJAX request the
    autocomplete fires via CDP network logging, rather than another guess).
    Left in place below since the result-parsing logic itself (once pointed
    at correct result HTML) is still probably fine -- untested against real
    result markup either way. `raise_for_status()` means calling this today
    fails loudly (404/403) rather than silently returning wrong data."""
    url = f"{BASE_URL}/"
    r = session.get(url, params={"s": query, "post_type": "seriesplan"}, timeout=20)
    r.raise_for_status()
    if looks_like_challenge_page(r.text):
        raise ChallengeExpired(f"Challenge page returned for search {query!r}")

    soup = BeautifulSoup(r.text, "html.parser")
    hits = []
    for link in soup.select("a.w-blog-entry-link"):
        href = link.get("href")
        if not href:
            continue
        title = link.get("title") or link.get_text(strip=True)
        hits.append(NUSearchHit(title=title, url=urljoin(BASE_URL, href)))
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


def fetch_series(session, url):
    """Fetch and parse one NU series page.

    Confirmed against a real captured page (2026-08-02): title,
    associated_names, genres, tags, author, translation_status,
    release_frequency, rating, votes. Still unverified: translation_groups
    (the one real page checked -- Reverend Insanity -- is an officially
    licensed release with no fan-translation groups to test against;
    selector kept as the reference scraper had it, low confidence).

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

    return NUSeriesMetadata(
        url=url, title=title, associated_names=associated_names, genres=genres, tags=tags,
        author=author, translation_status=translation_status,
        translation_groups=translation_groups,
        release_frequency=_release_frequency(soup),
        rating=rating,
        votes=votes,
    )


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
