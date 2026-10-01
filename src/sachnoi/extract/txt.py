"""Plain-text / Markdown backend.

Handles the formats a human is most likely to hand us: a `.txt` from
Project Gutenberg, a Markdown draft, or a `.wikitext` dump. Chapter detection
follows Sano's approach -- trust the heading, fall back to the chapter keyword
list, and never lose text that appears before the first heading.

The one thing this backend is really good at is **re-joining lines that were
broken for print**. A prose file wrapped at 72 columns is a single paragraph
split across 6 lines; reading each line as its own sentence would make the TTS
stutter. `join_wrapped_lines()` rebuilds paragraphs from line shape alone,
before any chapter detection happens.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from . import (
    Section,
    Document,
    count_words,
    detect_chapters,
    drop_page_number_lines,
    sanitize_prose,
)

__all__ = ["extract", "load", "join_wrapped_lines", "parse_markdown", "dehyphenate"]

#: A line is probably the *end* of a paragraph when it ends in terminal
#: punctuation. Everything else is a soft wrap.
_SENTENCE_END_RE = re.compile(r"[.!?…][\"'”’»)\]]*$")
#: A short line followed by an indented or blank-free block reads as a heading
#: in a Gutenberg-style text file.
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*#*$")
_WIKITEXT_HEADING_RE = re.compile(r"^\s*(={2,6})\s*(.*?\S)\s*\1\s*$")
_SETEXT_RE = re.compile(r"^([=\-~*_])\1{2,}\s*$")
#: `w:PageBreak` style markers left in a MediaWiki export.
_PAGEBREAK_RE = re.compile(r"^[\s]*(pagebreak|page break|-{4,}|={4,})[\s]*$", re.IGNORECASE)


def dehyphenate(text: str) -> str:
    """Join words split across a line break by a typesetting hyphen.

    Only touches a trailing `-` followed by a lowercase letter, so real hyphens
    in compounds (`chiếc-xe`) and the em-dash range notation (`2-3`) survive.
    """
    # A hyphen at end of line, next line starting lowercase -> no space between.
    return re.sub(r"(\w)-\n[ \t]*(?=[a-zà-ỹ])", r"\1", text)


def join_wrapped_lines(text: str) -> str:
    """Rebuild paragraphs from hard-wrapped prose.

    Rules, in order:
    * a blank line is always a paragraph break;
    * a line ending in `.`/`!`/`?`/`…` ends its paragraph;
    * a short line surrounded by blank lines is a heading, kept as its own
      paragraph so chapter detection can still see it;
    * everything else is joined with a single space.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = dehyphenate(text)
    raw = text.split("\n")

    # Pass 1: mark structural lines.
    kinds: list[str] = []
    for i, line in enumerate(raw):
        s = line.rstrip()
        if not s.strip():
            kinds.append("blank")
        elif _MD_HEADING_RE.match(s) or _WIKITEXT_HEADING_RE.match(s) or _PAGEBREAK_RE.match(s):
            kinds.append("heading")
        elif _SETEXT_RE.match(s) and i > 0 and raw[i - 1].strip():
            # Setext underline: title is the line above.
            kinds.append("setext")
        elif _SENTENCE_END_RE.search(s):
            kinds.append("end")
        else:
            kinds.append("cont")

    # Pass 2: emit paragraphs.
    out: list[str] = []
    buf: list[str] = []
    for i, (line, kind) in enumerate(zip(raw, kinds)):
        s = line.strip()
        if kind == "blank":
            if buf:
                out.append(" ".join(buf))
                buf = []
            out.append("")
            continue
        if kind == "setext":
            if buf:
                out.append(" ".join(buf))
                buf = []
            out.append(buf_line := (raw[i - 1].strip() or s))
            del buf_line
            continue
        if kind == "heading":
            if buf:
                out.append(" ".join(buf))
                buf = []
            out.append(s)
            continue
        buf.append(s)
        if kind == "end":
            out.append(" ".join(buf))
            buf = []
    if buf:
        out.append(" ".join(buf))

    # Collapse runs of blank lines, and drop a setext title that lost its
    # underline.
    result: list[str] = []
    for line in out:
        if not line.strip():
            if result and result[-1] == "":
                continue
            result.append("")
        else:
            result.append(line)
    while result and result[0] == "":
        result.pop(0)
    while result and result[-1] == "":
        result.pop()
    return "\n".join(result)


def parse_markdown(text: str) -> str:
    """Normalise Markdown to plain text while keeping headings as headings.

    Level-1 headings are demoted to `==`-style so `detect_chapters()` treats
    them as chapters, and lists/quotes lose their markers.
    """
    lines: list[str] = []
    in_fence = False
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            lines.append("")
            continue
        if in_fence:
            lines.append("")
            continue
        m = _MD_HEADING_RE.match(line.rstrip())
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            # Every heading becomes a wikitext heading so one code path handles
            # both `# X` and `== X ==`. Level is capped at 3: deeper headings
            # are subsections, not chapters.
            depth = min(level + 1, 4)
            lines.append("=" * depth + " " + title + " " + "=" * depth)
            continue
        if _SETEXT_RE.match(line.rstrip()) and lines and lines[-1].strip():
            # Setext: promote the previous line to a heading.
            prev = lines.pop().strip()
            lines.append("== " + prev + " ==")
            continue
        s = re.sub(r"^\s*>\s?", "", line)  # blockquote
        s = re.sub(r"^\s*[*+-]\s+", "", s)  # bullet
        s = re.sub(r"^\s*\d+[.)]\s+", "", s)  # ordered item
        s = re.sub(r"^\s*[*_]{3,}\s*$", "", s)  # thematic break
        lines.append(s)
    return "\n".join(lines)


def load(path: str | Path) -> str:
    """Read a source file, sniffing UTF-8 vs UTF-16 vs Latin-1."""
    p = Path(path)
    raw = p.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "cp1258", "latin-1"):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
        # Reject a decode that produced mojibake: too many replacement chars or
        # a BOM mismatch.
        if "\ufffd" in text[:2000]:
            continue
        return text
    return raw.decode("utf-8", errors="replace")


def extract(
    path: str | Path,
    *,
    min_words: int = 40,
    drop_front_matter: bool = True,
) -> Document:
    """Parse a `.txt` / `.md` / `.wikitext` file into chapters."""
    p = Path(path)
    text = load(p)
    suffix = p.suffix.lower()
    raw_is_markdown = suffix in {".md", ".markdown"}

    # A `.wikitext`/`.wiki` dump is already wikitext: run it through the same
    # cleaner the Wikisource backend uses so `{{...}}` and `<ref>` never reach
    # the TTS.
    if suffix in {".wikitext", ".wiki"}:
        from .wikisource import split_wikitext_sections

        sections = split_wikitext_sections(text, min_words=min_words)
        return Document(
            sections=sections,
            title=p.stem,
            source_format="txt",
            source_path=str(p),
        )

    if raw_is_markdown:
        text = parse_markdown(text)
    else:
        text = join_wrapped_lines(text)

    text = sanitize_prose(text)
    text = drop_page_number_lines(text)
    if drop_front_matter:
        text = _drop_gutenberg_boilerplate(text)

    sections = detect_chapters(text, min_words=min_words)
    return Document(
        sections=sections,
        title=p.stem,
        source_format="txt",
        source_path=str(p),
    )


def _drop_gutenberg_boilerplate(text: str) -> str:
    """Cut the standard Project Gutenberg header/footer."""
    start = re.search(
        r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG[^*]*\*\*\*", text, re.IGNORECASE
    )
    if start:
        text = text[start.end() :]
    end = re.search(
        r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG", text, re.IGNORECASE
    )
    if end:
        text = text[: end.start()]
    # "Produced by ..." / "Release date:" style front matter.
    lines = text.split("\n")
    while lines and (
        not lines[0].strip()
        or re.match(
            r"^(produced by|release date|encoding|audio:|language:|credits:)",
            lines[0].strip(),
            re.IGNORECASE,
        )
    ):
        lines.pop(0)
    text = "\n".join(lines)
    return text.strip()
