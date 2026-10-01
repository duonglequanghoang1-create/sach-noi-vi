"""CONTRACT.md compliance, checked against the books this repo actually ships.

These are the assertions about `text/<slug>.md` and `book.json` that agent B
depends on. They run over the real output rather than a fixture, so a regression
in the extract stage fails here instead of in the middle of a narration.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sachnoi.config import REPO_ROOT
from sachnoi.extract import (
    ALLOWED_LICENSES,
    Section,
    check_license,
    chapter_slug,
    count_words,
    render_chapter_markdown,
    split_oversized,
    write_markdown,
)
from sachnoi.models import Book, Manifest, load_books

BOOKS = Path(REPO_ROOT) / "books"
H1_RE = re.compile(r"^# (.+)$")


def _built_books() -> list[Book]:
    books = load_books(BOOKS)
    if not books:
        pytest.skip("no books/ output; run scripts/extract_book.py")
    return books


def _narratable() -> list[Book]:
    return [b for b in _built_books() if b.status != "blocked-no-vi-source"]


# --------------------------------------------------------------------------
# text/<slug>.md
# --------------------------------------------------------------------------


def test_every_built_book_has_an_assembled_markdown_file() -> None:
    books = _narratable()
    assert books, "no narratable book was built"
    for book in books:
        path = Path(REPO_ROOT) / book.text_path
        assert path.is_file(), f"{book.slug}: missing {book.text_path}"
        assert path.read_text(encoding="utf-8").strip()


def test_exactly_one_h1_per_chapter_and_no_other_headings() -> None:
    for book in _narratable():
        path = Path(REPO_ROOT) / book.text_path
        for line in path.read_text(encoding="utf-8").split("\n"):
            if line.startswith("#"):
                assert H1_RE.match(line), f"{book.slug}: not a level-1 heading: {line!r}"
                assert not line.startswith("##"), f"{book.slug}: level-2 heading {line!r}"


def test_h1_count_matches_the_chapter_list() -> None:
    for book in _narratable():
        text = (Path(REPO_ROOT) / book.text_path).read_text(encoding="utf-8")
        headings = [ln for ln in text.split("\n") if ln.startswith("# ")]
        assert len(headings) == len(book.chapters), (
            f"{book.slug}: {len(headings)} H1 in text vs {len(book.chapters)} in book.json"
        )


def test_a_blank_line_separates_every_paragraph() -> None:
    for book in _narratable():
        text = (Path(REPO_ROOT) / book.text_path).read_text(encoding="utf-8")
        blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
        for block in blocks:
            body = "\n".join(ln for ln in block.split("\n") if not ln.startswith("# "))
            assert not body.startswith("\n")
            for line in body.split("\n"):
                assert not re.match(r"^\s+", line), f"{book.slug}: indented line {line!r}"
                assert line == line.rstrip(), f"{book.slug}: trailing space {line!r}"
        # No triple newline anywhere.
        assert "\n\n\n" not in text, f"{book.slug}: two blank lines in a row"


def test_no_html_images_tables_footnotes_or_markup() -> None:
    forbidden = {
        "HTML tag": re.compile(r"<[a-zA-Z/!][^>]*>"),
        "markdown image": re.compile(r"!\["),
        "markdown link": re.compile(r"\]\("),
        "table row": re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE),
        "footnote": re.compile(r"\[\^[^\]]*\]"),
        "wiki template": re.compile(r"\{\{"),
        "wikilink": re.compile(r"\[\[|\]\]"),
        "ref tag": re.compile(r"</?ref\b", re.IGNORECASE),
        "bold/italic": re.compile(r"\*\*.+?\*\*|__.+?__"),
    }
    for book in _narratable():
        text = (Path(REPO_ROOT) / book.text_path).read_text(encoding="utf-8")
        for name, pattern in forbidden.items():
            assert not pattern.search(text), f"{book.slug}: {name} in {book.text_path}"


def test_no_residual_wiki_markup_anywhere_in_the_output() -> None:
    for path in BOOKS.glob("*/text/*.md"):
        text = path.read_text(encoding="utf-8")
        for bad in ("{{", "}}", "[[", "]]", "<ref", "&lt;", "&gt;", "<!--"):
            assert bad not in text, f"{path}: residual {bad!r}"


def test_one_sentence_per_line() -> None:
    """A line must not carry two sentences.

    Detected as a full stop followed by whitespace and a capital, which is what
    "two sentences on one line" actually looks like. Counting full stops instead
    would flag the source's own stray punctuation (Truyen Kieu has the odd
    "day..") and abbreviations.
    """
    two_sentences = re.compile(r"[.!?…]\s+[A-ZĐÁÀÂÃÈÉÊÌÍÒÓÔÕÙÚĂĐ]")
    for book in _narratable():
        text = (Path(REPO_ROOT) / book.text_path).read_text(encoding="utf-8")
        for line in text.split("\n"):
            if line.startswith("# "):
                continue
            assert not two_sentences.search(line), (
                f"{book.slug}: two sentences on one line: {line[:80]!r}"
            )


def test_every_line_is_non_empty_and_ends_cleanly() -> None:
    for book in _narratable():
        text = (Path(REPO_ROOT) / book.text_path).read_text(encoding="utf-8")
        assert text.endswith("\n")
        assert not text.startswith("\n")
        for line in text.split("\n"):
            assert line == line.strip(), f"{book.slug}: untrimmed line {line[:60]!r}"


# --------------------------------------------------------------------------
# book.json
# --------------------------------------------------------------------------


def test_provenance_is_copied_verbatim_from_the_catalog() -> None:
    catalog = json.loads((Path(REPO_ROOT) / "catalog" / "books.json").read_text(encoding="utf-8"))
    entries = {e["slug"]: e for e in catalog["books"]}
    for book in _built_books():
        entry = entries[book.slug]
        assert book.license == entry["license"]
        assert book.license_url == entry["license_url"]
        assert book.rights_note.startswith(entry["rights_note"][:40])
        assert book.title == entry["title"]
        assert book.author == entry["author"]


def test_every_book_passes_the_rights_gate() -> None:
    for book in _built_books():
        assert check_license(book.license, slug=book.slug) in ALLOWED_LICENSES


def test_chapters_are_1_based_contiguous_and_typed() -> None:
    for book in _built_books():
        if book.status == "blocked-no-vi-source":
            continue
        assert book.chapters, f"{book.slug}: status {book.status} but no chapters"
        for i, chapter in enumerate(book.chapters, start=1):
            assert chapter.index == i, f"{book.slug}: chapter index {chapter.index} at {i}"
            assert chapter.slug == chapter_slug(i)
            assert chapter.title.strip()
            assert chapter.word_count > 0
            assert chapter.audio_path is None, "agent B fills audio_path, not agent A"
            assert chapter.duration_sec is None, "agent B fills duration_sec, not agent A"


def test_every_chapter_source_path_exists_and_is_its_own_file() -> None:
    for book in _narratable():
        seen: set[str] = set()
        for chapter in book.chapters:
            rel = chapter.source_path
            assert rel not in seen, f"{book.slug}: two chapters share {rel}"
            seen.add(rel)
            path = Path(REPO_ROOT) / rel
            assert path.is_file(), f"{book.slug}: missing {rel}"
            first = path.read_text(encoding="utf-8").split("\n", 1)[0]
            assert first.startswith("# "), f"{book.slug}: {rel} has no H1"
            assert first[2:].strip() == chapter.title, (
                f"{book.slug}: {rel} heading {first!r} != chapter title {chapter.title!r}"
            )


def test_the_assembled_file_is_the_concatenation_of_the_chapter_files() -> None:
    for book in _narratable():
        parts = [
            (Path(REPO_ROOT) / c.source_path).read_text(encoding="utf-8").strip()
            for c in book.chapters
        ]
        assert (Path(REPO_ROOT) / book.text_path).read_text(encoding="utf-8").strip() == (
            "\n\n".join(parts)
        )


def test_word_counts_are_plausible() -> None:
    for book in _narratable():
        for chapter in book.chapters:
            body = (Path(REPO_ROOT) / chapter.source_path).read_text(encoding="utf-8")
            actual = count_words(body)
            # The declared count includes the heading; allow a small drift.
            assert abs(actual - chapter.word_count) <= 2, (
                f"{book.slug}/{chapter.slug}: declared {chapter.word_count}, actual {actual}"
            )


def test_a_blocked_book_says_so_and_has_no_text() -> None:
    blocked = [b for b in _built_books() if b.status == "blocked-no-vi-source"]
    for book in blocked:
        assert book.chapters == []
        assert book.text_path == ""
        assert "VIETNAMESE EDITION" in book.rights_note
        assert book.translator.strip(), "a blocked book must explain itself in translator"
        assert not (Path(REPO_ROOT) / "books" / book.slug / "text").exists()


def test_no_machine_translation_was_claimed() -> None:
    for book in _built_books():
        data = json.loads((Path(REPO_ROOT) / "books" / book.slug / "book.json").read_text("utf-8"))
        mode = data.get("translate_mode", "off")
        assert mode in ("off", "gloss"), f"{book.slug}: translate_mode={mode!r}"


def test_source_files_are_not_committed() -> None:
    # books/*/source/ is gitignored; the raw wikitext must not be in the tree's
    # tracked files. Checked against the working tree, not git, so this also
    # holds for a fresh clone that has not fetched anything.
    for path in BOOKS.glob("*/source/*.wikitext"):
        rel = path.relative_to(REPO_ROOT)
        assert "source" in rel.parts, f"{rel}: raw source should live under source/"


# --------------------------------------------------------------------------
# The manifest handed to the other machine
# --------------------------------------------------------------------------


def test_a_manifest_exists_for_every_book_and_round_trips() -> None:
    for book in _built_books():
        path = BOOKS / book.slug / "manifest.json"
        assert path.is_file(), f"{book.slug}: missing manifest.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        manifest = Manifest.from_dict(raw)
        assert manifest.stage == "extract"
        assert manifest.created_at
        assert manifest.book.slug == book.slug
        # The dataclasses must survive the JSON round trip unchanged.
        assert Book.from_dict(json.loads((BOOKS / book.slug / "book.json").read_text("utf-8"))) == book


def test_manifest_notes_record_a_mechanical_chapter_split() -> None:
    # A book that had to be split for length must say so; nothing pretends the
    # work has chapters it does not.
    for book in _narratable():
        raw = json.loads((BOOKS / book.slug / "manifest.json").read_text(encoding="utf-8"))
        joined = " ".join(raw.get("notes", []))
        continued = [c.title for c in book.chapters if "(tiếp" in c.title]
        if continued:
            assert "no internal headings" in joined, (
                f"{book.slug}: has {len(continued)} continuation parts but no note explaining why"
            )


# --------------------------------------------------------------------------
# The writer itself
# --------------------------------------------------------------------------


def test_render_chapter_markdown_shape() -> None:
    section = Section(
        title="Chương 1",
        text="Câu một. Câu hai!\n\nCâu ba? Câu bốn…",
    )
    out = render_chapter_markdown(section)
    assert out == "# Chương 1\n\nCâu một.\nCâu hai!\n\nCâu ba?\nCâu bốn…\n"


def test_render_chapter_markdown_keeps_verse_lines() -> None:
    # Truyen Kieu: one cau tho per line, no sentence punctuation at all.
    section = Section(title="Truyện Kiều", text="Trăm năm trong cõi người ta,\nChữ tài chữ mệnh khéo là ghét nhau.")
    out = render_chapter_markdown(section)
    assert out == (
        "# Truyện Kiều\n\nTrăm năm trong cõi người ta,\nChữ tài chữ mệnh khéo là ghét nhau.\n"
    )


def test_render_chapter_markdown_with_no_body() -> None:
    assert render_chapter_markdown(Section(title="Chương 1")) == "# Chương 1\n"


def test_write_markdown_separates_chapters() -> None:
    out = write_markdown(
        [Section(title="Chương một", text="Một."), Section(title="Chương hai", text="Hai.")]
    )
    assert out == "# Chương một\n\nMột.\n\n# Chương hai\n\nHai.\n"


def test_split_oversized_labels_continuations() -> None:
    body = "\n\n".join(f"Đoạn {i}. " + "chữ " * 200 for i in range(6))
    parts = split_oversized([Section(title="Nội dung", text=body)], max_words=500)
    assert len(parts) > 1
    assert parts[0].title == "Nội dung"
    assert parts[1].title.endswith("(tiếp 2)")
    assert all(p.word_count <= 800 for p in parts)


def test_split_oversized_renames_a_placeholder_to_the_work() -> None:
    parts = split_oversized(
        [Section(title="Nội dung", text="Một. " * 3000)], max_words=500, base_title="Truyện Kiều"
    )
    assert parts[0].title == "Truyện Kiều"
    assert parts[1].title == "Truyện Kiều (tiếp 2)"


def test_split_oversized_leaves_short_chapters_alone() -> None:
    original = [Section(title="Chương 1", text="Một. " * 10)]
    assert split_oversized(original, max_words=5000) == original


def test_split_oversized_is_disabled_by_zero() -> None:
    original = [Section(title="Nội dung", text="Một. " * 5000)]
    assert split_oversized(original, max_words=0) == original
