import re

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Machine tokens (CSS class names, vendor script paths). These never occur in
# prose, so matching them anywhere in the body is safe.
_CHALLENGE_TOKENS = (
    "cf-browser-verification",
    "ddos-guard",
    "captcha-delivery",
)

# Human-readable phrases. These are ordinary English and DO occur in real
# translated-webnovel prose ("Just a moment later, he turned around."), so they
# are only trusted inside <title> -- a false positive here is expensive and
# self-amplifying: the chapter is never cached, the site's pacing interval
# doubles and persists, and five such checks auto-disable the novel.
_CHALLENGE_TITLES = (
    "checking your browser",
    "just a moment",
    "attention required! | cloudflare",
)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S)


class ChallengeDetected(Exception):
    """Response body looks like a bot-challenge/interstitial page, not real content."""


def _looks_like_challenge(html):
    lowered = html.lower()
    if any(token in lowered for token in _CHALLENGE_TOKENS):
        return True
    m = _TITLE_RE.search(lowered)
    title = m.group(1) if m else ""
    return any(phrase in title for phrase in _CHALLENGE_TITLES)


def note_throttle(pacer, site_key, exc):
    """Widen `site_key`'s learned interval if `exc` is the site pushing back --
    an HTTP 429 (honouring its Retry-After header) or a challenge/interstitial
    page. No-op for a None pacer or any other exception; returns whether it
    throttled.

    Shared by every fetch call site -- chapter fetches inside scrape_chapters()
    and the index-page fetches in cli/update -- so a block on the index page
    (the first and most exposed request of a run) feeds the same backoff as a
    block partway through a chapter loop.
    """
    if pacer is None:
        return False
    if isinstance(exc, ChallengeDetected):
        pacer.throttled(site_key)
        return True
    if isinstance(exc, requests.HTTPError):
        response = getattr(exc, "response", None)
        if response is not None and response.status_code == 429:
            pacer.throttled(site_key, retry_after=response.headers.get("Retry-After"))
            return True
    return False


def fetch(url, session):
    r = session.get(url, timeout=15)
    r.raise_for_status()
    if _looks_like_challenge(r.text):
        raise ChallengeDetected(f"challenge page at {url}")
    return r.text
