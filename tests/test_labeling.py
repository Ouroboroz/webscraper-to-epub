from epub_scraper.dataspine_db import (get_label, get_novel, init_db, recompute_candidates,
                                        upsert_catalog_entry, upsert_embedding, upsert_label)
from epub_scraper.labeling import (_interleave_proportionally, build_seed_queue,
                                    next_review_candidate, rank_by_uncertainty, record_label,
                                    search_novels, train_uncertainty_model)
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


def _add_labeled_novel(conn, url, title, embedding, label, source="cold"):
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
    upsert_label(conn, novel["id"], label, source=source)
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


# -- _interleave_proportionally ----------------------------------------------------

def test_interleave_proportionally_spreads_each_bucket_by_its_own_density(db_path):
    # bucket0 (4 items) at positions .25/.5/.75/1.0, bucket1 (2 items) at
    # .5/1.0 -- ties broken by bucket order. B1 lands in the MIDDLE, not
    # bunched at the start or the end, proving this isn't plain round-robin
    # (which would give exactly one full round of [A,B] before any bucket
    # gets a second item) or drain-then-next (which would put both B's
    # together at one end).
    result = _interleave_proportionally([["A1", "A2", "A3", "A4"], ["B1", "B2"]])
    assert result == ["A1", "A2", "B1", "A3", "A4", "B2"]


def test_interleave_proportionally_skips_empty_buckets(db_path):
    assert _interleave_proportionally([[], ["A1"], []]) == ["A1"]


def test_interleave_proportionally_empty_input(db_path):
    assert _interleave_proportionally([]) == []


# -- build_seed_queue --------------------------------------------------------------

def test_build_seed_queue_weights_outliers_by_their_share_of_the_corpus(db_path):
    # 2026-08-03 finding: outliers (-1) were 56.6% of the real corpus but,
    # under the old equal-weight-per-bucket design, only ever got ~4% of a
    # 200-label session's slots -- the numeric majority of what Stage 3 will
    # actually have to score got almost no direct labeling coverage. Here:
    # 4 outliers / 8 total candidates = 50%, total_target=8 -> outlier_quota
    # should be round(8 * 4/8) = 4, i.e. ALL of them, not capped down to
    # some small fixed per-cluster number.
    conn = init_db(db_path)
    outlier_ids = {_add_candidate(conn, f"https://x/out{i}.html", f"Out{i}", cluster_id=-1)
                   for i in range(4)}
    cluster_a_ids = {_add_candidate(conn, f"https://x/a{i}.html", f"A{i}", cluster_id=0)
                      for i in range(2)}
    cluster_b_ids = {_add_candidate(conn, f"https://x/b{i}.html", f"B{i}", cluster_id=1)
                      for i in range(2)}

    queue = build_seed_queue(conn, SITE, total_target=8)

    assert set(queue) == outlier_ids | cluster_a_ids | cluster_b_ids
    assert sum(1 for n in queue if n in outlier_ids) == 4
    assert sum(1 for n in queue if n in cluster_a_ids) == 2
    assert sum(1 for n in queue if n in cluster_b_ids) == 2


def test_build_seed_queue_gives_every_real_cluster_at_least_one_slot(db_path):
    # A tight total_target split across many real clusters could round down
    # to 0 per cluster -- max(1, ...) guarantees every theme still gets
    # touched, which was this queue's original whole purpose.
    conn = init_db(db_path)
    ids = [_add_candidate(conn, f"https://x/{i}.html", f"N{i}", cluster_id=i) for i in range(20)]

    queue = build_seed_queue(conn, SITE, total_target=1)

    assert set(queue) == set(ids)


def test_build_seed_queue_excludes_already_labeled_novels(db_path):
    conn = init_db(db_path)
    a = _add_candidate(conn, "https://x/a.html", "A", cluster_id=0)
    b = _add_candidate(conn, "https://x/b.html", "B", cluster_id=0)
    upsert_label(conn, a, "like", source="cold")
    conn.commit()

    assert build_seed_queue(conn, SITE) == [b]


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


def test_build_seed_queue_empty_when_no_candidates(db_path):
    conn = init_db(db_path)
    assert build_seed_queue(conn, SITE) == []


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


def test_train_uncertainty_model_treats_love_and_obsessed_as_positive(db_path):
    # POSITIVE_LABELS = {like, love, obsessed} -- all three should land on
    # the same side of the binarization as 'like', not just 'like' itself.
    conn = init_db(db_path)
    _add_labeled_novel(conn, "https://x/love.html", "Love", [5.0, 5.0], "love")
    _add_labeled_novel(conn, "https://x/obsessed.html", "Obsessed", [4.9, 4.9], "obsessed")
    _add_labeled_novel(conn, "https://x/skip.html", "Skip", [-5.0, -5.0], "skip")
    _add_labeled_novel(conn, "https://x/drop.html", "Drop", [-4.9, -4.9], "drop")

    import numpy as np
    model = train_uncertainty_model(conn, SITE)
    assert model is not None
    assert model.predict_proba(np.array([[5.0, 5.0]], dtype=np.float32))[0][1] > 0.5
    assert model.predict_proba(np.array([[-5.0, -5.0]], dtype=np.float32))[0][1] < 0.5


def test_train_uncertainty_model_weights_read_labels_above_cold_ones(db_path):
    # A single 'read' (actually finished, real ground truth) "like" sits
    # right in the middle of a cluster of 'cold' (synopsis-only guess)
    # "meh" labels -- a real disagreement between weak-but-plentiful signal
    # and strong-but-single signal. At the default weighting the read label
    # should be able to outvote the surrounding cold ones for its own point;
    # with no extra weight at all it can't.
    conn = init_db(db_path)
    for i, coord in enumerate([(-5, -5), (-4.8, -4.8), (-4.6, -4.6), (-4.4, -4.4)]):
        _add_labeled_novel(conn, f"https://x/meh{i}.html", f"Meh{i}", list(coord), "meh")
    _add_labeled_novel(conn, "https://x/like.html", "Like", [5.0, 5.0], "like")
    _add_labeled_novel(conn, "https://x/read.html", "Read", [-4.5, -4.5], "like", source="read")

    import numpy as np
    probe = np.array([[-4.5, -4.5]], dtype=np.float32)

    unweighted = train_uncertainty_model(conn, SITE, read_weight=1.0)
    assert unweighted.predict_proba(probe)[0][1] < 0.5

    weighted = train_uncertainty_model(conn, SITE, read_weight=10.0)
    assert weighted.predict_proba(probe)[0][1] > 0.5


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
