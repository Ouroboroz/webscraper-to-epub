import json
import os

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


# -- default_interval as a floor -------------------------------------------------

def test_explicit_default_interval_acts_as_a_floor_over_a_smaller_persisted_one(tmp_path):
    # --delay 30 must not be silently ignored just because this site has a small
    # value on record from one long-past throttle.
    path = str(tmp_path / "pacing.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"fanmtl": 5.0}, f)

    pacer = Pacer.load(path, default_interval=30.0)

    assert pacer.current_interval("fanmtl") == 30.0


def test_larger_persisted_interval_still_wins_over_the_default(tmp_path):
    path = str(tmp_path / "pacing.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"fanmtl": 40.0}, f)

    pacer = Pacer.load(path, default_interval=2.5)

    assert pacer.current_interval("fanmtl") == 40.0


# -- corrupt / hand-edited pacing.json --------------------------------------------

def test_load_of_truncated_json_falls_back_to_defaults_without_raising(tmp_path, capsys):
    # A cron job killed mid-write used to leave a file that took down every
    # future scheduled run, since Pacer.load() sits outside cmd_check's try/except.
    path = str(tmp_path / "pacing.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"fanmtl": 30.')

    pacer = Pacer.load(path, default_interval=2.5)

    assert pacer.current_interval("fanmtl") == 2.5
    assert "warning" in capsys.readouterr().out


def test_load_of_non_object_json_falls_back_to_defaults(tmp_path):
    path = str(tmp_path / "pacing.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump([1, 2, 3], f)

    assert Pacer.load(path, default_interval=2.5).current_interval("fanmtl") == 2.5


def test_load_of_non_numeric_value_falls_back_to_defaults(tmp_path):
    path = str(tmp_path / "pacing.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"fanmtl": "banana"}, f)

    assert Pacer.load(path, default_interval=2.5).current_interval("fanmtl") == 2.5


def test_load_coerces_a_hand_edited_string_value_to_float(tmp_path):
    path = str(tmp_path / "pacing.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"fanmtl": "45"}, f)

    pacer = Pacer.load(path, default_interval=2.5)

    assert pacer.current_interval("fanmtl") == 45.0
    assert isinstance(pacer.intervals["fanmtl"], float)


def test_a_corrupt_file_is_repaired_by_the_next_save(tmp_path):
    path = str(tmp_path / "pacing.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("not json at all")

    pacer = Pacer.load(path, default_interval=2.5)
    pacer.throttled("fanmtl", retry_after="30")

    assert Pacer.load(path, default_interval=2.5).current_interval("fanmtl") == 30.0


# -- atomic save ------------------------------------------------------------------

def test_save_leaves_no_temp_files_behind(tmp_path):
    path = str(tmp_path / "pacing.json")
    pacer = Pacer.load(path, default_interval=2.5)
    pacer.throttled("fanmtl", retry_after="30")
    pacer.throttled("other", retry_after="40")

    assert sorted(os.listdir(str(tmp_path))) == ["pacing.json"]


def test_save_never_truncates_the_old_file_when_the_write_fails(monkeypatch, tmp_path):
    # The atomicity guarantee: os.replace() only swaps a fully-written temp file
    # into place, so a failure mid-write leaves the previous contents intact --
    # a plain open(path, "w") would already have truncated them.
    path = str(tmp_path / "pacing.json")
    pacer = Pacer.load(path, default_interval=2.5)
    pacer.throttled("fanmtl", retry_after="30")

    def boom(*a, **kw):
        raise RuntimeError("disk full")
    monkeypatch.setattr("epub_scraper.pacing.json.dump", boom)

    pacer.intervals["fanmtl"] = 99.0
    try:
        pacer.save()
    except RuntimeError:
        pass

    assert Pacer.load(path, default_interval=2.5).current_interval("fanmtl") == 30.0
    assert sorted(os.listdir(str(tmp_path))) == ["pacing.json"]  # temp file cleaned up


def test_save_creates_missing_parent_directories(tmp_path):
    path = str(tmp_path / "nested" / "dir" / "pacing.json")
    pacer = Pacer.load(path, default_interval=2.5)
    pacer.throttled("fanmtl", retry_after="30")

    assert Pacer.load(path, default_interval=2.5).current_interval("fanmtl") == 30.0
