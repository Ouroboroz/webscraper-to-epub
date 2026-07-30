import os

import pytest

from epub_scraper.library import (add_novel, find_novel, load_library,
                                   record_check, record_email, remove_novel, save_library)


def test_load_library_missing_file_returns_fresh_structure_without_creating_file(library_path):
    lib = load_library(library_path)
    assert lib == {"version": 1, "novels": []}
    assert not os.path.exists(library_path)


def test_save_then_load_roundtrip(library_path):
    lib = load_library(library_path)
    add_novel(lib, site_key="s", chapter_id="c", index_url="https://x", title="T",
              output_file="epubs/x.epub")
    save_library(lib, library_path)

    reloaded = load_library(library_path)
    assert reloaded == lib


def test_save_library_atomic_no_tmp_file_left_behind(library_path, tmp_path):
    save_library(load_library(library_path), library_path)
    leftovers = [f for f in os.listdir(tmp_path) if f.startswith(".library.")]
    assert leftovers == []


def test_load_library_invalid_json_raises_valueerror(library_path):
    with open(library_path, "w") as f:
        f.write("{not valid json")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_library(library_path)


def test_load_library_wrong_shape_raises_valueerror(library_path):
    with open(library_path, "w") as f:
        f.write('{"foo": "bar"}')
    with pytest.raises(ValueError, match="unexpected library file shape"):
        load_library(library_path)


def test_find_novel_matches():
    lib = {"novels": [{"site_key": "s", "chapter_id": "c", "title": "T"}]}
    assert find_novel(lib, "s", "c")["title"] == "T"


def test_find_novel_none_when_absent():
    lib = {"novels": []}
    assert find_novel(lib, "s", "c") is None


def test_add_novel_appends_with_correct_defaults():
    lib = {"novels": []}
    entry = add_novel(lib, site_key="s", chapter_id="c", index_url="https://x",
                       title="T", output_file="epubs/x.epub")
    assert entry["last_known_chapter"] == 0
    assert entry["failed_chapters"] == []
    assert entry["consecutive_failed_checks"] == 0
    assert entry["enabled"] is True
    assert entry["last_checked_at"] is None
    assert entry["last_error"] is None
    assert entry["last_emailed_chapter"] == 0
    assert entry["last_emailed_at"] is None
    assert entry["last_email_error"] is None
    assert lib["novels"] == [entry]


def test_add_novel_raises_on_duplicate():
    lib = {"novels": []}
    add_novel(lib, site_key="s", chapter_id="c", index_url="https://x", title="T",
              output_file="epubs/x.epub")
    with pytest.raises(ValueError, match="already tracked"):
        add_novel(lib, site_key="s", chapter_id="c", index_url="https://x", title="T2",
                  output_file="epubs/x2.epub")


def test_remove_novel_true_when_present():
    lib = {"novels": []}
    add_novel(lib, site_key="s", chapter_id="c", index_url="https://x", title="T",
              output_file="epubs/x.epub")
    assert remove_novel(lib, "s", "c") is True
    assert lib["novels"] == []


def test_remove_novel_false_when_absent():
    lib = {"novels": []}
    assert remove_novel(lib, "s", "c") is False


def test_record_check_always_stamps_last_checked_at_and_error():
    entry = {"last_checked_at": None, "last_error": None, "title": "old",
              "last_known_chapter": 1, "last_updated_at": None}
    record_check(entry, error="boom")
    assert entry["last_checked_at"] is not None
    assert entry["last_error"] == "boom"
    assert entry["title"] == "old"  # not given -> unchanged


def test_record_check_title_only_updated_when_given():
    entry = {"last_checked_at": None, "last_error": None, "title": "old",
              "last_known_chapter": 1, "last_updated_at": None}
    record_check(entry, title="new")
    assert entry["title"] == "new"
    assert entry["last_error"] is None


def test_record_check_updated_true_advances_last_known_chapter():
    entry = {"last_checked_at": None, "last_error": None, "title": "T",
              "last_known_chapter": 1, "last_updated_at": None}
    record_check(entry, total=5, updated=True)
    assert entry["last_known_chapter"] == 5
    assert entry["last_updated_at"] is not None


def test_record_check_updated_false_leaves_last_known_chapter_untouched():
    entry = {"last_checked_at": None, "last_error": None, "title": "T",
              "last_known_chapter": 1, "last_updated_at": None}
    record_check(entry, updated=False)
    assert entry["last_known_chapter"] == 1
    assert entry["last_updated_at"] is None


def test_record_email_error_stamps_last_emailed_at_and_error_leaves_chapter_untouched():
    entry = {"last_emailed_chapter": 5, "last_emailed_at": None, "last_email_error": None}
    record_email(entry, error="smtp boom")
    assert entry["last_emailed_at"] is not None
    assert entry["last_email_error"] == "smtp boom"
    assert entry["last_emailed_chapter"] == 5  # untouched on error


def test_record_email_success_sets_last_emailed_chapter():
    entry = {"last_emailed_chapter": 5, "last_emailed_at": None, "last_email_error": "old error"}
    record_email(entry, chapter=200)
    assert entry["last_emailed_chapter"] == 200
    assert entry["last_email_error"] is None
    assert entry["last_emailed_at"] is not None
