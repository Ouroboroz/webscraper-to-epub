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
| `--delay SECS` | `2.5` | Mean delay between chapter requests (actual gap is jittered around this) |
| `--pacing-file FILE` | `pacing.json` | Where to persist learned per-site request pacing |
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
| `check [--only SITE:ID ...] [--dry-run] [--email] [--email-threshold N] [--pacing-file FILE]` | Fetch new chapters for tracked novels and rebuild their EPUBs |
| `search <query>` | Filter tracked novels by title |
| `find <query> [--site KEY]` | Search a site for a novel (to get its URL before `add`) |
| `grep <query> [--context N]` | Full-text search inside downloaded epub chapters |
| `mail <site_key> <chapter_id> [--pacing-file FILE]` | Email a tracked novel's not-yet-sent chapters to Kindle right now (bypasses the email threshold) |

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

### Request pacing & resilience

Every fetch goes through a few cheap defenses adapted from reviewing
[lncrawl/scraper](https://github.com/lncrawl/scraper) (Apache-2.0):

- **Jittered pacing.** `--delay` is the *mean* of a randomized gap between
  chapter requests, not a fixed sleep — perfectly regular request timing is
  itself a signal that whatever's making the requests isn't a person.
- **Learned backoff.** A `429` (or a server's own `Retry-After` header, when
  present) widens the delay for that site and persists it to `pacing.json`
  (`--pacing-file` to override the path) so the next run — including the
  next `check` under cron — starts already slowed down instead of relearning
  the same limit from scratch. The interval only ever widens, never shrinks
  back down automatically: **delete `pacing.json` to reset every site's
  learned interval** back to whatever `--delay` says. A `--delay` larger than
  the learned value still wins meanwhile, since it's read as a floor — asking
  to be more polite than what was learned is always honoured.
- **Challenge-page detection.** A `200` response that looks like a
  bot-challenge/interstitial page (rather than real chapter content) is
  treated as a failure and triggers the same backoff as a `429` — on chapter
  fetches *and* on the index-page fetch that opens every run — instead of
  silently being cached and shipped into the EPUB as garbage. Vendor tokens
  (`cf-browser-verification`, `ddos-guard`, …) are matched anywhere in the
  response; giveaway *phrases* like "just a moment" are only trusted inside
  `<title>`, since they're ordinary English that turns up in real translated
  prose ("Just a moment later, he turned around.") and a false positive here
  would discard a good chapter and widen that site's pacing for nothing.
- **Honeypot-link filtering.** When scanning a novel's index page for its
  chapter ID, links that are `rel=nofollow`, `hidden`, `aria-hidden="true"`,
  or hidden via inline `display:none`/`visibility:hidden`/`opacity:0` are
  ignored — a defense against decoy links some sites plant specifically to
  catch scrapers walking every `<a href>` on the page.

### Auto-email to Kindle

`check --email` and `mail <site_key> <chapter_id>` can email a novel's
not-yet-sent chapters straight to a Kindle's Send-to-Kindle address.
Send-to-Kindle by email can't merge or replace an existing document — every
send creates a brand-new item on the device — so **each send covers only the
chapters not yet emailed** (`last_emailed_chapter+1 .. last_known_chapter`),
titled with that specific range (e.g. `[Ch 101 - Ch 200] Title.epub`), rather
than resending the whole growing book and duplicating everything already
delivered. `check --email` only fires once `EMAIL_CHAPTER_THRESHOLD` (default
`100`, override with `--email-threshold`) chapters have accumulated since the
last send, so a long-running novel arrives as a handful of non-overlapping
installments instead of a new document every time one chapter posts. `mail`
sends whatever's pending right now, bypassing the threshold — useful for
testing or for catching up on demand.

**One-time Amazon setup** (manual — not something the code can do for you):
1. Go to [amazon.com/myk](https://www.amazon.com/myk) → *Preferences* →
   *Personal Document Settings*.
2. Note your Kindle's `...@kindle.com` Send-to-Kindle address.
3. Under *Approved Personal Document E-mail List*, add the address you'll be
   sending FROM. Amazon silently drops mail from unapproved senders.

**Credentials**: env-var-first, falls back to a local `.env` file (repo root,
gitignored — copy `.env.example` to `.env` and fill in real values). **Cron
does not inherit your shell's exported env vars** — if
`scripts/check_updates.sh` will ever run `check --email`, use `.env`, not
`.bashrc`/`.zshrc` exports; `.env` is read directly by the Python process
either way, so it works identically interactively or under cron.

```
EPUB_MAIL_SMTP_HOST=smtp.gmail.com
EPUB_MAIL_SMTP_PORT=587
EPUB_MAIL_SMTP_USER=you@gmail.com
EPUB_MAIL_SMTP_PASSWORD=app-password
EPUB_MAIL_KINDLE_ADDR=yourname_XXXX@kindle.com
EPUB_MAIL_ALERT_ADDR=you@gmail.com
```

Each key works identically whether set in `.env` or as a real exported env
var (a real env var always wins if both are set):

| Key | Required |
|---|---|
| `EPUB_MAIL_SMTP_HOST` | yes |
| `EPUB_MAIL_SMTP_PORT` | yes |
| `EPUB_MAIL_SMTP_USER` | yes |
| `EPUB_MAIL_SMTP_PASSWORD` | yes |
| `EPUB_MAIL_FROM_ADDR` | no — defaults to `EPUB_MAIL_SMTP_USER` |
| `EPUB_MAIL_KINDLE_ADDR` | yes |
| `EPUB_MAIL_ALERT_ADDR` | yes — where failure alerts go |
| `EPUB_MAIL_SMTP_USE_SSL` | no, default `false` |

Recommended provider: **Gmail SMTP** (`smtp.gmail.com:587`, STARTTLS) using
your existing account — zero new signups, and Amazon needs one approved
sender regardless of provider. Gmail requires an **App Password** (your
regular password won't authenticate for SMTP).

Every send is preceded by a sanity check (minimum chapter count, non-empty
prose per chapter) — a broken scrape refuses to send rather than emailing a
garbled book, and a short failure-alert email goes to `alert_addr` instead
(same credentials), so a failure never just silently vanishes.

```
python -m epub_scraper.update mail fanmtl abc123
python -m epub_scraper.update check --email
python -m epub_scraper.update check --email --email-threshold 20
```

`scripts/check_updates.sh` does **not** pass `--email` by default — add it to
that script's `update check` line whenever ready to enable semi-automatic
sending under cron.

## Classification Data Spine (Stage 0)

A separate, self-contained pipeline (`epub_scraper/dataspine.py`) with a different job than
everything above: instead of downloading a novel to read, it builds a local SQLite database of
FanMTL's catalog — candidate filter, metadata, a chapter-prose sample, and Novel Updates
enrichment — as training data for a personal "would I like this novel" classifier. It doesn't
touch `epubs/`, `library.json`, or the EPUB-building path at all (though `chapters` does share the
same on-disk `.cache/` as the interactive scraper, so a chapter sampled here is already warm if
that novel later gets a full download).

**Setup**: the `crawl`/`metadata`/`chapters` commands need nothing beyond `requirements.txt`.
`nu-crawl`/`nu-metadata` also need `requirements-novelupdates.txt` (SeleniumBase + curl_cffi, and a
real Chrome — on Linux, an Xvfb display too — since they solve Novel Updates' Cloudflare challenge
with an actual browser). Verify that part in isolation first with
`python -m epub_scraper.novelupdates check` before trusting a full `nu-metadata` run. `enrich`
itself needs neither — see below.

```
pip install -r requirements-novelupdates.txt
```

| Command | Purpose |
|---|---|
| `crawl [--start-page N] [--pages N] [--min-chapters N] [--delay SECS] [--pacing-file FILE] [--refresh] [--db FILE]` | Page through the catalog listing, upsert every novel, and flag candidates (`chapter_count >= --min-chapters`) |
| `metadata [--limit N] [--delay SECS] [--pacing-file FILE] [--db FILE]` | Fetch full synopsis/genres/author/alt-title for each candidate still missing it |
| `chapters [--count N] [--limit N] [--delay SECS] [--pacing-file FILE] [--cache-dir DIR] [--db FILE]` | Sample each candidate's first `--count` chapters (default 5) as clean plain text |
| `nu-crawl [--start-page N] [--pages N] [--delay SECS] [--pacing-file FILE] [--db FILE]` | Page through Novel Updates' own bulk catalog listing into `nu_novels` (url+title only; short, ~100 pages total) |
| `nu-metadata [--limit N] [--delay SECS] [--pacing-file FILE] [--db FILE]` | Fetch each `nu_novels` row's full detail (synopsis/genres/tags/author/status/...) |
| `enrich [--limit N] [--db FILE]` | Match each pending FanMTL candidate against the locally-crawled `nu_novels` catalog — pure computation, no network |
| `stats [--db FILE]` | Progress summary: totals, candidates, how many have metadata/chapters, NU-resolution breakdown, `nu_novels` progress |

**Why `nu-crawl`/`nu-metadata`/`enrich` instead of one `enrich` that searches Novel Updates
per-candidate (the original design)**: real-world testing (2026-08-03) found Novel Updates' entire
catalog is only ~2,475 series total — tiny next to the FanMTL candidate pool (up to 105,049), so a
live search per candidate was guaranteed to come back empty for the vast majority of them (>95%),
against a site 40x smaller than what was being searched. `nu-crawl` + `nu-metadata` crawl NU's own
catalog into `nu_novels` once (independent of any FanMTL data); `enrich` then matches locally
against that fixed set — no Novel Updates network traffic on `enrich`'s critical path at all.

**Running the full pipeline** — run in this order (each stage reads what the previous one wrote).
`--min-chapters 300` and `--delay 1.2` are the settings actually chosen for this project (see
Technical Notes in the vault's `Classification Data Spine` story for why: the default
`--min-chapters 80` turned out to match 51k+ candidates against FanMTL's real catalog size, far
more than intended, and 1.2s was chosen after 60k+ requests at the default 2.5s produced zero
throttling/blocks — there's headroom, though not unlimited):

```
source ~/miniconda3/etc/profile.d/conda.sh && conda activate epub   # or your own env with the deps above

python -m epub_scraper.dataspine crawl --min-chapters 300 --pages 100000 --delay 1.2 --db dataspine.db
python -m epub_scraper.dataspine metadata --limit 1000000 --delay 1.2 --db dataspine.db
python -m epub_scraper.dataspine chapters --limit 1000000 --delay 1.2 --db dataspine.db
python -m epub_scraper.dataspine nu-crawl --delay 1.2 --db dataspine.db
python -m epub_scraper.dataspine nu-metadata --limit 1000000 --delay 1.2 --db dataspine.db
python -m epub_scraper.dataspine enrich --db dataspine.db

python -m epub_scraper.dataspine stats --db dataspine.db
```

`--pages 100000`/`--limit 1000000` aren't real expected totals — they're just "don't stop early,"
since `crawl` already stops on its own at the first empty page (end of catalog), `nu-crawl` stops
on its own once Novel Updates' own pagination runs out, and `metadata`/`chapters`/`nu-metadata`/
`enrich` already stop on their own once nothing is left pending; the defaults exist so a first-time
run doesn't run away unbounded, not because a real run should be split into many small invocations
(`enrich`'s own default is already a large number, since it's pure local computation now with
nothing left to be cautious about).

**Resumable and paced like the rest of this repo**: `crawl`/`nu-crawl`'s next page is persisted in
the DB itself (`crawl_state` table, keyed by site) after every page, so re-running either with no
`--start-page` just continues — pass `--start-page` explicitly only to override that.
`metadata`/`chapters`/`nu-metadata` are naturally resumable too (each just queries for rows still
missing that stage's data); `enrich` likewise only processes FanMTL candidates still missing a
resolution. `crawl`/`metadata`/`chapters`/`nu-crawl`/`nu-metadata` share one `pacing.json` (via
`--pacing-file`) with the interactive scraper's `Pacer` — a 429 or detected challenge page widens
the interval and it never resets on its own; delete `pacing.json` to reset. `crawl` additionally
retries a failed page (bounded, 5x) with backoff before giving up, rather than letting one
transient error kill an unattended multi-hour run; `nu-metadata` similarly forces a fresh Novel
Updates session after 3 sustained back-to-back 429s, on top of the pacer widening.

**Running it unattended, logged, tailable**:

```
mkdir -p logs
export PYTHONUNBUFFERED=1   # otherwise stdout is block-buffered when redirected to a file,
                             # and `tail -f` won't show anything until the buffer fills
python -m epub_scraper.dataspine crawl      --min-chapters 300 --pages 100000  --delay 1.2 --db dataspine.db >> logs/dataspine_crawl.log 2>&1
python -m epub_scraper.dataspine metadata   --limit 1000000     --delay 1.2 --db dataspine.db >> logs/dataspine_crawl.log 2>&1
python -m epub_scraper.dataspine chapters   --limit 1000000     --delay 1.2 --db dataspine.db >> logs/dataspine_crawl.log 2>&1
python -m epub_scraper.dataspine nu-crawl                        --delay 1.2 --db dataspine.db >> logs/dataspine_crawl.log 2>&1
python -m epub_scraper.dataspine nu-metadata --limit 1000000     --delay 1.2 --db dataspine.db >> logs/dataspine_crawl.log 2>&1
python -m epub_scraper.dataspine enrich     --db dataspine.db >> logs/dataspine_crawl.log 2>&1
```

then in another shell: `tail -f logs/dataspine_crawl.log`.

Output lives in `dataspine.db` (gitignored — mutates every run) and `pacing.json`; both stay at
whatever path `--db`/`--pacing-file` point to.

### Corpus structure (Stage 1)

A second layer on top of the same `dataspine.db`: understand what the corpus *contains*
(thematic clusters + tag communities) rather than adding more data to it. Pure local
computation — no network calls, no site to be polite to — so there's no pacing/resume story here
beyond what `embed` already gets from being resumable like `metadata`/`chapters`/`enrich`.
Produces zero recommendations by design; that's a later stage's job.

**Setup**: needs `requirements-ml.txt` (sentence-transformers + torch + umap-learn + hdbscan +
python-igraph + leidenalg — pulls in a real embedding model and, ideally, a GPU). Not part of
`requirements-ml.txt`'s sibling files because this is a meaningfully heavier, ML-specific
dependency set than either the base scraper or Novel Updates enrichment need.

```
pip install -r requirements-ml.txt
```

| Command | Purpose |
|---|---|
| `embed [--limit N] [--model NAME] [--db FILE]` | Embed each candidate's synopsis (default model: `BAAI/bge-m3`) — resumable, only embeds candidates missing one |
| `cluster [--umap-dims N] [--min-cluster-size N] [--db FILE]` | UMAP-reduce + HDBSCAN every embedded candidate into `cluster_id` (full recompute, not incremental — see below) |
| `tag-communities [--db FILE]` | Leiden-cluster the tag co-occurrence graph into `community_id` per tag (also a full recompute) |

```
python -m epub_scraper.dataspine embed --limit 1000000 --db dataspine.db
python -m epub_scraper.dataspine cluster --db dataspine.db
python -m epub_scraper.dataspine tag-communities --db dataspine.db
```

`embed` only needs `synopsis` (from `metadata`), not `chapters` or a finished `enrich` run — it's
usable as soon as `metadata` has processed anything, and just gets a richer tag graph as `enrich`
continues in the background. Unlike `crawl`/`metadata`/`chapters`/`nu-crawl`/`nu-metadata`/`enrich`,
`cluster` and `tag-communities` are **full recomputes** every time, not incremental — a cluster
boundary can shift for every novel as the corpus grows, so there's no meaningful "resume" for them;
just re-run them after `embed` has processed more candidates.

Verified live (2026-08-03) against 1,510 real novels: embedded in ~46s on a consumer GPU, 14
clusters + 786 outliers, and the clusters are genuinely thematically coherent (clean separation by
crossover fandom — Naruto, Douluo Continent, Pokémon, Hogwarts, One Piece, Marvel each formed
their own cluster — and by genre, e.g. cultivation novels separate from modern-urban/business
rebirth novels). `synopsis_embedding` is stored as a raw float32 BLOB directly on the `novels`
table (`numpy.tobytes()`/`np.frombuffer()`) rather than a second store.

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

All 210 tests run offline — no real site or SMTP server is ever hit. Network
calls are mocked at the `requests.Session` boundary (`tests/fakes.py`'s
`FakeSession`), which every fetch/scrape/search call already threads through
as a parameter, so failure modes (HTTP errors, connection errors,
circuit-breaker trips) can be injected precisely without touching a real
server. Email sends are mocked the same way at the SMTP boundary
(`tests/fakes.py`'s `FakeSMTP`). Parser tests run against real captured HTML
in `tests/fixtures/` rather than hand-authored stand-ins.

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
  mailer.py       sends a built .epub to a Kindle Send-to-Kindle address, plus failure alerts
  library.py      library.json load/save/entry helpers
  cache.py        per-chapter HTML cache on disk
  fetcher.py      HTTP fetch with shared headers
  sites/          one SiteProfile per supported aggregator site
  tools/          standalone helpers (e.g. fetch_sample.py) for site onboarding
  dataspine.py       Stage 0/1 CLI (crawl/metadata/chapters/nu-crawl/nu-metadata/
                     enrich/embed/cluster/tag-communities/stats) -- see
                     "Classification Data Spine" and "Corpus structure" sections above
  dataspine_db.py    SQLite schema + helpers for dataspine.py
  novelupdates.py    Novel Updates challenge-solving, catalog listing, search,
                     series-page scraping (synopsis included)
  entity_resolution.py  FanMTL <-> Novel Updates title matching (RapidFuzz cascade)
  corpus_structure.py    Stage 1: synopsis embedding, UMAP+HDBSCAN clustering,
                         tag-community detection (Leiden) -- pure local computation
scripts/
  check_updates.sh   cron-friendly wrapper around `update check` (lockfile + logging)
tests/
  fakes.py          FakeSession/FakeResponse/FakeSMTP test doubles
  html_builders.py  synthetic HTML generators for parametrized edge cases
  fixtures/         real captured HTML used as parser test fixtures
  test_*.py         one file per epub_scraper module
epubs/            built EPUBs (gitignored)
.cache/           raw scraped chapter HTML (gitignored)
library.json      tracked-novel state (gitignored)
pacing.json       learned per-site request pacing (gitignored)
dataspine.db      Stage 0 classification data spine (gitignored)
logs/             ad-hoc run logs, e.g. an unattended dataspine pipeline run (gitignored)
.env.example      template for .env -- copy and fill in (committed)
.env              Kindle-mail credentials (gitignored)
```
