import re
from datetime import datetime, timezone


def slugify(title):
    """Turn a novel title into a safe filename."""
    title = re.sub(r"[^\w\s-]", "", title)
    title = re.sub(r"\s+", "_", title.strip())
    return title + ".epub"


def get_base_url(url):
    m = re.match(r"(https?://[^/]+)", url)
    return m.group(1) if m else ""


def now_iso():
    return datetime.now(timezone.utc).isoformat()
