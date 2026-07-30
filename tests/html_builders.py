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
