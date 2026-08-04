import sqlite3

from . import entity_resolution
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

CREATE TABLE IF NOT EXISTS labels (
    novel_id INTEGER PRIMARY KEY REFERENCES novels(id),
    label TEXT NOT NULL,
    drop_chapter INTEGER,
    source TEXT NOT NULL,
    labeled_at TEXT NOT NULL
);

-- Novel Updates' OWN catalog, independent of the FanMTL `novels` table above
-- -- `nu-crawl` lists every NU series here (url+title only), `nu-metadata`
-- then fills in the rest per row. `enrich` matches FanMTL candidates against
-- this table locally instead of live-searching NU per candidate (see
-- epub_scraper/novelupdates.py's module docstring for why: NU's whole
-- catalog is only ~2,475 series, far smaller than the FanMTL pool that used
-- to be searched against it one at a time).
CREATE TABLE IF NOT EXISTS nu_novels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    associated_names TEXT,
    genres TEXT,
    tags TEXT,
    author TEXT,
    synopsis TEXT,
    translation_status TEXT,
    translation_groups TEXT,
    release_frequency TEXT,
    rating TEXT,
    votes TEXT,
    listed_at TEXT NOT NULL,
    fetched_at TEXT
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

# Stage 1 (corpus structure/clustering) columns. synopsis_embedding is a raw
# BLOB -- a float32 numpy array's .tobytes() -- deliberately stored/returned
# as opaque bytes by every helper below rather than decoded here, so this
# module (used by the base crawl/metadata/chapters/enrich pipeline) never
# needs numpy itself; only epub_scraper/corpus_structure.py, which already
# depends on it for real, does the encode/decode.
_CORPUS_STRUCTURE_COLUMNS = [
    ("synopsis_embedding", "BLOB"),
    ("cluster_id", "INTEGER"),
    ("umap_x", "REAL"),
    ("umap_y", "REAL"),
]

_TAG_COLUMNS = [
    ("community_id", "INTEGER"),
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
    responsible for commit()/close().

    WAL mode: needed once there's more than one process touching this file at
    a time (Stage 0's background crawl/metadata/chapters/enrich pipeline plus
    the Stage 2 labeling app now both hit the same dataspine.db concurrently)
    -- the default rollback-journal mode takes an exclusive lock for the
    whole duration of a write, which would otherwise surface as "database is
    locked" errors under that concurrency. :memory: DBs don't support WAL
    (no file to keep a -wal sidecar next to), so skip it there -- harmless
    either way since an in-memory DB is never shared across processes.

    busy_timeout: WAL still only allows one writer at a time -- confirmed
    live (2026-08-03), labeling a book while the background metadata pass
    was mid-write raised "database is locked" immediately, since the default
    busy_timeout is 0 (fail instantly on contention rather than wait). 5s is
    comfortably longer than a single commit ever takes here."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    for name, decl in _NU_COLUMNS + _CHAPTER_COLUMNS + _CORPUS_STRUCTURE_COLUMNS:
        _ensure_column(conn, "novels", name, decl)
    for name, decl in _TAG_COLUMNS:
        _ensure_column(conn, "tags", name, decl)
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


def get_novel_by_id(conn, novel_id):
    return conn.execute("SELECT * FROM novels WHERE id = ?", (novel_id,)).fetchone()


def tags_for_novel(conn, novel_id):
    return [row["name"] for row in conn.execute(
        "SELECT t.name FROM tags t JOIN novel_tags nt ON nt.tag_id = t.id "
        "WHERE nt.novel_id = ? ORDER BY t.name",
        (novel_id,),
    )]


def first_chapter_excerpt(conn, novel_id, max_chars=600):
    """The opening of chapter 1 (or the earliest chapter actually landed, if
    1 itself 404'd/decoyed out), truncated -- for the labeling app's
    optional "chapter, if we have it" context. None if `chapters` has
    nothing for this novel yet."""
    row = conn.execute(
        "SELECT body FROM chapters WHERE novel_id = ? ORDER BY chapter_number LIMIT 1",
        (novel_id,),
    ).fetchone()
    if row is None or not row["body"]:
        return None
    body = row["body"]
    return body if len(body) <= max_chars else body[:max_chars].rstrip() + "…"


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


# -- Novel Updates' own catalog (nu_novels) --------------------------------------
#
# Independent listing of ALL of Novel Updates' series, crawled once by
# `nu-crawl`/`nu-metadata` (see dataspine.py) rather than searched per FanMTL
# candidate. List-valued fields (associated_names/genres/tags/
# translation_groups) are stored comma-joined TEXT here, matching this file's
# existing convention for `nu_translation_groups` on the `novels` table above
# (", ".join(x) or None) -- not JSON, for consistency, even though that's
# technically lossy for a name containing a literal comma (an accepted
# tradeoff already made on the `novels` side).

def split_comma_list(text):
    """Reverse of the ", ".join(x) or None convention this file uses for
    list-valued TEXT columns -- [] if the column is NULL/empty. Exposed
    (not just used internally by all_resolved_nu_novels below) since
    dataspine.py's `enrich` needs the same reverse-split to reconstruct an
    NUSeriesMetadata from a matched nu_novels row."""
    return [s.strip() for s in text.split(",")] if text else []


def upsert_nu_novel_listing(conn, url, title):
    """Insert or refresh one entry from Novel Updates' own bulk catalog
    listing (`nu-crawl`) -- just url+title+listed_at, all the listing page
    itself exposes. Deliberately never touches fetched_at or any detail
    column (that's upsert_nu_novel_details' job) -- so re-crawling the
    listing (e.g. a title changed) can't reset a novel already past its
    detail fetch back to pending."""
    conn.execute(
        """
        INSERT INTO nu_novels (url, title, listed_at)
        VALUES (:url, :title, :now)
        ON CONFLICT(url) DO UPDATE SET title=excluded.title
        """,
        {"url": url, "title": title, "now": now_iso()},
    )


def iter_nu_novels_missing_details(conn, limit=None):
    """nu_novels rows still needing a `nu-metadata` detail fetch (fetched_at
    IS NULL). Ordered by id, same resumability shape as the
    iter_candidates_missing_* helpers above."""
    sql = "SELECT * FROM nu_novels WHERE fetched_at IS NULL ORDER BY id"
    params = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def upsert_nu_novel_details(conn, url, metadata):
    """Fill in one nu_novels row's detail columns from an NUSeriesMetadata
    (`nu-metadata`'s per-series fetch_series() result) and stamp fetched_at.
    Raises if the listing crawl hasn't seen this url yet -- same ValueError
    pattern as upsert_metadata/upsert_nu_metadata. Doesn't touch title --
    that's owned by the listing crawl (upsert_nu_novel_listing); a series
    page occasionally lacking a parseable title (see fetch_series()) must
    not null out a good one already on record."""
    row = conn.execute("SELECT id FROM nu_novels WHERE url = ?", (url,)).fetchone()
    if row is None:
        raise ValueError(f"No nu_novels row for {url} -- run the listing crawl (nu-crawl) first")

    conn.execute(
        """
        UPDATE nu_novels SET
            associated_names=:associated_names, genres=:genres, tags=:tags,
            author=:author, synopsis=:synopsis,
            translation_status=:translation_status,
            translation_groups=:translation_groups,
            release_frequency=:release_frequency, rating=:rating, votes=:votes,
            fetched_at=:now
        WHERE id=:id
        """,
        {
            "associated_names": ", ".join(metadata.associated_names) or None,
            "genres": ", ".join(metadata.genres) or None,
            "tags": ", ".join(metadata.tags) or None,
            "author": metadata.author,
            "synopsis": metadata.synopsis,
            "translation_status": metadata.translation_status,
            "translation_groups": ", ".join(metadata.translation_groups) or None,
            "release_frequency": metadata.release_frequency,
            "rating": metadata.rating,
            "votes": metadata.votes,
            "now": now_iso(),
            "id": row["id"],
        },
    )


def get_nu_novel(conn, url):
    """Full nu_novels row for `url`, or None. dataspine.py's `enrich` uses
    this to go from an entity_resolution.NUCandidate's .url (as returned by
    all_resolved_nu_novels below) back to the full detail row, to reconstruct
    an NUSeriesMetadata for upsert_nu_metadata -- whose signature/behavior is
    unchanged, it just expects that duck-typed shape regardless of source."""
    return conn.execute("SELECT * FROM nu_novels WHERE url = ?", (url,)).fetchone()


def all_resolved_nu_novels(conn):
    """Every nu_novels row with details already fetched (fetched_at IS NOT
    NULL), as entity_resolution.NUCandidate(title, url, associated_names) --
    what the local `enrich` scores every FanMTL candidate against. Meant to
    be loaded ONCE per `enrich` run and reused for every row, not requeried
    per candidate -- that's the whole point of crawling the catalog locally
    instead of live-searching it. associated_names is split back out of its
    comma-joined TEXT storage via split_comma_list(); [] if NULL/empty."""
    rows = conn.execute(
        "SELECT url, title, associated_names FROM nu_novels WHERE fetched_at IS NOT NULL"
    ).fetchall()
    return [
        entity_resolution.NUCandidate(
            title=row["title"], url=row["url"],
            associated_names=split_comma_list(row["associated_names"]),
        )
        for row in rows
    ]


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


def iter_candidates_missing_embedding(conn, site_key, limit=None):
    """Candidates with a synopsis but no stored embedding yet. Ordered by id,
    same resumability shape as the other iter_candidates_missing_* helpers."""
    sql = ("SELECT * FROM novels WHERE site_key = ? AND candidate = 1 "
           "AND synopsis IS NOT NULL AND synopsis_embedding IS NULL ORDER BY id")
    params = [site_key]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def upsert_embedding(conn, novel_id, embedding_bytes):
    """embedding_bytes: a float32 numpy array's .tobytes() -- see the
    _CORPUS_STRUCTURE_COLUMNS comment for why this module treats it as
    opaque bytes rather than decoding it."""
    conn.execute("UPDATE novels SET synopsis_embedding = ? WHERE id = ?",
                 (embedding_bytes, novel_id))


def iter_embeddings(conn, site_key):
    """[(novel_id, embedding_bytes), ...] for every candidate with a stored
    embedding -- decode with np.frombuffer(blob, dtype=np.float32)."""
    rows = conn.execute(
        "SELECT id, synopsis_embedding FROM novels "
        "WHERE site_key = ? AND candidate = 1 AND synopsis_embedding IS NOT NULL",
        (site_key,),
    ).fetchall()
    return [(row["id"], row["synopsis_embedding"]) for row in rows]


def write_cluster_assignments(conn, assignments):
    """assignments: dict[novel_id, (cluster_id, umap_x, umap_y)]. A full
    recompute over whatever embeddings currently exist, not incremental --
    every novel's cluster can shift when the corpus grows, unlike the
    append-only per-novel fetches elsewhere in this module."""
    conn.executemany(
        "UPDATE novels SET cluster_id = ?, umap_x = ?, umap_y = ? WHERE id = ?",
        [(cluster_id, x, y, novel_id) for novel_id, (cluster_id, x, y) in assignments.items()],
    )


def all_tags(conn):
    """[(tag_id, name), ...] for every tag -- corpus_structure.py needs the
    full node set for its co-occurrence graph, including tags that never
    co-occur with anything (iter_tag_cooccurrence alone would miss those)."""
    return conn.execute("SELECT id, name FROM tags").fetchall()


def iter_tag_cooccurrence(conn):
    """[(tag_id_a, tag_id_b, weight), ...] for every pair of tags that
    co-occur on at least one novel, weight = how many novels share both.
    a.tag_id < b.tag_id avoids double-counting and self-pairs. Explicit
    aliases (not just `a.tag_id, b.tag_id`) since sqlite3.Row's name-based
    access can't distinguish two same-named columns otherwise."""
    return conn.execute(
        """
        SELECT a.tag_id AS tag_id_a, b.tag_id AS tag_id_b, COUNT(*) as weight
        FROM novel_tags a JOIN novel_tags b
            ON a.novel_id = b.novel_id AND a.tag_id < b.tag_id
        GROUP BY a.tag_id, b.tag_id
        """
    ).fetchall()


def write_tag_communities(conn, communities):
    """communities: dict[tag_id, community_id]."""
    conn.executemany(
        "UPDATE tags SET community_id = ? WHERE id = ?",
        [(community_id, tag_id) for tag_id, community_id in communities.items()],
    )


def upsert_label(conn, novel_id, label, drop_chapter=None, source="cold"):
    """label: 'like' | 'meh' | 'drop'. drop_chapter only makes sense for a
    'drop' recorded from source='read' -- a 'cold' judgment (never actually
    read) has no chapter to attach. One label per novel; re-labeling
    overwrites (upsert), same convention as upsert_metadata etc."""
    conn.execute(
        """
        INSERT INTO labels (novel_id, label, drop_chapter, source, labeled_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(novel_id) DO UPDATE SET
            label=excluded.label, drop_chapter=excluded.drop_chapter,
            source=excluded.source, labeled_at=excluded.labeled_at
        """,
        (novel_id, label, drop_chapter, source, now_iso()),
    )


def get_label(conn, novel_id):
    return conn.execute("SELECT * FROM labels WHERE novel_id = ?", (novel_id,)).fetchone()


def count_labels(conn):
    return conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0]


def label_counts_by_type(conn):
    """dict[label -> count], e.g. {"like": 40, "meh": 12, "drop": 30} -- for
    the labeling app's progress readout. Missing labels just aren't keys."""
    return dict(conn.execute("SELECT label, COUNT(*) FROM labels GROUP BY label").fetchall())


def iter_labeled_novel_ids(conn):
    return {row["novel_id"] for row in conn.execute("SELECT novel_id FROM labels")}


def delete_most_recent_label(conn):
    """Single-level undo: remove whichever label was written last. Returns
    the deleted novel_id, or None if there were no labels to undo."""
    row = conn.execute("SELECT novel_id FROM labels ORDER BY labeled_at DESC LIMIT 1").fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM labels WHERE novel_id = ?", (row["novel_id"],))
    return row["novel_id"]


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
    with_embedding = conn.execute(
        f"SELECT COUNT(*) FROM novels {where}{' AND' if where else 'WHERE'} "
        "candidate = 1 AND synopsis_embedding IS NOT NULL",
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
    nu_novels_listed = conn.execute("SELECT COUNT(*) FROM nu_novels").fetchone()[0]
    nu_novels_with_details = conn.execute(
        "SELECT COUNT(*) FROM nu_novels WHERE fetched_at IS NOT NULL"
    ).fetchone()[0]

    return {
        "total": total,
        "candidates": candidates,
        "candidates_with_metadata": with_metadata,
        "candidates_with_chapters": with_chapters,
        "candidates_with_embedding": with_embedding,
        "candidates_by_status": by_status,
        "candidates_by_nu_resolution": by_nu_resolution,
        "nu_novels_listed": nu_novels_listed,
        "nu_novels_with_details": nu_novels_with_details,
    }
