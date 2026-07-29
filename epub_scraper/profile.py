from dataclasses import dataclass, field
from typing import Callable, NamedTuple, Optional


class IndexResult(NamedTuple):
    title: str
    chapter_id: Optional[str]
    total: Optional[int]
    base_url: str


class ChapterResult(NamedTuple):
    title: str
    body_html: str


class SearchResult(NamedTuple):
    title: str
    url: str
    chapters: Optional[int]


@dataclass(frozen=True)
class SiteProfile:
    site_key: str
    domains: list
    chapter_link_pattern: str
    index_url_id_pattern: str
    chapter_number_fallback_pattern: str
    chapter_count_pattern: str
    chapter_url_template: str
    skip_phrases: list = field(default_factory=list)

    index_title_selector: str = "h1"
    chapter_title_selector: str = "h2"
    chapter_title_fallback: str = "Chapter {n}"
    paragraph_selector: str = "p"
    min_paragraph_length: int = 4
    link_paragraph_max_length: int = 80

    # Escape hatch: when set, bypasses the declarative fields above entirely.
    parse_index_fn: Optional[Callable[[str, str], IndexResult]] = None
    parse_chapter_fn: Optional[Callable[[str, int], ChapterResult]] = None

    # -- Site search (optional: None means this site doesn't support `find`) ----
    search_base_url: Optional[str] = None
    search_url: Optional[str] = None
    search_method: str = "get"                 # "get" or "post"
    search_query_param: Optional[str] = None
    search_extra_params: dict = field(default_factory=dict)
    search_result_selector: Optional[str] = None
    search_link_selector: str = "a[href]"
    search_chapter_count_pattern: Optional[str] = None

    # Escape hatch for sites whose search doesn't fit the declarative shape above.
    search_fn: Optional[Callable[[object, str], list]] = None
