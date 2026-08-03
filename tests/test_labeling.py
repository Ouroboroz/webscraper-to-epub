from epub_scraper.dataspine_db import (get_label, get_novel, init_db, recompute_candidates,
                                        upsert_catalog_entry, upsert_embedding, upsert_label)
from epub_scraper.labeling import (build_seed_queue, next_review_candidate, rank_by_uncertainty,
                                    record_label, search_novels, train_uncertainty_model)
from test_dataspine_db import make_entry

SITE = "fanmtl"


def _embedding_bytes(*values):
    # Same hand-built-vector convention as test_dataspine_db.py's
    # _fake_embedding_bytes -- exercises the DB/model round-trip, not a real
    # embedding model.
    import numpy as np
    return np.array(values, dtype=np.float32).tobytes()


def _add_candidate(conn, url, title, cluster_id=None, embedding=None):
    """A candidate novel (chapter_count high enough to pass recompute_candidates),
    optionally clustered (for the seed-queue tests) and/or embedded (for the
    uncertainty-ranking tests)."""
    upsert_catalog_entry(conn, make_entry(url, 100, title=title), site_key=SITE)
    conn.commit()
    recompute_candidates(conn, min_chapters=80, site_key=SITE)
    conn.commit()
    novel = get_novel(conn, SITE, url)
    if cluster_id is not None:
        conn.execute("UPDATE novels SET cluster_id = ? WHERE id = ?", (cluster_id, novel["id"]))
    if embedding is not None:
        upsert_embedding(conn, novel["id"], _embedding_bytes(*embedding))
    conn.commit()
    return novel["id"]


def _add_labeled_novel(conn, url, title, embedding, label):
    """A novel with an embedding and an existing label -- training data for
    train_uncertainty_model. Deliberately not run through
    recompute_candidates/cluster_id: already-labeled novels are excluded from
    the seed queue and uncertainty ranking by id regardless of those flags,
    so leaving them unset keeps these tests from depending on that exclusion
    logic too."""
    upsert_catalog_entry(conn, make_entry(url, 100, title=title), site_key=SITE)
    conn.commit()
    novel = get_novel(conn, SITE, url)
    upsert_embedding(conn, novel["id"], _embedding_bytes(*embedding))
    upsert_label(conn, novel["id"], label, source="cold")
    conn.commit()
    return novel["id"]


def _stub_model():
    """A fake model whose predict_proba treats an embedding's first element
    as P(class=1) directly -- lets rank_by_uncertainty's own sort/exclude
    logic be tested without depending on where a real LogisticRegression fit
    happens to land."""
    import numpy as np

    class _Stub:
        def predict_proba(self, X):
            p = X[:, 0]
            return np.stack([1 - p, p], axis=1)

    return _Stub()


# -- search_novels -----------------------------------------------------------------

def test_search_novels_matches_title_substring_case_insensitively(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/a.html", 100, title="Cultivation Chat Group"),
                          site_key=SITE)
    upsert_catalog_entry(conn, make_entry("https://x/b.html", 100, title="Reverend Insanity"),
                          site_key=SITE)
    conn.commit()

    hits = search_novels(conn, SITE, "cultivation")
    assert [row["title"] for row in hits] == ["Cultivation Chat Group"]


def test_search_novels_matches_alt_title_and_nu_title(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/a.html", 100, title="Book One"), site_key=SITE)
    upsert_catalog_entry(conn, make_entry("https://x/b.html", 100, title="Book Two"), site_key=SITE)
    conn.commit()
    a = get_novel(conn, SITE, "https://x/a.html")
    b = get_novel(conn, SITE, "https://x/b.html")
    conn.execute("UPDATE novels SET alt_title = ? WHERE id = ?", ("Zephyr Saga", a["id"]))
    conn.execute("UPDATE novels SET nu_title = ? WHERE id = ?", ("Nimbus Chronicle", b["id"]))
    conn.commit()

    assert [row["id"] for row in search_novels(conn, SITE, "Zephyr")] == [a["id"]]
    assert [row["id"] for row in search_novels(conn, SITE, "Nimbus")] == [b["id"]]


def test_search_novels_respects_limit(db_path):
    conn = init_db(db_path)
    for i in range(5):
        upsert_catalog_entry(conn, make_entry(f"https://x/{i}.html", 100, title=f"Novel {i}"),
                              site_key=SITE)
    conn.commit()

    assert len(search_novels(conn, SITE, "Novel", limit=3)) == 3


# -- build_seed_queue --------------------------------------------------------------

def test_build_seed_queue_interleaves_round_robin_across_clusters(db_path):
    conn = init_db(db_path)
    a1 = _add_candidate(conn, "https://x/a1.html", "A1", cluster_id=-1)
    a2 = _add_candidate(conn, "https://x/a2.html", "A2", cluster_id=-1)
    b1 = _add_candidate(conn, "https://x/b1.html", "B1", cluster_id=0)
    b2 = _add_candidate(conn, "https://x/b2.html", "B2", cluster_id=0)
    b3 = _add_candidate(conn, "https://x/b3.html", "B3", cluster_id=0)
    c1 = _add_candidate(conn, "https://x/c1.html", "C1", cluster_id=1)

    # Round-robin, not "drain cluster -1, then 0, then 1": one from each
    # cluster in turn, shorter clusters simply drop out of later rounds.
    assert build_seed_queue(conn, SITE) == [a1, b1, c1, a2, b2, b3]


def test_build_seed_queue_excludes_already_labeled_novels(db_path):
    conn = init_db(db_path)
    a = _add_candidate(conn, "https://x/a.html", "A", cluster_id=0)
    b = _add_candidate(conn, "https://x/b.html", "B", cluster_id=0)
    upsert_label(conn, a, "like", source="cold")
    conn.commit()

    assert build_seed_queue(conn, SITE) == [b]


def test_build_seed_queue_respects_per_cluster_cap(db_path):
    conn = init_db(db_path)
    ids = [_add_candidate(conn, f"https://x/{i}.html", f"N{i}", cluster_id=0) for i in range(5)]

    assert build_seed_queue(conn, SITE, per_cluster=2) == ids[:2]


def test_build_seed_queue_excludes_non_candidates_and_unclustered(db_path):
    conn = init_db(db_path)
    good = _add_candidate(conn, "https://x/good.html", "Good", cluster_id=0)

    # Not a candidate (too few chapters), even though it's clustered.
    upsert_catalog_entry(conn, make_entry("https://x/small.html", 10, title="Small"), site_key=SITE)
    conn.commit()
    recompute_candidates(conn, min_chapters=80, site_key=SITE)
    conn.commit()
    small = get_novel(conn, SITE, "https://x/small.html")
    conn.execute("UPDATE novels SET cluster_id = 0 WHERE id = ?", (small["id"],))
    conn.commit()

    # A candidate, but not clustered yet.
    _add_candidate(conn, "https://x/unclustered.html", "Unclustered", cluster_id=None)

    assert build_seed_queue(conn, SITE) == [good]


# -- train_uncertainty_model --------------------------------------------------------

def test_train_uncertainty_model_returns_none_with_no_labels(db_path):
    conn = init_db(db_path)
    assert train_uncertainty_model(conn, SITE) is None


def test_train_uncertainty_model_returns_none_with_one_label(db_path):
    conn = init_db(db_path)
    _add_labeled_novel(conn, "https://x/a.html", "A", [1.0, 0.0], "like")

    assert train_uncertainty_model(conn, SITE) is None


def test_train_uncertainty_model_returns_none_when_all_labels_are_like(db_path):
    conn = init_db(db_path)
    _add_labeled_novel(conn, "https://x/a.html", "A", [1.0, 0.0], "like")
    _add_labeled_novel(conn, "https://x/b.html", "B", [0.0, 1.0], "like")

    assert train_uncertainty_model(conn, SITE) is None


def test_train_uncertainty_model_fits_once_labels_span_both_classes(db_path):
    conn = init_db(db_path)
    _add_labeled_novel(conn, "https://x/a.html", "A", [5.0, 5.0], "like")
    _add_labeled_novel(conn, "https://x/b.html", "B", [-5.0, -5.0], "meh")

    model = train_uncertainty_model(conn, SITE)
    assert model is not None

    import numpy as np
    # Like=1 / Meh=0 binarization: the "like" novel's own embedding should
    # come back on the Like side of 0.5, the "meh" novel's on the other.
    proba_like = model.predict_proba(np.array([[5.0, 5.0]], dtype=np.float32))[0][1]
    proba_meh = model.predict_proba(np.array([[-5.0, -5.0]], dtype=np.float32))[0][1]
    assert proba_like > 0.5
    assert proba_meh < 0.5


# -- rank_by_uncertainty ------------------------------------------------------------

def test_rank_by_uncertainty_orders_most_uncertain_first(db_path):
    conn = init_db(db_path)
    id_50 = _add_candidate(conn, "https://x/50.html", "P50", embedding=[0.5])
    id_60 = _add_candidate(conn, "https://x/60.html", "P60", embedding=[0.6])
    id_80 = _add_candidate(conn, "https://x/80.html", "P80", embedding=[0.8])
    id_95 = _add_candidate(conn, "https://x/95.html", "P95", embedding=[0.95])

    ranked = rank_by_uncertainty(_stub_model(), conn, SITE, exclude_ids=set())

    assert ranked == [id_50, id_60, id_80, id_95]


def test_rank_by_uncertainty_excludes_given_ids(db_path):
    conn = init_db(db_path)
    id_50 = _add_candidate(conn, "https://x/50.html", "P50", embedding=[0.5])
    id_60 = _add_candidate(conn, "https://x/60.html", "P60", embedding=[0.6])

    ranked = rank_by_uncertainty(_stub_model(), conn, SITE, exclude_ids={id_50})

    assert ranked == [id_60]


def test_rank_by_uncertainty_empty_when_no_candidates_left(db_path):
    conn = init_db(db_path)
    id_50 = _add_candidate(conn, "https://x/50.html", "P50", embedding=[0.5])

    assert rank_by_uncertainty(_stub_model(), conn, SITE, exclude_ids={id_50}) == []


def test_rank_by_uncertainty_empty_when_no_embeddings_at_all(db_path):
    conn = init_db(db_path)
    assert rank_by_uncertainty(_stub_model(), conn, SITE, exclude_ids=set()) == []


# -- next_review_candidate -----------------------------------------------------------

def test_next_review_candidate_drains_seed_queue_before_using_model(db_path):
    conn = init_db(db_path)
    # Two labels spanning both classes -- a model *could* be fit already --
    # but the seed queue isn't empty yet, so it should still win.
    _add_labeled_novel(conn, "https://x/like.html", "Liked", [10.0, 10.0], "like")
    _add_labeled_novel(conn, "https://x/meh.html", "Mehed", [-10.0, -10.0], "meh")
    seed_id = _add_candidate(conn, "https://x/seed.html", "Seed", cluster_id=0)

    candidate = next_review_candidate(conn, SITE)

    assert candidate["id"] == seed_id


def test_next_review_candidate_respects_skipped_ids_in_seed_queue(db_path):
    conn = init_db(db_path)
    a = _add_candidate(conn, "https://x/a.html", "A", cluster_id=0)
    b = _add_candidate(conn, "https://x/b.html", "B", cluster_id=0)

    candidate = next_review_candidate(conn, SITE, skipped_ids={a})

    assert candidate["id"] == b


def test_next_review_candidate_falls_back_to_uncertainty_model_once_seed_queue_exhausted(db_path):
    conn = init_db(db_path)
    _add_labeled_novel(conn, "https://x/like.html", "Liked", [10.0, 10.0], "like")
    _add_labeled_novel(conn, "https://x/meh.html", "Mehed", [-10.0, -10.0], "meh")
    # No cluster_id on either -- seed queue is empty, so this must fall
    # through to the uncertainty model. [0, 0] sits equidistant between the
    # two labeled points (P(like) ~= 0.5); [10, 10] sits right on the "like"
    # training point (P(like) ~= 0.99) -- the equidistant one is more
    # uncertain and should be picked.
    uncertain_id = _add_candidate(conn, "https://x/uncertain.html", "Uncertain", embedding=[0.0, 0.0])
    _add_candidate(conn, "https://x/confident.html", "Confident", embedding=[10.0, 10.0])

    candidate = next_review_candidate(conn, SITE)

    assert candidate["id"] == uncertain_id


def test_next_review_candidate_respects_skipped_ids_in_model_fallback(db_path):
    conn = init_db(db_path)
    _add_labeled_novel(conn, "https://x/like.html", "Liked", [10.0, 10.0], "like")
    _add_labeled_novel(conn, "https://x/meh.html", "Mehed", [-10.0, -10.0], "meh")
    uncertain_id = _add_candidate(conn, "https://x/uncertain.html", "Uncertain", embedding=[0.0, 0.0])
    other_id = _add_candidate(conn, "https://x/other.html", "Other", embedding=[0.1, 0.1])

    candidate = next_review_candidate(conn, SITE, skipped_ids={uncertain_id})

    assert candidate["id"] == other_id


def test_next_review_candidate_returns_none_when_nothing_left(db_path):
    conn = init_db(db_path)
    assert next_review_candidate(conn, SITE) is None


# -- record_label ---------------------------------------------------------------

def test_record_label_writes_through_upsert_label_and_commits(db_path):
    conn = init_db(db_path)
    upsert_catalog_entry(conn, make_entry("https://x/a.html", 100, title="A"), site_key=SITE)
    conn.commit()
    novel = get_novel(conn, SITE, "https://x/a.html")

    record_label(conn, novel["id"], "drop", drop_chapter=12, source="read")

    # A fresh connection to the same file proves this was actually
    # committed, not just pending in conn's own transaction.
    conn2 = init_db(db_path)
    label = get_label(conn2, novel["id"])
    assert label["label"] == "drop"
    assert label["drop_chapter"] == 12
    assert label["source"] == "read"
