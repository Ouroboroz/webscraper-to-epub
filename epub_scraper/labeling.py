"""
epub_scraper.labeling -- Stage 2 of the classification data spine: gather the
150-250 Like/Meh/Drop labels everything downstream (the classifier, ranked
serving) depends on. There are zero labels before this stage; that's the
whole reason it exists.

Two ways to add a label, both writing to the same `labels` table
(dataspine_db.py) distinguished by `source`:
- Search for and label a book you've actually read ("read") -- real ground
  truth, but capped by how much of this specific corpus you've read.
- Work through a review queue and judge a book cold, from its synopsis/tags
  alone ("cold") -- how most of the 150-250 target realistically gets hit.
  Cluster-stratified at first (so early labels span the taste space Stage 1
  found, not just whatever's most common), then uncertainty-sampling-driven
  once there's enough label diversity to fit a quick internal model.

Pure logic lives here, testable without a server; `python -m epub_scraper.
labeling`'s FastHTML/HTMX route layer (a thin wrapper calling these
functions) is the untested integration boundary, same split as
`corpus_structure.py`/`novelupdates.py`. `scikit-learn`/`numpy` (needed for
the uncertainty-sampling helper) are NOT base dependencies (see
requirements-labeling.txt) -- local-imported inside the one function that
needs them, so this module stays importable without either.

The uncertainty model built here is an internal sampling helper only, not
Stage 3's actual classifier -- it exists purely to pick which unlabeled
novel is most worth asking about next.
"""

import heapq
import itertools

from .dataspine_db import (DEFAULT_DB_PATH, count_labels, delete_most_recent_label,
                            first_chapter_excerpt, get_novel_by_id, init_db, iter_embeddings,
                            iter_labeled_novel_ids, label_counts_by_type, tags_for_novel,
                            upsert_label)

SITE_KEY = "fanmtl"
DEFAULT_PORT = 5001
LABEL_TARGET = 200


def search_novels(conn, site_key, query, limit=20):
    """Title-substring search (title, alt_title, and NU's title if resolved)
    for the "label a book you've read" flow -- plain SQL LIKE for v1, see
    module/plan notes on why RapidFuzz ranking isn't here yet."""
    pattern = f"%{query}%"
    return conn.execute(
        "SELECT * FROM novels WHERE site_key = ? AND "
        "(title LIKE ? OR alt_title LIKE ? OR nu_title LIKE ?) "
        "ORDER BY title LIMIT ?",
        (site_key, pattern, pattern, pattern, limit),
    ).fetchall()


def _interleave_proportionally(buckets):
    """Merge several already size-capped id lists into one queue where each
    bucket's items are spread evenly across the WHOLE output in proportion
    to that bucket's own size, rather than round-robin (which gives every
    bucket equal weight per round, drastically under-representing a bucket
    that's deliberately sized much bigger than the others) or draining one
    bucket before the next (which front-loads it instead of spreading it).

    A bucket of size N gets its k-th item (0-indexed) placed at fractional
    position (k+1)/N in the merge order; globally sorting all items by that
    fraction interleaves every bucket at its own density throughout the
    whole sequence -- so a short prefix of the result already reflects each
    bucket's intended share, not just the eventual full drain."""
    heap = []
    for bidx, items in enumerate(buckets):
        if items:
            heapq.heappush(heap, (1.0 / len(items), bidx, 0))

    queue = []
    while heap:
        pos, bidx, idx = heapq.heappop(heap)
        items = buckets[bidx]
        queue.append(items[idx])
        idx += 1
        if idx < len(items):
            heapq.heappush(heap, (pos + 1.0 / len(items), bidx, idx))
    return queue


def build_seed_queue(conn, site_key, total_target=LABEL_TARGET):
    """Cluster-stratified list of unlabeled novel_ids with a known
    cluster_id, sized and interleaved so a session of about `total_target`
    labels touches every real theme Stage 1 found AND gets outliers (-1)
    represented close to their actual share of the corpus.

    Confirmed live (2026-08-03) this needs deliberate handling, not plain
    equal-weight round-robin: on the real corpus, -1 alone was 56.6% of all
    candidates (59,398 of 104,954) spread across zero coherent themes,
    against 122 real clusters. Equal per-bucket round-robin (the original
    design here) gives -1 the SAME weight as any single 15-novel niche
    cluster -- with 123 buckets total and a ~200-label target, that's ~8
    outlier labels out of 200 (4%) for a bucket that's the numeric majority
    of the corpus Stage 3 will actually have to score later.

    Built in two levels, deliberately NOT by handing _interleave_proportionally
    123 buckets directly (tried that first -- confirmed live it silently
    clumps: with total_target=200 spread over 122 real clusters, each ends
    up sized 1, and EVERY size-1 bucket's single item lands at the exact
    same fractional position [1.0] in that function's math, so all 122 pile
    up together at the very end while the one outlier bucket -- the only
    one with more than 1 item -- occupies the entire rest of the sequence
    alone. Degenerate, not proportional, the opposite of the goal.):
    1. Round-robin ONLY across the real clusters into one combined stream
       (up to `per_cluster` from each -- already naturally diverse, since
       every item in it comes from a different theme).
    2. Interleave that ONE combined stream against the outlier bucket --
       now just two comparably-sized buckets, which
       _interleave_proportionally handles correctly (see its own docstring
       and tests) -- sized so outliers land at their live share of
       (outliers + however many real-cluster slots the >=1-per-cluster
       floor actually produced), not diluted by that floor the way sizing
       directly off `total_target` would be.

    Recomputed fresh on every call rather than cached -- cheap (a handful of
    SQL queries at this row count) and automatically correct as labels land
    or Stage 1 gets re-run with different clustering, with no "where was I"
    state to track across requests."""
    labeled = iter_labeled_novel_ids(conn)
    rows = conn.execute(
        "SELECT id, cluster_id FROM novels "
        "WHERE site_key = ? AND candidate = 1 AND cluster_id IS NOT NULL "
        "ORDER BY cluster_id, id",
        (site_key,),
    ).fetchall()

    by_cluster = {}
    for row in rows:
        if row["id"] in labeled:
            continue
        by_cluster.setdefault(row["cluster_id"], []).append(row["id"])

    outlier_ids = by_cluster.pop(-1, [])
    real_cluster_lists = list(by_cluster.values())
    n_real = len(real_cluster_lists)

    # True population share -- from the FULL (uncapped) counts, not the
    # per_cluster-capped ones below, which are a small sample of the real
    # clusters and would badly overstate outliers' share if used here
    # (confirmed live: using the capped counts put outliers at 99.8% of the
    # queue instead of the intended ~57%).
    n_total = len(outlier_ids) + sum(len(ids) for ids in real_cluster_lists)
    outlier_fraction = len(outlier_ids) / n_total if n_total else 0

    per_cluster = max(1, total_target // n_real) if n_real else 0
    real_capped = [ids[:per_cluster] for ids in real_cluster_lists]
    real_combined = [novel_id for group in itertools.zip_longest(*real_capped)
                      for novel_id in group if novel_id is not None]
    if 0 < outlier_fraction < 1:
        outlier_quota = round(len(real_combined) * outlier_fraction / (1 - outlier_fraction))
    else:
        outlier_quota = len(outlier_ids)  # all-outlier or all-real-cluster corpus -- no split to do

    return _interleave_proportionally([outlier_ids[:outlier_quota], real_combined])


def train_uncertainty_model(conn, site_key, read_weight=2.0):
    """Fit a quick LogisticRegression over every labeled novel's synopsis
    embedding, binarized Like=1 / Meh+Drop=0 (matching the settled Stage 3
    binarization rule). Returns None if there aren't at least two labels
    with both classes represented yet -- can't fit a real decision boundary
    from one class, and the seed queue should still be the one driving
    selection at that point anyway.

    read_weight: sample_weight multiplier for source='read' labels (books
    actually read) relative to 1.0 for source='cold' ones (judged from
    synopsis/tags alone). A 'read' label is real ground truth; a 'cold' one
    is a real but noisier signal -- the module docstring's own framing.
    2.0 is a deliberately moderate default, not maxed out: 'cold' labels
    are how most of the 150-250 target actually gets hit (see module
    docstring), so a much higher multiplier would let a likely-small set of
    'read' labels dominate the fit and defeat the point of that broader
    cold-labeled coverage. This is an internal active-learning helper, not
    Stage 3's actual classifier -- revisit there with more labels in hand
    once that's real, rather than treating this number as load-bearing."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    rows = conn.execute(
        "SELECT n.synopsis_embedding AS embedding, l.label AS label, l.source AS source "
        "FROM novels n JOIN labels l ON l.novel_id = n.id "
        "WHERE n.site_key = ? AND n.synopsis_embedding IS NOT NULL",
        (site_key,),
    ).fetchall()
    if len(rows) < 2:
        return None

    y = [1 if row["label"] == "like" else 0 for row in rows]
    if len(set(y)) < 2:
        return None

    X = np.stack([np.frombuffer(row["embedding"], dtype=np.float32) for row in rows])
    sample_weight = [read_weight if row["source"] == "read" else 1.0 for row in rows]
    model = LogisticRegression(max_iter=1000)
    model.fit(X, y, sample_weight=sample_weight)
    return model


def rank_by_uncertainty(model, conn, site_key, exclude_ids):
    """Every unlabeled candidate with a stored embedding, ranked most-
    uncertain-first (|predict_proba(Like) - 0.5| ascending) -- the standard
    margin-uncertainty active-learning heuristic."""
    import numpy as np

    candidates = [(novel_id, blob) for novel_id, blob in iter_embeddings(conn, site_key)
                  if novel_id not in exclude_ids]
    if not candidates:
        return []

    X = np.stack([np.frombuffer(blob, dtype=np.float32) for _, blob in candidates])
    proba = model.predict_proba(X)[:, 1]
    order = np.argsort(np.abs(proba - 0.5))
    return [candidates[i][0] for i in order]


def next_review_candidate(conn, site_key, skipped_ids=()):
    """The next novel to show in the review queue: drains the cluster-
    stratified seed queue first, then falls back to uncertainty-sampling
    once it's exhausted (or immediately, if there aren't enough labels yet
    to have a seed queue worth draining -- same function either way, no
    separate "phase" flag to track). Returns None once there's nothing left
    to review at all.

    skipped_ids: novel_ids to treat as unavailable for this call, on top of
    already-labeled ones -- see labeling.py's plan notes on why "skip" needs
    an in-process set rather than being state-free like everything else
    here."""
    skipped = set(skipped_ids)

    for novel_id in build_seed_queue(conn, site_key):
        if novel_id not in skipped:
            return get_novel_by_id(conn, novel_id)

    model = train_uncertainty_model(conn, site_key)
    if model is None:
        return None

    exclude = iter_labeled_novel_ids(conn) | skipped
    ranked = rank_by_uncertainty(model, conn, site_key, exclude)
    if not ranked:
        return None
    return get_novel_by_id(conn, ranked[0])


def record_label(conn, novel_id, label, drop_chapter=None, source="cold"):
    upsert_label(conn, novel_id, label, drop_chapter=drop_chapter, source=source)
    conn.commit()


# ============================================================================
# FastHTML/HTMX app -- everything below is the untested UI/integration layer.
# Every route is a thin wrapper: parse the request, call a function above,
# render its result. `python-fasthtml`/`scikit-learn` are NOT base
# dependencies (see requirements-labeling.txt); this section is never
# imported by anything except build_app()/main(), so the rest of this module
# (all the logic above) stays importable without either.
# ============================================================================

_CSS = """
:root {
  --bg: #0d1017; --surface: #171b24; --surface-2: #1e2330; --border: #2a2f3a;
  --text: #e8e6e1; --text-dim: #9aa0ac; --text-faint: #5b6472;
  --accent: #e8a33d; --accent-ink: #1a1204;
  --like: #6fae6f; --meh: #c9a227; --drop: #c96a4a;
  --focus-ring: #e8a33d;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg: #f2f3f6; --surface: #ffffff; --surface-2: #eaecf1; --border: #dadfe6;
    --text: #1b1e27; --text-dim: #5b6472; --text-faint: #8a92a0;
    --accent: #c97f1e; --accent-ink: #fff7ea;
  }
}
:root[data-theme="dark"] {
  --bg: #0d1017; --surface: #171b24; --surface-2: #1e2330; --border: #2a2f3a;
  --text: #e8e6e1; --text-dim: #9aa0ac; --text-faint: #5b6472;
  --accent: #e8a33d; --accent-ink: #1a1204;
}
:root[data-theme="light"] {
  --bg: #f2f3f6; --surface: #ffffff; --surface-2: #eaecf1; --border: #dadfe6;
  --text: #1b1e27; --text-dim: #5b6472; --text-faint: #8a92a0;
  --accent: #c97f1e; --accent-ink: #fff7ea;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font-family: ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace;
  font-variant-numeric: tabular-nums;
}
main.container { max-width: 720px; margin: 0 auto; padding: 1.25rem 1.25rem 4rem; }
header.topbar {
  display: flex; align-items: baseline; gap: 1.25rem; flex-wrap: wrap;
  padding: 0.9rem 1.25rem; border-bottom: 1px solid var(--border); background: var(--surface);
}
header.topbar h1 {
  margin: 0; font-size: 0.95rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.03em;
}
header.topbar nav { display: flex; gap: 0.9rem; }
header.topbar nav a {
  color: var(--text-dim); text-decoration: none; font-size: 0.82rem;
  padding-bottom: 0.15rem; border-bottom: 2px solid transparent;
}
header.topbar nav a.active { color: var(--accent); border-bottom-color: var(--accent); }
header.topbar .progress { margin-left: auto; font-size: 0.78rem; color: var(--text-dim); }
header.topbar .progress b { color: var(--accent); }
.card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 6px;
  padding: 1.1rem 1.25rem; margin-top: 1.1rem;
}
.card .title {
  font-family: Georgia, "Iowan Old Style", "Palatino Linotype", serif;
  font-size: 1.25rem; font-weight: 600; margin: 0 0 0.3rem; text-wrap: balance;
}
.card .meta { font-size: 0.78rem; color: var(--text-faint); margin-bottom: 0.7rem; }
.card .synopsis {
  font-family: Georgia, "Iowan Old Style", "Palatino Linotype", serif;
  font-size: 0.95rem; line-height: 1.55; color: var(--text-dim); max-width: 65ch;
}
.chips { display: flex; flex-wrap: wrap; gap: 0.4rem; margin-top: 0.7rem; }
.chip {
  font-size: 0.72rem; padding: 0.15rem 0.55rem; border-radius: 99px;
  background: var(--surface-2); color: var(--text-dim); border: 1px solid var(--border);
}
.excerpt {
  margin-top: 0.9rem; padding-top: 0.9rem; border-top: 1px dashed var(--border);
  font-family: Georgia, "Iowan Old Style", "Palatino Linotype", serif;
  font-size: 0.88rem; line-height: 1.6; color: var(--text-faint); max-width: 65ch;
  white-space: pre-line;
}
.excerpt .label { font-family: inherit; font-size: 0.68rem; text-transform: uppercase;
  letter-spacing: 0.05em; color: var(--text-faint); display: block; margin-bottom: 0.4rem; }
.actions { display: flex; gap: 0.6rem; margin-top: 1.1rem; flex-wrap: wrap; align-items: center; }
button {
  font-family: inherit; font-size: 0.85rem; padding: 0.5rem 1rem; border-radius: 5px;
  border: 1px solid var(--border); background: var(--surface-2); color: var(--text);
  cursor: pointer;
}
button:hover { border-color: var(--accent); }
button:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
button.like:hover { border-color: var(--like); color: var(--like); }
button.meh:hover { border-color: var(--meh); color: var(--meh); }
button.drop:hover { border-color: var(--drop); color: var(--drop); }
button.ghost { background: transparent; color: var(--text-faint); font-size: 0.76rem; }
.kbd { font-size: 0.7rem; color: var(--text-faint); margin-left: 0.3rem; }
input[type=text], input[type=number] {
  font-family: inherit; font-size: 0.9rem; padding: 0.5rem 0.7rem; border-radius: 5px;
  border: 1px solid var(--border); background: var(--surface-2); color: var(--text); width: 100%;
}
input:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.results { margin-top: 0.6rem; }
.result-row {
  padding: 0.5rem 0.7rem; border-radius: 4px; cursor: pointer; font-size: 0.88rem;
}
.result-row:hover { background: var(--surface-2); }
.empty { color: var(--text-faint); font-size: 0.9rem; padding: 1.5rem 0; }
.drop-chapter-field { display: flex; align-items: center; gap: 0.5rem; }
.drop-chapter-field label { font-size: 0.8rem; color: var(--text-dim); }
.drop-chapter-field input { width: 6rem; }
"""

_KEYBOARD_JS = """
document.body.addEventListener('keydown', function (e) {
  var tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea') return;
  var map = {'1': 'like', '2': 'meh', '3': 'drop'};
  var kind = map[e.key];
  if (!kind) return;
  var btn = document.querySelector('.actions button.' + kind);
  if (btn) btn.click();
});
"""


def _progress(conn):
    counts = label_counts_by_type(conn)
    total = count_labels(conn)
    parts = ", ".join(f"{counts.get(k, 0)} {k}" for k in ("like", "meh", "drop") if counts.get(k))
    return f"{total}/{LABEL_TARGET} labeled" + (f" ({parts})" if parts else "")


def _layout(conn, *content, active=None):
    # Deliberately not using FastHTML's Titled() -- it wraps its own children
    # in <main><h1>{title}</h1>...</main>, which would double up with (and
    # semantically conflict with) this page's own <header>/<main> structure.
    from fasthtml.common import H1, A, Div, Header, Main, Nav, Script, Title

    return (
        Title("Taste Labeling"),
        Header(
            H1("Taste Labeling"),
            Nav(
                A("Review Queue", href="/queue", cls="active" if active == "queue" else ""),
                A("Label a Book You've Read", href="/read", cls="active" if active == "read" else ""),
            ),
            Div(_progress(conn), cls="progress", id="progress"),
            cls="topbar",
        ),
        Main(*content, cls="container"),
        Script(_KEYBOARD_JS),
    )


def _novel_display(conn, novel, *, show_excerpt=True):
    from fasthtml.common import Div, P

    tags = tags_for_novel(conn, novel["id"])
    bits = [
        P(novel["title"], cls="title"),
        Div(f"{novel['author'] or 'Unknown author'} · {novel['status'] or 'status unknown'}",
            cls="meta"),
    ]
    if novel["synopsis"]:
        bits.append(Div(novel["synopsis"], cls="synopsis"))
    if tags:
        bits.append(Div(*[Div(t, cls="chip") for t in tags], cls="chips"))
    if show_excerpt:
        excerpt = first_chapter_excerpt(conn, novel["id"])
        if excerpt:
            bits.append(Div(Div("Opening of chapter 1", cls="label"), excerpt, cls="excerpt"))
    return Div(*bits)


def _progress_oob(conn):
    """A second copy of the header's progress readout, out-of-band-swapped
    into #progress on every queue action -- the main HTMX response target is
    always #queue-panel, this rides along in the same response so the count
    stays live without a full page reload."""
    from fasthtml.common import Div
    return Div(_progress(conn), cls="progress", id="progress", hx_swap_oob="true")


def _queue_card(conn, skipped_ids):
    """Just the swappable panel -- id="queue-panel", no OOB sibling. Used
    both for the initial full-page render (where the header's own _progress()
    call already has the right count) and, wrapped alongside
    _progress_oob(), for the AJAX follow-ups after a label/skip/undo."""
    from fasthtml.common import Button, Div, Form, Hidden, Span

    candidate = next_review_candidate(conn, SITE_KEY, skipped_ids=skipped_ids)
    if candidate is None:
        body = Div("Nothing left to review right now -- run more of Stage 0/1's pipeline, "
                    "or check back later.", cls="empty")
    else:
        body = Div(
            _novel_display(conn, candidate),
            Form(
                Hidden(name="novel_id", value=str(candidate["id"])),
                Div(
                    Button("Like", cls="like", type="submit", name="label", value="like"),
                    Span("1", cls="kbd"),
                    Button("Meh", cls="meh", type="submit", name="label", value="meh"),
                    Span("2", cls="kbd"),
                    Button("Drop", cls="drop", type="submit", name="label", value="drop"),
                    Span("3", cls="kbd"),
                    cls="actions",
                ),
                hx_post="/queue/label", hx_target="#queue-panel", hx_swap="outerHTML",
            ),
            Div(
                Button("Skip", cls="ghost", hx_post="/queue/skip",
                       hx_vals=f'{{"novel_id": {candidate["id"]}}}',
                       hx_target="#queue-panel", hx_swap="outerHTML"),
                Button("Undo last label", cls="ghost", hx_post="/queue/undo",
                       hx_target="#queue-panel", hx_swap="outerHTML"),
                cls="actions",
            ),
            cls="card",
        )
    return Div(body, id="queue-panel")


def _read_panel():
    from fasthtml.common import Div, Input

    return Div(
        Input(type="text", name="q", placeholder="Search titles you've read...",
              hx_get="/read/search", hx_trigger="keyup changed delay:300ms", hx_target="#results"),
        Div(id="results", cls="results"),
        Div(id="detail"),
        id="read-panel",
    )


def _search_results(conn, query):
    from fasthtml.common import Div

    if not query.strip():
        return Div(id="results", cls="results")
    hits = search_novels(conn, SITE_KEY, query)
    if not hits:
        return Div(Div("No matches.", cls="empty"), id="results", cls="results")
    rows = [Div(row["title"], cls="result-row", hx_get=f"/read/{row['id']}", hx_target="#detail")
            for row in hits]
    return Div(*rows, id="results", cls="results")


def _read_detail(conn, novel):
    from fasthtml.common import Button, Div, Form, Hidden, Input, Label

    return Div(
        _novel_display(conn, novel),
        Form(
            Hidden(name="novel_id", value=str(novel["id"])),
            Div(
                Button("Like", cls="like", type="submit", name="label", value="like"),
                Button("Meh", cls="meh", type="submit", name="label", value="meh"),
                Button("Drop", cls="drop", type="submit", name="label", value="drop"),
                Div(Label("Dropped at chapter"),
                    Input(type="number", name="drop_chapter", min="0"),
                    cls="drop-chapter-field"),
                cls="actions",
            ),
            hx_post="/read/label", hx_target="#detail", hx_swap="innerHTML",
        ),
        cls="card",
    )


def build_app(db_path=DEFAULT_DB_PATH):
    """Construct the FastHTML app. A fresh, process-lifetime-only set of
    skipped novel_ids lives here (see next_review_candidate's docstring) --
    not persisted, not shared across processes; resets on every restart.

    A fresh sqlite3 connection is opened *per request* rather than one
    shared connection captured in the closure -- confirmed live: FastHTML/
    Starlette dispatches sync route handlers through a thread pool, and
    sqlite3 connections are thread-affine (raises "SQLite objects created in
    a thread can only be used in that same thread" the moment a different
    worker thread touches a connection created elsewhere). init_db() is
    cheap and idempotent (WAL mode + schema are persisted in the file
    itself, not per-connection state), so opening one per request has no
    real cost."""
    from fasthtml.common import Div, P, RedirectResponse, Style, fast_app

    skipped_ids = set()

    app, rt = fast_app(pico=False, title="Taste Labeling", hdrs=(Style(_CSS),))

    @app.get("/")
    def index():
        return RedirectResponse("/queue")

    @app.get("/queue")
    def queue_page():
        conn = init_db(db_path)
        return _layout(conn, _queue_card(conn, skipped_ids), active="queue")

    @app.post("/queue/label")
    def queue_label(novel_id: int, label: str):
        conn = init_db(db_path)
        record_label(conn, novel_id, label, source="cold")
        return _queue_card(conn, skipped_ids), _progress_oob(conn)

    @app.post("/queue/skip")
    def queue_skip(novel_id: int):
        conn = init_db(db_path)
        skipped_ids.add(novel_id)
        return _queue_card(conn, skipped_ids), _progress_oob(conn)

    @app.post("/queue/undo")
    def queue_undo():
        conn = init_db(db_path)
        delete_most_recent_label(conn)
        return _queue_card(conn, skipped_ids), _progress_oob(conn)

    @app.get("/read")
    def read_page():
        conn = init_db(db_path)
        return _layout(conn, _read_panel(), active="read")

    @app.get("/read/search")
    def read_search(q: str = ""):
        conn = init_db(db_path)
        return _search_results(conn, q)

    @app.get("/read/{novel_id}")
    def read_detail(novel_id: int):
        conn = init_db(db_path)
        novel = get_novel_by_id(conn, novel_id)
        if novel is None:
            return P("Not found.")
        return _read_detail(conn, novel)

    @app.post("/read/label")
    def read_label(novel_id: int, label: str, drop_chapter: int = None):
        conn = init_db(db_path)
        record_label(conn, novel_id, label, drop_chapter=drop_chapter, source="read")
        return Div(P("Saved -- search again to label another."), id="detail"), _progress_oob(conn)

    return app


def main():
    import argparse

    parser = argparse.ArgumentParser(prog="python -m epub_scraper.labeling")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, metavar="FILE")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, metavar="N")
    args = parser.parse_args()

    import uvicorn
    app = build_app(args.db)
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
