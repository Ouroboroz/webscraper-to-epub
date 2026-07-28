from ..profile import SiteProfile

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
)
