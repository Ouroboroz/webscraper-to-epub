import pytest

from epub_scraper.dataspine_db import (all_tags, get_novel, init_db, recompute_candidates,
                                        upsert_catalog_entry, upsert_embedding, upsert_metadata)
from epub_scraper.profile import CatalogEntry, MetadataResult

umap = pytest.importorskip("umap")
hdbscan = pytest.importorskip("hdbscan")
igraph = pytest.importorskip("igraph")
leidenalg = pytest.importorskip("leidenalg")

from epub_scraper import corpus_structure  # noqa: E402  (after importorskip gating)


def make_entry(url, chapters, title=None):
    return CatalogEntry(title=title or url, url=url, chapter_id=url.rsplit("/", 1)[-1],
                         chapters=chapters, status="Ongoing", updated_text="1 hour ago")


def _add_embedded_novel(conn, url, title, embedding):
    import numpy as np
    upsert_catalog_entry(conn, make_entry(url, 100, title=title), site_key="fanmtl")
    conn.commit()
    recompute_candidates(conn, min_chapters=80, site_key="fanmtl")
    conn.commit()
    upsert_metadata(conn, "fanmtl", url,
                     MetadataResult(synopsis="S", genres=[], author=None,
                                     alt_title=None, status=None, rating=None))
    conn.commit()
    novel = get_novel(conn, "fanmtl", url)
    upsert_embedding(conn, novel["id"], np.asarray(embedding, dtype=np.float32).tobytes())
    conn.commit()
    return novel["id"]


def _add_novel_with_tags(conn, url, title, tag_names):
    upsert_catalog_entry(conn, make_entry(url, 100, title=title), site_key="fanmtl")
    conn.commit()
    upsert_metadata(conn, "fanmtl", url,
                     MetadataResult(synopsis="S", genres=tag_names, author=None,
                                     alt_title=None, status=None, rating=None))
    conn.commit()


# -- cluster_corpus ---------------------------------------------------------------

def test_cluster_corpus_no_embeddings_returns_zero(db_path):
    conn = init_db(db_path)
    assert corpus_structure.cluster_corpus(conn, "fanmtl") == (0, 0, 0)


def test_cluster_corpus_separates_two_well_separated_blobs(db_path):
    import numpy as np

    conn = init_db(db_path)
    rng = np.random.default_rng(0)
    dim = 16

    blob_a_ids = [_add_embedded_novel(conn, f"https://x/a{i}.html", f"A{i}",
                                       rng.normal(loc=0.0, scale=0.05, size=dim))
                  for i in range(15)]
    blob_b_ids = [_add_embedded_novel(conn, f"https://x/b{i}.html", f"B{i}",
                                       rng.normal(loc=20.0, scale=0.05, size=dim))
                  for i in range(15)]

    n, n_clusters, n_outliers = corpus_structure.cluster_corpus(
        conn, "fanmtl", umap_dims=4, min_cluster_size=3)

    assert n == 30
    assert n_clusters >= 1

    rows = {r["id"]: r for r in conn.execute("SELECT id, cluster_id, umap_x, umap_y FROM novels")}
    for novel_id in blob_a_ids + blob_b_ids:
        assert rows[novel_id]["cluster_id"] is not None
        assert rows[novel_id]["umap_x"] is not None
        assert rows[novel_id]["umap_y"] is not None

    # The two blobs are 400 std-devs apart -- they must not collapse into one
    # cluster, whatever the exact labels/outlier count HDBSCAN lands on.
    a_clusters = {rows[i]["cluster_id"] for i in blob_a_ids if rows[i]["cluster_id"] != -1}
    b_clusters = {rows[i]["cluster_id"] for i in blob_b_ids if rows[i]["cluster_id"] != -1}
    assert a_clusters, "blob A collapsed entirely to outliers"
    assert b_clusters, "blob B collapsed entirely to outliers"
    assert a_clusters.isdisjoint(b_clusters)


def test_cluster_corpus_recovers_a_small_niche_hiding_in_the_outliers(db_path):
    # Mirrors what happened on the real corpus (2026-08-03): a small, tight
    # niche group that's too small to compete against a big dense cluster in
    # the FULL-corpus pass, but forms an obvious cluster once whatever's left
    # as -1 gets a second look on its own.
    import numpy as np

    conn = init_db(db_path)
    rng = np.random.default_rng(0)
    dim = 16

    big_ids = [_add_embedded_novel(conn, f"https://x/big{i}.html", f"Big{i}",
                                    rng.normal(loc=0.0, scale=0.05, size=dim))
               for i in range(30)]
    niche_ids = [_add_embedded_novel(conn, f"https://x/niche{i}.html", f"Niche{i}",
                                      rng.normal(loc=20.0, scale=0.05, size=dim))
                 for i in range(8)]
    scattered_ids = [_add_embedded_novel(conn, f"https://x/scat{i}.html", f"Scat{i}",
                                          rng.normal(loc=-20.0 * (i + 1), scale=0.01, size=dim))
                      for i in range(4)]

    n, n_clusters, n_outliers = corpus_structure.cluster_corpus(
        conn, "fanmtl", umap_dims=4, min_cluster_size=10,
        outlier_min_cluster_size=3, outlier_max_cluster_fraction=0.5)

    assert n == 30 + 8 + 4
    rows = {r["id"]: r["cluster_id"] for r in conn.execute("SELECT id, cluster_id FROM novels")}

    # The niche group (too small for the main min_cluster_size=10 pass) was
    # left as -1 by the main pass but must be recovered as its own cluster,
    # distinct from the big cluster, by the second pass.
    big_clusters = {rows[i] for i in big_ids if rows[i] != -1}
    niche_clusters = {rows[i] for i in niche_ids}
    assert -1 not in niche_clusters, "the niche group should have been recovered, not left as outliers"
    assert len(niche_clusters) == 1
    assert niche_clusters.isdisjoint(big_clusters)

    # The truly one-off scattered novels (nothing near them, even amongst
    # each other) must stay outliers -- the second pass shouldn't force
    # everything into some cluster.
    for i in scattered_ids:
        assert rows[i] == -1


def test_recluster_outliers_keeps_small_sub_cluster_but_discards_oversized_one():
    import numpy as np

    from epub_scraper.corpus_structure import _recluster_outliers

    rng = np.random.default_rng(0)
    dim = 16
    # Same well-separated-blobs recipe already proven reliable elsewhere in
    # this file. Within the outlier set: a small sub-group (4 points, 20% of
    # the 20 outliers) that should be recovered as real structure, and a
    # bigger one (16 points, 80%) standing in for the density-chained
    # mega-cluster artifact seen on the real corpus -- max_cluster_fraction
    # (0.5) sits between the two, so exactly one should survive.
    main_pass = rng.normal(loc=-40.0, scale=0.05, size=(5, dim))
    small_sub = rng.normal(loc=0.0, scale=0.05, size=(4, dim))
    big_sub = rng.normal(loc=20.0, scale=0.05, size=(16, dim))
    reduced = np.vstack([main_pass, small_sub, big_sub])
    cluster_ids = np.array([0] * 5 + [-1] * 20)

    result = _recluster_outliers(reduced, cluster_ids, min_cluster_size=2, max_cluster_fraction=0.5)

    assert (result[:5] == 0).all()  # the main pass's assignments are untouched
    small_result = result[5:9]
    big_result = result[9:]
    assert (small_result != -1).all(), "the small real sub-cluster should have been recovered"
    assert len(set(small_result.tolist())) == 1
    assert small_result[0] != 0  # a new ID, not colliding with the main pass's cluster 0
    assert (big_result == -1).all(), "the oversized sub-cluster should be treated as an artifact"


def test_recluster_outliers_noop_when_too_few_outliers_to_bother():
    import numpy as np

    from epub_scraper.corpus_structure import _recluster_outliers

    cluster_ids = np.array([0, 0, 0, -1, -1])  # only 2 outliers, min_cluster_size=15
    result = _recluster_outliers(np.zeros((5, 3)), cluster_ids, min_cluster_size=15)

    assert (result == cluster_ids).all()


# -- build_tag_communities ---------------------------------------------------------

def test_build_tag_communities_no_tags_returns_zero(db_path):
    conn = init_db(db_path)
    assert corpus_structure.build_tag_communities(conn) == (0, 0)


def test_build_tag_communities_separates_disjoint_tag_groups(db_path):
    conn = init_db(db_path)
    # Group 1 always co-occurs together; group 2 always co-occurs together;
    # zero edges between the groups -- two disconnected graph components,
    # which any community-detection algorithm must keep separate.
    for i in range(3):
        _add_novel_with_tags(conn, f"https://x/g1-{i}.html", f"G1-{i}", ["Cultivation", "Regression", "Revenge"])
    for i in range(3):
        _add_novel_with_tags(conn, f"https://x/g2-{i}.html", f"G2-{i}", ["LitRPG", "System", "Dungeon"])

    n_tags, n_communities = corpus_structure.build_tag_communities(conn)

    assert n_tags == 6
    assert n_communities == 2

    community_by_name = {row["name"]: row["community_id"] for row in
                          conn.execute("SELECT name, community_id FROM tags")}
    group1 = {community_by_name[n] for n in ("Cultivation", "Regression", "Revenge")}
    group2 = {community_by_name[n] for n in ("LitRPG", "System", "Dungeon")}
    assert len(group1) == 1
    assert len(group2) == 1
    assert group1 != group2
