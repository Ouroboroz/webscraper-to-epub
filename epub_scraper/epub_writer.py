import os

from ebooklib import epub


def _escape_xhtml(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_epub(novel_title, site_key, chapter_id, chapters, output_file):
    book = epub.EpubBook()
    book.set_identifier(f"{site_key}-{chapter_id}")
    book.set_title(novel_title)
    book.set_language("en")

    css_content = b"""
body  { font-family: serif; line-height: 1.7; margin: 4% 6%; }
h1    { font-size: 1.3em; margin-bottom: 1.2em;
        border-bottom: 1px solid #ccc; padding-bottom: 0.3em; }
p     { margin: 0 0 0.8em 0; text-indent: 1.5em; }
"""
    css = epub.EpubItem(uid="main-css", file_name="style/main.css",
                        media_type="text/css", content=css_content)
    book.add_item(css)

    epub_chapters = []
    for i, (ch_title, body_html) in enumerate(chapters):
        fname = f"chap_{i+1:04d}.xhtml"
        safe_title = _escape_xhtml(ch_title)
        c = epub.EpubHtml(title=ch_title, file_name=fname, lang="en")
        c.content = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
            '<meta charset="utf-8"/>'
            f'<title>{safe_title}</title>'
            '<link rel="stylesheet" type="text/css" href="../style/main.css"/>'
            f"</head><body><h1>{safe_title}</h1>\n{body_html}\n</body></html>"
        ).encode("utf-8")
        book.add_item(c)
        epub_chapters.append(c)

    book.toc = epub_chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + epub_chapters

    directory = os.path.dirname(output_file)
    if directory:
        os.makedirs(directory, exist_ok=True)
    epub.write_epub(output_file, book)
