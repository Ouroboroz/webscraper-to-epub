from urllib.parse import urlparse

from . import fanmtl

PROFILES = {
    "fanmtl": fanmtl.PROFILE,
}


def resolve_profile(url, site_key=None):
    if site_key:
        if site_key not in PROFILES:
            raise SystemExit(f"Unknown site '{site_key}'. Available: {', '.join(PROFILES)}")
        return PROFILES[site_key]

    netloc = urlparse(url).netloc.lower()
    for profile in PROFILES.values():
        if any(netloc == d or netloc.endswith("." + d) for d in profile.domains):
            return profile

    raise SystemExit(
        f"Could not auto-detect site for URL '{url}'. Use --site to specify one of: {', '.join(PROFILES)}"
    )
