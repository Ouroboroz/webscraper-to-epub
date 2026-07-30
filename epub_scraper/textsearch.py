"""Full-text search across chapters inside already-downloaded .epub files."""

import re
from collections import namedtuple

from bs4 import BeautifulSoup
from ebooklib import epub, ITEM_DOCUMENT

Hit = namedtuple("Hit", "chapter_title snippet")


def search_epub_text(path, query, ignore_case=True, context=60):
    """Search every chapter document inside the epub at `path` for `query`.
    Returns a list[Hit], one per match, in chapter order."""
    book = epub.read_epub(path, options={"ignore_ncx": True})
    pattern = re.compile(re.escape(query), re.IGNORECASE if ignore_case else 0)

    hits = []
    for item in book.get_items_of_type(ITEM_DOCUMENT):
        # get_items_of_type(ITEM_DOCUMENT) also yields ebooklib's own nav.xhtml
        # (the TOC document) alongside our chap_NNNN.xhtml chapters -- skip it,
        # or a query matching any chapter title spuriously "matches" the TOC too.
        if not item.get_name().startswith("chap_"):
            continue

        soup = BeautifulSoup(item.get_content(), "html.parser")
        title_tag = soup.find("h1") or soup.find("title")
        chapter_title = title_tag.get_text(strip=True) if title_tag else item.get_name()

        text = soup.get_text(" ", strip=True)
        for m in pattern.finditer(text):
            start = max(0, m.start() - context)
            end = min(len(text), m.end() + context)
            hits.append(Hit(chapter_title, text[start:end]))

    return hits
