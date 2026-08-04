"""
epub_scraper.corpus_structure -- Stage 1 of the classification data spine:
understand what the corpus actually contains (thematic clusters + tag
communities), so Stage 2's labeling sample can be stratified across the
taste space instead of drawing an accidentally lopsided sample (e.g. 200
near-identical cultivation novels). Produces zero recommendations by design
-- that's Stage 3+'s job.

Pure local computation, no network calls at all -- unlike the rest of Stage
0, nothing here needs epub_scraper.fetcher/pacing. `sentence-transformers`/
`umap-learn`/`hdbscan`/`python-igraph`/`leidenalg`/`numpy` are NOT base
dependencies (see requirements-ml.txt) -- most users of this package (FanMTL
scraping, or even just Stage 0's crawl/metadata/chapters/enrich) don't need
a GPU embedding model. Every function below does its own local import of
whatever it needs, so this module itself -- and dataspine.py, which imports
it at module level to wire in the embed/cluster/tag-communities CLI
subcommands -- stays importable without any of them installed.

Only synopsis + tags are used here, both already landing during Stage 0's
`metadata`/`enrich` -- this stage does NOT need `chapters` or a fully
finished `enrich` run to produce useful results; it just gets richer (more
NU tags) as `enrich` continues.
"""

from .dataspine_db import (all_tags, iter_candidates_missing_embedding, iter_embeddings,
                            iter_tag_cooccurrence, upsert_embedding, write_cluster_assignments,
                            write_tag_communities)

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"


def embed_synopses(conn, site_key, model_name=DEFAULT_EMBEDDING_MODEL, limit=None, batch_size=32):
    """Embed every candidate's synopsis still missing one, batched through a
    real sentence-transformers model. Resumable like the rest of Stage 0
    (iter_candidates_missing_embedding only selects rows with no embedding
    yet). Returns how many novels were embedded this call."""
    import numpy as np  # local import -- see module docstring
    from sentence_transformers import SentenceTransformer

    rows = iter_candidates_missing_embedding(conn, site_key, limit=limit)
    if not rows:
        return 0

    model = SentenceTransformer(model_name)
    texts = [row["synopsis"] for row in rows]
    embeddings = model.encode(texts, batch_size=batch_size, show_progress_bar=False,
                               convert_to_numpy=True)

    for row, embedding in zip(rows, embeddings):
        upsert_embedding(conn, row["id"], np.asarray(embedding, dtype=np.float32).tobytes())
    conn.commit()
    return len(rows)


def _recluster_outliers(reduced, cluster_ids, min_cluster_size=15, max_cluster_fraction=0.05):
    """Second pass: re-cluster whatever the main HDBSCAN pass left as -1, on
    its own. "Doesn't fit any cluster" only means each point individually
    failed a density test against the WHOLE corpus -- it doesn't mean the
    leftover points are all similar to each other, so real small niche
    themes can still hide in there. Confirmed live (2026-08-03): re-
    clustering a real corpus's ~60k leftover outliers surfaced a genuine
    ~150-novel "NBA/basketball isekai" micro-genre, plus several ~20-50-
    novel anime-crossover ones, that the full-corpus pass was too diluted
    to ever separate out.

    But re-clustering a point set this different in size/density from the
    original corpus is unreliable taken at face value: HDBSCAN's distance
    notion is relative to whatever data it's given, and with the dense main
    clusters removed it readily density-chains almost everything remaining
    into one giant, semantically incoherent mega-cluster (confirmed live:
    eom selection found one such cluster covering 97% of the leftover
    points, containing completely unrelated genres side by side -- cutting
    it back out with `cluster_selection_method='leaf'` on the very same
    points instead swung outliers from 0.8% to 89.6%, a two-orders-of-
    magnitude gap that's itself the tell this isn't real structure).
    Filtered out via max_cluster_fraction: any second-pass cluster bigger
    than that fraction of the leftover set is treated as this artifact, not
    a real theme, and stays -1. The real niche clusters found live were all
    under 0.3% of the leftover set -- max_cluster_fraction defaults well
    above that (0.05) rather than right at it, since the actual boundary
    between "real niche" and "artifact" is a per-run judgment call, not a
    hard mathematical line.

    Returns a NEW cluster_ids array (input is not mutated) -- accepted
    sub-cluster IDs start right after the highest ID already used by the
    caller's main pass, so IDs stay globally unique across both passes."""
    import hdbscan
    import numpy as np

    cluster_ids = np.array(cluster_ids, copy=True)
    outlier_mask = cluster_ids == -1
    n_outliers = int(outlier_mask.sum())
    if n_outliers < min_cluster_size:
        return cluster_ids

    sub_labels = hdbscan.HDBSCAN(
        min_cluster_size=min(min_cluster_size, n_outliers)).fit_predict(reduced[outlier_mask])

    next_id = int(cluster_ids.max()) + 1 if (cluster_ids != -1).any() else 0
    max_size = max_cluster_fraction * n_outliers
    outlier_indices = np.flatnonzero(outlier_mask)

    sub_ids, sub_counts = np.unique(sub_labels, return_counts=True)
    for sub_id, count in zip(sub_ids, sub_counts):
        if sub_id == -1 or count > max_size:
            continue  # not a real sub-cluster, or the density-chained mega-cluster artifact
        cluster_ids[outlier_indices[sub_labels == sub_id]] = next_id
        next_id += 1

    return cluster_ids


def cluster_corpus(conn, site_key, umap_dims=8, min_cluster_size=30, random_state=42,
                    outlier_min_cluster_size=15, outlier_max_cluster_fraction=0.05):
    """Full recompute of cluster_id (HDBSCAN over a UMAP_dims-dim UMAP
    reduction) plus a separate 2D umap_x/umap_y projection purely for
    visualization, for every candidate with a stored embedding.

    Not incremental, unlike the append-only per-novel writes elsewhere in
    Stage 0 -- a cluster boundary can shift for every novel as the corpus
    grows, so this always recomputes from scratch over whatever embeddings
    currently exist. random_state is fixed since UMAP is otherwise
    stochastic -- re-running without new data would silently reshuffle
    cluster_id/coordinates for no reason.

    Returns (n_novels, n_clusters, n_outliers) -- n_outliers is how many got
    HDBSCAN's -1 label (not noise to be dropped; a real "doesn't fit a
    theme" signal worth keeping).

    min_cluster_size=30: swept a real 12-config grid against the full
    104,954-novel corpus (2026-08-03) -- metric (euclidean/cosine),
    n_neighbors (15/30/50), min_cluster_size (10/30/50/100), explicit
    min_samples. Outlier rate stayed in a 55-62% band across EVERY
    combination (UMAP's spectral init failed on most of them too -- a
    genuinely small eigengap, not a config artifact), so this is a real
    property of the corpus at this scale, not a bug to tune away -- the
    much lower outlier rate on the original 1,510-novel run was almost
    certainly small/less-diverse-sample bias, not a baseline to chase.
    30 was the best of the swept values (60.0%->55.9% outliers, 268->113
    clusters vs. the old default of 10); going higher (50, 100) plateaued
    with no further gain.

    outlier_min_cluster_size/outlier_max_cluster_fraction: passed straight
    through to a second pass over whatever's left as -1 after the main
    clustering above -- see _recluster_outliers()'s docstring for why this
    exists and how the defaults were chosen."""
    import hdbscan
    import numpy as np
    import umap

    pairs = iter_embeddings(conn, site_key)
    if not pairs:
        return 0, 0, 0

    novel_ids = [novel_id for novel_id, _ in pairs]
    embeddings = np.stack([np.frombuffer(blob, dtype=np.float32) for _, blob in pairs])

    # UMAP needs n_neighbors < n_samples; degrades gracefully on a tiny dev-scale run.
    n_neighbors = min(15, len(novel_ids) - 1)
    cluster_dims = min(umap_dims, max(len(novel_ids) - 2, 1))

    cluster_reducer = umap.UMAP(n_components=cluster_dims, n_neighbors=n_neighbors,
                                 random_state=random_state)
    reduced = cluster_reducer.fit_transform(embeddings)

    clusterer = hdbscan.HDBSCAN(min_cluster_size=min(min_cluster_size, len(novel_ids)))
    cluster_ids = clusterer.fit_predict(reduced)
    cluster_ids = _recluster_outliers(reduced, cluster_ids, min_cluster_size=outlier_min_cluster_size,
                                       max_cluster_fraction=outlier_max_cluster_fraction)

    viz_reducer = umap.UMAP(n_components=2, n_neighbors=n_neighbors, random_state=random_state)
    coords_2d = viz_reducer.fit_transform(embeddings)

    assignments = {
        novel_id: (int(cluster_ids[i]), float(coords_2d[i][0]), float(coords_2d[i][1]))
        for i, novel_id in enumerate(novel_ids)
    }
    write_cluster_assignments(conn, assignments)
    conn.commit()

    n_clusters = len(set(cluster_ids.tolist()) - {-1})
    n_outliers = int((cluster_ids == -1).sum())
    return len(novel_ids), n_clusters, n_outliers


def build_tag_communities(conn):
    """Full recompute of every tag's Leiden community_id from the current
    tags/novel_tags join: build a co-occurrence graph (edge weight = how
    many novels share both tags), run Leiden modularity-maximizing
    community detection. Not incremental, same reasoning as cluster_corpus.

    Returns (n_tags, n_communities)."""
    import igraph as ig
    import leidenalg

    tags = all_tags(conn)
    if not tags:
        return 0, 0

    tag_ids = [row["id"] for row in tags]
    index_by_tag_id = {tag_id: i for i, tag_id in enumerate(tag_ids)}

    edges = []
    weights = []
    for row in iter_tag_cooccurrence(conn):
        edges.append((index_by_tag_id[row["tag_id_a"]], index_by_tag_id[row["tag_id_b"]]))
        weights.append(row["weight"])

    graph = ig.Graph(n=len(tag_ids), edges=edges)
    graph.es["weight"] = weights

    partition = leidenalg.find_partition(
        graph, leidenalg.ModularityVertexPartition, weights="weight" if edges else None)

    communities = {tag_id: partition.membership[index_by_tag_id[tag_id]] for tag_id in tag_ids}
    write_tag_communities(conn, communities)
    conn.commit()

    n_communities = len(set(communities.values()))
    return len(tag_ids), n_communities
