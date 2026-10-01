"""Source extraction: turn a source file (or URL) into Vietnamese prose.

Agent A owns this package (may1). Nothing here imports `sachnoi.cli`; the
pipeline is reachable through plain importable functions and through
`python -m sachnoi.extract`.

The backends (`pdf`, `epub`, `txt`, `wikisource`) all return the same
intermediate shape so the markdown writer and the rights gate stay in one
place:

    Document
      └── Section[]        one `Section` == one audio chapter
            ├── title
            ├── text       paragraphs joined by "\n\n"
            └── level      heading depth that produced it (1 == chapter)

Two rules from CONTRACT.md are enforced here rather than in the backends,
because every backend could otherwise get them wrong:

1. **Rights gate.** `check_license()` raises `RightsError` for anything that is
   not `public-domain`, `CC0`, `CC-BY-4.0` or `CC-BY-SA-4.0`. It is a hard
   failure, never a warning.
2. **Markdown contract.** `write_markdown()` emits exactly one level-1 heading
   per chapter, a blank line between every paragraph, one sentence per line,
   and no HTML, images, tables or footnotes.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from ..models import Book, Chapter, Manifest, slugify

__all__ = [
    "ALLOWED_LICENSES",
    "RightsError",
    "Section",
    "Document",
    "check_license",
    "normalize_license",
    "detect_chapters",
    "chapter_title",
    "split_sentences",
    "render_chapter_markdown",
    "write_markdown",
    "assemble_book",
    "extract_file",
    "extract_url",
    "main",
]


# --------------------------------------------------------------------------
# Rights gate
# --------------------------------------------------------------------------

#: Licences the pipeline is allowed to narrate. Mirrors CONTRACT.md "Rights
#: gate"; agent B repeats the same check in `audio/tts.py` and `package.py`.
ALLOWED_LICENSES: frozenset[str] = frozenset(
    {"public-domain", "cc0", "cc-by-4.0", "cc-by-sa-4.0"}
)

#: Spellings that mean the same thing as an allowed licence. Anything not in
#: here and not already allowed is rejected -- we never guess.
_LICENSE_ALIASES: dict[str, str] = {
    "public domain": "public-domain",
    "publicdomain": "public-domain",
    "public-domain/traditional": "public-domain",
    "pdm": "public-domain",
    "cc0": "cc0",
    "cc-0": "cc0",
    "cc0-1.0": "cc0",
    "cc by 4.0": "cc-by-4.0",
    "cc-by-4.0": "cc-by-4.0",
    "cc by sa 4.0": "cc-by-sa-4.0",
    "cc-by-sa-4.0": "cc-by-sa-4.0",
    "ccbysa-4.0": "cc-by-sa-4.0",
}


class RightsError(RuntimeError):
    """Raised when a book's licence forbids redistribution/narration.

    This is deliberately a distinct exception type: agent B and the CLI can
    catch it and print a one-line reason instead of a traceback, and tests can
    assert the gate fired.
    """


def normalize_license(license_id: str) -> str:
    """Fold a licence string to its canonical lowercase form."""
    raw = (license_id or "").strip()
    if not raw:
        return ""
    key = raw.lower().replace("_", " ").replace("  ", " ")
    if key in ALLOWED_LICENSES:
        return key
    return _LICENSE_ALIASES.get(key, key)


def check_license(license_id: str, *, slug: str = "") -> str:
    """Return the canonical licence, or raise :class:`RightsError`.

    Called by every extract entrypoint before a single byte of text is
    written. An empty or unrecognised licence is a failure, not a pass: a book
    with no provenance cannot be narrated.
    """
    canonical = normalize_license(license_id)
    label = f" for {slug!r}" if slug else ""
    if not canonical:
        raise RightsError(
            f"no licence declared{label}: a public-domain or redistributable "
            f"licence is required before narration"
        )
    if canonical not in ALLOWED_LICENSES:
        raise RightsError(
            f"licence {license_id!r}{label} is not public domain and not "
            f"redistributable; allowed: {', '.join(sorted(ALLOWED_LICENSES))}"
        )
    return canonical


# --------------------------------------------------------------------------
# Intermediate representation
# --------------------------------------------------------------------------


@dataclass
class Section:
    """One candidate audio chapter, straight out of a backend."""

    title: str
    text: str = ""
    level: int = 1
    source_path: str = ""

    @property
    def word_count(self) -> int:
        return count_words(self.text)

    def is_empty(self) -> bool:
        return not self.text.strip()


@dataclass
class Document:
    """Backend output: an ordered list of chapters plus provenance."""

    sections: list[Section] = field(default_factory=list)
    title: str = ""
    source_format: str = ""
    source_path: str = ""
    source_url: str = ""
    translator: str = ""
    language: str = "vi"
    notes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.sections)

    def __iter__(self) -> Iterable[Section]:
        return iter(self.sections)


# --------------------------------------------------------------------------
# Chapter detection
# --------------------------------------------------------------------------

#: Sano's `scripts/pdf_to_docx.py` chapter word list. Kept verbatim in spirit:
#: the same trigger words are what Vietnamese and English editions actually use.
CHAPTER_RE = re.compile(
    r"^[\s]*(chương|chuong|phần|phan|mục|muc|bài|bai|thi|chuyện|chuyen|"
    r"chapter|part|book|cuốn|cuon)\s*"
    r"([0-9]+|[ivxlcdm]+|[a-z])\b[^\n]*$",
    re.IGNORECASE,
)

#: A bare numbered heading with no keyword at all, e.g. a lone "12." or "IV."
#: on its own line. Only honoured when the document is short enough that this is
#: unambiguous -- see `detect_chapters`.
_BARE_NUMBER_RE = re.compile(r"^[\s]*([0-9]{1,3}|[ivxlcdm]{1,7})[.):]?[\s]*$", re.IGNORECASE)

#: Front/back matter that must never become an audio chapter.
NON_CHAPTER_TITLES = re.compile(
    r"^(mục lục|mụclục|nội dung|table of contents|contents|toc|danh mục|"
    r"liên kết ngoài|liên kết|tham khảo|nguồn|ghi chú|ghi chú bản dịch|chú thích|"
    r"chú thích sách|bản quyền|giấy phép|license|licence|"
    r"tác giả|soạn giả|người dịch|ban dịch|bản dịch|translator|translator[s]?|"
    r"xem thêm|see also|liên quan|bibliography|thư mục|"
    r"phụ lục|phụlục|appendix|phần đầu|phần cuối|"
    r"lời giới thiệu|lời nói đầu|lời ngỏ|lời cảm ơn)",
    re.IGNORECASE,
)

#: A heading line in markdown (`#`), wikitext (`==`) or plain text (`==`).
_HEADING_MARKDOWN_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*#*\s*$")
_HEADING_WIKITEXT_RE = re.compile(r"^\s*(={2,6})\s*(.*?\S)\s*\1\s*$")
_HEADING_UNDERLINE_RE = re.compile(r"^([=\-~*_#])\1{2,}\s*$")

#: Standalone page-number / running-head lines, e.g. "12", "- 13 -", "Trang 14".
PAGE_NUMBER_LINE_RE = re.compile(
    r"^[\s]*(trang\s+|page\s+)?[-–—]?\s*\d{1,4}\s*[-–—]?\s*$", re.IGNORECASE
)


def looks_like_chapter_heading(line: str) -> bool:
    """True when `line` is plausibly a chapter title rather than prose."""
    if not line.strip():
        return False
    heading = parse_heading(line)
    if heading:
        return True
    if len(line) > 90:
        return False
    if CHAPTER_RE.match(line):
        return True
    return False


def parse_heading(line: str) -> tuple[int, str] | None:
    """Return `(level, title)` for a markdown or wikitext heading.

    Returns ``None`` for ordinary prose. Wikitext `=== X ===` yields level 2
    and markdown `## X` also yields level 2, so both sources compare equal.
    Setext headings (`Title` then `====`) need the *next* line and are handled
    by :func:`detect_chapters` instead.
    """
    m = _HEADING_WIKITEXT_RE.match(line)
    if m:
        # `== X ==` is the outermost level, so it becomes level 1 and `===` level 2.
        # Getting this backwards silently folds every chapter into the previous
        # one, because `split_wikitext_sections` treats `level > max_level` as a
        # subsection.
        return (len(m.group(1)) - 1, m.group(2).strip())
    m = _HEADING_MARKDOWN_RE.match(line)
    if m:
        return (len(m.group(1)), m.group(2).strip())
    return None


def is_non_chapter_title(title: str) -> bool:
    """True when `title` is *exactly* a front/back-matter heading.

    The whole line has to be the apparatus title. A prefix match would be a
    trap: `NON_CHAPTER_TITLES` contains "lời nói đầu", and a paragraph that
    merely starts with those words is prose, not a heading -- prefix-matching it
    deleted the first chapter of any book with an introduction.
    """
    cleaned = re.sub(r"^[\d\W_]+", "", title).strip()
    if not cleaned or len(cleaned) > 60:
        return False
    m = NON_CHAPTER_TITLES.match(cleaned)
    if not m:
        return False
    # Allow only trailing decoration, e.g. "Ghi chú bản dịch." or "Mục lục 2".
    return not cleaned[m.end() :].strip(" .:-–—")


def detect_chapters(
    text: str,
    *,
    min_words: int = 40,
    allow_bare_numbers: bool = True,
) -> list[Section]:
    """Split flat text into chapters using headings, then Sano's keyword rules.

    Strategy, in order of trust:

    1. explicit headings -- markdown `#`, wikitext `==`, or a setext underline;
    2. lines matching :data:`CHAPTER_RE` ("Chương 3", "Phần II", "Chapter 4");
    3. a run of short ALL-CAPS lines (how a PDF prints chapter titles when the
       heading structure is lost).

    Content before the first chapter becomes chapter 1 with a generated title
    rather than being dropped. Chapters shorter than `min_words` are merged into
    the following chapter so a stray "Phụ lục" cannot become a 20-word track.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    total_words = count_words(text)
    # Bare numbers are only safe to trust in a short document; in a 100k-word
    # novel "12." is a list item or a stray number, not a chapter.
    allow_bare = allow_bare_numbers and total_words < 4000

    # (line index, level, title, is_apparatus)
    marks: list[tuple[int, int, str, bool]] = []
    for i, line in enumerate(lines):
        heading = parse_heading(line)
        if heading:
            title = heading[1]
            marks.append((i, heading[0], title, is_non_chapter_title(title)))
            continue
        # Setext: the *next* line is an underline, so this line is the title.
        if (
            i + 1 < len(lines)
            and line.strip()
            and len(line) <= 90
            and _HEADING_UNDERLINE_RE.match(lines[i + 1].rstrip())
        ):
            marks.append((i, 1, line.strip(), is_non_chapter_title(line)))
            continue
        if is_non_chapter_title(line):
            # Record apparatus as a mark so the text *under* it is skipped too.
            # Without this, a table of contents becomes the preamble of the
            # first real chapter.
            marks.append((i, 1, line.strip(), True))
            continue
        if CHAPTER_RE.match(line):
            marks.append((i, 1, line.strip(), False))
            continue
        if allow_bare and _BARE_NUMBER_RE.match(line) and i + 1 < len(lines):
            marks.append((i, 1, line.strip(), False))
            continue
        if _is_caps_heading(line):
            marks.append((i, 1, line.strip(), False))

    if not marks:
        # No structure at all: the whole document is one chapter.
        body = "\n".join(lines)
        return [Section(title="Nội dung", text=body, level=1)] if body.strip() else []

    # --- carve the text into sections ------------------------------------
    # Anything before the first *content* mark is kept as a preamble chapter,
    # unless it is only apparatus (then it is dropped).
    first_content = next((i for i, m in enumerate(marks) if not m[3]), len(marks))
    if first_content == len(marks):
        # Structure was found, and all of it is front/back matter. Returning the
        # raw text here would turn a table of contents into a chapter of
        # navigation links, so return nothing and let the caller try another
        # source (a linked edition, a subpage).
        return []
    if marks[first_content][0] > 0:
        preamble = "\n".join(lines[: marks[first_content][0]]).strip()
        if count_words(preamble) >= 8:
            # line_no = -1 so the body slice starts at line 0: a synthetic
            # preamble has no heading line of its own to step over.
            marks.insert(first_content, (-1, 1, "Phần mở đầu", False))

    sections: list[Section] = []
    for idx, (line_no, _level, title, apparatus) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(lines)
        if apparatus:
            continue  # heading and body both dropped
        body = "\n".join(lines[line_no + 1 : end]).strip()
        sections.append(Section(title=title, text=body, level=1))

    sections = [s for s in sections if not s.is_empty()]
    sections = _merge_short_sections(sections, min_words=min_words)
    return sections


def _is_caps_heading(line: str) -> bool:
    """A short line in ALL CAPS is how a PDF prints a lost chapter title.

    Needs at least one word of three letters or more, and must not look like a
    path or a wiki link: without that guard `../II` (a leftover navigation link)
    reads as a heading and turns an index page into a chapter of links.
    """
    words = line.split()
    if not 1 <= len(words) <= 8 or len(line) > 80:
        return False
    if "/" in line or line.lstrip().startswith((".", "#", "|", "[", "*")):
        return False
    if not any(len(re.sub(r"[^\w]", "", w)) >= 3 for w in words):
        return False
    letters = [c for c in line if c.isalpha()]
    if len(letters) < 2:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return upper * 5 >= len(letters) * 4


def _merge_short_sections(sections: list[Section], *, min_words: int) -> list[Section]:
    """Fold runs of tiny sections forward into the next substantial one.

    A trailing run is folded *backwards* into the previous chapter, unless it is
    itself a full chapter -- otherwise the last chapter of every book would be
    glued onto the one before it.
    """
    if min_words <= 0 or len(sections) < 2:
        return sections
    merged: list[Section] = []
    pending: list[Section] = []
    for sec in sections:
        pending.append(sec)
        if sec.word_count >= min_words:
            merged.append(_combine(pending))
            pending = []
    if pending:
        if len(pending) == 1 and pending[0].word_count >= min_words:
            merged.append(pending[0])
        elif merged:
            merged[-1] = _combine([merged[-1], *pending])
        else:
            merged.append(_combine(pending))
    return merged


def _combine(parts: Sequence[Section]) -> Section:
    """Join several sections into one, keeping the longest section's title.

    The longest section is the real chapter; the others are stubs folded into it
    (a short preamble, a "Ghi chú" leftover). Taking `parts[0]` instead would
    label chapter 1 with the title of the four-line blurb in front of it.
    """
    if len(parts) == 1:
        return parts[0]
    best = max(parts, key=lambda p: p.word_count)
    body = "\n\n".join(p.text.strip() for p in parts if p.text.strip())
    return Section(title=best.title, text=body, level=1)


# --------------------------------------------------------------------------
# Sentence splitting + markdown contract
# --------------------------------------------------------------------------

# Sentence ends at . ! ? or the ellipsis, keeping the punctuation, with closing
# quotes/brackets allowed to trail it. Ported from Sano's
# `desktop/frontend/src/lib/lyrics.ts`, which is the stricter of its two
# splitters and therefore the safer one for TTS chunking.
_SENTENCE_RE = re.compile(r"[^.!?…]+(?:[.!?…]+[\"'”’»)\]]*)?|[.!?…]+")
_ALNUM_RE = re.compile(r"[^\W_]", re.UNICODE)


def split_sentences(text: str) -> list[str]:
    """Split one paragraph into sentences. Punctuation is kept.

    Fragments shorter than three alphanumerics, or a lone run of punctuation,
    are glued onto the previous sentence -- that is what stops a list item such
    as "1." from becoming its own track.
    """
    out: list[str] = []
    for raw in _SENTENCE_RE.findall(text):
        s = raw.strip()
        if not s:
            continue
        if out and (len(_ALNUM_RE.findall(s)) < 3 or s[0] in ".!?…"):
            out[-1] = f"{out[-1]} {s}"
        else:
            out.append(s)
    return out


_FORBIDDEN_MD_RE = re.compile(
    r"(<[a-zA-Z/!][^>]*>"  # HTML
    r"|!\[[^\]]*\]\([^)]*\)"  # markdown images
    r"|\[[^\]]*\]\([^)]*\)"  # markdown links
    r"|^\s*\|.*\|\s*$"  # table rows
    r"|\[\^[^\]]+\]"  # footnotes
    r"|\{\{.*?\}\}"  # leftover templates
    r"|\[\[.*?\]\]"  # leftover wikilinks
    r"|[*_]{1,3}[^*_]+[*_]{1,3}"  # bold/italic markers
    r"|<ref.*?>|</ref>)",
    re.MULTILINE,
)


def sanitize_prose(text: str) -> str:
    """Strip HTML, images, links, tables, footnotes and markup from prose.

    CONTRACT.md forbids all of those in `text/<slug>.md`. Link *text* is kept --
    dropping it would silently lose prose -- but the URL goes.
    """
    out = text.replace("\xa0", " ")
    out = re.sub(r"<ref[^>]*?/>", "", out, flags=re.IGNORECASE)
    out = re.sub(r"<ref[^>]*>.*?</ref>", "", out, flags=re.IGNORECASE | re.DOTALL)
    out = re.sub(r"<[^>]+>", "", out)
    out = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", out)  # images
    out = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", out)  # links -> text
    out = re.sub(r"\[([^\]]*)\]\[[^\]]*\]", r"\1", out)  # wikilinks -> text
    out = re.sub(r"^\s*\|.*$", "", out, flags=re.MULTILINE)  # table rows
    out = re.sub(r"\{\{.*?\}\}", "", out, flags=re.DOTALL)
    out = re.sub(r"[\*_]{2,}", "", out)  # bold/italic
    out = out.replace("'''", "").replace("''", "")
    out = re.sub(r"\[\^[^\]]*\]", "", out)  # footnotes
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r" ?\n ?", "\n", out)
    return out


def _head_key(line: str) -> str | None:
    """Return a running-head key for `line`, or None if it cannot be one.

    A running head is a short line that ends in no terminal punctuation. A real
    sentence of prose almost always does, so this is a cheap and surprisingly
    reliable discriminator -- and it does not depend on the text being title
    case, which Vietnamese running heads frequently are not.
    """
    s = line.strip()
    if not s or len(s) > 80 or len(s.split()) > 8:
        return None
    if s[-1] in ".!?…,:;\"'”’»)]}":
        return None
    return s


def drop_page_number_lines(text: str, *, min_repeats: int = 5) -> str:
    """Remove running headers/footers: page numbers, then repeated short lines.

    The first pass drops a line that is nothing but a number (`12`, `- 13 -`,
    `Trang 14`). The second drops a short, punctuation-free line that repeats at
    least `min_repeats` times -- which is what a PDF's running title looks like
    once `pdftotext -layout` has flattened it.
    """
    kept = [ln for ln in text.split("\n") if not PAGE_NUMBER_LINE_RE.match(ln)]
    counter: dict[str, int] = {}
    for line in kept:
        key = _head_key(line)
        if key:
            counter[key] = counter.get(key, 0) + 1
    noisy = {k for k, n in counter.items() if n >= min_repeats}
    if noisy:
        kept = [line for line in kept if _head_key(line) not in noisy]
    return "\n".join(kept)


def count_words(text: str) -> int:
    """Whitespace token count. Deterministic and good enough for TTS timing."""
    return len([t for t in text.split() if t])


_BARE_ROMAN_RE = re.compile(r"^[\s]*([IVXLCDM]{1,7})[\s]*$", re.IGNORECASE)
_BARE_NUMBER_TITLE_RE = re.compile(r"^[\s]*(\d{1,3})[\s]*$")


def _clean_title(title: str) -> str:
    title = sanitize_prose(title)
    title = re.sub(r"^[\s#=*_]+|[\s#=*_]+$", "", title)
    title = re.sub(r"^[\d\W_]+", "", title).strip()
    title = re.sub(r"\s+", " ", title)
    # Guard against an over-eager regex eating every Vietnamese vowel.
    if sum(1 for c in title if unicodedata.category(c).startswith("L")) < 2:
        return "Chương"
    return title or "Chương"


def chapter_title(title: str) -> str:
    """A chapter title usable as both a markdown H1 and an M4B track name.

    A bare numeral is the common case on Vietnamese Wikisource, where chapters
    are subpages named `I`, `II`, `III`. `I` alone is a terrible track name and
    `_clean_title` would reject it as too short, so it is expanded to
    `Chương một` -- the same expansion Sano applies to roman chapter headings.
    """
    raw = (title or "").strip()
    if not raw:
        return "Chương"
    m = _BARE_ROMAN_RE.match(raw)
    if m:
        from ..translate.gloss import roman_to_int, ROMAN_WORDS

        n = roman_to_int(m.group(1))
        if 0 < n < len(ROMAN_WORDS):
            return f"Chương {ROMAN_WORDS[n]}"
    m = _BARE_NUMBER_TITLE_RE.match(raw)
    if m:
        from ..translate.gloss import int_to_viet

        return f"Chương {int_to_viet(int(m.group(1)))}"
    return _clean_title(raw)


def render_chapter_markdown(section: Section, *, index: int | None = None) -> str:
    """Render one chapter exactly the way CONTRACT.md demands.

    * one and only one level-1 heading, `# <chapter title>`;
    * a blank line between every paragraph;
    * one sentence per line inside a paragraph;
    * no HTML, images, tables, footnotes or emphasis markers.
    """
    title = chapter_title(section.title)
    body = sanitize_prose(section.text)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    # A "paragraph" can also be a run of soft-wrapped lines; group them.
    grouped: list[str] = []
    for para in paragraphs:
        sentences = split_sentences(re.sub(r"\s*\n\s*", " ", para))
        text = " ".join(sentences).strip()
        if text:
            grouped.append(text)
    heading = f"# {title}"
    if not grouped:
        return f"{heading}\n"
    return heading + "\n\n" + "\n\n".join(grouped) + "\n"


def write_markdown(sections: Sequence[Section]) -> str:
    """Assemble the whole book into `text/<slug>.md` form."""
    blocks = [render_chapter_markdown(s).strip("\n") for s in sections if not s.is_empty()]
    return "\n\n".join(b for b in blocks if b) + "\n"


def chapter_slug(index: int) -> str:
    """`chuong-01`, `chuong-02`, ... Matches the CONTRACT example and is
    stable across reruns, unlike a slug of the (mutable) chapter title."""
    return f"chuong-{index:02d}"


# --------------------------------------------------------------------------
# Backend dispatch
# --------------------------------------------------------------------------


def _suffix(path: str | Path) -> str:
    return Path(path).suffix.lower()


def extract_file(
    path: str | Path,
    *,
    license_id: str = "",
    source_url: str = "",
    translator: str = "",
    min_words: int = 40,
) -> Document:
    """Extract a local source file. Refuses unlicensed books before reading."""
    check_license(license_id, slug=Path(path).stem)
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)
    suffix = _suffix(p)
    if suffix == ".pdf":
        from . import pdf as backend
    elif suffix == ".epub":
        from . import epub as backend
    elif suffix in {".txt", ".md", ".markdown", ".text", ".wikitext", ".wiki"}:
        from . import txt as backend
    else:
        raise ValueError(f"unsupported source format: {suffix or p.name!r}")
    doc = backend.extract(p, min_words=min_words)
    doc.source_path = str(p)
    doc.source_url = source_url or doc.source_url
    doc.translator = translator or doc.translator
    return doc


def extract_url(
    url: str,
    *,
    license_id: str = "",
    translator: str = "",
    language: str = "vi",
    fallback_language: str = "",
    min_words: int = 40,
) -> Document:
    """Fetch and extract a MediaWiki (Wikisource) page. Refuses bad licences."""
    from . import wikisource

    check_license(license_id, slug=url)
    return wikisource.extract(
        url,
        translator=translator,
        language=language,
        fallback_language=fallback_language,
        min_words=min_words,
    )


# --------------------------------------------------------------------------
# book.json assembly
# --------------------------------------------------------------------------


def split_oversized(
    sections: Sequence[Section],
    *,
    max_words: int = 6000,
    min_words: int = 400,
) -> list[Section]:
    """Break a chapter that is too long for one audio track, at paragraph edges.

    Some sources have no internal structure at all: *Truyện Kiều* on
    vi.wikisource is a single 22,000-word `<poem>` with no headings. Emitting
    that as one track is not usable, but inventing chapter titles would be a
    lie about the source.

    So the split is mechanical, on paragraph boundaries, and every part is
    labelled as a continuation (`Chương một (tiếp 2)`). The caller records the
    fact in the manifest, and nothing pretends the work has chapters it does not.

    Returns the input unchanged when nothing is oversized.
    """
    if max_words <= 0:
        return list(sections)
    out: list[Section] = []
    for sec in sections:
        if sec.word_count <= max_words:
            out.append(sec)
            continue
        parts = _split_text_by_words(sec.text, max_words=max_words, min_words=min_words)
        if len(parts) <= 1:
            out.append(sec)
            continue
        for i, chunk in enumerate(parts, start=1):
            title = sec.title if i == 1 else f"{sec.title} (tiếp {i})"
            out.append(Section(title=title, text=chunk, level=sec.level))
    return out


def _split_text_by_words(text: str, *, max_words: int, min_words: int) -> list[str]:
    """Greedily pack paragraphs up to `max_words`, never below `min_words`.

    A single paragraph that is itself over the limit is split at line boundaries
    instead. That matters for verse: *Truyện Kiều* is one 22,000-word `<poem>`
    with a hard return per câu thơ and no blank lines at all, so paragraph
    packing alone cannot divide it. Breaks are preferred after a line that ends
    in punctuation, so a chunk boundary never lands mid-sentence in prose.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for para in paragraphs:
        n = count_words(para)
        if n > max_words:
            if buf:
                chunks.append("\n\n".join(buf))
                buf, size = [], 0
            chunks.extend(_split_long_paragraph(para, max_words=max_words))
            continue
        if buf and size + n > max_words and size >= min_words:
            chunks.append("\n\n".join(buf))
            buf, size = [para], n
        else:
            buf.append(para)
            size += n
    if buf:
        chunks.append("\n\n".join(buf))
    return chunks


_SENTENCE_TAIL_RE = re.compile(r"[.!?…][\"'”’»)\]]*$")


def _split_long_paragraph(para: str, *, max_words: int) -> list[str]:
    """Split one oversized block at line boundaries, preferring sentence ends."""
    lines = [ln.strip() for ln in para.split("\n") if ln.strip()]
    if len(lines) <= 1:
        # A single very long line: cut on whitespace so no word is broken.
        words = para.split()
        return [" ".join(words[i : i + max_words]) for i in range(0, len(words), max_words)]

    out: list[str] = []
    buf: list[str] = []
    size = 0
    for line in lines:
        n = count_words(line)
        if buf and size + n > max_words:
            out.append(" ".join(buf))
            buf, size = [line], n
            continue
        buf.append(line)
        size += n
        # Cut early at a sentence end so a chunk does not end mid-thought.
        if size >= max_words * 0.8 and _SENTENCE_TAIL_RE.search(line):
            out.append(" ".join(buf))
            buf, size = [], 0
    if buf:
        out.append(" ".join(buf))
    return out


def write_chapter_files(book_dir: str | Path, sections: Sequence[Section]) -> dict[int, str]:
    """Write `books/<slug>/text/chNN.md` per chapter; return index -> repo path.

    The per-chapter files are what `Chapter.source_path` points at, so agent B
    can read one track without re-parsing the assembled book. They are generated
    from the same `Section` objects as `<slug>.md`, so the two cannot diverge.
    """
    root = Path(book_dir)
    text_dir = root / "text"
    text_dir.mkdir(parents=True, exist_ok=True)
    repo = _repo()
    paths: dict[int, str] = {}
    for i, section in enumerate(sections, start=1):
        (text_dir / f"{chapter_slug(i)}.md").write_text(
            render_chapter_markdown(section), encoding="utf-8"
        )
        paths[i] = str((text_dir / f"{chapter_slug(i)}.md").resolve().relative_to(repo))
    return paths


def assemble_book(
    entry: dict[str, Any],
    document: Document,
    *,
    text_path: str = "",
    write_files: bool = True,
    extra_notes: Sequence[str] = (),
) -> Book:
    """Build a `models.Book` from a catalog entry plus an extracted document.

    Provenance fields (`source_url`, `license`, `license_url`, `rights_note`)
    are copied from the catalog verbatim -- this function never invents them.
    """
    slug = entry["slug"]
    license_id = check_license(entry.get("license", ""), slug=slug)
    sections = [s for s in document.sections if not s.is_empty()]
    if not sections:
        raise ValueError(f"{slug}: extraction produced no chapters")

    text_rel = text_path or f"books/{slug}/text/{slug}.md"
    book_dir = _repo() / "books" / slug
    chapter_paths = write_chapter_files(book_dir, sections) if write_files else {}

    chapters: list[Chapter] = []
    for i, section in enumerate(sections, start=1):
        md = render_chapter_markdown(section)
        chapters.append(
            Chapter(
                index=i,
                slug=chapter_slug(i),
                title=chapter_title(section.title),
                source_path=chapter_paths.get(i, text_rel),
                word_count=count_words(md),
            )
        )

    if write_files:
        target = book_dir / "text" / Path(text_rel).name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(write_markdown(sections), encoding="utf-8")

    book = Book(
        slug=slug,
        title=entry.get("title", ""),
        author=entry.get("author", ""),
        translator=entry.get("translator", "") or document.translator or "",
        language=document.language or entry.get("language", "vi"),
        source_language=entry.get("source_language", "en"),
        source_format=document.source_format or entry.get("source_format", "txt"),
        source_path=document.source_path,
        source_url=document.source_url or entry.get("source_url", ""),
        license=license_id,
        license_url=entry.get("license_url", ""),
        rights_note=entry.get("rights_note", ""),
        summary=entry.get("summary", ""),
        narrator=entry.get("narrator", ""),
        text_path=text_rel,
        chapters=chapters,
        status="extracted",
    )
    del extra_notes
    return book


def save_book(book: Book, path: str | Path, *, extra: dict[str, Any] | None = None) -> Path:
    """Write `book.json`, merging in provenance fields `models.Book` has no slot for.

    `models.Book` is frozen by CONTRACT.md, but the repository rights gate
    (`tools/check_rights.py`) needs `author_dates` or `author_death_year` to check
    a `public-domain` claim arithmetically. There is no field for it, so the extra
    keys are merged into the JSON after `Book.save()`.

    This is safe for the other machine: `Book.from_dict()` drops unknown keys, so
    agent B's `Book.load()` sees exactly the dataclass it expects.
    """
    out = Path(path)
    book.save(out)
    if not extra:
        return out
    data = json.loads(out.read_text(encoding="utf-8"))
    for key, value in extra.items():
        if value in (None, "", [], {}):
            continue
        data[key] = value
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def write_manifest(book: Book, path: str | Path, notes: Sequence[str] = ()) -> Path:
    """Write the `models.Manifest` handed to agent B's machine."""
    import datetime as _dt

    manifest = Manifest(
        book=book,
        stage="extract",
        created_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        notes=list(notes),
    )
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return out


def _repo() -> Path:
    from ..config import REPO_ROOT

    return REPO_ROOT


# --------------------------------------------------------------------------
# `python -m sachnoi.extract`
# --------------------------------------------------------------------------


def _cmd_probe(args: argparse.Namespace) -> int:
    """Report whether a catalog entry has a usable source, without writing."""
    from . import wikisource

    catalog = Path(args.catalog)
    data = json.loads(catalog.read_text(encoding="utf-8"))
    rc = 0
    for entry in data.get("books", []):
        if args.slug and entry["slug"] not in args.slug:
            continue
        try:
            check_license(entry.get("license", ""), slug=entry["slug"])
        except RightsError as exc:
            print(f"[rights-blocked] {entry['slug']}: {exc}")
            rc = 1
            continue
        info = wikisource.probe(entry)
        flag = "OK " if info.available else "MISS"
        print(f"[{flag}] {entry['slug']:<40} {info.detail}")
        if not info.available:
            rc = 2
    return rc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sachnoi.extract",
        description="Inspect catalog sources and the rights gate without running the build.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    probe = sub.add_parser("probe", help="check which catalog books have a fetchable source")
    probe.add_argument("--catalog", default="catalog/books.json")
    probe.add_argument("--slug", action="append", help="limit to these slugs (repeatable)")
    probe.set_defaults(func=_cmd_probe)
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
