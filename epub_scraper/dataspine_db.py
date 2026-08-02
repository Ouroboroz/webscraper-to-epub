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
"""


def init_db(path=DEFAULT_DB_PATH):
    """Open (creating if needed) the dataspine SQLite DB and ensure its schema
    exists. Returns a connection with Row-based access; callers are
    responsible for commit()/close()."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
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
    by_status = dict(conn.execute(
        f"SELECT status, COUNT(*) FROM novels {where}{' AND' if where else 'WHERE'} "
        "candidate = 1 GROUP BY status",
        params,
    ).fetchall())

    return {
        "total": total,
        "candidates": candidates,
        "candidates_with_metadata": with_metadata,
        "candidates_by_status": by_status,
    }
