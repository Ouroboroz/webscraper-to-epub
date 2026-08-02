import pytest

from epub_scraper.dataspine_db import (get_novel, init_db, iter_candidates_missing_metadata,
                                        recompute_candidates, stats, upsert_catalog_entry,
                                        upsert_metadata)
from epub_scraper.profile import CatalogEntry, MetadataResult


def make_entry(url, chapters, status="Ongoing", title=None):
    return CatalogEntry(title=title or url, url=url, chapter_id=url.rsplit("/", 1)[-1],
                         chapters=chapters, status=status, updated_text="1 hour ago")


def test_upsert_catalog_entry_then_get_novel(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 120, title="A"), site_key="fanmtl")
    conn.commit()

    row = get_novel(conn, "fanmtl", "https://x/novel/a.html")
    assert row["title"] == "A"
    assert row["chapter_count"] == 120
    assert row["candidate"] == 0  # not yet computed


def test_upsert_catalog_entry_is_idempotent_upsert(db_path):
    conn = init_db(db_path)
    entry = make_entry("https://x/novel/a.html", 10, title="Old Title")
    upsert_catalog_entry(conn, entry, site_key="fanmtl")
    conn.commit()

    updated = make_entry("https://x/novel/a.html", 20, title="New Title")
    upsert_catalog_entry(conn, updated, site_key="fanmtl")
    conn.commit()

    rows = conn.execute("SELECT * FROM novels").fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "New Title"
    assert rows[0]["chapter_count"] == 20


def test_recompute_candidates_applies_threshold(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100), site_key="fanmtl")
    upsert_catalog_entry(conn, make_entry("https://x/novel/b.html", 10), site_key="fanmtl")
    conn.commit()

    recompute_candidates(conn, min_chapters=80, site_key="fanmtl")
    conn.commit()

    a = get_novel(conn, "fanmtl", "https://x/novel/a.html")
    b = get_novel(conn, "fanmtl", "https://x/novel/b.html")
    assert a["candidate"] == 1
    assert b["candidate"] == 0


def test_recompute_candidates_is_idempotent_on_threshold_change(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100), site_key="fanmtl")
    conn.commit()

    recompute_candidates(conn, min_chapters=80, site_key="fanmtl")
    conn.commit()
    assert get_novel(conn, "fanmtl", "https://x/novel/a.html")["candidate"] == 1

    recompute_candidates(conn, min_chapters=200, site_key="fanmtl")
    conn.commit()
    assert get_novel(conn, "fanmtl", "https://x/novel/a.html")["candidate"] == 0


def test_iter_candidates_missing_metadata_excludes_non_candidates_and_done(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100, title="A"), site_key="fanmtl")
    upsert_catalog_entry(conn, make_entry("https://x/novel/b.html", 10, title="B"), site_key="fanmtl")
    conn.commit()
    recompute_candidates(conn, min_chapters=80, site_key="fanmtl")
    conn.commit()

    pending = iter_candidates_missing_metadata(conn, "fanmtl")
    assert [row["title"] for row in pending] == ["A"]

    upsert_metadata(conn, "fanmtl", "https://x/novel/a.html",
                     MetadataResult(synopsis="S", genres=[], author=None,
                                     alt_title=None, status=None, rating=None))
    conn.commit()

    assert iter_candidates_missing_metadata(conn, "fanmtl") == []


def test_upsert_metadata_links_genres_as_tags(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100, title="A"), site_key="fanmtl")
    conn.commit()

    upsert_metadata(conn, "fanmtl", "https://x/novel/a.html",
                     MetadataResult(synopsis="S", genres=["Fantasy", "Action"],
                                     author="Someone", alt_title="Alt", status="Completed",
                                     rating="4.5"))
    conn.commit()

    novel = get_novel(conn, "fanmtl", "https://x/novel/a.html")
    assert novel["synopsis"] == "S"
    assert novel["alt_title"] == "Alt"
    assert novel["status"] == "Completed"

    tag_names = {row["name"] for row in conn.execute(
        "SELECT t.name FROM tags t JOIN novel_tags nt ON nt.tag_id = t.id "
        "WHERE nt.novel_id = ?", (novel["id"],))}
    assert tag_names == {"Fantasy", "Action"}


def test_upsert_metadata_raises_for_unknown_novel(db_path):
    conn = init_db(db_path)
    with pytest.raises(ValueError):
        upsert_metadata(conn, "fanmtl", "https://x/novel/missing.html",
                         MetadataResult(None, [], None, None, None, None))


def test_stats_counts(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100, status="Ongoing"),
                          site_key="fanmtl")
    upsert_catalog_entry(conn, make_entry("https://x/novel/b.html", 10, status="Ongoing"),
                          site_key="fanmtl")
    conn.commit()
    recompute_candidates(conn, min_chapters=80, site_key="fanmtl")
    conn.commit()

    summary = stats(conn, site_key="fanmtl")
    assert summary["total"] == 2
    assert summary["candidates"] == 1
    assert summary["candidates_with_metadata"] == 0
    assert summary["candidates_by_status"] == {"Ongoing": 1}
