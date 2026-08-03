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


def cluster_corpus(conn, site_key, umap_dims=8, min_cluster_size=10, random_state=42):
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
    theme" signal worth keeping)."""
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
