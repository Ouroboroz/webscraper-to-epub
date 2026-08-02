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
