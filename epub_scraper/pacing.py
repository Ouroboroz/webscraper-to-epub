import json
import os
import random
import tempfile

DEFAULT_PACING_PATH = "pacing.json"

BACKOFF_FACTOR = 2.0
MAX_INTERVAL = 120.0
GAMMA_SHAPE = 2.5


class Pacer:
    """Per-site request pacing: jittered gaps around a learned interval that
    widens (and persists) when a site signals it's being asked for too much.
    One Pacer covers every site_key for the process's run -- pacing.json
    holds {site_key: interval} for all of them, never just one site."""

    def __init__(self, path, default_interval, intervals=None):
        self.path = path
        self.default_interval = default_interval
        self.intervals = dict(intervals or {})

    @classmethod
    def load(cls, path=DEFAULT_PACING_PATH, default_interval=2.5):
        """Read learned per-site intervals from `path`, tolerating a damaged file.

        Unlike library.json -- irreplaceable user data, where load_library()
        deliberately raises rather than silently losing entries -- pacing.json
        is a derived cache: every value in it is re-learned the next time a site
        pushes back. So a corrupt file here must NOT be fatal. Pacer.load() runs
        once per `check` run, before the per-novel loop and outside any
        try/except, and throttled() rewrites the file on every 429 -- so a cron
        job killed mid-write would otherwise take down every future scheduled
        run until someone deleted the file by hand.
        """
        intervals = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError(f"expected a JSON object, got {type(data).__name__}")
                # Coerce: a hand-edited file can easily hold "5.0" instead of 5.0.
                intervals = {str(k): float(v) for k, v in data.items()}
            except (OSError, TypeError, ValueError) as e:
                print(f"warning: ignoring unreadable {path} ({e}); "
                      f"starting from default pacing intervals")
                intervals = {}
        return cls(path, default_interval, intervals)

    def current_interval(self, site_key):
        """The learned value wins when it is larger (that's the whole point of
        persisting backoff), but `default_interval` acts as a FLOOR: a user who
        explicitly passes `--delay 30` to be extra polite must not have it
        silently ignored just because this site has some smaller value on
        record from a long-past throttle."""
        return max(self.intervals.get(site_key, 0.0), self.default_interval)

    def gap(self, site_key):
        mean = self.current_interval(site_key)
        if mean <= 0:
            return 0.0
        draw = random.gammavariate(GAMMA_SHAPE, mean / GAMMA_SHAPE)
        return min(max(draw, mean * 0.2), mean * 3.0)

    def throttled(self, site_key, retry_after=None):
        current = self.current_interval(site_key)
        widened = current * BACKOFF_FACTOR
        if retry_after is not None:
            try:
                widened = float(retry_after)
            except (TypeError, ValueError):
                pass
        widened = min(max(widened, current), MAX_INTERVAL)
        self.intervals[site_key] = widened
        self.save()
        return widened

    def save(self):
        """Write atomically -- temp file in the same directory, then
        os.replace() -- exactly as library.save_library() does. throttled()
        saves on every single 429, so a run killed mid-write must never be able
        to leave a truncated pacing.json behind."""
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".pacing.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.intervals, f, indent=2)
                f.write("\n")
            os.replace(tmp_path, self.path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
