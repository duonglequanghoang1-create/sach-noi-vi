"""PDF and EPUB backends, built from real files generated in the test."""

from __future__ import annotations

from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")
ebooklib = pytest.importorskip("ebooklib")
epub_mod = pytest.importorskip("ebooklib.epub")

from sachnoi.extract import Section, detect_chapters  # noqa: E402
from sachnoi.extract import epub as epub_backend  # noqa: E402
from sachnoi.extract import pdf as pdf_backend  # noqa: E402

# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------

PROSE = [
    "Chương 1",
    "",
    "Cô bé ngồi bên bờ sông, nhìn chị mình đọc sách mà chẳng có gì để làm.",
    "Chị gái không ngẩng lên, lúc này cũng chẳng đáp.",
    "Cô bé nghĩ rằng một cuốn sách không có hình vẽ thì vô dụng.",
    "",
    "Chương 2",
    "",
    "Một con thỏ trắng chạy qua, tai đỏ ửng và mắt hồng hồng.",
    "Cô bé chạy theo nó ra khỏi bờ sông, vào rừng sâu.",
    "Ở đó có một ngôi nhà nhỏ bằng bàn cờ, bên cạnh một cái sổ.",
]


#: The built-in PDF fonts have no Vietnamese glyphs -- every diacritic comes out
#: as a middle dot. DejaVu is used instead so the fixture really is Vietnamese.
FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
FONTNAME = "dejavu"


def _fontname(page) -> str:
    if FONT.is_file():
        page.insert_font(fontname=FONTNAME, fontfile=str(FONT))
        return FONTNAME
    pytest.skip("no Unicode TTF available to build the fixture")


def _make_pdf(path, *, with_outline: bool = True, page_numbers: bool = True) -> None:
    doc = pymupdf.open()
    for chapter in (PROSE[:4], PROSE[4:]):
        page = doc.new_page()
        font = _fontname(page)
        top = 60
        for line in chapter:
            if not line:
                top += 10
                continue
            page.insert_text((50, top), line, fontsize=11, fontname=font)
            top += 18
        if page_numbers:
            # A page number in the middle of the text, as a real scan has.
            page.insert_text((280, 800), str(doc.page_count), fontsize=9, fontname=font)
    if with_outline:
        doc.set_toc([[1, "Chương 1", 1], [1, "Chương 2", 2]])
    doc.save(str(path))
    doc.close()


def test_pdf_outline_is_read(tmp_path) -> None:
    p = tmp_path / "book.pdf"
    _make_pdf(p)
    outline = pdf_backend.pdf_outline(p)
    assert [(t, lvl) for _, t, lvl in outline] == [("Chương 1", 1), ("Chương 2", 1)]


def test_pdf_outline_wins_when_it_is_usable(tmp_path) -> None:
    p = tmp_path / "book.pdf"
    _make_pdf(p)
    doc = pdf_backend.extract(p, min_words=10)
    assert doc.source_format == "pdf"
    assert [s.title for s in doc.sections] == ["Chương 1", "Chương 2"]


def test_pdf_without_an_outline_falls_back_to_heading_detection(tmp_path) -> None:
    p = tmp_path / "book.pdf"
    _make_pdf(p, with_outline=False)
    doc = pdf_backend.extract(p, min_words=10)
    assert len(doc.sections) == 2
    assert [s.title for s in doc.sections] == ["Chương 1", "Chương 2"]


def test_pdf_page_numbers_are_dropped(tmp_path) -> None:
    p = tmp_path / "book.pdf"
    _make_pdf(p, page_numbers=True)
    text, _pages = pdf_backend.extract_with_pymupdf(p)
    cleaned = pdf_backend._clean_pdf_text(text, pages=[text], use_layout=False)
    assert not [ln for ln in cleaned.split("\n") if ln.strip() in {"1", "2"}]


def test_pdf_columns_do_not_split_sentences(tmp_path) -> None:
    p = tmp_path / "book.pdf"
    _make_pdf(p, with_outline=False)
    doc = pdf_backend.extract(p, min_words=10)
    body = "\n".join(s.text for s in doc.sections)
    assert "vô dụng" in body
    assert "không có hình vẽ" in body  # a soft wrap was repaired


def test_pdftotext_and_pymupdf_agree_on_the_text(tmp_path) -> None:
    p = tmp_path / "book.pdf"
    _make_pdf(p, with_outline=False)
    if not pdf_backend.pdftotext_available():
        pytest.skip("poppler not installed")
    poppler, _ = pdf_backend.extract_with_pdftotext(p)
    mupdf, _ = pdf_backend.extract_with_pymupdf(p)
    for needle in ("thỏ trắng", "Chương 1"):
        assert needle in poppler
        assert needle in mupdf


def test_a_pdf_with_no_text_is_an_error(tmp_path) -> None:
    p = tmp_path / "blank.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(str(p))
    doc.close()
    with pytest.raises(ValueError) as exc:
        pdf_backend.extract(p)
    assert "scanned" in str(exc.value)


# --------------------------------------------------------------------------
# EPUB
# --------------------------------------------------------------------------

XHTML = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title></head><body>
<h1>Chương 1</h1>
<p>Cô bé ngồi bên bờ sông, nhìn chị mình đọc sách mà chẳng có gì để làm.
Chị gái không ngẩng lên, lúc này cũng chẳng đáp.</p>
<p>Cô bé nghĩ rằng một cuốn sách không có hình vẽ thì vô dụng.</p>
<figure><img src="a.png" alt="một hình vẽ"/><figcaption>Hình 1. Một cái sổ</figcaption></figure>
<table><tr><td>cột một</td><td>cột hai</td></tr></table>
<p>Ghi chú<sup class="footnote">[1]</sup> thêm cho có.</p>
<h1>Chương 2</h1>
<p>Một con thỏ trắng chạy qua, tai đỏ ửng và mắt hồng hồng.</p>
<p>Cô bé chạy theo nó ra khỏi bờ sông, vào rừng sâu.</p>
<ul><li>Một mục danh sách.</li><li>Mục thứ hai.</li></ul>
<nav><a href="x.html">điều hướng</a></nav>
</body></html>
"""


def _make_epub(path, markup: str = XHTML) -> None:
    book = epub_mod.EpubBook()
    book.set_identifier("id1")
    book.set_title("Cuốn sách thử nghiệm")
    book.set_language("vi")
    book.add_metadata("DC", "creator", "Nguyễn Du")
    item = epub_mod.EpubHtml(title="Chương", file_name="ch1.xhtml")
    # ebooklib wants bytes; a str makes it write an empty document.
    item.content = markup.encode("utf-8")
    book.add_item(item)
    book.toc = (item,)
    book.spine = [item]
    book.add_item(epub_mod.EpubNcx())
    epub_mod.write_epub(str(path), book)


def test_epub_chapters_come_from_the_headings(tmp_path) -> None:
    p = tmp_path / "book.epub"
    _make_epub(p)
    doc = epub_backend.extract(p, min_words=10)
    assert doc.source_format == "epub"
    assert [s.title for s in doc.sections] == ["Chương 1", "Chương 2"]


def test_epub_drops_images_tables_and_navigation(tmp_path) -> None:
    p = tmp_path / "book.epub"
    _make_epub(p)
    body = "\n".join(s.text for s in epub_backend.extract(p, min_words=10).sections)
    for noise in ("một hình vẽ", "Hình 1", "cột một", "điều hướng", "<img", "<table", "[1]"):
        assert noise not in body


def test_epub_keeps_the_prose_and_its_sentences(tmp_path) -> None:
    p = tmp_path / "book.epub"
    _make_epub(p)
    body = "\n".join(s.text for s in epub_backend.extract(p, min_words=10).sections)
    assert "không có hình vẽ thì vô dụng" in body
    assert "con thỏ trắng" in body


def test_epub_list_markers_are_removed(tmp_path) -> None:
    p = tmp_path / "book.epub"
    _make_epub(p)
    body = "\n".join(s.text for s in epub_backend.extract(p, min_words=10).sections)
    assert "Một mục danh sách." in body
    assert "<li" not in body


def test_epub_h3_only_book_still_yields_chapters(tmp_path) -> None:
    # A book whose top level is <h3>: the smallest heading present becomes the
    # chapter level, the same rule Sano applies to .docx.
    markup = XHTML.replace("<h1>", "<h3>").replace("</h1>", "</h3>")
    p = tmp_path / "h3.epub"
    _make_epub(p, markup)
    sections = epub_backend.parse_document(markup, min_words=10)
    assert [s.title for s in sections] == ["Chương 1", "Chương 2"]


def test_epub_ruby_readings_are_unwrapped_not_read_aloud() -> None:
    markup = """<html><body><h1>Chương 1</h1>
    <p><ruby>東京<rt>とうきょう</rt></ruby> là một thành phố.</p>
    <p>Câu hai đủ dài để được coi là một chương nhỏ trong tài liệu thử nghiệm.</p>
    </body></html>"""
    sections = epub_backend.parse_document(markup, min_words=5)
    body = "\n".join(s.text for s in sections)
    assert "東京" in body
    assert "とうきょう" not in body


def test_missing_epub_file(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        epub_backend.extract(tmp_path / "nope.epub")
