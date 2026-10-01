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

__all__ = ["extract", "parse_document"]

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

#: Unwrap (keep the text, drop the tag) for annotation-ish elements.
_UNWRAP_TAGS = ("sup", "span", "a", "b", "i", "em", "strong", "u", "small", "sub", "rt", "rp")

_FOOTNOTE_MARK_RE = re.compile(r"^\s*(?:\[\d+\]|\d{1,3}|[a-z]\)|\*{1,3})\s*$")
_MD_LIST_RE = re.compile(r"^\s*(?:[-•*+]|\d+[.)])\s+")


def _require_bs4():
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "epub extraction needs beautifulsoup4: pip install 'sach-noi-vi[extract]'"
        ) from exc
    return BeautifulSoup


def _html_to_text(markup: str) -> str:
    """Flatten one XHTML document, keeping `==`-style markers for headings."""
    BeautifulSoup = _require_bs4()
    soup = BeautifulSoup(markup, "html.parser")

    for tag in soup.find_all(list(_DROP_TAGS)):
        tag.decompose()
    for tag in soup.find_all("rt"):
        tag.decompose()  # furigana reading -- reading it aloud would be noise
    for tag in soup.find_all(list(_UNWRAP_TAGS)):
        tag.unwrap()

    # Walk the body once, emitting a marker line for each block-level element so
    # paragraph boundaries survive the flatten.
    root = soup.body or soup
    return "\n".join(_walk(root))


def _walk(node) -> list[str]:
    from bs4 import NavigableString, Tag

    out: list[str] = []
    block_tags = {"p", "div", "section", "article", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd", "pre", "figcaption"}
    for child in node.children:
        if isinstance(child, NavigableString):
            text = str(child).replace("\xa0", " ")
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

    # Promote h3 (===) to h2 (==) if the book never uses h1/h2, so a book whose
    # top level is <h3> still yields chapters.
    has_12 = any(re.match(r"^==\s", ln) for ln in lines)
    if not has_12:
        lines = [re.sub(r"^===\s*(.*?)\s*===$", r"== \1 ==", ln) for ln in lines]

    return detect_chapters("\n".join(lines), min_words=min_words)


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
    # fallback for a malformed package.
    items = list(getattr(book, "spine", []) or [])
    ordered: list = []
    seen: set[str] = set()
    for entry in items:
        item = book.get_item_with_id(entry) if isinstance(entry, str) else entry
        if item is None:
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
