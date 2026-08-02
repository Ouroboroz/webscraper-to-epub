"""Synthetic HTML generators for edge cases that need a specific value per
test (chapter totals, presence/absence of count text, paragraph combos) --
clearer per-test intent than indexing into a shared frozen fixture file.
Shaped to match epub_scraper.sites.fanmtl.PROFILE's selectors/patterns.
"""


def fanmtl_chapter_html(paragraphs, title="Chapter Title", include_h2=True):
    """paragraphs: list[str], each wrapped in <p> inside div.chapter-content."""
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    h2 = f"<h2>{title}</h2>" if include_h2 else ""
    return f'<html><body>{h2}<div class="chapter-content">{body}</div></body></html>'


def fanmtl_index_html(chapter_id, total, title="Test Novel",
                       with_count_text=False, count_text_total=None):
    count_html = f"<span>{count_text_total if count_text_total is not None else total} Chapters</span>" \
        if with_count_text else ""
    links = "".join(f'<a href="/novel/{chapter_id}_{n}.html">Chapter {n}</a>'
                     for n in range(1, total + 1))
    return f"<html><body><h1>{title}</h1>{count_html}{links}</body></html>"


def fanmtl_index_html_no_links(title="Test Novel"):
    return f"<html><body><h1>{title}</h1></body></html>"


def nu_search_html(hits):
    """hits: list of (title, url) tuples -> a Novel Updates search-results
    page shaped to match novelupdates.search()'s selector (a.w-blog-entry-link)."""
    items = "".join(
        f'<div class="w-blog-entry"><a class="w-blog-entry-link" href="{url}" '
        f'title="{title}">{title}</a></div>'
        for title, url in hits)
    return f"<html><body>{items}</body></html>"


def nu_series_html(*, title="Test Series", associated_names=None, genres=None, tags=None,
                    author=None, translation_status=None, translation_groups=None,
                    release_frequency=None, rating=None, votes=None):
    """A Novel Updates series page shaped to match novelupdates.fetch_series()'s
    selectors -- synthetic/best-effort, NOT a captured real page (NU is
    behind Cloudflare and couldn't be fetched this session; selectors are
    grounded in two independent open-source NU scrapers' real source instead,
    see epub_scraper/novelupdates.py's docstring)."""
    names_html = "<br>".join(associated_names or [])
    genres_html = "".join(f"<a>{g}</a>" for g in (genres or []))
    tags_html = "".join(f"<a>{t}</a>" for t in (tags or []))
    author_html = f"<a>{author}</a>" if author else ""
    groups_html = "".join(
        f'<li><span style="padding-left:20px;">{g}</span></li>' for g in (translation_groups or []))
    sidebar_bits = []
    if release_frequency:
        sidebar_bits.append(f'<h5 class="seriesother">Release Frequency</h5><span>{release_frequency}</span>')
    if rating:
        sidebar_bits.append(f'<h5 class="seriesother">Rating</h5><span>{rating}</span>')
    if votes:
        sidebar_bits.append(f'<h5 class="seriesother">Vote Count</h5><span>{votes}</span>')

    return f'''<html><body>
<span class="seriestitlenu">{title}</span>
<div id="editassociated">{names_html}</div>
<div id="seriesgenre">{genres_html}</div>
<div id="showtags">{tags_html}</div>
<div id="showauthors">{author_html}</div>
<div id="showtranslated">{translation_status or ""}</div>
<ol class="sp_grouptable">{groups_html}</ol>
{"".join(sidebar_bits)}
</body></html>'''


def fanmtl_catalog_html(entries):
    """entries: list of (title, chapter_id, chapters, status) tuples -> a
    catalog browse page (CATALOG_URL_TEMPLATE) with one li.novel-item per
    entry, shaped to match parse_fanmtl_catalog_page's selectors."""
    cards = "".join(f'''
<li class="novel-item">
<a href="/novel/{chapter_id}.html" title="{title}">
<h4 class="novel-title">{title}</h4>
<div class="novel-stats"><span><i class="material-icons">book</i> {chapters} Chapters</span></div>
<div class="novel-stats">Status: <span class="status">{status}</span></div>
</a>
</li>''' for title, chapter_id, chapters, status in entries)
    return f'<html><body><ul class="novel-list">{cards}</ul></body></html>'
