import os
import re
from datetime import datetime, timezone

EPUB_DIR = "epubs"


def sanitize_title(title):
    """Make a novel title safe as a filename while keeping it human-readable:
    colons become ' -' (common subtitle separator), other characters illegal
    on Windows/exFAT/FAT32 (what most e-readers use) are dropped outright."""
    title = title.replace(":", " -")
    title = re.sub(r'[\\/*?"<>|]', "", title)
    return re.sub(r"\s+", " ", title.strip())


def epub_filename(title, start, end):
    """'[Ch {start} - Ch {end}] Title.epub' — chapter range up front so it's
    visible in a sorted file list, spaces instead of underscores so it reads
    naturally on-device (e.g. Kindle)."""
    return f"[Ch {start} - Ch {end}] {sanitize_title(title)}.epub"


def epub_path(title, start, end, directory=EPUB_DIR):
    return os.path.join(directory, epub_filename(title, start, end))


def get_base_url(url):
    m = re.match(r"(https?://[^/]+)", url)
    return m.group(1) if m else ""


def now_iso():
    return datetime.now(timezone.utc).isoformat()
