"""TXT / Markdown backend: chapter detection and wrapped-line repair."""

from __future__ import annotations

import pytest

from sachnoi.extract import Section, detect_chapters, drop_page_number_lines
from sachnoi.extract import txt


# --------------------------------------------------------------------------
# Wrapped-line repair: the thing a text backend exists to get right
# --------------------------------------------------------------------------


def test_wrapped_lines_are_rejoined_into_one_sentence() -> None:
    raw = (
        "Cô bé nghĩ rằng một cuốn sách không có hình vẽ\n"
        "thì cũng chẳng có ý nghĩa gì cả.\n"
    )
    out = txt.join_wrapped_lines(raw)
    # One sentence in, one line out -- which is exactly the markdown contract.
    assert out.strip() == "Cô bé nghĩ rằng một cuốn sách không có hình vẽ thì cũng chẳng có ý nghĩa gì cả."


def test_printed_column_margins_do_not_split_sentences() -> None:
    # `pdftotext -layout` pads every line to the column width; if that padding
    # were treated as a line break, every sentence would become several.
    raw = "Câu này ngắn.\nCâu này dài hơn và bị đẩy sang dòng kế.\n"
    out = txt.join_wrapped_lines(raw)
    assert out.count("\n") == 1
    assert "bị đẩy sang dòng kế." in out


def test_a_blank_line_still_breaks_the_paragraph() -> None:
    raw = "Câu một nằm trên\ndòng này.\n\nCâu hai nằm ở\nđoạn khác.\n"
    out = txt.join_wrapped_lines(raw)
    assert out.count("\n\n") == 1
    assert len([p for p in out.split("\n\n") if p.strip()]) == 2


def test_surrounding_blank_lines_are_squeezed_to_one() -> None:
    out = txt.join_wrapped_lines("A.\n\n\n\n\nB.\n\n\n")
    assert out == "A.\n\nB."


def test_dehyphenation_joins_a_word_split_by_a_hyphen() -> None:
    assert "chuyến-" not in txt.dehyphenate("chuyến-\ntàu")
    assert txt.dehyphenate("chuyến-\ntàu") == "chuyếntàu"


def test_dehyphenation_leaves_a_real_hyphen_alone() -> None:
    # No line break, no join: `chiếc-xe` is one compound.
    assert txt.dehyphenate("chiếc-xe") == "chiếc-xe"
    # A break followed by a digit is not a split word either.
    assert txt.dehyphenate("thứ-\n2") == "thứ-\n2"


def test_markdown_headings_survive_the_repair() -> None:
    raw = "# Chương 1\n\nDòng một\nvẫn còn.\n\n## Mục 1\n\nNội dung mục.\n"
    out = txt.join_wrapped_lines(raw)
    assert "# Chương 1" in out
    assert "## Mục 1" in out
    assert "Dòng một vẫn còn." in out


# --------------------------------------------------------------------------
# Markdown parsing
# --------------------------------------------------------------------------


def test_markdown_headings_become_wikitext_headings() -> None:
    out = txt.parse_markdown("# Chương 1\n\nNội dung.\n\n### Tiểu mục\n\nThêm.\n")
    assert "== Chương 1 ==" in out
    assert "==== Tiểu mục ====" in out


def test_markdown_list_and_quote_markers_are_removed() -> None:
    out = txt.parse_markdown("- một\n* hai\n1. ba\n> trích\n")
    assert out.split("\n")[:3] == ["một", "hai", "ba"]
    assert "trích" in out


def test_markdown_code_fences_do_not_become_chapters() -> None:
    out = txt.parse_markdown("# Chương 1\n\n```\n# không phải tiêu đề\n```\n\nCâu văn trong sách.\n")
    sections = detect_chapters(out, min_words=1)
    assert [s.title for s in sections] == ["Chương 1"]


# --------------------------------------------------------------------------
# Chapter detection
# --------------------------------------------------------------------------


def test_chapter_keyword_without_markdown() -> None:
    text = "\n\n".join(
        [
            "Chương 1\n\n" + ("A " * 60),
            "Chương 2\n\n" + ("B " * 60),
            "Chương 3\n\n" + ("C " * 60),
        ]
    )
    assert [s.title for s in detect_chapters(text)] == ["Chương 1", "Chương 2", "Chương 3"]


def test_vietnamese_and_english_chapter_words() -> None:
    for word in ("Chương", "Phần", "Mục", "Bài", "Chapter", "Part"):
        text = f"{word} 1\n\n" + ("A " * 60) + f"\n\n{word} 2\n\n" + ("B " * 60)
        titles = [s.title for s in detect_chapters(text)]
        assert len(titles) == 2, f"{word!r} not recognised as a chapter word"


def test_all_caps_line_is_treated_as_a_heading() -> None:
    text = "MỞ ĐẦU\n\n" + ("A " * 60) + "\n\nKẾT\n\n" + ("B " * 60)
    titles = [s.title for s in detect_chapters(text)]
    assert titles == ["MỞ ĐẦU", "KẾT"]


def test_bare_numbers_are_ignored_in_a_long_document() -> None:
    # "12." in a 100k-word novel is a list item, not a chapter.
    body = "Câu văn xin chữ. " * 2000  # 8000 words
    text = "12.\n\n" + body + "\n\nChương 1\n\n" + ("A " * 60)
    titles = [s.title for s in detect_chapters(text)]
    # "12." is a list item, not a chapter; the unlabelled body before the first
    # real heading becomes a front-matter chapter instead.
    assert "12." not in titles
    assert "Chương 1" in titles


def test_bare_numbers_are_trusted_in_a_short_document() -> None:
    text = "1\n\n" + ("A " * 60) + "\n\n2\n\n" + ("B " * 60)
    assert [s.title for s in detect_chapters(text)] == ["1", "2"]


def test_preamble_before_the_first_chapter_is_kept() -> None:
    text = "Lời nói đầu. " * 30 + "\n\nChương 1\n\n" + ("A " * 120)
    sections = detect_chapters(text)
    assert [s.title for s in sections] == ["Phần mở đầu", "Chương 1"]
    assert "Lời nói đầu" in sections[0].text


def test_a_stub_preamble_is_merged_into_the_first_chapter() -> None:
    text = "Lời nói đầu.\n\nChương 1\n\n" + ("A " * 60)
    assert [s.title for s in detect_chapters(text)] == ["Chương 1"]


def test_a_document_that_is_only_apparatus_yields_nothing() -> None:
    assert detect_chapters("MỤC LỤC\n\nMột .... 3\nHai .... 7") == []


def test_table_of_contents_is_not_a_chapter() -> None:
    text = "\n\n".join(
        [
            "MỤC LỤC\n\nMột. Mục một ....... 3\nHai. Mục hai ....... 7\n",
            "Chương 1\n\n" + ("A " * 60),
            "Chương 2\n\n" + ("B " * 60),
        ]
    )
    titles = [s.title for s in detect_chapters(text)]
    assert titles == ["Chương 1", "Chương 2"]


def test_text_with_no_structure_becomes_one_chapter() -> None:
    sections = detect_chapters("Chỉ có văn xuôi liên tục. " * 30)
    assert len(sections) == 1
    assert sections[0].title == "Nội dung"


def test_empty_text_yields_no_chapters() -> None:
    assert detect_chapters("") == []
    assert detect_chapters("   \n\n  \n") == []


# --------------------------------------------------------------------------
# Page furniture
# --------------------------------------------------------------------------


@pytest.mark.parametrize("line", ["12", "  345 ", "- 13 -", "– 14 –", "Trang 15", "Page 16"])
def test_page_number_lines_are_dropped(line: str) -> None:
    out = drop_page_number_lines(f"Nội dung một.\n{line}\nNội dung hai.")
    assert line.strip() not in out
    assert "Nội dung một." in out and "Nội dung hai." in out


def test_a_repeated_running_head_is_dropped() -> None:
    # A running head repeats on every page, so interleave it with the prose.
    lines = []
    for i in range(10):
        lines += ["Tắt đèn — Ngô Tất Tố", "12", f"Câu văn số {i}."]
    out = drop_page_number_lines("\n".join(lines))
    assert "Ngô Tất Tố" not in out
    assert "Câu văn số 0." in out and "Câu văn số 9." in out


def test_a_head_that_appears_once_is_kept() -> None:
    lines = ["MỤC LỤC"] + [f"Câu văn số {i}." for i in range(10)] + ["MỤC LỤC"]
    assert "MỤC LỤC" in drop_page_number_lines("\n".join(lines))


def test_a_single_long_line_is_not_mistaken_for_a_running_head() -> None:
    line = "Một câu văn dài nhưng chỉ xuất hiện đúng một lần trong toàn bộ tài liệu này"
    assert line in drop_page_number_lines(f"{line}\n{line}")


# --------------------------------------------------------------------------
# Whole-file extraction
# --------------------------------------------------------------------------


def test_extract_txt_file(tmp_path) -> None:
    p = tmp_path / "book.txt"
    p.write_text(
        "Chương 1\n\n" + ("Câu một. " * 40) + "\n\nChương 2\n\n" + ("Câu hai. " * 40),
        encoding="utf-8",
    )
    doc = txt.extract(p)
    assert len(doc.sections) == 2
    assert doc.source_format == "txt"
    assert [s.title for s in doc.sections] == ["Chương 1", "Chương 2"]


def test_extract_markdown_file(tmp_path) -> None:
    p = tmp_path / "book.md"
    p.write_text(
        "# Chương 1\n\n" + ("Câu một. " * 40) + "\n\n# Chương 2\n\n" + ("Câu hai. " * 40),
        encoding="utf-8",
    )
    doc = txt.extract(p)
    assert len(doc.sections) == 2


def test_extract_strips_gutenberg_boilerplate(tmp_path) -> None:
    p = tmp_path / "pg.txt"
    p.write_text(
        "The Project Gutenberg eBook of Something\n"
        "This ebook is for the use of anyone anywhere.\n"
        "*** START OF THE PROJECT GUTENBERG EBOOK SOMETHING ***\n\n"
        "Chương 1\n\n" + ("Câu một. " * 40) + "\n\n"
        "*** END OF THE PROJECT GUTENBERG EBOOK SOMETHING ***\n",
        encoding="utf-8",
    )
    doc = txt.extract(p)
    body = "\n".join(s.text for s in doc.sections)
    assert "Project Gutenberg" not in body
    assert "Câu một" in body


def test_extract_wikitext_dump_uses_the_wikisource_cleaner(tmp_path) -> None:
    p = tmp_path / "book.wikitext"
    p.write_text(
        "{{đầu đề|phần=Chương 1}}{{văn|"
        + ("Nội dung sạch. " * 40)
        + "<ref>ghi chú</ref> [[Thể loại:X]]}}\n",
        encoding="utf-8",
    )
    doc = txt.extract(p)
    body = "\n".join(s.text for s in doc.sections)
    assert "Nội dung sạch." in body
    assert "ghi chú" not in body
    assert "Thể loại" not in body


def test_load_sniffs_encoding(tmp_path) -> None:
    p = tmp_path / "u16.txt"
    p.write_bytes("Chương 1\n\nNội dung tiếng Việt.".encode("utf-16"))
    assert "Nội dung tiếng Việt" in txt.load(p)

    p8 = tmp_path / "u8.txt"
    p8.write_text("Chương 1\n\nNội dung tiếng Việt.", encoding="utf-8")
    assert "Nội dung tiếng Việt" in txt.load(p8)


def test_word_count_is_whitespace_tokens() -> None:
    assert Section(title="x", text="một hai ba\n\nbốn năm").word_count == 5
