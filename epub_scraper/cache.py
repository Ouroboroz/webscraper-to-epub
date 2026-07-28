import os


def cache_path(cache_dir, chapter_id, n):
    return os.path.join(cache_dir, f"{chapter_id}_{n}.html")


def load_cached(cache_dir, chapter_id, n):
    p = cache_path(cache_dir, chapter_id, n)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    return None


def save_cache(cache_dir, chapter_id, n, html):
    os.makedirs(cache_dir, exist_ok=True)
    with open(cache_path(cache_dir, chapter_id, n), "w", encoding="utf-8") as f:
        f.write(html)
