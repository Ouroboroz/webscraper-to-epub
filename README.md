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
.env.example      template for .env -- copy and fill in (committed)
.env              Kindle-mail credentials (gitignored)
```
