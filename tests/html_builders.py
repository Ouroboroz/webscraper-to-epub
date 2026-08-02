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
