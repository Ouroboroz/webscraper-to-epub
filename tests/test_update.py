import argparse
import copy
import os

import pytest
import requests

from epub_scraper import cache, update
from epub_scraper.library import add_novel, load_library, save_library
from conftest import load_fixture
from fakes import FakeResponse, FakeSession
from html_builders import fanmtl_chapter_html, fanmtl_index_html, fanmtl_index_html_no_links

BASE_URL = "https://www.fanmtl.com"
CHAPTER_ID = "abc"
INDEX_URL = f"{BASE_URL}/novel/{CHAPTER_ID}.html"


def chapter_url(n):
    return f"{BASE_URL}/novel/{CHAPTER_ID}_{n}.html"


def chapter_page(n):
    return fanmtl_chapter_html([f"Real prose for chapter {n}, long enough to keep around."],
                                title=f"Chapter {n}")


def index_page(total, title="Test Novel", chapter_id=CHAPTER_ID):
    return fanmtl_index_html(chapter_id=chapter_id, total=total, title=title)


def make_entry(**overrides):
    lib = {"novels": []}
    entry = add_novel(
        lib, site_key=overrides.pop("site_key", "fanmtl"),
        chapter_id=overrides.pop("chapter_id", CHAPTER_ID),
        index_url=overrides.pop("index_url", INDEX_URL),
        title=overrides.pop("title", "Test Novel"),
        output_file=overrides.pop("output_file", "epubs/placeholder.epub"),
        last_known_chapter=overrides.pop("last_known_chapter", 0))
    entry.update(overrides)
    return entry


class BuildEpubRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, novel_title, site_key, chapter_id, chapters, output_file):
        self.calls.append((novel_title, site_key, chapter_id, chapters, output_file))


def use_session(monkeypatch, session):
    monkeypatch.setattr(update, "_session_for", lambda url: session)


# ===== _check_one ================================================================

def test_check_one_unknown_site_key_returns_error(cache_dir):
    entry = make_entry(site_key="doesnotexist")
    status = update._check_one(entry, cache_dir, delay=0, dry_run=False)
    assert status == "error"
    assert entry["last_error"] == "unknown site_key 'doesnotexist'"


def test_check_one_raises_when_index_fetch_fails_direct_call(monkeypatch, cache_dir):
    session = FakeSession({INDEX_URL: FakeResponse("", 500, INDEX_URL)})
    use_session(monkeypatch, session)
    entry = make_entry()
    with pytest.raises(requests.HTTPError):
        update._check_one(entry, cache_dir, delay=0, dry_run=False)


def test_check_one_chapter_id_drift_prints_warning_and_keeps_stored_id(monkeypatch, cache_dir, capsys):
    html = index_page(total=5, chapter_id="xyz")
    session = FakeSession({INDEX_URL: FakeResponse(html, 200, INDEX_URL)})
    use_session(monkeypatch, session)
    entry = make_entry(chapter_id="abc", last_known_chapter=5)

    status = update._check_one(entry, cache_dir, delay=0, dry_run=False)

    out = capsys.readouterr().out
    assert "chapter_id drifted (abc -> xyz)" in out
    assert entry["chapter_id"] == "abc"
    assert status == "unchanged"


def test_check_one_total_none_returns_error(monkeypatch, cache_dir):
    html = fanmtl_index_html_no_links()
    session = FakeSession({INDEX_URL: FakeResponse(html, 200, INDEX_URL)})
    use_session(monkeypatch, session)
    entry = make_entry()

    status = update._check_one(entry, cache_dir, delay=0, dry_run=False)
    assert status == "error"
    assert entry["last_error"] == "could not determine chapter count"


def test_check_one_unchanged_when_no_new_chapters_and_no_failed(monkeypatch, cache_dir):
    html = index_page(total=5, title="Fresh Title")
    session = FakeSession({INDEX_URL: FakeResponse(html, 200, INDEX_URL)})
    use_session(monkeypatch, session)
    entry = make_entry(last_known_chapter=5)

    status = update._check_one(entry, cache_dir, delay=0, dry_run=False)
    assert status == "unchanged"
    assert entry["title"] == "Fresh Title"
    assert entry["last_checked_at"] is not None


def test_check_one_unchanged_does_not_touch_consecutive_failed_checks(monkeypatch, cache_dir):
    html = index_page(total=5)
    session = FakeSession({INDEX_URL: FakeResponse(html, 200, INDEX_URL)})
    use_session(monkeypatch, session)
    entry = make_entry(last_known_chapter=5, consecutive_failed_checks=3)

    update._check_one(entry, cache_dir, delay=0, dry_run=False)
    assert entry["consecutive_failed_checks"] == 3


# -- dry run ------------------------------------------------------------------

def test_check_one_dry_run_new_chapters_only_prints_delta_line(monkeypatch, cache_dir, capsys):
    html = index_page(total=7)
    session = FakeSession({INDEX_URL: FakeResponse(html, 200, INDEX_URL)})
    use_session(monkeypatch, session)
    entry = make_entry(last_known_chapter=5)

    status = update._check_one(entry, cache_dir, delay=0, dry_run=True)
    out = capsys.readouterr().out
    assert status == "dry-run"
    assert "2 new chapter(s) available (6..7)" in out
    assert "retried" not in out


def test_check_one_dry_run_failed_retry_only_prints_retry_line(monkeypatch, cache_dir, capsys):
    html = index_page(total=5)
    session = FakeSession({INDEX_URL: FakeResponse(html, 200, INDEX_URL)})
    use_session(monkeypatch, session)
    entry = make_entry(last_known_chapter=5, failed_chapters=[3])

    status = update._check_one(entry, cache_dir, delay=0, dry_run=True)
    out = capsys.readouterr().out
    assert status == "dry-run"
    assert "new chapter(s) available" not in out
    assert "1 previously-failed chapter(s) would be retried" in out


def test_check_one_dry_run_both_prints_both_lines(monkeypatch, cache_dir, capsys):
    html = index_page(total=7)
    session = FakeSession({INDEX_URL: FakeResponse(html, 200, INDEX_URL)})
    use_session(monkeypatch, session)
    entry = make_entry(last_known_chapter=5, failed_chapters=[3])

    update._check_one(entry, cache_dir, delay=0, dry_run=True)
    out = capsys.readouterr().out
    assert "2 new chapter(s) available (6..7)" in out
    assert "1 previously-failed chapter(s) would be retried" in out


def test_check_one_dry_run_leaves_entry_byte_for_byte_untouched(monkeypatch, cache_dir):
    html = index_page(total=7)
    session = FakeSession({INDEX_URL: FakeResponse(html, 200, INDEX_URL)})
    use_session(monkeypatch, session)
    entry = make_entry(last_known_chapter=5, failed_chapters=[3])
    before = copy.deepcopy(entry)

    update._check_one(entry, cache_dir, delay=0, dry_run=True)
    assert entry == before


# -- retry-only path (delta<=0, has failed_chapters) -----------------------------

def test_check_one_retry_all_failed_succeed_rebuilds_epub(monkeypatch, cache_dir):
    for n in (1, 3, 5):
        cache.save_cache(cache_dir, CHAPTER_ID, n, chapter_page(n))
    session = FakeSession({
        INDEX_URL: FakeResponse(index_page(total=5), 200, INDEX_URL),
        chapter_url(2): FakeResponse(chapter_page(2), 200, chapter_url(2)),
        chapter_url(4): FakeResponse(chapter_page(4), 200, chapter_url(4)),
    })
    use_session(monkeypatch, session)
    recorder = BuildEpubRecorder()
    monkeypatch.setattr(update, "build_epub", recorder)
    entry = make_entry(last_known_chapter=5, failed_chapters=[2, 4])

    status = update._check_one(entry, cache_dir, delay=0, dry_run=False)

    assert entry["failed_chapters"] == []
    assert len(recorder.calls) == 1
    assert len(recorder.calls[0][3]) == 5  # chapters arg
    assert status == "updated"


def test_check_one_retry_none_succeed_skips_rebuild_entirely(monkeypatch, cache_dir):
    session = FakeSession({
        INDEX_URL: FakeResponse(index_page(total=5), 200, INDEX_URL),
        chapter_url(2): FakeResponse("", 500, chapter_url(2)),
        chapter_url(4): FakeResponse("", 500, chapter_url(4)),
    })
    use_session(monkeypatch, session)
    recorder = BuildEpubRecorder()
    monkeypatch.setattr(update, "build_epub", recorder)
    entry = make_entry(last_known_chapter=5, failed_chapters=[2, 4])

    status = update._check_one(entry, cache_dir, delay=0, dry_run=False)

    assert recorder.calls == []
    assert entry["failed_chapters"] == [2, 4]
    assert status == "no-progress"


def test_check_one_retry_partial_success_still_triggers_rebuild(monkeypatch, cache_dir):
    for n in (1, 3, 5):
        cache.save_cache(cache_dir, CHAPTER_ID, n, chapter_page(n))
    session = FakeSession({
        INDEX_URL: FakeResponse(index_page(total=5), 200, INDEX_URL),
        chapter_url(2): FakeResponse(chapter_page(2), 200, chapter_url(2)),
        chapter_url(4): FakeResponse("", 500, chapter_url(4)),
    })
    use_session(monkeypatch, session)
    recorder = BuildEpubRecorder()
    monkeypatch.setattr(update, "build_epub", recorder)
    entry = make_entry(last_known_chapter=5, failed_chapters=[2, 4])

    status = update._check_one(entry, cache_dir, delay=0, dry_run=False)

    assert entry["failed_chapters"] == [4]
    assert len(recorder.calls) == 1
    assert status == "updated"


def test_check_one_retry_breaker_trip_drops_untried_failed_chapters(monkeypatch, cache_dir):
    # Characterization test, not a fix: scrape_chapters' internal circuit
    # breaker trips after 3 consecutive failures (2, 4, 6), so chapter 8 --
    # sorted last in the retry list -- is never attempted this round. But
    # entry["failed_chapters"] is set to exactly what scrape_chapters
    # returned (only chapters actually attempted), so chapter 8 silently
    # falls out of tracking instead of staying recorded as still-failed.
    session = FakeSession({
        INDEX_URL: FakeResponse(index_page(total=8), 200, INDEX_URL),
        chapter_url(2): FakeResponse("", 500, chapter_url(2)),
        chapter_url(4): FakeResponse("", 500, chapter_url(4)),
        chapter_url(6): FakeResponse("", 500, chapter_url(6)),
        # chapter_url(8) deliberately unstubbed -- must never be reached
    })
    use_session(monkeypatch, session)
    recorder = BuildEpubRecorder()
    monkeypatch.setattr(update, "build_epub", recorder)
    entry = make_entry(last_known_chapter=8, failed_chapters=[2, 4, 6, 8])

    status = update._check_one(entry, cache_dir, delay=0, dry_run=False)

    assert entry["failed_chapters"] == [2, 4, 6]  # 8 dropped, not preserved
    assert recorder.calls == []
    assert status == "no-progress"


# -- full-rebuild path (delta > 0) ------------------------------------------------

def test_check_one_full_rebuild_sporadic_failures_still_advance_last_known(monkeypatch, cache_dir):
    for n in range(1, 6):
        cache.save_cache(cache_dir, CHAPTER_ID, n, chapter_page(n))
    responses = {INDEX_URL: FakeResponse(index_page(total=10), 200, INDEX_URL)}
    for n in (7, 9, 10):
        responses[chapter_url(n)] = FakeResponse(chapter_page(n), 200, chapter_url(n))
    for n in (6, 8):
        responses[chapter_url(n)] = FakeResponse("", 500, chapter_url(n))
    session = FakeSession(responses)
    use_session(monkeypatch, session)
    recorder = BuildEpubRecorder()
    monkeypatch.setattr(update, "build_epub", recorder)
    entry = make_entry(last_known_chapter=5)

    status = update._check_one(entry, cache_dir, delay=0, dry_run=False)

    assert entry["last_known_chapter"] == 10  # never a 3-in-a-row streak -> never trips
    assert entry["failed_chapters"] == [6, 8]
    assert status == "updated"
    assert entry["consecutive_failed_checks"] == 0


def test_check_one_full_rebuild_breaker_trip_sets_last_known_and_error(monkeypatch, cache_dir):
    responses = {INDEX_URL: FakeResponse(index_page(total=10), 200, INDEX_URL)}
    for n in (1, 2):
        responses[chapter_url(n)] = FakeResponse(chapter_page(n), 200, chapter_url(n))
    for n in (3, 4, 5):
        responses[chapter_url(n)] = FakeResponse("", 500, chapter_url(n))
    # 6..10 deliberately unstubbed -- must never be reached after the trip
    session = FakeSession(responses)
    use_session(monkeypatch, session)
    monkeypatch.setattr(update, "build_epub", BuildEpubRecorder())
    entry = make_entry(last_known_chapter=0)

    status = update._check_one(entry, cache_dir, delay=0, dry_run=False)

    assert entry["last_known_chapter"] == 2  # stopped_at(3) - 1
    assert entry["last_error"] == "circuit breaker: 3 consecutive failures starting at chapter 3"
    assert chapter_url(6) not in [c[1] for c in session.calls]
    assert status == "updated"  # fetched_count = 2 > 0 (chapters 1, 2)


def test_check_one_full_rebuild_immediate_breaker_trip_yields_zero_progress(monkeypatch, cache_dir):
    responses = {INDEX_URL: FakeResponse(index_page(total=5), 200, INDEX_URL)}
    for n in (1, 2, 3):
        responses[chapter_url(n)] = FakeResponse("", 500, chapter_url(n))
    session = FakeSession(responses)
    use_session(monkeypatch, session)
    monkeypatch.setattr(update, "build_epub", BuildEpubRecorder())
    entry = make_entry(last_known_chapter=0)

    status = update._check_one(entry, cache_dir, delay=0, dry_run=False)

    assert entry["last_known_chapter"] == 0
    assert entry["last_updated_at"] is None
    assert status == "no-progress"


def test_check_one_full_rebuild_regressed_chapters_clamp_fetched_count_to_zero(monkeypatch, cache_dir):
    # chapters 1, 2 (already "known good") fail on this refetch attempt, so
    # len(all_chapters) ends up SMALLER than last_known_chapter -- the
    # `max(0, ...)` guard in update.py must clamp fetched_count to 0, not go
    # negative.
    for n in (3, 4, 5):
        cache.save_cache(cache_dir, CHAPTER_ID, n, chapter_page(n))
    session = FakeSession({
        INDEX_URL: FakeResponse(index_page(total=6), 200, INDEX_URL),
        chapter_url(1): FakeResponse("", 500, chapter_url(1)),
        chapter_url(2): FakeResponse("", 500, chapter_url(2)),
        chapter_url(6): FakeResponse(chapter_page(6), 200, chapter_url(6)),
    })
    use_session(monkeypatch, session)
    monkeypatch.setattr(update, "build_epub", BuildEpubRecorder())
    entry = make_entry(last_known_chapter=5)

    status = update._check_one(entry, cache_dir, delay=0, dry_run=False)

    assert entry["last_known_chapter"] == 6
    assert entry["failed_chapters"] == [1, 2]
    assert status == "no-progress"  # fetched_count clamped to 0, not -1


# -- consecutive_failed_checks / auto-disable -------------------------------------

def test_check_one_fetched_count_positive_resets_consecutive_failed_checks(monkeypatch, cache_dir):
    session = FakeSession({
        INDEX_URL: FakeResponse(index_page(total=1), 200, INDEX_URL),
        chapter_url(1): FakeResponse(chapter_page(1), 200, chapter_url(1)),
    })
    use_session(monkeypatch, session)
    monkeypatch.setattr(update, "build_epub", BuildEpubRecorder())
    entry = make_entry(last_known_chapter=0, consecutive_failed_checks=4)

    status = update._check_one(entry, cache_dir, delay=0, dry_run=False)
    assert entry["consecutive_failed_checks"] == 0
    assert status == "updated"


def test_check_one_fetched_count_zero_increments_consecutive_failed_checks(monkeypatch, cache_dir):
    session = FakeSession({
        INDEX_URL: FakeResponse(index_page(total=5), 200, INDEX_URL),
        chapter_url(3): FakeResponse("", 500, chapter_url(3)),
    })
    use_session(monkeypatch, session)
    monkeypatch.setattr(update, "build_epub", BuildEpubRecorder())
    entry = make_entry(last_known_chapter=5, failed_chapters=[3], consecutive_failed_checks=2)

    status = update._check_one(entry, cache_dir, delay=0, dry_run=False)
    assert entry["consecutive_failed_checks"] == 3
    assert status == "no-progress"


def test_check_one_auto_disables_at_threshold(monkeypatch, cache_dir):
    session = FakeSession({
        INDEX_URL: FakeResponse(index_page(total=5), 200, INDEX_URL),
        chapter_url(3): FakeResponse("", 500, chapter_url(3)),
    })
    use_session(monkeypatch, session)
    monkeypatch.setattr(update, "build_epub", BuildEpubRecorder())
    entry = make_entry(last_known_chapter=5, failed_chapters=[3], consecutive_failed_checks=4)

    status = update._check_one(entry, cache_dir, delay=0, dry_run=False)

    assert entry["consecutive_failed_checks"] == 5
    assert entry["enabled"] is False
    assert "auto-disabled after 5 consecutive checks" in entry["last_error"]
    assert status == "disabled"


def test_check_one_auto_disable_message_overrides_breaker_message(monkeypatch, cache_dir):
    responses = {INDEX_URL: FakeResponse(index_page(total=5), 200, INDEX_URL)}
    for n in (1, 2, 3):
        responses[chapter_url(n)] = FakeResponse("", 500, chapter_url(n))
    session = FakeSession(responses)
    use_session(monkeypatch, session)
    monkeypatch.setattr(update, "build_epub", BuildEpubRecorder())
    entry = make_entry(last_known_chapter=0, consecutive_failed_checks=4)

    status = update._check_one(entry, cache_dir, delay=0, dry_run=False)

    assert status == "disabled"
    assert entry["last_error"].startswith("auto-disabled after 5 consecutive checks")
    assert "circuit breaker" not in entry["last_error"]


def test_check_one_stays_enabled_below_auto_disable_threshold(monkeypatch, cache_dir):
    session = FakeSession({
        INDEX_URL: FakeResponse(index_page(total=5), 200, INDEX_URL),
        chapter_url(3): FakeResponse("", 500, chapter_url(3)),
    })
    use_session(monkeypatch, session)
    monkeypatch.setattr(update, "build_epub", BuildEpubRecorder())
    entry = make_entry(last_known_chapter=5, failed_chapters=[3], consecutive_failed_checks=3)

    status = update._check_one(entry, cache_dir, delay=0, dry_run=False)
    assert entry["consecutive_failed_checks"] == 4
    assert entry["enabled"] is True
    assert status == "no-progress"


# ===== _retarget_output ===========================================================

def test_retarget_output_renames_removes_old_file_when_name_changes(tmp_path):
    old_path = tmp_path / "[Ch 1 - Ch 5] My Title.epub"
    old_path.write_text("old content")
    entry = {"output_file": str(old_path)}

    new_path = update._retarget_output(entry, "My Title", 10)

    assert not old_path.exists()
    assert entry["output_file"] == new_path
    assert new_path == "epubs/[Ch 1 - Ch 10] My Title.epub"


def test_retarget_output_noop_when_filename_unchanged(tmp_path):
    unchanged_path = "epubs/[Ch 1 - Ch 5] My Title.epub"
    real_file = tmp_path / "kept.epub"
    real_file.write_text("keep me")
    entry = {"output_file": str(real_file)}

    # deliberately compute the SAME path twice to prove no removal happens
    new_path = update._retarget_output(entry, "My Title", 5)
    assert new_path != str(real_file)  # sanity: different because unrelated paths

    # now test true no-op: old path already equals the freshly computed path
    entry2 = {"output_file": unchanged_path}
    result = update._retarget_output(entry2, "My Title", 5)
    assert result == unchanged_path
    assert entry2["output_file"] == unchanged_path


def test_retarget_output_missing_old_file_does_not_raise(tmp_path):
    entry = {"output_file": str(tmp_path / "does-not-exist.epub")}
    new_path = update._retarget_output(entry, "Title", 3)
    assert entry["output_file"] == new_path


def test_retarget_output_entry_missing_output_file_key():
    entry = {}
    new_path = update._retarget_output(entry, "Title", 3)
    assert entry["output_file"] == new_path
    assert new_path == "epubs/[Ch 1 - Ch 3] Title.epub"


def test_retarget_output_title_sanitization_reflected_in_path():
    entry = {}
    new_path = update._retarget_output(entry, "A: B/C", 1)
    assert new_path == "epubs/[Ch 1 - Ch 1] A - BC.epub"


# ===== _parse_only ================================================================

def test_parse_only_none_or_empty_returns_none():
    assert update._parse_only(None) is None
    assert update._parse_only([]) is None


def test_parse_only_parses_site_colon_id_pairs():
    assert update._parse_only(["fanmtl:abc", "other:xyz"]) == {("fanmtl", "abc"), ("other", "xyz")}


def test_parse_only_missing_colon_raises_systemexit():
    with pytest.raises(SystemExit):
        update._parse_only(["not-valid"])


# ===== _session_for ================================================================

def test_session_for_sets_referer_and_default_headers():
    session = update._session_for("https://www.fanmtl.com/novel/x.html")
    assert session.headers["Referer"] == "https://www.fanmtl.com"
    assert "User-Agent" in session.headers


# ===== cmd_add / cmd_remove / cmd_list / cmd_search =================================

def test_cmd_add_happy_path_creates_entry(tmp_path, monkeypatch):
    library_path = str(tmp_path / "library.json")
    session = FakeSession({INDEX_URL: FakeResponse(index_page(total=10), 200, INDEX_URL)})
    use_session(monkeypatch, session)

    args = argparse.Namespace(url=INDEX_URL, site=None, output=None, last_known=None,
                               library=library_path)
    update.cmd_add(args)

    lib = load_library(library_path)
    assert len(lib["novels"]) == 1
    assert lib["novels"][0]["chapter_id"] == CHAPTER_ID
    assert lib["novels"][0]["last_known_chapter"] == 0


def test_cmd_add_missing_chapter_id_exits_1(tmp_path, monkeypatch):
    url = "https://www.fanmtl.com/not-a-novel-path"
    session = FakeSession({url: FakeResponse(fanmtl_index_html_no_links(), 200, url)})
    use_session(monkeypatch, session)
    args = argparse.Namespace(url=url, site=None, output=None, last_known=None,
                               library=str(tmp_path / "library.json"))
    with pytest.raises(SystemExit) as exc_info:
        update.cmd_add(args)
    assert exc_info.value.code == 1


def test_cmd_add_duplicate_exits_1(tmp_path, monkeypatch):
    library_path = str(tmp_path / "library.json")
    lib = {"novels": []}
    add_novel(lib, site_key="fanmtl", chapter_id=CHAPTER_ID, index_url=INDEX_URL,
              title="T", output_file="epubs/x.epub")
    save_library(lib, library_path)

    session = FakeSession({INDEX_URL: FakeResponse(index_page(total=10), 200, INDEX_URL)})
    use_session(monkeypatch, session)
    args = argparse.Namespace(url=INDEX_URL, site=None, output=None, last_known=None,
                               library=library_path)
    with pytest.raises(SystemExit) as exc_info:
        update.cmd_add(args)
    assert exc_info.value.code == 1


def test_cmd_add_index_fetch_failure_exits_1(tmp_path, monkeypatch):
    session = FakeSession({INDEX_URL: FakeResponse("", 500, INDEX_URL)})
    use_session(monkeypatch, session)
    args = argparse.Namespace(url=INDEX_URL, site=None, output=None, last_known=None,
                               library=str(tmp_path / "library.json"))
    with pytest.raises(SystemExit) as exc_info:
        update.cmd_add(args)
    assert exc_info.value.code == 1


def test_cmd_remove_existing(tmp_path):
    library_path = str(tmp_path / "library.json")
    lib = {"novels": []}
    add_novel(lib, site_key="fanmtl", chapter_id=CHAPTER_ID, index_url=INDEX_URL,
              title="T", output_file="epubs/x.epub")
    save_library(lib, library_path)

    args = argparse.Namespace(site_key="fanmtl", chapter_id=CHAPTER_ID, library=library_path)
    update.cmd_remove(args)
    assert load_library(library_path)["novels"] == []


def test_cmd_remove_nonexistent_exits_1(tmp_path):
    library_path = str(tmp_path / "library.json")
    args = argparse.Namespace(site_key="fanmtl", chapter_id="nope", library=library_path)
    with pytest.raises(SystemExit) as exc_info:
        update.cmd_remove(args)
    assert exc_info.value.code == 1


def test_cmd_list_empty(tmp_path, capsys):
    args = argparse.Namespace(library=str(tmp_path / "library.json"))
    update.cmd_list(args)
    assert "No tracked novels." in capsys.readouterr().out


def test_cmd_list_shows_disabled_failed_error_annotations(tmp_path, capsys):
    library_path = str(tmp_path / "library.json")
    lib = {"novels": []}
    entry = add_novel(lib, site_key="fanmtl", chapter_id=CHAPTER_ID, index_url=INDEX_URL,
                       title="T", output_file="epubs/x.epub")
    entry["enabled"] = False
    entry["failed_chapters"] = [1, 2]
    entry["last_error"] = "boom"
    save_library(lib, library_path)

    update.cmd_list(argparse.Namespace(library=library_path))
    out = capsys.readouterr().out
    assert "DISABLED" in out
    assert "failed=[1, 2]" in out
    assert "error='boom'" in out


def test_cmd_search_case_insensitive(tmp_path, capsys):
    library_path = str(tmp_path / "library.json")
    lib = {"novels": []}
    add_novel(lib, site_key="fanmtl", chapter_id=CHAPTER_ID, index_url=INDEX_URL,
              title="The Cult of Farming", output_file="epubs/x.epub")
    save_library(lib, library_path)

    update.cmd_search(argparse.Namespace(query="CULT", library=library_path))
    assert "The Cult of Farming" in capsys.readouterr().out


def test_cmd_search_no_match(tmp_path, capsys):
    library_path = str(tmp_path / "library.json")
    save_library({"version": 1, "novels": []}, library_path)
    update.cmd_search(argparse.Namespace(query="zzz", library=library_path))
    assert "No tracked novels matching 'zzz'" in capsys.readouterr().out


# ===== cmd_find ====================================================================

def use_requests_session(monkeypatch, session):
    monkeypatch.setattr(update.requests, "Session", lambda: session)


def test_cmd_find_uses_correct_post_payload_and_limit_truncates(monkeypatch, capsys):
    from epub_scraper.sites.fanmtl import PROFILE
    html = load_fixture("fanmtl_search_results.html")
    session = FakeSession({PROFILE.search_url: FakeResponse(html, 200, PROFILE.search_url)})
    use_requests_session(monkeypatch, session)

    update.cmd_find(argparse.Namespace(query="cult", site=None, limit=5))

    out = capsys.readouterr().out
    assert out.count("https://www.fanmtl.com/novel/") == 5
    method, url, kwargs = session.calls[0]
    assert kwargs["data"]["keyboard"] == "cult"


def test_cmd_find_no_results(monkeypatch, capsys):
    from epub_scraper.sites.fanmtl import PROFILE
    session = FakeSession({PROFILE.search_url: FakeResponse("<ul></ul>", 200, PROFILE.search_url)})
    use_requests_session(monkeypatch, session)

    update.cmd_find(argparse.Namespace(query="zzz", site=None, limit=15))
    assert "No results." in capsys.readouterr().out


def test_cmd_find_site_search_error_prints_and_continues(monkeypatch, capsys):
    from epub_scraper.sites.fanmtl import PROFILE
    session = FakeSession({PROFILE.search_url: requests.exceptions.ConnectionError("down")})
    use_requests_session(monkeypatch, session)

    update.cmd_find(argparse.Namespace(query="x", site=None, limit=15))
    out = capsys.readouterr().out
    assert "[fanmtl] search failed: down" in out
    assert "No results." in out


# ===== cmd_grep ====================================================================

def test_cmd_grep_missing_dir_exits_1(tmp_path):
    args = argparse.Namespace(epubs_dir=str(tmp_path / "nope"), query="x",
                               case_sensitive=False, context=60)
    with pytest.raises(SystemExit) as exc_info:
        update.cmd_grep(args)
    assert exc_info.value.code == 1


def test_cmd_grep_no_epub_files(epubs_dir, capsys):
    args = argparse.Namespace(epubs_dir=epubs_dir, query="x", case_sensitive=False, context=60)
    update.cmd_grep(args)
    assert "No .epub files found" in capsys.readouterr().out


def test_cmd_grep_finds_hits_across_multiple_epubs(epubs_dir, capsys):
    from epub_scraper.epub_writer import build_epub
    build_epub("Book One", "s", "1", [("Ch 1", "<p>The dragon roared.</p>")],
               os.path.join(epubs_dir, "one.epub"))
    build_epub("Book Two", "s", "2", [("Ch 1", "<p>Another dragon appeared.</p>")],
               os.path.join(epubs_dir, "two.epub"))

    update.cmd_grep(argparse.Namespace(epubs_dir=epubs_dir, query="dragon",
                                        case_sensitive=False, context=60))
    out = capsys.readouterr().out
    assert "one.epub" in out
    assert "two.epub" in out
    assert "2 match(es) across 2 epub(s)" in out


def test_cmd_grep_case_sensitivity_flag(epubs_dir, capsys):
    from epub_scraper.epub_writer import build_epub
    build_epub("Book", "s", "1", [("Ch 1", "<p>The Dragon roared.</p>")],
               os.path.join(epubs_dir, "one.epub"))

    update.cmd_grep(argparse.Namespace(epubs_dir=epubs_dir, query="dragon",
                                        case_sensitive=True, context=60))
    assert "0 match(es)" in capsys.readouterr().out


# ===== build_parser ================================================================

def test_build_parser_check_defaults():
    args = update.build_parser().parse_args(["check"])
    assert args.delay == 2.5
    assert args.novel_delay == 5.0
    assert args.cache_dir == ".cache"
    assert args.dry_run is False
    assert args.only is None


def test_build_parser_add_requires_url():
    with pytest.raises(SystemExit):
        update.build_parser().parse_args(["add"])


def test_build_parser_only_repeatable():
    args = update.build_parser().parse_args(["check", "--only", "a:b", "--only", "c:d"])
    assert args.only == ["a:b", "c:d"]
