import pytest

from epub_scraper.dataspine_db import (all_tags, count_labels, delete_most_recent_label,
                                        first_chapter_excerpt, get_next_page, get_novel,
                                        get_novel_by_id, init_db, iter_candidates_missing_chapters,
                                        iter_candidates_missing_embedding,
                                        iter_candidates_missing_metadata,
                                        iter_candidates_missing_nu_resolution, iter_embeddings,
                                        iter_labeled_novel_ids, iter_tag_cooccurrence,
                                        label_counts_by_type, recompute_candidates, set_next_page,
                                        stats, tags_for_novel, upsert_catalog_entry,
                                        upsert_chapters, upsert_embedding, upsert_label,
                                        upsert_metadata, upsert_nu_metadata,
                                        write_cluster_assignments, write_tag_communities)
from epub_scraper.novelupdates import NUSeriesMetadata
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


# -- Novel Updates enrichment columns/helpers ----------------------------------

def test_init_db_migration_is_idempotent_across_reopens(db_path):
    init_db(db_path)
    conn = init_db(db_path)  # reopening an existing file re-runs _ensure_column
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100), site_key="fanmtl")
    conn.commit()
    assert get_novel(conn, "fanmtl", "https://x/novel/a.html")["nu_resolution"] is None


def test_iter_candidates_missing_nu_resolution_excludes_resolved(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100, title="A"), site_key="fanmtl")
    upsert_catalog_entry(conn, make_entry("https://x/novel/b.html", 100, title="B"), site_key="fanmtl")
    conn.commit()
    recompute_candidates(conn, min_chapters=80, site_key="fanmtl")
    conn.commit()

    pending = iter_candidates_missing_nu_resolution(conn, "fanmtl")
    assert {row["title"] for row in pending} == {"A", "B"}

    upsert_nu_metadata(conn, "fanmtl", "https://x/novel/a.html", "no_candidates")
    conn.commit()

    pending = iter_candidates_missing_nu_resolution(conn, "fanmtl")
    assert [row["title"] for row in pending] == ["B"]


def test_upsert_nu_metadata_auto_fills_fields_and_links_tags(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100, title="A"), site_key="fanmtl")
    conn.commit()

    metadata = NUSeriesMetadata(
        url="https://nu/series/a/", title="A (NU Title)", associated_names=["Alt A"],
        genres=["Fantasy"], tags=["Reincarnation"], author="Some Author",
        translation_status="Ongoing", translation_groups=["Group One", "Group Two"],
        release_frequency="1/week", rating="4.5", votes="200")
    upsert_nu_metadata(conn, "fanmtl", "https://x/novel/a.html", "auto", metadata)
    conn.commit()

    novel = get_novel(conn, "fanmtl", "https://x/novel/a.html")
    assert novel["nu_url"] == "https://nu/series/a/"
    assert novel["nu_title"] == "A (NU Title)"
    assert novel["nu_author"] == "Some Author"
    assert novel["nu_status"] == "Ongoing"
    assert novel["nu_translation_groups"] == "Group One, Group Two"
    assert novel["nu_resolution"] == "auto"

    tag_names = {row["name"] for row in conn.execute(
        "SELECT t.name FROM tags t JOIN novel_tags nt ON nt.tag_id = t.id "
        "WHERE nt.novel_id = ?", (novel["id"],))}
    assert tag_names == {"Fantasy", "Reincarnation"}


def test_upsert_nu_metadata_ambiguous_only_stamps_resolution(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100, title="A"), site_key="fanmtl")
    conn.commit()

    upsert_nu_metadata(conn, "fanmtl", "https://x/novel/a.html", "ambiguous")
    conn.commit()

    novel = get_novel(conn, "fanmtl", "https://x/novel/a.html")
    assert novel["nu_resolution"] == "ambiguous"
    assert novel["nu_url"] is None


def test_upsert_nu_metadata_raises_for_unknown_novel(db_path):
    conn = init_db(db_path)
    with pytest.raises(ValueError):
        upsert_nu_metadata(conn, "fanmtl", "https://x/novel/missing.html", "no_candidates")


# -- crawl resume checkpoint ---------------------------------------------------

def test_get_next_page_defaults_to_zero_for_unseen_site(db_path):
    conn = init_db(db_path)
    assert get_next_page(conn, "fanmtl") == 0


def test_set_next_page_then_get_roundtrips(db_path):
    conn = init_db(db_path)
    set_next_page(conn, "fanmtl", 7)
    conn.commit()
    assert get_next_page(conn, "fanmtl") == 7


def test_set_next_page_upserts_not_duplicates(db_path):
    conn = init_db(db_path)
    set_next_page(conn, "fanmtl", 3)
    conn.commit()
    set_next_page(conn, "fanmtl", 9)
    conn.commit()
    assert get_next_page(conn, "fanmtl") == 9
    assert conn.execute("SELECT COUNT(*) FROM crawl_state").fetchone()[0] == 1


def test_set_next_page_is_per_site(db_path):
    conn = init_db(db_path)
    set_next_page(conn, "fanmtl", 5)
    conn.commit()
    assert get_next_page(conn, "other_site") == 0
    assert get_next_page(conn, "fanmtl") == 5


# -- chapter sampling -----------------------------------------------------------

def test_iter_candidates_missing_chapters_excludes_sampled(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100, title="A"), site_key="fanmtl")
    upsert_catalog_entry(conn, make_entry("https://x/novel/b.html", 100, title="B"), site_key="fanmtl")
    conn.commit()
    recompute_candidates(conn, min_chapters=80, site_key="fanmtl")
    conn.commit()

    pending = iter_candidates_missing_chapters(conn, "fanmtl")
    assert {row["title"] for row in pending} == {"A", "B"}

    novel_a = get_novel(conn, "fanmtl", "https://x/novel/a.html")
    upsert_chapters(conn, novel_a["id"], [(1, "Chapter 1", "Some prose.")])
    conn.commit()

    pending = iter_candidates_missing_chapters(conn, "fanmtl")
    assert [row["title"] for row in pending] == ["B"]


def test_upsert_chapters_stores_rows_and_stamps_sampled_at(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100, title="A"), site_key="fanmtl")
    conn.commit()
    novel = get_novel(conn, "fanmtl", "https://x/novel/a.html")

    upsert_chapters(conn, novel["id"], [
        (1, "Chapter 1", "First chapter prose."),
        (2, "Chapter 2", "Second chapter prose."),
    ])
    conn.commit()

    rows = conn.execute(
        "SELECT * FROM chapters WHERE novel_id = ? ORDER BY chapter_number", (novel["id"],)
    ).fetchall()
    assert [r["title"] for r in rows] == ["Chapter 1", "Chapter 2"]
    assert [r["body"] for r in rows] == ["First chapter prose.", "Second chapter prose."]
    assert get_novel(conn, "fanmtl", "https://x/novel/a.html")["chapters_sampled_at"] is not None


def test_upsert_chapters_stamps_sampled_at_even_with_partial_results(db_path):
    # A novel that only yields 1/5 chapters (others 404/decoy out) must still
    # be marked processed -- otherwise it'd be retried forever.
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100, title="A"), site_key="fanmtl")
    conn.commit()
    novel = get_novel(conn, "fanmtl", "https://x/novel/a.html")

    upsert_chapters(conn, novel["id"], [(1, "Chapter 1", "Only this one landed.")])
    conn.commit()

    assert get_novel(conn, "fanmtl", "https://x/novel/a.html")["chapters_sampled_at"] is not None
    assert iter_candidates_missing_chapters(conn, "fanmtl") == []


def test_upsert_chapters_is_idempotent_upsert(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100, title="A"), site_key="fanmtl")
    conn.commit()
    novel = get_novel(conn, "fanmtl", "https://x/novel/a.html")

    upsert_chapters(conn, novel["id"], [(1, "Chapter 1", "Old text.")])
    conn.commit()
    upsert_chapters(conn, novel["id"], [(1, "Chapter 1", "New text.")])
    conn.commit()

    rows = conn.execute("SELECT * FROM chapters WHERE novel_id = ?", (novel["id"],)).fetchall()
    assert len(rows) == 1
    assert rows[0]["body"] == "New text."


def test_stats_includes_chapters_progress(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100, title="A"), site_key="fanmtl")
    conn.commit()
    recompute_candidates(conn, min_chapters=80, site_key="fanmtl")
    conn.commit()
    novel = get_novel(conn, "fanmtl", "https://x/novel/a.html")
    upsert_chapters(conn, novel["id"], [(1, "Chapter 1", "Prose.")])
    conn.commit()

    summary = stats(conn, site_key="fanmtl")
    assert summary["candidates_with_chapters"] == 1


# -- corpus structure (Stage 1) -------------------------------------------------

def _fake_embedding_bytes(*values):
    # Tiny hand-built vectors stand in for a real BGE-M3 embedding -- these
    # tests exercise the DB round-trip, not sentence-transformers itself.
    import numpy as np
    return np.array(values, dtype=np.float32).tobytes()


def test_iter_candidates_missing_embedding_requires_synopsis_first(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100, title="A"), site_key="fanmtl")
    conn.commit()
    recompute_candidates(conn, min_chapters=80, site_key="fanmtl")
    conn.commit()

    # No synopsis yet -- not pending an embedding (nothing to embed).
    assert iter_candidates_missing_embedding(conn, "fanmtl") == []

    upsert_metadata(conn, "fanmtl", "https://x/novel/a.html",
                     MetadataResult(synopsis="S", genres=[], author=None,
                                     alt_title=None, status=None, rating=None))
    conn.commit()
    assert [row["title"] for row in iter_candidates_missing_embedding(conn, "fanmtl")] == ["A"]


def test_upsert_embedding_then_iter_roundtrips(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100, title="A"), site_key="fanmtl")
    conn.commit()
    recompute_candidates(conn, min_chapters=80, site_key="fanmtl")
    conn.commit()
    upsert_metadata(conn, "fanmtl", "https://x/novel/a.html",
                     MetadataResult(synopsis="S", genres=[], author=None,
                                     alt_title=None, status=None, rating=None))
    conn.commit()
    novel = get_novel(conn, "fanmtl", "https://x/novel/a.html")

    blob = _fake_embedding_bytes(1.0, 2.0, 3.0)
    upsert_embedding(conn, novel["id"], blob)
    conn.commit()

    assert iter_candidates_missing_embedding(conn, "fanmtl") == []
    pairs = iter_embeddings(conn, "fanmtl")
    assert pairs == [(novel["id"], blob)]


def test_write_cluster_assignments_updates_every_novel(db_path):
    conn = init_db(db_path)
    ids = []
    for i in range(3):
        upsert_catalog_entry(conn, make_entry(f"https://x/novel/{i}.html", 100, title=f"N{i}"),
                              site_key="fanmtl")
        conn.commit()
        ids.append(get_novel(conn, "fanmtl", f"https://x/novel/{i}.html")["id"])

    write_cluster_assignments(conn, {
        ids[0]: (0, 1.0, 2.0),
        ids[1]: (0, 1.1, 2.1),
        ids[2]: (-1, 5.0, 5.0),
    })
    conn.commit()

    rows = {r["id"]: r for r in conn.execute("SELECT * FROM novels")}
    assert rows[ids[0]]["cluster_id"] == 0
    assert rows[ids[0]]["umap_x"] == 1.0
    assert rows[ids[2]]["cluster_id"] == -1


def test_all_tags_and_write_tag_communities_roundtrip(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100, title="A"), site_key="fanmtl")
    conn.commit()
    upsert_metadata(conn, "fanmtl", "https://x/novel/a.html",
                     MetadataResult(synopsis="S", genres=["Fantasy", "Action"], author=None,
                                     alt_title=None, status=None, rating=None))
    conn.commit()

    tags = all_tags(conn)
    assert {row["name"] for row in tags} == {"Fantasy", "Action"}

    write_tag_communities(conn, {row["id"]: 0 for row in tags})
    conn.commit()

    community_ids = {row["community_id"] for row in conn.execute("SELECT community_id FROM tags")}
    assert community_ids == {0}


def test_stats_includes_embedding_progress(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100, title="A"), site_key="fanmtl")
    conn.commit()
    recompute_candidates(conn, min_chapters=80, site_key="fanmtl")
    conn.commit()
    upsert_metadata(conn, "fanmtl", "https://x/novel/a.html",
                     MetadataResult(synopsis="S", genres=[], author=None,
                                     alt_title=None, status=None, rating=None))
    conn.commit()
    novel = get_novel(conn, "fanmtl", "https://x/novel/a.html")
    upsert_embedding(conn, novel["id"], _fake_embedding_bytes(1.0, 2.0))
    conn.commit()

    summary = stats(conn, site_key="fanmtl")
    assert summary["candidates_with_embedding"] == 1


def test_iter_tag_cooccurrence_counts_shared_novels_without_self_pairs_or_duplicates(db_path):
    conn = init_db(db_path)
    # A+B co-occur on 2 novels, A+C on 1 novel, B+C never.
    for i, genres in enumerate([["A", "B"], ["A", "B"], ["A", "C"]]):
        url = f"https://x/novel/{i}.html"
        upsert_catalog_entry(conn, make_entry(url, 100, title=f"N{i}"), site_key="fanmtl")
        conn.commit()
        upsert_metadata(conn, "fanmtl", url,
                         MetadataResult(synopsis="S", genres=genres, author=None,
                                         alt_title=None, status=None, rating=None))
        conn.commit()

    tag_id = {row["name"]: row["id"] for row in all_tags(conn)}
    pairs = {(row["tag_id_a"], row["tag_id_b"]): row["weight"] for row in iter_tag_cooccurrence(conn)}

    a, b, c = tag_id["A"], tag_id["B"], tag_id["C"]
    expected_key = (min(a, b), max(a, b))
    assert pairs[expected_key] == 2
    assert pairs[(min(a, c), max(a, c))] == 1
    assert (min(b, c), max(b, c)) not in pairs  # never co-occur
    assert (a, a) not in pairs  # no self-pairs


def test_stats_includes_nu_resolution_breakdown(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100), site_key="fanmtl")
    conn.commit()
    recompute_candidates(conn, min_chapters=80, site_key="fanmtl")
    conn.commit()
    upsert_nu_metadata(conn, "fanmtl", "https://x/novel/a.html", "no_candidates")
    conn.commit()

    summary = stats(conn, site_key="fanmtl")
    assert summary["candidates_by_nu_resolution"] == {"no_candidates": 1}


# -- labeling (Stage 2) ----------------------------------------------------------

def test_init_db_sets_wal_journal_mode(db_path):
    conn = init_db(db_path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_init_db_memory_skips_wal(db_path):
    conn = init_db(":memory:")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "memory"


def test_get_novel_by_id(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100, title="A"), site_key="fanmtl")
    conn.commit()
    novel = get_novel(conn, "fanmtl", "https://x/novel/a.html")
    assert get_novel_by_id(conn, novel["id"])["title"] == "A"
    assert get_novel_by_id(conn, 99999) is None


def test_upsert_label_then_get_roundtrips(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100, title="A"), site_key="fanmtl")
    conn.commit()
    novel = get_novel(conn, "fanmtl", "https://x/novel/a.html")

    upsert_label(conn, novel["id"], "like", source="cold")
    conn.commit()

    label = get_label(conn, novel["id"]) if False else conn.execute(
        "SELECT * FROM labels WHERE novel_id = ?", (novel["id"],)).fetchone()
    assert label["label"] == "like"
    assert label["drop_chapter"] is None
    assert label["source"] == "cold"


def test_upsert_label_overwrites_on_relabel(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100, title="A"), site_key="fanmtl")
    conn.commit()
    novel = get_novel(conn, "fanmtl", "https://x/novel/a.html")

    upsert_label(conn, novel["id"], "like", source="cold")
    conn.commit()
    upsert_label(conn, novel["id"], "drop", drop_chapter=7, source="read")
    conn.commit()

    rows = conn.execute("SELECT * FROM labels").fetchall()
    assert len(rows) == 1
    assert rows[0]["label"] == "drop"
    assert rows[0]["drop_chapter"] == 7
    assert rows[0]["source"] == "read"


def test_count_labels_and_label_counts_by_type(db_path):
    conn = init_db(db_path)
    for i, label in enumerate(["like", "like", "meh", "drop"]):
        upsert_catalog_entry(conn, make_entry(f"https://x/novel/{i}.html", 100, title=f"N{i}"),
                              site_key="fanmtl")
        conn.commit()
        novel = get_novel(conn, "fanmtl", f"https://x/novel/{i}.html")
        upsert_label(conn, novel["id"], label, source="cold")
        conn.commit()

    assert count_labels(conn) == 4
    assert label_counts_by_type(conn) == {"like": 2, "meh": 1, "drop": 1}


def test_iter_labeled_novel_ids(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100, title="A"), site_key="fanmtl")
    upsert_catalog_entry(conn, make_entry("https://x/novel/b.html", 100, title="B"), site_key="fanmtl")
    conn.commit()
    a = get_novel(conn, "fanmtl", "https://x/novel/a.html")
    upsert_label(conn, a["id"], "like", source="cold")
    conn.commit()

    assert iter_labeled_novel_ids(conn) == {a["id"]}


def test_delete_most_recent_label_removes_the_last_one_written(db_path):
    conn = init_db(db_path)
    ids = []
    for i in range(3):
        upsert_catalog_entry(conn, make_entry(f"https://x/novel/{i}.html", 100, title=f"N{i}"),
                              site_key="fanmtl")
        conn.commit()
        novel = get_novel(conn, "fanmtl", f"https://x/novel/{i}.html")
        upsert_label(conn, novel["id"], "like", source="cold")
        conn.commit()
        ids.append(novel["id"])

    deleted = delete_most_recent_label(conn)
    conn.commit()

    assert deleted == ids[-1]
    assert count_labels(conn) == 2
    assert iter_labeled_novel_ids(conn) == set(ids[:-1])


def test_delete_most_recent_label_returns_none_when_empty(db_path):
    conn = init_db(db_path)
    assert delete_most_recent_label(conn) is None


def test_tags_for_novel(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100, title="A"), site_key="fanmtl")
    conn.commit()
    upsert_metadata(conn, "fanmtl", "https://x/novel/a.html",
                     MetadataResult(synopsis="S", genres=["Fantasy", "Action"], author=None,
                                     alt_title=None, status=None, rating=None))
    conn.commit()
    novel = get_novel(conn, "fanmtl", "https://x/novel/a.html")
    assert tags_for_novel(conn, novel["id"]) == ["Action", "Fantasy"]


def test_first_chapter_excerpt_present_absent_and_truncated(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/novel/a.html", 100, title="A"), site_key="fanmtl")
    upsert_catalog_entry(conn, make_entry("https://x/novel/b.html", 100, title="B"), site_key="fanmtl")
    conn.commit()
    a = get_novel(conn, "fanmtl", "https://x/novel/a.html")
    b = get_novel(conn, "fanmtl", "https://x/novel/b.html")

    assert first_chapter_excerpt(conn, b["id"]) is None  # no chapters at all

    upsert_chapters(conn, a["id"], [(1, "Chapter 1", "Short opening.")])
    conn.commit()
    assert first_chapter_excerpt(conn, a["id"]) == "Short opening."

    long_body = "x" * 1000
    upsert_chapters(conn, a["id"], [(1, "Chapter 1", long_body)])
    conn.commit()
    excerpt = first_chapter_excerpt(conn, a["id"], max_chars=600)
    assert len(excerpt) == 601  # 600 chars + the truncation ellipsis
    assert excerpt.endswith("…")
