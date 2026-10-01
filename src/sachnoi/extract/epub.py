"""EPUB backend.

EPUB is the friendliest format we get: it is XHTML in a zip, and the heading
elements survive publication. So the chapter structure comes straight from
`h1`/`h2` (with a documented fallback to `h3` when a book only uses `h3` for its
top level -- the same "smallest heading level present becomes the chapter level"
rule Sano applies to .docx).

Images, tables and footnotes are removed at the BeautifulSoup level rather than
by regex on text, so a `figure` with a long `alt` cannot leak a caption into the
narration. `<ruby>`/`<rt>` (furigana) is unwrapped, keeping the base text.
"""

from __future__ import annotations

import re
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

__all__ = ["extract", "parse_document", "FURNITURE_CLASSES", "CONTENT_ROOT_SELECTORS"]

#: Blocks that must not be narrated. Mirrors CONTRACT.md's "no HTML, no images,
#: no tables, no footnotes".
_DROP_TAGS = (
    "script",
    "style",
    "img",
    "image",
    "svg",
    "figure",
    "figcaption",
    "picture",
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "td",
    "th",
    "caption",
    "nav",
    "aside",
    "form",
    "input",
    "button",
    "select",
    "iframe",
    "object",
    "embed",
    "video",
    "audio",
    "canvas",
    "hr",
)

#: Subtrees that are site or edition furniture, never narration. On a
#: Wikisource *Index:* (Page namespace) page these carry, between them,
#: everything that must not be read aloud:
#:
#: * the NewPP parser debug report -- `ws-noexport`;
#: * the proofread page number printed between stanzas -- `ws-pagenum`;
#: * the `← Previous | Next →` navigation bar -- `ws-noexport noprint`;
#: * the running title / author banner of the printed edition;
#: * the licence banner, the category list, the edit links, the reference list.
#:
#: Dropping by class rather than by text pattern is what keeps this from turning
#: into an arms race: the debug report's wording changes between MediaWiki
#: releases, its CSS class does not.
FURNITURE_CLASSES: tuple[str, ...] = (
    "ws-noexport",
    "ws-pagenum",
    "pagenum",
    "pagenum-inner",
    "prp-pages-nav",
    "noprint",
    "printfooter",
    "mw-editsection",
    "mw-editsection-bracket",
    "mw-jump-link",
    "navbox",
    "vertical-navbox",
    "catlinks",
    "sisterproject",
    "references",
    "reflist",
    "refbegin",
    "reference",
    "reference-text",
    "licenseContainer",
    "licensetpl",
    "headertemplate",
    "dynlayout-exempt",
    "ws-data",
    "toc",
    "toctitle",
    "shortdescription",
    "mw-jump",
    "thumb",
    "thumbinner",
    "gallery",
    "hatnote",
    "dablink",
)

#: When one of these exists it is the whole content of the page. A Wikisource
#: proofread page wraps the scan in `.prp-pages-output`; everything outside it is
#: navigation, the debug report and the edition banner. Scoping to it is what
#: turns a 6,500-line junk dump into 2,600 lines of Vietnamese.
CONTENT_ROOT_SELECTORS: tuple[str, ...] = (
    "div.prp-pages-output",
    "div.prp-page-body",
    "div.poem",
    "div.mw-parser-output",
    "article",
    "body",
)

#: Unwrap (keep the text, drop the tag) for annotation-ish elements.
_UNWRAP_TAGS = ("sup", "span", "a", "b", "i", "em", "strong", "u", "small", "sub", "rt", "rp")

_FOOTNOTE_MARK_RE = re.compile(r"^\s*(?:\[\d+\]|\d{1,3}|[a-z]\)|\*{1,3})\s*$")
_MD_LIST_RE = re.compile(r"^\s*(?:[-•*+]|\d+[.)])\s+")
#: Zero-width, bidi and non-breaking control characters that survive copy-paste
#: out of rendered HTML. A zero-width space at the start of every proofread block
#: is not prose.
_INVISIBLE_RE = re.compile(
    "[-‏‪-‮⁠-⁤﻿­᠎]"
)


def _require_bs4():
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "epub extraction needs beautifulsoup4: pip install 'sach-noi-vi[extract]'"
        ) from exc
    return BeautifulSoup


def _content_root(soup):
    """The narrowest element that holds only this page's content.

    Scoping to it is the single most valuable rule in this module: it is what
    keeps a Wikisource proofread page's parser debug report, its navigation bar
    and its printed page numbers out of the narration without any text matching.
    """
    for selector in CONTENT_ROOT_SELECTORS:
        found = soup.select_one(selector)
        if found is not None and found.get_text(strip=True):
            return found
    return soup.body or soup


def _html_to_text(markup: str) -> str:
    """Flatten one XHTML document, keeping `==`-style markers for headings."""
    BeautifulSoup = _require_bs4()
    soup = BeautifulSoup(markup, "html.parser")

    for tag in soup.find_all(list(_DROP_TAGS)):
        tag.decompose()
    # Furniture by CSS class, before anything is unwrapped: `span` unwrapping
    # would otherwise destroy the class names we are matching on.
    for name in FURNITURE_CLASSES:
        for tag in soup.find_all(class_=name):
            tag.decompose()
    for tag in soup.find_all("rt"):
        tag.decompose()  # furigana reading -- reading it aloud would be noise
    for tag in soup.find_all(["sup", "sub"]):
        # A footnote *marker* ("[1]", "12") is noise; a real subscript is not.
        marker = tag.get_text(strip=True)
        classes = " ".join(tag.get("class") or []).lower()
        if "footnote" in classes or "note" in classes or re.fullmatch(r"[\[\(]?\d+[\]\)]?", marker):
            tag.decompose()
    for tag in soup.find_all(list(_UNWRAP_TAGS)):
        tag.unwrap()

    root = _content_root(soup)
    return "\n".join(_walk(root))


def _walk(node) -> list[str]:
    from bs4 import NavigableString, Tag

    out: list[str] = []
    block_tags = {"p", "div", "section", "article", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd", "pre", "figcaption"}
    for child in node.children:
        if isinstance(child, NavigableString):
            text = _INVISIBLE_RE.sub("", str(child).replace("\xa0", " "))
            if text.strip():
                out.append(text)
            continue
        if not isinstance(child, Tag):
            continue
        name = child.name.lower()
        if name in {"script", "style"}:
            continue
        if name == "br":
            out.append("\n")
            continue
        if re.fullmatch(r"h[1-6]", name):
            title = child.get_text(" ", strip=True)
            if title:
                level = int(name[1])
                depth = min(level + 1, 4)
                out.append("=" * depth + " " + title + " " + "=" * depth)
            continue
        if name in block_tags:
            inner = _walk(child)
            if any(line.strip() for line in inner):
                out.append("\n".join(inner))
                out.append("\n")
            continue
        out.extend(_walk(child))
    return out


def parse_document(markup: str, *, min_words: int = 40) -> list[Section]:
    """Turn one EPUB XHTML file into chapters."""
    text = _html_to_text(markup)
    text = join_wrapped_lines(text)
    text = drop_page_number_lines(text)
    text = sanitize_prose(text)
    lines = text.split("\n")

    # Promote a book whose top level is <h3> (or deeper) down to <h2>, so its
    # top-level headings become chapters. Sano applies the same "smallest
    # heading level present becomes the chapter level" rule to .docx.
    headings = [ln for ln in lines if re.match(r"^={2,6}\s*.*?\s*=+\s*$", ln)]
    if headings and not any(re.match(r"^==\s*[^=]", ln) for ln in headings):
        lines = [_promote_heading(ln) for ln in lines]

    return detect_chapters("\n".join(lines), min_words=min_words)


def _promote_heading(line: str) -> str:
    """Rewrite `==== X ====` as `== X ==`, keeping the title intact."""
    m = re.match(r"^\s*(=+)\s*(.*?)\s*\1\s*$", line)
    if not m:
        return line
    return f"== {m.group(2).strip()} =="


def extract(
    path: str | Path,
    *,
    min_words: int = 40,
) -> Document:
    """Extract an EPUB, following the OPF spine in reading order."""
    try:
        import ebooklib
        from ebooklib import epub
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "epub extraction needs ebooklib: pip install 'sach-noi-vi[extract]'"
        ) from exc

    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)

    book = epub.read_epub(str(p), options={"ignore_ncx": True})

    # The spine is the reading order; the manifest order is a reasonable
    # fallback for a malformed package. ebooklib 0.18+ yields
    # `(id, href)` tuples rather than bare id strings, so both are accepted.
    items = list(getattr(book, "spine", []) or [])
    ordered: list = []
    seen: set[str] = set()
    for entry in items:
        item: object | None
        if isinstance(entry, str):
            item = book.get_item_with_id(entry)
        elif isinstance(entry, (tuple, list)) and entry:
            item = book.get_item_with_id(entry[0])
        else:
            item = entry
        if item is None or not hasattr(item, "get_name"):
            continue
        key = item.get_name()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(item)
    if not ordered:
        ordered = [i for i in book.get_items_of_type(ebooklib.ITEM_DOCUMENT)]

    title = ""
    try:
        titles = book.get_metadata("DC", "title")
        title = titles[0][0] if titles else ""
    except Exception:  # noqa: BLE001
        title = ""

    translator = ""
    try:
        contribs = book.get_metadata("DC", "contributor")
        for role, name in contribs or []:
            if role and "trans" in role.lower():
                translator = name
                break
    except Exception:  # noqa: BLE001
        pass

    sections: list[Section] = []
    for item in ordered:
        if not item.get_name().lower().endswith((".xhtml", ".html", ".htm")):
            continue
        markup = item.get_content()
        if isinstance(markup, bytes):
            markup = markup.decode("utf-8", errors="replace")
        found = parse_document(markup, min_words=min_words)
        if not found:
            continue
        if len(found) == 1 and not found[0].title.strip():
            found[0].title = _stem(item.get_name())
        sections.extend(found)

    # Cover / nav / copyright pages become stray one-line chapters; drop them.
    sections = [s for s in sections if s.word_count >= 5]
    if not sections:
        raise ValueError(f"{p.name}: no chapters found")

    return Document(
        sections=sections,
        title=title or p.stem,
        source_format="epub",
        source_path=str(p),
        translator=translator,
        extra={"spine_items": len(ordered)},
    )


def _stem(name: str) -> str:
    stem = Path(name).stem
    return re.sub(r"[_-]+", " ", stem).strip().title() or "Nội dung"
