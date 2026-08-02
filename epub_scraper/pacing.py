import json
import os
import random

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
        intervals = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                intervals = json.load(f)
        return cls(path, default_interval, intervals)

    def current_interval(self, site_key):
        return self.intervals.get(site_key, self.default_interval)

    def gap(self, site_key):
        mean = self.current_interval(site_key)
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
        dirname = os.path.dirname(self.path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.intervals, f, indent=2)
