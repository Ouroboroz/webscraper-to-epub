import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..profile import CatalogEntry, MetadataResult, SiteProfile

BASE_URL = "https://www.fanmtl.com"

# Catalog browse listing: paginated, ~30 novels/page. Page count grows over
# time, so callers should paginate until a page returns zero entries rather
# than trusting a hardcoded last-page number.
CATALOG_URL_TEMPLATE = BASE_URL + "/list/all/all-newstime-{page}.html"

_CATALOG_ID_PATTERN = re.compile(r"/novel/([^/]+?)\.html")
_CATALOG_CHAPTERS_PATTERN = re.compile(r"(\d+)\s+Chapters?", re.I)


def _text(tag):
    return tag.get_text(strip=True) if tag else None


def parse_fanmtl_metadata(html):
    """Parse a FanMTL novel index page's full metadata (synopsis, genres,
    author, alternate title, status, rating) -- beyond what engine.parse_index()
    captures. Chapters/status live inside div.header-stats as
    <strong>value</strong><small>label</small> pairs, not simple selectors."""
    soup = BeautifulSoup(html, "html.parser")

    alt_title = _text(soup.select_one("h2.alternative-title"))
    author = _text(soup.select_one('div.author span[itemprop="author"]'))
    rating = _text(soup.select_one("div.rating"))

    status = None
    for span in soup.select("div.header-stats span"):
        label = _text(span.select_one("small"))
        if label and label.lower() == "status":
            status = _text(span.select_one("strong"))

    genres = [a.get_text(strip=True) for a in soup.select("div.categories ul li a")]

    synopsis = None
    content = soup.select_one("div.summary div.content")
    if content is not None:
        paragraphs = [p.get_text(strip=True) for p in content.select("p")]
        paragraphs = [p for p in paragraphs if p]
        synopsis = "\n\n".join(paragraphs) if paragraphs else (_text(content) or None)

    return MetadataResult(
        synopsis=synopsis,
        genres=genres,
        author=author,
        alt_title=alt_title,
        status=status,
        rating=rating,
    )


def parse_fanmtl_catalog_page(html):
    """Parse one page of the catalog browse listing (CATALOG_URL_TEMPLATE)
    into a list of CatalogEntry. Title, chapter count, and status are all
    present on the card itself, so the candidate-pool filter can run without
    fetching each novel's own page."""
    soup = BeautifulSoup(html, "html.parser")
    entries = []

    for card in soup.select("li.novel-item"):
        link = card.select_one("a[href]")
        if not link:
            continue

        url = urljoin(BASE_URL, link["href"])
        title = link.get("title") or _text(card.select_one("h4.novel-title")) or _text(link)

        id_match = _CATALOG_ID_PATTERN.search(link["href"])
        chapter_id = id_match.group(1) if id_match else None

        chapters = None
        status = None
        updated_text = None
        for stat in card.select("div.novel-stats"):
            status_span = stat.select_one("span.status")
            if status_span is not None:
                status = status_span.get_text(strip=True)
                continue
            # Material Icons render their glyph via a ligature -- the icon's
            # own tag text is a literal word ("book", "update") that would
            # otherwise leak into the parsed text below.
            icon = stat.find("i")
            if icon is not None:
                icon.extract()
            text = stat.get_text(" ", strip=True)
            count_match = _CATALOG_CHAPTERS_PATTERN.search(text)
            if count_match:
                chapters = int(count_match.group(1))
            elif "ago" in text.lower():
                updated_text = text

        entries.append(CatalogEntry(
            title=title, url=url, chapter_id=chapter_id,
            chapters=chapters, status=status, updated_text=updated_text,
        ))

    return entries


PROFILE = SiteProfile(
    site_key="fanmtl",
    domains=["fanmtl.com", "www.fanmtl.com"],
    chapter_link_pattern=r"/novel/([^/]+?)_(\d+)\.html",
    index_url_id_pattern=r"/novel/([^/]+?)\.html",
    chapter_number_fallback_pattern=r"_(\d+)\.html",
    # Site's live chapter-count text has since disappeared from the index page;
    # kept as a harmless non-matching pattern, engine falls back to
    # chapter_number_fallback_pattern's max-link scan (confirmed working).
    chapter_count_pattern=r"(\d+)\s+[Cc]hapters?",
    chapter_url_template="{base_url}/novel/{chapter_id}_{n}.html",
    # Scoped to div.chapter-content -- removes reliance on skip_phrases for
    # ordinary nav/UI chrome (none of that lives inside this container). BUT:
    # on rare chapters (confirmed: kks30150 ch266, ch313) FanMTL splices a
    # self-promotional "Bookmark this page to continue reading '<title>'" ad
    # paragraph INSIDE this same container, sometimes appended straight onto a
    # real sentence -- so skip_phrases stays as a backstop even with a scoped
    # selector; scoping alone isn't sufficient here.
    paragraph_selector="div.chapter-content p",
    skip_phrases=[
        "chevron_left", "chevron_right", "nights_stay",
        "Tap the screen", "Use arrow keys", "keyboard keys",
        "You'll Also Like", "Bookmark this page",
    ],
    # Search: the site's own search box POSTs to this endpoint with field name
    # "keyboard" (a typo baked into their markup, not ours), plus three hidden
    # fields (show/tempid/tbname) the backend requires or it 404s, and returns
    # a page of <li class="novel-item"> results.
    search_base_url="https://www.fanmtl.com",
    search_url="https://www.fanmtl.com/e/search/index.php",
    search_method="post",
    search_query_param="keyboard",
    search_extra_params={"show": "title", "tempid": "1", "tbname": "news"},
    search_result_selector="li.novel-item",
    search_link_selector='a[href^="/novel/"]',
    search_chapter_count_pattern=r"(\d+)\s+Chapters?",
    parse_metadata_fn=parse_fanmtl_metadata,
)
