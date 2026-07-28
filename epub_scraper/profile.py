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
