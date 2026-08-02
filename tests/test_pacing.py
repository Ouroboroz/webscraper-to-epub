import json

from epub_scraper.pacing import Pacer


def test_load_with_no_existing_file_uses_default_interval(tmp_path):
    path = str(tmp_path / "pacing.json")
    pacer = Pacer.load(path, default_interval=3.0)
    assert pacer.current_interval("fanmtl") == 3.0


def test_load_reads_persisted_interval_for_known_site(tmp_path):
    path = str(tmp_path / "pacing.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"fanmtl": 9.0}, f)

    pacer = Pacer.load(path, default_interval=3.0)

    assert pacer.current_interval("fanmtl") == 9.0
    assert pacer.current_interval("other_site") == 3.0


def test_gap_varies_across_calls(tmp_path):
    pacer = Pacer.load(str(tmp_path / "pacing.json"), default_interval=4.0)
    gaps = [pacer.gap("fanmtl") for _ in range(20)]
    assert len(set(gaps)) > 1


def test_gap_stays_within_floor_and_ceiling(tmp_path):
    pacer = Pacer.load(str(tmp_path / "pacing.json"), default_interval=4.0)
    gaps = [pacer.gap("fanmtl") for _ in range(200)]
    assert all(0.8 <= g <= 12.0 for g in gaps)  # 0.2x .. 3x of mean=4.0


def test_gap_with_zero_default_interval_returns_zero(tmp_path):
    # A caller may deliberately configure a zero delay (e.g. fast tests); the
    # gamma distribution is undefined for a zero mean, so this must short-circuit
    # rather than raise.
    pacer = Pacer.load(str(tmp_path / "pacing.json"), default_interval=0)
    assert pacer.gap("fanmtl") == 0.0


def test_throttled_with_retry_after_sets_interval(tmp_path):
    pacer = Pacer.load(str(tmp_path / "pacing.json"), default_interval=2.5)
    widened = pacer.throttled("fanmtl", retry_after="30")
    assert widened == 30.0
    assert pacer.current_interval("fanmtl") == 30.0


def test_throttled_without_retry_after_widens_by_backoff_factor(tmp_path):
    pacer = Pacer.load(str(tmp_path / "pacing.json"), default_interval=2.5)
    widened = pacer.throttled("fanmtl")
    assert widened == 5.0  # 2.5 * BACKOFF_FACTOR (2.0)


def test_throttled_never_shrinks_below_current_interval(tmp_path):
    pacer = Pacer.load(str(tmp_path / "pacing.json"), default_interval=30.0)
    widened = pacer.throttled("fanmtl", retry_after="5")  # server asks for less than we're already at
    assert widened == 30.0


def test_throttled_caps_at_max_interval(tmp_path):
    pacer = Pacer.load(str(tmp_path / "pacing.json"), default_interval=2.5)
    widened = pacer.throttled("fanmtl", retry_after="99999")
    assert widened == 120.0  # MAX_INTERVAL


def test_throttled_with_unparseable_retry_after_widens_by_backoff_factor(tmp_path):
    pacer = Pacer.load(str(tmp_path / "pacing.json"), default_interval=2.5)
    widened = pacer.throttled("fanmtl", retry_after="not-a-number")
    assert widened == 5.0


def test_throttled_persists_immediately(tmp_path):
    path = str(tmp_path / "pacing.json")
    pacer = Pacer.load(path, default_interval=2.5)
    pacer.throttled("fanmtl", retry_after="30")

    reloaded = Pacer.load(path, default_interval=2.5)
    assert reloaded.current_interval("fanmtl") == 30.0


def test_two_sites_stay_independent_in_same_file(tmp_path):
    pacer = Pacer.load(str(tmp_path / "pacing.json"), default_interval=2.5)
    pacer.throttled("fanmtl", retry_after="30")
    assert pacer.current_interval("fanmtl") == 30.0
    assert pacer.current_interval("other_site") == 2.5
