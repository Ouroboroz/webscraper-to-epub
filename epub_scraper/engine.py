import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .profile import ChapterResult, IndexResult, SearchResult
from .util import get_base_url


def _select_text(soup, selector):
    tag = soup.select_one(selector)
    return tag.get_text(strip=True) if tag else None


def _escape_xhtml(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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


def parse_index(profile, html, url):
    """Return an IndexResult for the novel's index page."""
    if profile.parse_index_fn is not None:
        return profile.parse_index_fn(html, url)

    soup = BeautifulSoup(html, "html.parser")
    base_url = get_base_url(url)

    title = _select_text(soup, profile.index_title_selector) or "Unknown Novel"

    # Chapter count from e.g. "NNN Chapters" text
    total = None
    for text_node in soup.find_all(string=re.compile(profile.chapter_count_pattern)):
        m = re.search(profile.chapter_count_pattern, text_node)
        if m:
            total = int(m.group(1))
            break

    # Extract slug/id from chapter links
    chapter_id = None
    for a in _safe_anchors(soup):
        m = re.search(profile.chapter_link_pattern, a["href"])
        if m:
            chapter_id = m.group(1)
            break

    # Fallback: derive chapter_id from the index URL itself
    if chapter_id is None:
        m = re.search(profile.index_url_id_pattern, url)
        if m:
            chapter_id = m.group(1)

    # Fallback total: scan all chapter hrefs for the highest number
    if total is None:
        nums = []
        for a in _safe_anchors(soup):
            m = re.search(profile.chapter_number_fallback_pattern, a["href"])
            if m:
                nums.append(int(m.group(1)))
        if nums:
            total = max(nums)

    return IndexResult(title, chapter_id, total, base_url)


def chapter_url(profile, base_url, chapter_id, n):
    return profile.chapter_url_template.format(base_url=base_url, chapter_id=chapter_id, n=n)


def parse_chapter(profile, html, n):
    """Return a ChapterResult for a single chapter page."""
    if profile.parse_chapter_fn is not None:
        return profile.parse_chapter_fn(html, n)

    soup = BeautifulSoup(html, "html.parser")

    title = _select_text(soup, profile.chapter_title_selector) or profile.chapter_title_fallback.format(n=n)

    paragraphs = []
    for p in soup.select(profile.paragraph_selector):
        text = p.get_text(strip=True)

        if not text or len(text) < profile.min_paragraph_length:
            continue

        if any(phrase in text for phrase in profile.skip_phrases):
            continue

        if p.find("a") and len(text) < profile.link_paragraph_max_length:
            continue

        text = _escape_xhtml(text)
        paragraphs.append(f"<p>{text}</p>")

    return ChapterResult(title, "\n".join(paragraphs))


def search_novels(profile, session, query):
    """Query a site's search feature for novels matching `query`. Returns a
    list[SearchResult]. Raises NotImplementedError if the site profile hasn't
    declared search support (neither search_fn nor search_url is set)."""
    if profile.search_fn is not None:
        return profile.search_fn(session, query)

    if not profile.search_url:
        raise NotImplementedError(f"Site '{profile.site_key}' does not support search.")

    payload = {**profile.search_extra_params, profile.search_query_param: query}
    if profile.search_method == "post":
        r = session.post(profile.search_url, data=payload, timeout=15)
    else:
        r = session.get(profile.search_url, params=payload, timeout=15)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    for item in soup.select(profile.search_result_selector):
        link = item.select_one(profile.search_link_selector)
        if not link or not link.get("href"):
            continue
        title = link.get("title") or link.get_text(strip=True)
        url = urljoin(profile.search_base_url or "", link["href"])

        chapters = None
        if profile.search_chapter_count_pattern:
            m = re.search(profile.search_chapter_count_pattern, item.get_text(" "))
            if m:
                chapters = int(m.group(1))

        results.append(SearchResult(title, url, chapters))
    return results
