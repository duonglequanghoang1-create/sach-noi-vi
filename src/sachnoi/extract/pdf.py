"""PDF backend.

Two extraction paths, in order of preference:

1. **PyMuPDF outline.** If the PDF has bookmarks, the outline *is* the chapter
   structure -- no guessing. This is the highest-quality path and the one
   Sano prefers when it is available.
2. **`pdftotext -layout` (poppler).** The primary text path. `-layout` preserves
   the column/indent geometry, which is what makes the wrapped-line repair in
   `txt.join_wrapped_lines` work. PyMuPDF's own text extraction is the
   fallback when poppler is not installed.

Either way the output goes through the same cleanup as every other backend:
page-number lines dropped, de-hyphenated, re-joined, then chapter-detected.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import (
    Section,
    Document,
    count_words,
    detect_chapters,
    drop_page_number_lines,
    sanitize_prose,
)
from .txt import join_wrapped_lines

__all__ = ["extract", "pdftotext_available", "extract_with_pdftotext", "extract_with_pymupdf", "pdf_outline"]

#: Only a bookmark that opens a real page counts as a chapter.
_MIN_OUTLINE_PAGE_CHARS = 120


def pdftotext_available() -> bool:
    return shutil.which("pdftotext") is not None


def extract_with_pdftotext(path: str | Path, *, timeout: int = 300) -> tuple[str, list[str]]:
    """Run `pdftotext -layout` and return `(text, form_feed_pages)`."""
    p = Path(path)
    with tempfile.TemporaryDirectory(prefix="sachnoi-pdf-") as tmp:
        out = Path(tmp) / "out.txt"
        cmd = ["pdftotext", "-layout", "-enc", "UTF-8", "-q", str(p), str(out)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0 or not out.is_file():
            raise RuntimeError(f"pdftotext failed ({proc.returncode}): {proc.stderr.strip()[:400]}")
        text = out.read_text(encoding="utf-8", errors="replace")
    pages = text.split("\f")
    return text, pages


def extract_with_pymupdf(path: str | Path) -> tuple[str, list[str]]:
    """Fallback text extraction via PyMuPDF, one string per page."""
    import pymupdf  # PyMuPDF>=1.24 exposes the `pymupdf` name

    pages: list[str] = []
    with pymupdf.open(str(path)) as doc:
        for page in doc:
            pages.append(page.get_text("text"))
    return "\f".join(pages), pages


def pdf_outline(path: str | Path) -> list[tuple[int, str, int]]:
    """Return `[(page_index, title, level)]` from the PDF bookmarks.

    Returns `[]` when the PDF has no usable outline. PyMuPDF's `toc` is a list
    of `[level, title, page]` with 1-based pages.
    """
    try:
        import pymupdf
    except ImportError:  # pragma: no cover
        return []
    try:
        with pymupdf.open(str(path)) as doc:
            toc = doc.get_toc(simple=True) or []
    except Exception:  # noqa: BLE001 - a broken PDF is not a crash
        return []
    out: list[tuple[int, str, int]] = []
    for item in toc:
        if len(item) < 3:
            continue
        level, title, page = int(item[0]), str(item[1]).strip(), int(item[2])
        if not title or page < 1:
            continue
        out.append((page - 1, title, level))
    return out


def _pages_to_sections(pages: list[str], outline: list[tuple[int, str, int]]) -> list[Section]:
    """Slice per-page text at bookmark boundaries."""
    if not outline:
        return []
    min_level = min(lvl for _, _, lvl in outline)
    boundaries = [(p, t) for p, t, lvl in outline if lvl == min_level]
    if not boundaries:
        return []

    sections: list[Section] = []
    for i, (start_page, title) in enumerate(boundaries):
        end_page = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(pages)
        body = "\n".join(pages[start_page:end_page]).strip()
        if count_words(body) < 5:
            continue
        sections.append(Section(title=title, text=body, level=1))
    return sections


def extract(
    path: str | Path,
    *,
    min_words: int = 40,
    use_outline: bool = True,
) -> Document:
    """Extract a PDF into chapters.

    The outline wins when it is present *and* yields at least two chapters with
    real prose; otherwise we fall back to layout text plus heading detection.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)

    engine = "pdftotext"
    try:
        raw, pages = extract_with_pdftotext(p)
    except Exception:  # noqa: BLE001 - poppler missing or unhappy
        raw, pages = extract_with_pymupdf(p)
        engine = "pymupdf"

    sections: list[Section] = []
    if use_outline:
        outline = pdf_outline(p)
        sections = _pages_to_sections(pages, outline)

    if len(sections) < 2:
        # No usable outline: clean the whole document and detect chapters.
        cleaned = _clean_pdf_text(raw, pages=pages, use_layout=engine == "pdftotext")
        sections = detect_chapters(cleaned, min_words=min_words)

    if not sections:
        raise ValueError(f"{p.name}: no extractable text (scanned image PDF?)")

    return Document(
        sections=sections,
        title=p.stem,
        source_format="pdf",
        source_path=str(p),
        extra={"engine": engine, "pages": len(pages)},
    )


#: A line that is only a page number, or the repeated running head. `drop_page_number_lines`
#: handles the first; the second is caught by the repeat detector in that function.
_PDF_JUNK_RE = re.compile(
    r"^[\s]*(đ[èa]o trang\s+\d+|\d+)$", re.IGNORECASE
)


def _clean_pdf_text(raw: str, *, pages: list[str], use_layout: bool) -> str:
    """Repair the two things `pdftotext -layout` does badly: gutters and wraps."""
    if use_layout:
        # `-layout` pads short lines with trailing spaces to the column width.
        # Strip them, otherwise the wrapped-line repair thinks every line is a
        # full line and never joins anything.
        raw = "\n".join(line.rstrip() for line in raw.split("\n"))
        # Collapse the 2-4 space gutter that column layout inserts mid-sentence.
        # Only inside a line, and only when the gap is not sentence-final.
        raw = re.sub(r"(?<=[^\s\d])\s{2,}(?=[^\s])", " ", raw)
    raw = _PDF_JUNK_RE.sub("", raw)
    raw = drop_page_number_lines(raw)
    raw = join_wrapped_lines(raw)
    raw = sanitize_prose(raw)
    return raw
