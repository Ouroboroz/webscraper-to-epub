import os

from ebooklib import epub, ITEM_DOCUMENT

from epub_scraper.epub_writer import build_epub


def test_build_epub_creates_file_at_output_path(tmp_path):
    out = str(tmp_path / "book.epub")
    build_epub("Title", "site", "id", [("Ch 1", "<p>Body.</p>")], out)
    assert os.path.exists(out)


def test_build_epub_creates_parent_directory_if_missing(tmp_path):
    out = str(tmp_path / "nested" / "dir" / "book.epub")
    build_epub("Title", "site", "id", [("Ch 1", "<p>Body.</p>")], out)
    assert os.path.exists(out)


def test_build_epub_chapter_order_and_titles_preserved(tmp_path):
    out = str(tmp_path / "book.epub")
    chapters = [("First", "<p>One.</p>"), ("Second", "<p>Two.</p>"), ("Third", "<p>Three.</p>")]
    build_epub("Title", "site", "id", chapters, out)

    book = epub.read_epub(out, options={"ignore_ncx": True})
    docs = sorted(book.get_items_of_type(ITEM_DOCUMENT), key=lambda i: i.get_name())
    chap_docs = [d for d in docs if d.get_name().startswith("chap_")]
    assert len(chap_docs) == 3
    assert b"One." in chap_docs[0].get_content()
    assert b"Two." in chap_docs[1].get_content()
    assert b"Three." in chap_docs[2].get_content()


def test_build_epub_empty_chapters_list_does_not_crash(tmp_path):
    out = str(tmp_path / "book.epub")
    build_epub("Title", "site", "id", [], out)
    assert os.path.exists(out)


def test_build_epub_escapes_ampersand_and_angle_brackets_in_title(tmp_path):
    out = str(tmp_path / "book.epub")
    build_epub("Title", "site", "id", [("Cats & Dogs <fight>", "<p>Body.</p>")], out)

    book = epub.read_epub(out, options={"ignore_ncx": True})
    chap = next(i for i in book.get_items_of_type(ITEM_DOCUMENT) if i.get_name().startswith("chap_"))
    content = chap.get_content().decode("utf-8")
    assert "&amp;" in content
    assert "&lt;fight&gt;" in content
    # raw, unescaped forms must not appear (would be malformed XHTML)
    assert "Cats & Dogs <fight>" not in content
