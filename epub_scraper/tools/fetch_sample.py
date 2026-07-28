"""Fetch a single page with the project's standard headers and print it to stdout.

For site-onboarding use (inspecting a new site's HTML by hand/eye) — not part of
the scrape pipeline itself, which fetches through cli.py's own loop.

Usage:
  python -m epub_scraper.tools.fetch_sample <url> [--out FILE]
"""
import argparse
import sys

import requests

from ..fetcher import HEADERS, fetch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--out", default=None, metavar="FILE",
                        help="Write HTML to this file instead of stdout")
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update(HEADERS)
    html = fetch(args.url, session)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Saved {len(html)} bytes to {args.out}", file=sys.stderr)
    else:
        print(html)


if __name__ == "__main__":
    main()
