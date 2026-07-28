---
name: onboard-site
description: Add support for a new webnovel aggregator site to the epub_scraper package. Given just a novel index page URL on a site epub_scraper doesn't support yet, fetches the index page and a couple of real chapter pages, inspects their HTML, drafts a new SiteProfile config, registers it, and spot-checks the extraction before handing it back for review. Use when the user says things like "add support for <site>", "onboard <url>", "scrape this new site", or gives a URL from a domain not in epub_scraper/sites/.
---

# Onboard a new site for epub_scraper

`epub_scraper` (in this repo) turns a webnovel aggregator's index page into a clean EPUB.
Site-specific behavior lives entirely in declarative `SiteProfile` objects
(`epub_scraper/profile.py`), one per site, registered in `epub_scraper/sites/__init__.py`.
This skill is the repeatable procedure for drafting a new one — the user only needs to supply
a URL; do the fetching and HTML inspection yourself, don't ask them to paste HTML.

Read `epub_scraper/profile.py` and an existing profile (`epub_scraper/sites/fanmtl.py`) first if
you haven't already this session, so the field shapes below are concrete rather than abstract.

## Inputs

The user gives you a novel **index page URL** on the new site (e.g.
`https://example-novels.com/book/some-title`). If they instead give a chapter URL, ask for the
index page too — you need both.

## Procedure

1. **Fetch the index page.** Use the project's own conda env and the existing headers/fetch
   logic rather than a generic tool — this matters because some sites block requests without a
   browser-like `User-Agent`, which `epub_scraper/fetcher.py`'s `HEADERS` already handles:
   ```
   /home/hbyang/miniconda3/envs/epub/bin/python -m epub_scraper.tools.fetch_sample <index-url> --out /tmp/onboard_index.html
   ```
   Read the saved file. Don't dump the whole thing into context if it's large — grep for
   structural landmarks first (title tags, `<a href>` patterns, anything containing "chapter").

2. **Work out the index-page fields:**
   - `domains`: the URL's hostname, plus the `www.`-prefixed and bare variants (whichever the
     site doesn't use natively, so auto-detect matches either way).
   - `index_title_selector`: a CSS selector for the novel title. Prefer the most specific
     selector that still reads naturally as "the title" (e.g. `h1.novel-title` beats bare `h1`
     if the page has other `h1`s) — check by counting matches, not just finding one that works.
   - `chapter_link_pattern`: a regex over `<a href>` values that isolates a stable chapter-id
     component. Look at **several** chapter links, not just one, to confirm the pattern actually
     holds and that the numeric part you're keying on is a true sequential chapter number (not
     an unrelated id). If chapter URLs are NOT arithmetically derivable from a template (e.g.
     fully opaque/random slugs per chapter with no way to compute chapter N's URL from N alone),
     stop and flag this to the user — `SiteProfile.chapter_url_template` only supports simple
     `.format()` templates today; sites needing a full chapter-list crawl instead need a small
     `engine.py` extension that doesn't exist yet. Don't build that extension speculatively;
     just report the limitation.
   - `index_url_id_pattern`: fallback regex to pull the same chapter-id straight out of the
     index URL itself (mirrors `chapter_link_pattern`'s id group but matched against the index
     URL's own shape).
   - `chapter_count_pattern`: regex for a "NNN Chapters"-style count on the page, if one exists.
     If nothing like that exists, it's fine to reuse a pattern that just won't match — the
     engine falls back to scanning chapter links for the max chapter number automatically
     (`chapter_number_fallback_pattern`). Note in your final report which path this site uses.
   - `chapter_url_template`: the `.format()` string that builds a chapter URL from
     `{base_url}`, `{chapter_id}`, `{n}`.

3. **Fetch 2-3 real sample chapters** using the `chapter_url_template` you just derived (e.g.
   chapters 1 and 2, maybe a mid-novel one) via the same `fetch_sample.py` tool. Sleep ~2s
   between requests — treat an unfamiliar site with the same courtesy `cli.py` already applies
   to known ones. Confirm the page you fetched actually *is* the chapter you expected (e.g. its
   own displayed chapter number/title matches `n`) — this is your cross-check that the URL
   template is right, not just plausible.

4. **Work out the chapter-page fields:**
   - `chapter_title_selector` / `chapter_title_fallback`.
   - **Prefer a scoped content selector over the whole-page bare-`p` fallback.** If this site
     has something like `div.chapter-content`/`#content`/`article`, set `paragraph_selector` to
     a scoped selector under it (e.g. `"div.chapter-content p"`) — this eliminates the *nav/UI
     chrome* class of junk (menus, prev/next links, tooltips) that typically lives outside the
     content container, and is more robust than a whole-page scan since it doesn't depend on
     guessing every current wording of that chrome.
   - **But don't assume a scoped selector means `skip_phrases` can be dropped.** Confirmed
     on fanmtl.com by running the full 1316-cached-chapter regression check (not just the 2-3
     sample chapters this step normally fetches): a scoped `div.chapter-content` container can
     still contain **injected self-promotional/ad paragraphs** ("Bookmark this page to continue
     reading '<title>'"), sometimes appended straight onto the end of a real sentence rather
     than as their own paragraph — these are structurally indistinguishable from real prose
     (plain `<p>`, no distinguishing class, no `<a>` tag), so scoping alone can't catch them.
     They only showed up in 2 of 1316 chapters — rare enough that a 2-3 chapter sample would very
     plausibly miss them entirely. So: keep a `skip_phrases` list even with a good scoped
     selector, watching specifically for self-referential/promotional patterns (mentions of the
     novel's own title, "bookmark", "continue reading", site name) in addition to whatever nav
     chrome you found in step 3's samples. If you have a way to check more chapters than the
     usual 2-3 sample (e.g. an existing `.cache/` from a site already partially scraped some
     other way), do — it's the only thing that would have caught this on fanmtl.com.
   - `skip_phrases`: seed it from whatever chrome/ad text you actually observe in the sample
     chapters' extracted paragraph text — don't guess, and don't assume "none observed in 2-3
     samples" means "none exists."
   - `min_paragraph_length` / `link_paragraph_max_length`: keep the existing defaults (4 / 80)
     unless a specific sample chapter shows they're wrong for this site.
   - `site_key`: short lowercase slug from the domain (e.g. `example-novels.com` → `examplenovels`
     or similar — your judgment call, just keep it filesystem/identifier-safe).

5. **Write `epub_scraper/sites/<site_key>.py`**, following the exact shape of
   `epub_scraper/sites/fanmtl.py` — a module-level `PROFILE = SiteProfile(...)`.

6. **Register it** in `epub_scraper/sites/__init__.py`: add the import and one entry to
   `PROFILES`.

7. **Verify by running the real engine against the real samples**, not by eyeballing your own
   guesses. From the repo root:
   ```
   /home/hbyang/miniconda3/envs/epub/bin/python -c "
   from epub_scraper import engine
   from epub_scraper.sites.<site_key> import PROFILE
   with open('/tmp/onboard_index.html') as f: idx_html = f.read()
   print(engine.parse_index(PROFILE, idx_html, '<index-url>'))
   "
   ```
   and similarly run `engine.parse_chapter(PROFILE, chapter_html, n)` against each fetched
   sample, printing the full extracted title + body. Read the output yourself and check for:
   no leftover nav/UI text, no missing/truncated paragraphs, sensible chapter numbering.
   If anything's off, fix the profile and re-run — don't hand back a first draft you haven't
   actually looked at the output of.

8. **Save the sample chapters to `.cache/`** via `epub_scraper.cache.save_cache(cache_dir,
   chapter_id, n, html)` for the ones you fetched — free head start on the real scrape later,
   reusing the existing cache module rather than leaving throwaway files in `/tmp`.

9. **Report back to the user**, not just "done": site key, which fallback paths this site
   relies on (chapter-count text vs max-link scan; scoped selector vs bare-`p`+skip-phrases),
   any assumptions made, and the verification output from step 7 so they can spot-check without
   re-deriving it themselves. Don't run a full-novel scrape as part of onboarding — a handful of
   sample chapters is enough to validate the profile; the user runs the real scrape via the
   normal `python -m epub_scraper <url> --site <site_key>` once they're happy.

## Explicitly out of scope

- Full-novel scraping as part of onboarding (sample chapters only).
- Extending `SiteProfile`/`engine.py` to support non-template chapter URL schemes — flag it,
  don't build it speculatively for a hypothetical future site.
- Retry/backoff, scheduling, auto-email — unrelated stories, not this skill's job.
