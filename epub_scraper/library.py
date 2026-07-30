import json
import os
import tempfile

from .util import now_iso

DEFAULT_LIBRARY_PATH = "library.json"


def load_library(path=DEFAULT_LIBRARY_PATH):
    """Return {"version": 1, "novels": [...]}. If the file doesn't exist, return
    a fresh empty structure without creating it — call save_library() to persist."""
    if not os.path.exists(path):
        return {"version": 1, "novels": []}
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}: invalid JSON ({e})") from e
    if not isinstance(data, dict) or not isinstance(data.get("novels"), list):
        raise ValueError(f"{path}: unexpected library file shape (expected {{'novels': [...]}})")
    return data


def save_library(library, path=DEFAULT_LIBRARY_PATH):
    """Write atomically: temp file in the same directory, then os.replace() over
    `path` — a crash or kill mid-write can never truncate/corrupt the existing file."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".library.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(library, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def find_novel(library, site_key, chapter_id):
    for entry in library["novels"]:
        if entry["site_key"] == site_key and entry["chapter_id"] == chapter_id:
            return entry
    return None


def add_novel(library, *, site_key, chapter_id, index_url, title, output_file, last_known_chapter=0):
    """Append a new tracked-novel entry. Raises ValueError if (site_key, chapter_id)
    is already tracked. Does not save — caller calls save_library()."""
    if find_novel(library, site_key, chapter_id) is not None:
        raise ValueError(f"{site_key}:{chapter_id} is already tracked")
    entry = {
        "site_key": site_key,
        "chapter_id": chapter_id,
        "index_url": index_url,
        "title": title,
        "output_file": output_file,
        "last_known_chapter": last_known_chapter,
        "failed_chapters": [],
        "consecutive_failed_checks": 0,
        "added_at": now_iso(),
        "last_checked_at": None,
        "last_updated_at": None,
        "last_error": None,
        "last_emailed_chapter": 0,
        "last_emailed_at": None,
        "last_email_error": None,
        "enabled": True,
    }
    library["novels"].append(entry)
    return entry


def remove_novel(library, site_key, chapter_id):
    entry = find_novel(library, site_key, chapter_id)
    if entry is None:
        return False
    library["novels"].remove(entry)
    return True


def record_check(entry, *, total=None, title=None, error=None, updated=False):
    """Single bookkeeping call site per novel per check: always stamps
    last_checked_at and last_error; optionally refreshes title; if updated,
    advances last_known_chapter to `total` and stamps last_updated_at."""
    entry["last_checked_at"] = now_iso()
    entry["last_error"] = error
    if title is not None:
        entry["title"] = title
    if updated:
        entry["last_known_chapter"] = total
        entry["last_updated_at"] = now_iso()


def record_email(entry, *, chapter=None, error=None):
    """Single bookkeeping call site per novel per Kindle-send attempt: always
    stamps last_emailed_at and last_email_error; on success (error is None)
    advances last_emailed_chapter to `chapter`."""
    entry["last_emailed_at"] = now_iso()
    entry["last_email_error"] = error
    if error is None:
        entry["last_emailed_chapter"] = chapter
