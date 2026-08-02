import sqlite3

from .util import now_iso

DEFAULT_DB_PATH = "dataspine.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS novels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_key TEXT NOT NULL,
    chapter_id TEXT,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    alt_title TEXT,
    author TEXT,
    synopsis TEXT,
    chapter_count INTEGER,
    status TEXT,
    rating TEXT,
    updated_text TEXT,
    candidate INTEGER NOT NULL DEFAULT 0,
    nu_url TEXT,
    nu_resolved_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(site_key, url)
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS novel_tags (
    novel_id INTEGER NOT NULL REFERENCES novels(id),
    tag_id INTEGER NOT NULL REFERENCES tags(id),
    PRIMARY KEY (novel_id, tag_id)
);

CREATE TABLE IF NOT EXISTS crawl_state (
    site_key TEXT PRIMARY KEY,
    next_page INTEGER NOT NULL DEFAULT 0,
    last_crawled_at TEXT
);

CREATE TABLE IF NOT EXISTS chapters (
    novel_id INTEGER NOT NULL REFERENCES novels(id),
    chapter_number INTEGER NOT NULL,
    title TEXT,
    body TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (novel_id, chapter_number)
);
"""


# Novel Updates enrichment columns, added after the initial FanMTL-only
# schema shipped -- guarded ALTER TABLE via _ensure_column() so existing
# dataspine.db files pick them up without a real migration framework.
_NU_COLUMNS = [
    ("nu_title", "TEXT"),
    ("nu_author", "TEXT"),
    ("nu_status", "TEXT"),
    ("nu_release_frequency", "TEXT"),
    ("nu_rating", "TEXT"),
    ("nu_votes", "TEXT"),
    ("nu_translation_groups", "TEXT"),
    ("nu_resolution", "TEXT"),  # 'auto' | 'ambiguous' | 'no_candidates'
]

# Chapter-sample tracking, added after NU enrichment shipped -- same guarded
# ALTER TABLE approach. Not NULL <=> "chapters" attempted (successfully or
# not) for this novel; how many actually landed lives in the chapters table
# itself, since partial failures (a chapter 404s/decoys out) are expected and
# shouldn't trigger endless retries of the whole novel.
_CHAPTER_COLUMNS = [
    ("chapters_sampled_at", "TEXT"),
]


def _ensure_column(conn, table, name, decl):
    # table/name/decl are always internal literals from _NU_COLUMNS above,
    # never user input -- f-string interpolation here is safe.
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init_db(path=DEFAULT_DB_PATH):
    """Open (creating if needed) the dataspine SQLite DB and ensure its schema
    exists. Returns a connection with Row-based access; callers are
    responsible for commit()/close()."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for name, decl in _NU_COLUMNS + _CHAPTER_COLUMNS:
        _ensure_column(conn, "novels", name, decl)
    conn.commit()
    return conn


def upsert_catalog_entry(conn, entry, *, site_key):
    """Insert or refresh a novel row from a CatalogEntry (title, chapter
    count, status, last-updated text -- everything the catalog listing card
    itself exposes, no per-novel fetch needed)."""
    now = now_iso()
    conn.execute(
        """
        INSERT INTO novels (site_key, chapter_id, url, title, chapter_count,
                             status, updated_text, created_at, updated_at)
        VALUES (:site_key, :chapter_id, :url, :title, :chapters,
                :status, :updated_text, :now, :now)
        ON CONFLICT(site_key, url) DO UPDATE SET
            chapter_id=excluded.chapter_id,
            title=excluded.title,
            chapter_count=excluded.chapter_count,
            status=excluded.status,
            updated_text=excluded.updated_text,
            updated_at=excluded.updated_at
        """,
        {
            "site_key": site_key,
            "chapter_id": entry.chapter_id,
            "url": entry.url,
            "title": entry.title,
            "chapters": entry.chapters,
            "status": entry.status,
            "updated_text": entry.updated_text,
            "now": now,
        },
    )


def get_next_page(conn, site_key):
    """The page `crawl` should resume from when --start-page isn't given
    explicitly -- 0 if this site has never been crawled before."""
    row = conn.execute(
        "SELECT next_page FROM crawl_state WHERE site_key = ?", (site_key,)
    ).fetchone()
    return row["next_page"] if row is not None else 0


def set_next_page(conn, site_key, next_page):
    """Persist the resume point after every page (successful or not), so a
    killed/interrupted crawl picks back up on its own on the next run instead
    of relying on the operator to notice and pass back --start-page."""
    conn.execute(
        """
        INSERT INTO crawl_state (site_key, next_page, last_crawled_at)
        VALUES (?, ?, ?)
        ON CONFLICT(site_key) DO UPDATE SET
            next_page=excluded.next_page, last_crawled_at=excluded.last_crawled_at
        """,
        (site_key, next_page, now_iso()),
    )


def recompute_candidates(conn, *, min_chapters, site_key=None):
    """Recompute the candidate flag for every novel (or just `site_key`'s)
    from its current chapter_count -- idempotent, so re-running `crawl` (even
    with a different --min-chapters) always leaves `candidate` correct rather
    than only ever setting it."""
    if site_key is None:
        conn.execute(
            "UPDATE novels SET candidate = CASE WHEN chapter_count >= ? THEN 1 ELSE 0 END",
            (min_chapters,),
        )
    else:
        conn.execute(
            "UPDATE novels SET candidate = CASE WHEN chapter_count >= ? THEN 1 ELSE 0 END "
            "WHERE site_key = ?",
            (min_chapters, site_key),
        )


def get_novel(conn, site_key, url):
    return conn.execute(
        "SELECT * FROM novels WHERE site_key = ? AND url = ?", (site_key, url)
    ).fetchone()


def iter_candidates_missing_metadata(conn, site_key, limit=None):
    """Candidates that still need a metadata-pass fetch (no synopsis yet).
    Ordered by id so repeated runs make steady forward progress."""
    sql = ("SELECT * FROM novels WHERE site_key = ? AND candidate = 1 "
           "AND synopsis IS NULL ORDER BY id")
    params = [site_key]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def _get_or_create_tag(conn, name):
    conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    return row["id"]


def upsert_metadata(conn, site_key, url, metadata):
    """Fill in a novel row's full metadata from a MetadataResult, and link its
    genres into tags/novel_tags (FanMTL genres are the closest thing it has to
    tags; Novel Updates' own user-tags will land in the same tables later)."""
    novel = get_novel(conn, site_key, url)
    if novel is None:
        raise ValueError(f"No novel row for {site_key}:{url} -- run the catalog crawl first")

    conn.execute(
        """
        UPDATE novels SET
            synopsis=:synopsis, author=:author, alt_title=:alt_title,
            status=COALESCE(:status, status), rating=:rating, updated_at=:now
        WHERE id=:id
        """,
        {
            "synopsis": metadata.synopsis,
            "author": metadata.author,
            "alt_title": metadata.alt_title,
            "status": metadata.status,
            "rating": metadata.rating,
            "now": now_iso(),
            "id": novel["id"],
        },
    )

    for name in metadata.genres:
        tag_id = _get_or_create_tag(conn, name)
        conn.execute(
            "INSERT OR IGNORE INTO novel_tags (novel_id, tag_id) VALUES (?, ?)",
            (novel["id"], tag_id),
        )


def iter_candidates_missing_nu_resolution(conn, site_key, limit=None):
    """Candidates that still need a Novel Updates enrichment attempt -- no
    resolution recorded yet, successful or not. Ordered by id, same
    resumability shape as iter_candidates_missing_metadata."""
    sql = ("SELECT * FROM novels WHERE site_key = ? AND candidate = 1 "
           "AND nu_resolution IS NULL ORDER BY id")
    params = [site_key]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def upsert_nu_metadata(conn, site_key, url, resolution, metadata=None):
    """Record a Novel Updates entity-resolution outcome. On "auto" (metadata
    given), fills in nu_* fields and links NU's genres/tags into
    tags/novel_tags alongside FanMTL's. On "ambiguous"/"no_candidates", just
    stamps nu_resolution so `enrich` treats the novel as processed (resumable)
    -- manual/LLM adjudication for those is a separate, later concern."""
    novel = get_novel(conn, site_key, url)
    if novel is None:
        raise ValueError(f"No novel row for {site_key}:{url} -- run the catalog crawl first")

    if metadata is None:
        conn.execute(
            "UPDATE novels SET nu_resolution=?, nu_resolved_at=? WHERE id=?",
            (resolution, now_iso(), novel["id"]),
        )
        return

    conn.execute(
        """
        UPDATE novels SET
            nu_url=:nu_url, nu_title=:nu_title, nu_author=:nu_author,
            nu_status=:nu_status, nu_release_frequency=:nu_release_frequency,
            nu_rating=:nu_rating, nu_votes=:nu_votes,
            nu_translation_groups=:nu_translation_groups,
            nu_resolution=:resolution, nu_resolved_at=:now
        WHERE id=:id
        """,
        {
            "nu_url": metadata.url,
            "nu_title": metadata.title,
            "nu_author": metadata.author,
            "nu_status": metadata.translation_status,
            "nu_release_frequency": metadata.release_frequency,
            "nu_rating": metadata.rating,
            "nu_votes": metadata.votes,
            "nu_translation_groups": ", ".join(metadata.translation_groups) or None,
            "resolution": resolution,
            "now": now_iso(),
            "id": novel["id"],
        },
    )

    for name in metadata.genres + metadata.tags:
        tag_id = _get_or_create_tag(conn, name)
        conn.execute(
            "INSERT OR IGNORE INTO novel_tags (novel_id, tag_id) VALUES (?, ?)",
            (novel["id"], tag_id),
        )


def iter_candidates_missing_chapters(conn, site_key, limit=None):
    """Candidates that still need a chapter-sample attempt -- no attempt
    recorded yet, successful or not. Ordered by id, same resumability shape
    as iter_candidates_missing_metadata/_nu_resolution."""
    sql = ("SELECT * FROM novels WHERE site_key = ? AND candidate = 1 "
           "AND chapters_sampled_at IS NULL ORDER BY id")
    params = [site_key]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def upsert_chapters(conn, novel_id, chapters):
    """chapters: list[(chapter_number, title, body)]. Always stamps
    chapters_sampled_at on the novel, regardless of how many chapters
    actually came back -- a novel that only yields 3/5 (a chapter 404s, gets
    skipped as a decoy, etc.) must not be retried forever; whatever landed is
    still a usable sample."""
    now = now_iso()
    for number, title, body in chapters:
        conn.execute(
            """
            INSERT INTO chapters (novel_id, chapter_number, title, body, fetched_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(novel_id, chapter_number) DO UPDATE SET
                title=excluded.title, body=excluded.body, fetched_at=excluded.fetched_at
            """,
            (novel_id, number, title, body, now),
        )
    conn.execute("UPDATE novels SET chapters_sampled_at = ? WHERE id = ?", (now, novel_id))


def stats(conn, site_key=None):
    """Summary counts: total catalogued, candidates, candidates with metadata
    already fetched, and a status -> count breakdown among candidates."""
    where = "WHERE site_key = ?" if site_key else ""
    params = [site_key] if site_key else []

    total = conn.execute(f"SELECT COUNT(*) FROM novels {where}", params).fetchone()[0]
    candidates = conn.execute(
        f"SELECT COUNT(*) FROM novels {where}{' AND' if where else 'WHERE'} candidate = 1",
        params,
    ).fetchone()[0]
    with_metadata = conn.execute(
        f"SELECT COUNT(*) FROM novels {where}{' AND' if where else 'WHERE'} "
        "candidate = 1 AND synopsis IS NOT NULL",
        params,
    ).fetchone()[0]
    with_chapters = conn.execute(
        f"SELECT COUNT(*) FROM novels {where}{' AND' if where else 'WHERE'} "
        "candidate = 1 AND chapters_sampled_at IS NOT NULL",
        params,
    ).fetchone()[0]
    by_status = dict(conn.execute(
        f"SELECT status, COUNT(*) FROM novels {where}{' AND' if where else 'WHERE'} "
        "candidate = 1 GROUP BY status",
        params,
    ).fetchall())
    by_nu_resolution = dict(conn.execute(
        f"SELECT nu_resolution, COUNT(*) FROM novels {where}{' AND' if where else 'WHERE'} "
        "candidate = 1 AND nu_resolution IS NOT NULL GROUP BY nu_resolution",
        params,
    ).fetchall())

    return {
        "total": total,
        "candidates": candidates,
        "candidates_with_metadata": with_metadata,
        "candidates_with_chapters": with_chapters,
        "candidates_by_status": by_status,
        "candidates_by_nu_resolution": by_nu_resolution,
    }
