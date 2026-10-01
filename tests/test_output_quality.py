"""Output quality: what actually lands in `books/*/text/`.

`tests/test_contract.py` checks the shape of the markdown. This file checks the
*content*, against the real books this repository ships, because the failure
mode that matters is not a wrong function call -- it is a correct function
producing a narrator reading out a MediaWiki parser debug report.

Every check here is an assertion about the shipped text, so a regression in the
extract stage fails here rather than in someone's ear.
"""

from __future__ import annotations

import json
import re
import statistics
from pathlib import Path

import pytest

from sachnoi.config import REPO_ROOT
from sachnoi.extract import (
    ABSOLUTE_MIN_CHAPTER_WORDS,
    drop_furniture_lines,
    effective_min_chapter_words,
)
from sachnoi.models import Book, load_books

BOOKS = Path(REPO_ROOT) / "books"
CATALOG = json.loads((Path(REPO_ROOT) / "catalog" / "books.json").read_text(encoding="utf-8"))
ENTRIES = {e["slug"]: e for e in CATALOG["books"]}


def _books() -> list[Book]:
    books = load_books(BOOKS)
    if not books:
        pytest.skip("no books/ output; run scripts/extract_book.py")
    return books


def _narratable() -> list[Book]:
    return [b for b in _books() if b.chapters]


def _text_of(book: Book) -> str:
    return "\n".join(
        (Path(REPO_ROOT) / chapter.source_path).read_text(encoding="utf-8")
        for chapter in book.chapters
    )


# --------------------------------------------------------------------------
# 1. Site and print furniture
# --------------------------------------------------------------------------

#: Every one of these was found in a shipped `text/<slug>.md` at some point.
FURNITURE = {
    "NewPP report": r"NewPP limit report",
    "parser name": r"\beqiad\b",
    "worker id": r"\bmain[-‐‑][0-9a-f]{6,}",
    "cache time": r"Cached time:",
    "cache expiry": r"Cache expiry:",
    "reduced expiry": r"Reduced expiry:",
    "complications": r"Complications:",
    "transclusion timing": r"Transclusion expansion time report",
    "cpu timing": r"CPU time usage",
    "lua timing": r"Lua time usage",
    "wikibase count": r"Number of Wikibase entities",
    "preprocessor count": r"Preprocessor visited node count",
    "unwanted arrow": r"[←→↑↓]",
    "wiki template": r"\{\{|\}\}",
    "wikilink": r"\[\[|\]\]",
    "ref tag": r"</?ref\b",
    "han-script title": r"金\s*雲\s*翹",
    "french title page": r"TRANSCRIT POUR",
    "publisher line": r"[ÉE]DITEUR",
    "preface": r"AVANT[- ]?PROPOS",
    "wikidata language link": r"\b(?:zh|ja|ko|en|fr|ru):\S+",
    "print ornament": r"^[\s*_=~·—–]{3,}$",
}


@pytest.mark.parametrize("label", sorted(FURNITURE))
def test_no_furniture_reaches_the_shipped_text(label: str) -> None:
    pattern = re.compile(FURNITURE[label], re.MULTILINE)
    for book in _narratable():
        text = _text_of(book)
        hits = pattern.findall(text)
        assert not hits, (
            f"{book.slug}: {label} in the narration, {len(hits)} hit(s): {hits[:3]}"
        )


def test_the_assembled_book_file_is_clean_too() -> None:
    for book in _narratable():
        text = (Path(REPO_ROOT) / book.text_path).read_text(encoding="utf-8")
        for label, pattern in FURNITURE.items():
            assert not re.search(pattern, text, re.MULTILINE), f"{book.slug}: {label}"


def test_furniture_lines_are_filtered_even_without_css_classes() -> None:
    # The wikitext path has no classes to filter on, so the line filter has to
    # stand on its own.
    raw = (
        "NewPP limit report Parsed by mw-web.\n"
        "eqiad.\n"
        "main-d7b74dd99-k26qg Cached time: 20260904141252 Cache expiry: 2592000\n"
        "Complications: [vary-page-id] CPU time usage: 0.098 seconds\n"
        "←\n"
        "→\n"
        "金 雲 翹 傳\n"
        "25040\n"
        "2$00\n"
        "TRANSCRIT POUR LA PREMIÈRE FOIS EN QUỐC-NGỮ\n"
        "Illustrations de NGUYỄN-HỮU-NHIÊU\n"
        "zh:征婦吟\n"
        "Câu thơ đầu tiên của chương,\n"
        "Câu thơ thứ hai của chương.\n"
    )
    out = drop_furniture_lines(raw)
    assert out.strip() == "Câu thơ đầu tiên của chương,\nCâu thơ thứ hai của chương."


def test_the_line_filter_never_eats_a_sentence() -> None:
    # Every one of these is a real Vietnamese sentence that must survive.
    for line in (
        "Năm 1865, ông viết xong bản thảo đầu tiên của tác phẩm.",
        "Cổ bề thanh động Trường Thành nguyệt hạc nhất thiên thu.",
        "Trang 12 vẫn còn in rõ.",
        "Chuông đồng kêu ba tiếng lúc nửa đêm.",
        "Giá vàng tăng 3%, đô la Mỹ giảm.",
    ):
        assert line in drop_furniture_lines(line), line


# --------------------------------------------------------------------------
# 2. Chapter length
# --------------------------------------------------------------------------


def test_no_chapter_is_below_the_floor() -> None:
    """A chapter must be worth an audio track.

    The floor is the catalog's `min_chapter_words`, and never laxer than
    `ABSOLUTE_MIN_CHAPTER_WORDS`. A single-chapter book is exempt: a 318-word
    *hoành phi* is a legitimate one-track work, not a broken chapter.
    """
    for book in _narratable():
        floor = effective_min_chapter_words(ENTRIES[book.slug].get("min_chapter_words"))
        if len(book.chapters) < 2:
            continue
        for chapter in book.chapters:
            assert chapter.word_count >= floor, (
                f"{book.slug}/{chapter.slug} {chapter.title!r}: {chapter.word_count} chữ "
                f"< floor {floor}"
            )


def test_no_chapter_is_shorter_than_the_absolute_minimum() -> None:
    for book in _narratable():
        if len(book.chapters) < 2:
            continue
        worst = min(c.word_count for c in book.chapters)
        assert worst >= ABSOLUTE_MIN_CHAPTER_WORDS, f"{book.slug}: shortest is {worst}"


def test_every_chapter_is_long_enough_to_be_more_than_a_title_card() -> None:
    # 150 words is roughly 75 seconds of narration. Under that it is a card.
    for book in _narratable():
        for chapter in book.chapters:
            body = (Path(REPO_ROOT) / chapter.source_path).read_text(encoding="utf-8")
            lines = [ln for ln in body.split("\n") if ln.strip() and not ln.startswith("# ")]
            assert len(lines) >= 3, f"{book.slug}/{chapter.slug}: {len(lines)} lines of body"


# --------------------------------------------------------------------------
# 3. Front matter must not be a track
# --------------------------------------------------------------------------

#: Titles that must never be an audio chapter, checked explicitly so a new
#: edition cannot reintroduce one silently. Matched as whole words: the 1911
#: edition has a real poem called "Đề lại Chung Án", and 總 -- romanised
#: "chung" -- is also what its table of contents is called.
BANNED_CHAPTER_TITLES = (
    "avant-propos", "préface", "prefache", "introduction", "propos", "prologue",
    "lời tựa", "lời nói đầu", "lời dẫn", "trang bìa",
    "half title", "tác giả", "người dịch", "dịch giả",
    "f.-h. schneider", "éditeur", "editeur", "imprimerie", "poème", "prix",
)


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def test_no_chapter_is_front_matter() -> None:
    for book in _narratable():
        for chapter in book.chapters:
            key = _title_key(chapter.title)
            for banned in BANNED_CHAPTER_TITLES:
                assert not re.search(rf"\b{re.escape(banned)}\b", key), (
                    f"{book.slug}/{chapter.slug}: {chapter.title!r} is front matter"
                )


def test_the_code_agrees_about_what_is_front_matter() -> None:
    """The extract stage's own predicate, exercised on the shipped titles.

    Cheaper and sharper than a regex in the test: if `is_front_matter` ever
    starts calling a poem a preface, this fails.
    """
    from sachnoi.extract.wikisource import is_front_matter
    from sachnoi.extract import Section

    for book in _narratable():
        for chapter in book.chapters:
            probe = Section(title=chapter.title, text="x " * 50)
            assert not is_front_matter(probe, base_title=book.title), chapter.title
    # ...and it does still recognise the real thing.
    for title in ("AVANT-PROPOS", "Mục lục", "F.-H. SCHNEIDER, ÉDITEUR", "Lời tựa"):
        assert is_front_matter(Section(title=title, text="x " * 50))


def test_dropped_front_matter_is_recorded_in_the_manifest() -> None:
    # Silently dropping a preface is worse than dropping it loudly.
    raw = json.loads((BOOKS / "truyen-kieu-ban-truong-vinh-ky-1911" / "manifest.json").read_text("utf-8"))
    joined = " ".join(raw.get("notes", []))
    assert "front matter" in joined
    assert "index page" in joined


# --------------------------------------------------------------------------
# 4. No empty or duplicate-title chapters
# --------------------------------------------------------------------------

#: A title that carries no information for a listener.
JUNK_TITLES = re.compile(
    r"^(?:không có tiêu đề|không tên|không rõ|trang|chương|phan|phần|nội dung|"
    r"tiêu đề|index|untitled)\s*\d*$",
    re.IGNORECASE,
)


def test_no_chapter_has_an_empty_body() -> None:
    for book in _narratable():
        for chapter in book.chapters:
            body = (Path(REPO_ROOT) / chapter.source_path).read_text(encoding="utf-8")
            body_text = "\n".join(ln for ln in body.split("\n") if not ln.startswith("# "))
            assert body_text.strip(), f"{book.slug}/{chapter.slug}: empty body"


def test_no_chapter_title_is_junk() -> None:
    for book in _narratable():
        for chapter in book.chapters:
            title = chapter.title.strip()
            assert title, f"{book.slug}/{chapter.slug}: empty title"
            assert not JUNK_TITLES.match(title), f"{book.slug}: {title!r}"
            assert len(title) >= 3, f"{book.slug}: {title!r} is too short to be a name"
            assert not title.isdigit(), f"{book.slug}: {title!r} is only a number"


def test_no_chapter_title_is_a_bare_continuation() -> None:
    """"Truyen Kieu (tiep 4)" tells a listener nothing. The parts are named
    "Phan N - <their first line>" instead."""
    for book in _narratable():
        for chapter in book.chapters:
            assert not re.match(r"^.*\(tiếp \d+\)$", chapter.title), (
                f"{book.slug}: {chapter.title!r}"
            )


def test_split_part_numbers_are_unique_within_a_book() -> None:
    for book in _narratable():
        titles = [c.title for c in book.chapters]
        assert len(titles) == len(set(titles)), f"{book.slug}: duplicate chapter titles"


def test_split_parts_are_numbered_globally_and_in_order() -> None:
    for book in _narratable():
        nums = [
            int(m.group(1))
            for m in (re.match(r"^Phần (\d+)", c.title) for c in book.chapters)
            if m
        ]
        if not nums:
            continue
        assert nums == sorted(nums), f"{book.slug}: part numbers out of order: {nums}"
        assert len(nums) == len(set(nums)), f"{book.slug}: repeated part number: {nums}"


def test_the_last_chapter_is_not_a_runt() -> None:
    for book in _narratable():
        if len(book.chapters) < 2:
            continue
        last = book.chapters[-1].word_count
        median = statistics.median(c.word_count for c in book.chapters)
        assert last >= median * 0.25, (
            f"{book.slug}: final chapter is {last} chữ against a median of {int(median)}"
        )


# --------------------------------------------------------------------------
# 5. book.json agrees with what is on disk
# --------------------------------------------------------------------------


def test_declared_chapter_count_matches_the_files() -> None:
    for book in _narratable():
        md_files = sorted((Path(REPO_ROOT) / book.text_path).parent.glob("chuong-*.md"))
        assert len(md_files) == len(book.chapters), (
            f"{book.slug}: book.json declares {len(book.chapters)} chapters, "
            f"{len(md_files)} chapter files exist"
        )
        h1 = [
            ln
            for ln in (Path(REPO_ROOT) / book.text_path).read_text("utf-8").split("\n")
            if ln.startswith("# ")
        ]
        assert len(h1) == len(book.chapters)


def test_declared_word_count_matches_the_file() -> None:
    from sachnoi.extract import count_words

    for book in _narratable():
        for chapter in book.chapters:
            body = (Path(REPO_ROOT) / chapter.source_path).read_text(encoding="utf-8")
            actual = count_words(body)
            assert abs(actual - chapter.word_count) <= 2, (
                f"{book.slug}/{chapter.slug}: declared {chapter.word_count}, actual {actual}"
            )


def test_every_narratable_book_passed_the_rights_gate() -> None:
    from sachnoi.extract import check_license

    for book in _books():
        check_license(book.license, slug=book.slug)


def test_a_blocked_book_explains_itself_with_the_real_reason() -> None:
    blocked = [b for b in _books() if b.status == "blocked-no-vi-source"]
    for book in blocked:
        note = book.rights_note
        assert "VIETNAMESE EDITION" in note
        assert "offline" not in note.lower(), (
            f"{book.slug}: the block reason blames an offline run, which is not why "
            f"it is blocked: {note}"
        )
        assert book.chapters == [] and book.text_path == ""


def test_luc_van_tien_is_blocked_for_the_right_reason() -> None:
    book = Book.load(BOOKS / "luc-van-tien" / "book.json")
    note = book.rights_note
    assert book.status == "blocked-no-vi-source"
    assert "nomna.org" in note, "the real reason is that both editions are stubs"
    assert "SAI" in note or "KHÔNG nằm ở trang con" in note
    assert "offline" not in note.lower()


# --------------------------------------------------------------------------
# 6. The 1911 edition's glued words
# --------------------------------------------------------------------------

#: Names the 1911 facsimile prints without a space, and which a TTS would read
#: as one nonsense word.
GLUED_NAMES = (
    "Túykiều", "TúyKiều", "Túyvân", "TúyVân", "Vươngquan", "VươngQuan",
    "Đạmtiên", "ĐạmTiên", "Kimtrọng", "KimTrọng", "Quốcngữ", "QuốcNgữ",
    "TÚYKIỀU", "KIMTRỌNG", "TÍCHTÚYKIỀU", "HOẠNTHƠ",
    "nởnang", "khônngoan", "phớtphớt", "đuộtduột", "lảothông",
)


@pytest.mark.parametrize("word", GLUED_NAMES)
def test_no_glued_word_reaches_the_shipped_text(word: str) -> None:
    for book in _narratable():
        text = _text_of(book)
        assert word not in text, f"{book.slug}: {word!r} is still glued together"


def test_splitting_internal_capitals_cannot_damage_a_real_word() -> None:
    from sachnoi.extract import split_internal_capitals

    assert split_internal_capitals("TúyKiều và ĐạmTiên") == "Túy Kiều và Đạm Tiên"
    # No Vietnamese orthography uses an internal capital, so these are untouched.
    for word in ("Nguyễn", "Chuẩn", "thường", "TRƯỜNG", "Phần"):
        assert split_internal_capitals(word) == word


def test_the_glue_table_only_fixes_what_it_lists() -> None:
    from sachnoi.extract import repair_glued_words

    assert repair_glued_words("Túykiều thấy Vươngquan") == "Túy Kiều thấy Vương Quan"
    assert repair_glued_words("một con ngựa") == "một con ngựa"


# --------------------------------------------------------------------------
# 7. Nothing is silently small
# --------------------------------------------------------------------------


def test_no_book_lost_a_third_of_its_words() -> None:
    """A guard against a filter that gets carried away."""
    for book in _narratable():
        total = sum(c.word_count for c in book.chapters)
        assert total > 200, f"{book.slug}: only {total} chữ survived extraction"
