# epub_scraper

Scrape webnovels from aggregator sites into EPUBs, and keep a tracked library
of them up to date as new chapters come out.

## Setup

```
pip install -r requirements.txt
```

Supported sites live in `epub_scraper/sites/`. Currently: **fanmtl.com**.

## One-shot scrape

Download a novel straight to an EPUB, no tracking:

```
python -m epub_scraper <novel-index-url> [options]
```

| Option | Default | Description |
|---|---|---|
| `--start N` | `1` | First chapter to fetch |
| `--end N` | auto-detect | Last chapter to fetch (inclusive) |
| `--delay SECS` | `2.5` | Delay between chapter requests |
| `--output FILE` | auto | Output path (default: `epubs/[Ch start - Ch end] Title.epub`) |
| `--cache-dir DIR` | `.cache` | Where raw chapter HTML is cached |
| `--no-cache` | off | Ignore and overwrite any existing cache |
| `--site KEY` | auto-detect | Force a specific site profile |

Example:

```
python -m epub_scraper https://www.fanmtl.com/novel/some-novel.html --start 50 --end 100
```

## Tracked library

`python -m epub_scraper.update <command>` manages `library.json` — novels you
want to keep up to date. Every rebuild writes to `epubs/`.

| Command | Purpose |
|---|---|
| `add <url> [--last-known N]` | Start tracking a novel |
| `remove <site_key> <chapter_id>` | Stop tracking a novel |
| `list` | List everything tracked |
| `check [--only SITE:ID ...] [--dry-run]` | Fetch new chapters for tracked novels and rebuild their EPUBs |
| `search <query>` | Filter tracked novels by title |
| `find <query> [--site KEY]` | Search a site for a novel (to get its URL before `add`) |
| `grep <query> [--context N]` | Full-text search inside downloaded epub chapters |

Examples:

```
python -m epub_scraper.update find "mysterious merchant"
python -m epub_scraper.update add https://www.fanmtl.com/novel/kks30107.html
python -m epub_scraper.update check
python -m epub_scraper.update search cult
python -m epub_scraper.update grep "yang energy"
```

`check` is what `scripts/check_updates.sh` runs on a schedule (cron/systemd
timer) to keep every tracked novel current. It re-derives the whole EPUB from
`.cache/` + newly fetched chapters each time, so a chapter that failed to
fetch on one run gets silently retried on the next. Three consecutive
real-fetch failures for a novel trip a circuit breaker and stop that novel's
run early (so one dead chapter doesn't burn through a rate-limited fetch loop
for nothing); five consecutive checks with zero progress auto-disable it.

## Output files

EPUBs are written to `epubs/` as:

```
[Ch <start> - Ch <end>] <Title>.epub
```

e.g. `[Ch 1 - Ch 265] Global Lord - One more god-level talent every month.epub`.
The chapter range is always `1..last_known_chapter` for tracked novels, so
the filename (and thus what shows up on a Kindle/e-reader) shifts
automatically as new chapters are fetched — the old filename is removed when
the new one is written.

## Tests

```
pip install -r requirements-dev.txt
python -m pytest
```

All 160 tests run offline — no real site is ever hit. Network calls are
mocked at the `requests.Session` boundary (`tests/fakes.py`'s `FakeSession`),
which every fetch/scrape/search call already threads through as a parameter,
so failure modes (HTTP errors, connection errors, circuit-breaker trips) can
be injected precisely without touching a real server. Parser tests run
against real captured HTML in `tests/fixtures/` rather than hand-authored
stand-ins.

## Adding a new site

Use the `onboard-site` skill (or see `epub_scraper/sites/fanmtl.py` for a
worked example + `epub_scraper/profile.py` for the fields a `SiteProfile`
supports). `epub_scraper/tools/fetch_sample.py` is handy for pulling a page's
raw HTML by hand while figuring out selectors.

## Project layout

```
epub_scraper/
  cli.py          one-shot scrape entry point (python -m epub_scraper)
  update.py       tracked-library entry point (python -m epub_scraper.update)
  engine.py       site-agnostic index/chapter/search parsing over a SiteProfile
  profile.py      SiteProfile dataclass + result types
  scrape.py       chapter-range fetch loop (cache, retries, circuit breaker)
  epub_writer.py  chapters -> .epub
  textsearch.py   full-text search over a built .epub's chapters
  library.py      library.json load/save/entry helpers
  cache.py        per-chapter HTML cache on disk
  fetcher.py      HTTP fetch with shared headers
  sites/          one SiteProfile per supported aggregator site
  tools/          standalone helpers (e.g. fetch_sample.py) for site onboarding
scripts/
  check_updates.sh   cron-friendly wrapper around `update check` (lockfile + logging)
tests/
  fakes.py          FakeSession/FakeResponse requests.Session test double
  html_builders.py  synthetic HTML generators for parametrized edge cases
  fixtures/         real captured HTML used as parser test fixtures
  test_*.py         one file per epub_scraper module
epubs/            built EPUBs (gitignored)
.cache/           raw scraped chapter HTML (gitignored)
library.json      tracked-novel state (gitignored)
```
