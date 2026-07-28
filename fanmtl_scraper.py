#!/usr/bin/env python3
"""
fanmtl_scraper.py — FanMTL → EPUB

Usage:
  python fanmtl_scraper.py <novel-index-url> [options]

Options:
  --start N       First chapter (default: 1)
  --end N         Last chapter inclusive (default: auto-detect)
  --delay N       Seconds between requests (default: 2.5)
  --output FILE   Output filename (default: auto from title)

Examples:
  python fanmtl_scraper.py https://www.fanmtl.com/novel/some-novel.html
  python fanmtl_scraper.py https://www.fanmtl.com/novel/some-novel.html --start 50 --end 100
  python fanmtl_scraper.py https://www.fanmtl.com/novel/some-novel.html --delay 3 --output mybook.epub

Requirements:
  pip install requests beautifulsoup4 ebooklib
"""

import argparse
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup
from ebooklib import epub

# ---------------------------------------------------------------------------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

SKIP_PHRASES = [
    "chevron_left", "chevron_right", "nights_stay",
    "Tap the screen", "Use arrow keys", "keyboard keys",
    "You'll Also Like", "Bookmark this page",
]
# ---------------------------------------------------------------------------


def slugify(title):
    """Turn a novel title into a safe filename."""
    title = re.sub(r"[^\w\s-]", "", title)
    title = re.sub(r"\s+", "_", title.strip())
    return title + ".epub"


def get_base_url(url):
    m = re.match(r"(https?://[^/]+)", url)
    return m.group(1) if m else ""


def fetch(url, session):
    r = session.get(url, timeout=15)
    r.raise_for_status()
    return r.text


def parse_index(html, url):
    """Return (novel_title, slug_or_id, total_chapters, base_url)."""
    soup = BeautifulSoup(html, "html.parser")
    base_url = get_base_url(url)

    # Title
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else "Unknown Novel"

    # Chapter count from "NNN Chapters" text
    total = None
    for text_node in soup.find_all(string=re.compile(r"\d+\s+[Cc]hapters?")):
        m = re.search(r"(\d+)\s+[Cc]hapters?", text_node)
        if m:
            total = int(m.group(1))
            break

    # Extract slug/id from chapter links — handles both slug and numeric ID URLs
    chapter_id = None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"/novel/([^/]+?)_(\d+)\.html", href)
        if m:
            chapter_id = m.group(1)
            # If total not found yet, track max chapter number
            if total is None:
                pass
            break

    # Fallback: derive chapter_id from index URL itself
    if chapter_id is None:
        m = re.search(r"/novel/([^/]+?)\.html", url)
        if m:
            chapter_id = m.group(1)

    # Fallback total: scan all chapter hrefs for highest number
    if total is None:
        nums = []
        for a in soup.find_all("a", href=True):
            m = re.search(r"_(\d+)\.html", a["href"])
            if m:
                nums.append(int(m.group(1)))
        if nums:
            total = max(nums)

    return title, chapter_id, total, base_url


def chapter_url(base_url, chapter_id, n):
    return f"{base_url}/novel/{chapter_id}_{n}.html"


def cache_path(cache_dir, chapter_id, n):
    return os.path.join(cache_dir, f"{chapter_id}_{n}.html")


def load_cached(cache_dir, chapter_id, n):
    p = cache_path(cache_dir, chapter_id, n)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    return None


def save_cache(cache_dir, chapter_id, n, html):
    os.makedirs(cache_dir, exist_ok=True)
    with open(cache_path(cache_dir, chapter_id, n), "w", encoding="utf-8") as f:
        f.write(html)


def parse_chapter(html, n):
    """Return (chapter_title, clean_paragraphs_html)."""
    soup = BeautifulSoup(html, "html.parser")

    # Title from h2
    h2 = soup.find("h2")
    title = h2.get_text(strip=True) if h2 else f"Chapter {n}"

    paragraphs = []
    for p in soup.find_all("p"):
        text = p.get_text(strip=True)

        # Skip empty / very short
        if not text or len(text) < 4:
            continue

        # Skip UI/nav phrases
        if any(phrase in text for phrase in SKIP_PHRASES):
            continue

        # Skip short nav-link paragraphs
        if p.find("a") and len(text) < 80:
            continue

        # Escape for XHTML
        text = (text
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))
        paragraphs.append(f"<p>{text}</p>")

    return title, "\n".join(paragraphs)


def build_epub(novel_title, chapter_id, chapters, output_file):
    book = epub.EpubBook()
    book.set_identifier(f"fanmtl-{chapter_id}")
    book.set_title(novel_title)
    book.set_language("en")

    css_content = b"""
body  { font-family: serif; line-height: 1.7; margin: 4% 6%; }
h1    { font-size: 1.3em; margin-bottom: 1.2em;
        border-bottom: 1px solid #ccc; padding-bottom: 0.3em; }
p     { margin: 0 0 0.8em 0; text-indent: 1.5em; }
"""
    css = epub.EpubItem(uid="main-css", file_name="style/main.css",
                        media_type="text/css", content=css_content)
    book.add_item(css)

    epub_chapters = []
    for i, (ch_title, body_html) in enumerate(chapters):
        fname = f"chap_{i+1:04d}.xhtml"
        c = epub.EpubHtml(title=ch_title, file_name=fname, lang="en")
        c.content = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
            '<meta charset="utf-8"/>'
            f'<title>{ch_title}</title>'
            '<link rel="stylesheet" type="text/css" href="../style/main.css"/>'
            f"</head><body><h1>{ch_title}</h1>\n{body_html}\n</body></html>"
        ).encode("utf-8")
        book.add_item(c)
        epub_chapters.append(c)

    book.toc = epub_chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + epub_chapters

    epub.write_epub(output_file, book)


def main():
    parser = argparse.ArgumentParser(
        description="Scrape a FanMTL novel and save it as an EPUB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Requirements:")[0].strip(),
    )
    parser.add_argument("url", help="Novel index page URL")
    parser.add_argument("--start", type=int, default=1, metavar="N",
                        help="First chapter to fetch (default: 1)")
    parser.add_argument("--end", type=int, default=None, metavar="N",
                        help="Last chapter to fetch inclusive (default: auto)")
    parser.add_argument("--delay", type=float, default=2.5, metavar="SECS",
                        help="Delay between requests in seconds (default: 2.5)")
    parser.add_argument("--output", default=None, metavar="FILE",
                        help="Output .epub filename (default: auto from title)")
    parser.add_argument("--cache-dir", default=".cache", metavar="DIR",
                        help="Directory to cache raw chapter HTML (default: .cache)")
    parser.add_argument("--no-cache", action="store_true",
                        help="Ignore and overwrite any existing cache")
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update(HEADERS)
    session.headers["Referer"] = get_base_url(args.url)

    # -- Index ----------------------------------------------------------------
    print(f"Fetching index: {args.url}")
    try:
        index_html = fetch(args.url, session)
    except Exception as e:
        print(f"Error fetching index: {e}")
        sys.exit(1)

    novel_title, chapter_id, total, base_url = parse_index(index_html, args.url)

    if not chapter_id:
        print("Could not determine chapter ID from index page. Exiting.")
        sys.exit(1)

    print(f"Title  : {novel_title}")
    print(f"ID     : {chapter_id}")
    print(f"Total  : {total if total else 'unknown'} chapters")

    end = args.end if args.end is not None else total
    if end is None:
        print("Could not auto-detect chapter count. Use --end N to set it manually.")
        sys.exit(1)

    output_file = args.output or slugify(novel_title)
    chapter_range = range(args.start, end + 1)

    print(f"Range  : {args.start}–{end}  ({len(chapter_range)} chapters)")
    print(f"Delay  : {args.delay}s")
    print(f"Cache  : {args.cache_dir}{'  (disabled)' if args.no_cache else ''}")
    print(f"Output : {output_file}")
    print("-" * 56)

    # -- Fetch chapters -------------------------------------------------------
    chapters = []
    skipped = 0

    for i, n in enumerate(chapter_range):
        url = chapter_url(base_url, chapter_id, n)
        try:
            # Try cache first
            cached = None if args.no_cache else load_cached(args.cache_dir, chapter_id, n)
            if cached:
                html = cached
                src = "cache"
            else:
                html = fetch(url, session)
                save_cache(args.cache_dir, chapter_id, n, html)
                src = "web"

            ch_title, body = parse_chapter(html, n)
            chapters.append((ch_title, body))
            pct = int((i + 1) / len(chapter_range) * 100)
            flag = "·" if src == "cache" else "↓"
            print(f"  [{pct:3d}%] {flag} Ch {n:>4d}  {ch_title[:50]}")
        except requests.HTTPError as e:
            print(f"  [SKIP] Ch {n:>4d}  HTTP {e.response.status_code}")
            skipped += 1
        except Exception as e:
            print(f"  [SKIP] Ch {n:>4d}  {e}")
            skipped += 1

        if i < len(chapter_range) - 1 and src == "web":
            time.sleep(args.delay)

    print("-" * 56)

    if not chapters:
        print("No chapters fetched. Exiting.")
        sys.exit(1)

    # -- Build EPUB -----------------------------------------------------------
    print(f"Building EPUB  ({len(chapters)} chapters, {skipped} skipped)…")
    try:
        build_epub(novel_title, chapter_id, chapters, output_file)
        print(f"✓  Saved: {output_file}")
    except Exception as e:
        print(f"Error building EPUB: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
