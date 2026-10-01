"""Wikisource backend: MediaWiki API -> Vietnamese prose.

This is the primary source for this project. `vi.wikisource.org` already holds
Vietnamese editions of public-domain works, so the extract stage normally only
has to *clean* the text -- it must never machine-translate it (see
`translate/translate.py`, whose default mode is `off`).

How the fetch works
-------------------
1. `action=query` resolves the page title from a wiki URL, following redirects.
   `catalog/books.json` carries hand-written URLs and some of them are
   mistyped, so a `list=search` fallback (guarded by a similarity threshold)
   runs before we give up.
2. `action=parse&prop=wikitext` returns the raw wikitext, which we strip
   ourselves rather than trusting the rendered HTML: keeping `==` and `===` as
   chapter boundaries is trivial in wikitext and lossy in HTML.
3. `action=query&prop=revisions` records the revision id / timestamp / editor
   so `book.json` can point at an exact, auditable revision of the source.

If the page has subpages (`Tắt đèn/IX`) they are fetched in natural title order
and concatenated, which is how Vietnamese Wikisource lays out most multi-chapter
works.
"""

from __future__ import annotations

import difflib
import json
import re
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import httpx

from . import Section, Document, detect_chapters, sanitize_prose, count_words

__all__ = [
    "MediaWikiClient",
    "ProbeResult",
    "extract",
    "probe",
    "strip_wikitext",
    "split_wikitext_sections",
    "natural_key",
    "title_from_url",
    "SOURCE_LANGS",
]

#: Where a page can live. Vietnamese first: it is the language we narrate.
SOURCE_LANGS: dict[str, str] = {
    "vi": "https://vi.wikisource.org/w/api.php",
    "en": "https://en.wikisource.org/w/api.php",
    "fr": "https://fr.wikisource.org/w/api.php",
}

#: Wikimedia's User-Agent policy: identify the tool and give a contact URL.
USER_AGENT = (
    "sach-noi-vi/0.1 (https://github.com/duonglequanghoang1-create/sach-noi-vi)"
)

#: Search results below this similarity to the wanted title are noise.
SEARCH_SIMILARITY = 0.72

_NETWORK: bool = True  # tests flip this off; see the module docstring


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def title_from_url(url: str) -> str:
    """`.../wiki/Foo_Bar?x=1` -> `Foo Bar`. Handles percent-encoding."""
    if "://" in url:
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path
    else:
        path = url
    if "/wiki/" in path:
        path = path.split("/wiki/", 1)[1]
    title = urllib.parse.unquote(path).replace("_", " ").strip()
    return title.split("#", 1)[0].strip()


def _norm_title(title: str) -> str:
    """Case- and accent-insensitive key, for fuzzy title comparison."""
    t = unicodedata.normalize("NFD", title.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = t.replace("đ", "d").replace("Đ", "D")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def natural_key(title: str) -> list[Any]:
    """Sort key that orders `Chương 2` before `Chương 10`."""
    parts = re.split(r"(\d+)", title.lower())
    return [int(p) if p.isdigit() else p for p in parts]


# --------------------------------------------------------------------------
# MediaWiki client
# --------------------------------------------------------------------------


@dataclass
class Revision:
    """Provenance for the exact revision we extracted."""

    revid: int = 0
    timestamp: str = ""
    user: str = ""
    title: str = ""

    def describe(self) -> str:
        when = self.timestamp.replace("T", " ").replace("Z", " UTC") if self.timestamp else "?"
        return f"{self.title} (rev {self.revid}, {when}, {self.user or '?'})"


@dataclass
class PageResult:
    title: str
    pageid: int = 0
    wikitext: str = ""
    revision: Revision = field(default_factory=Revision)
    subpages: list[str] = field(default_factory=list)


class MediaWikiClient:
    """Thin, polite MediaWiki API client.

    Every call sets a descriptive User-Agent (Wikimedia's UA policy) and the
    requests are spaced by `min_interval` so a full-catalog run does not hammer
    the API. Retries are exponential and only on 429/5xx.
    """

    def __init__(
        self,
        language: str = "vi",
        *,
        timeout: float = 30.0,
        min_interval: float = 0.34,
        max_retries: int = 3,
        client: httpx.Client | None = None,
    ) -> None:
        if language not in SOURCE_LANGS:
            raise ValueError(f"unsupported language {language!r}; pick one of {sorted(SOURCE_LANGS)}")
        self.language = language
        self.endpoint = SOURCE_LANGS[language]
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._last = 0.0
        self._http = client or httpx.Client(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )

    # -- plumbing ---------------------------------------------------------
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "MediaWikiClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def api(self, **params: Any) -> dict[str, Any]:
        """Call `action=query`-style endpoints and return the decoded JSON."""
        if not _NETWORK:
            raise RuntimeError("network disabled (tests)")
        params.setdefault("format", "json")
        params.setdefault("formatversion", "2")
        params.setdefault("errorformat", "plaintext")
        last: Exception | None = None
        for attempt in range(self.max_retries):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                resp = self._http.get(self.endpoint, params=params)
                self._last = time.monotonic()
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise httpx.HTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:  # noqa: BLE001 - retried below
                last = exc
                self._last = time.monotonic()
                time.sleep(0.5 * (2**attempt))
                continue
            if "error" in data:
                raise RuntimeError(str(data["error"]))
            return data
        raise RuntimeError(f"MediaWiki request failed after {self.max_retries} attempts: {last}")

    # -- queries ----------------------------------------------------------
    def page_exists(self, title: str) -> bool:
        data = self.api(action="query", titles=title, prop="info", redirects="1")
        return any(p.get("missing") is not True for p in data.get("query", {}).get("pages", []))

    def resolve(self, title: str) -> str | None:
        """Return the canonical existing title, or None.

        Tries the exact title first, then (because catalog URLs are hand-written
        and sometimes mistyped) the first search hit that is similar enough to
        be the same book.
        """
        data = self.api(action="query", titles=title, prop="info", redirects="1")
        pages = data.get("query", {}).get("pages", [])
        if pages and pages[0].get("missing") is not True:
            return str(pages[0].get("title", title))
        if not title.strip():
            return None
        hits = self.api(
            action="query",
            list="search",
            srsearch=title,
            srnamespace="0",
            srlimit="8",
        ).get("query", {}).get("search", [])
        want = _norm_title(title)
        best: tuple[float, str] | None = None
        for hit in hits:
            score = difflib.SequenceMatcher(None, want, _norm_title(hit["title"])).ratio()
            if best is None or score > best[0]:
                best = (score, str(hit["title"]))
        if best and best[0] >= SEARCH_SIMILARITY:
            return best[1]
        return None

    def wikitext(self, title: str) -> tuple[str, str]:
        """Return `(canonical_title, wikitext)` for a page."""
        data = self.api(action="parse", page=title, prop="wikitext", redirects="1")
        parse = data.get("parse", {})
        return str(parse.get("title", title)), str(parse.get("wikitext", ""))

    def revision(self, title: str) -> Revision:
        data = self.api(
            action="query",
            titles=title,
            prop="revisions",
            rvprop="ids|timestamp|user",
            rvslots="main",
            redirects="1",
        )
        pages = data.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing") is True:
            return Revision(title=title)
        page = pages[0]
        revs = page.get("revisions") or [{}]
        rev = revs[0]
        return Revision(
            revid=int(rev.get("revid", 0) or 0),
            timestamp=str(rev.get("timestamp", "")),
            user=str(rev.get("user", "")),
            title=str(page.get("title", title)),
        )

    def subpages(self, title: str) -> list[str]:
        """Child pages under `title/`, in natural order (Chương 2 before 10)."""
        prefix = f"{title}/"
        data = self.api(
            action="query",
            list="allpages",
            apnamespace="0",
            apprefix=prefix,
            aplimit="max",
        )
        return sorted((p["title"] for p in data.get("query", {}).get("allpages", [])), key=natural_key)


# --------------------------------------------------------------------------
# Wikitext -> prose
# --------------------------------------------------------------------------

#: Sections that are apparatus, not story. Their heading *and* body are dropped.
_APPARATUS_RE = re.compile(
    r"^(mục lục|nội dung|table of contents|contents|toc|liên kết ngoài|"
    r"tham khảo|nguồn|ghi chú|chú thích|bản quyền|giấy phép|license|tác giả|"
    r"soạn giả|người dịch|ban dịch|bản dịch|translator|xem thêm|bibliography|"
    r"thư mục|phụ lục|chú thích sách|editor|source)",
    re.IGNORECASE,
)

_HEADING_RE = re.compile(r"^[ \t]*(={2,6})[ \t]*(.*?)[ \t]*\1[ \t]*$")
_ANY_HEADING_RE = re.compile(r"^[ \t]*(={1,6})[ \t]*(.*?)[ \t]*\1?[ \t]*$")
_TABLE_START_RE = re.compile(r"^[ \t]*\{\|")
_LIST_LINE_RE = re.compile(r"^[ \t]*[*#:;]+")

#: `<nowiki>`, `<math>`, `<gallery>`, `<timeline>`, `<pre>` blocks whose content
#: is source-ish rather than prose.
_DROP_BLOCK_RE = re.compile(
    r"<(nowiki|math|gallery|timeline|pre|source|score|imagemap|mapframe|maplink)\b[^>]*>.*?"
    r"</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_DROP_EMPTY_RE = re.compile(r"<(nowiki|math|gallery|timeline|pre|score|imagemap)\b[^>]*/>", re.IGNORECASE)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_REF_BLOCK_RE = re.compile(r"<ref\b[^>]*>.*?</ref\s*>", re.IGNORECASE | re.DOTALL)
_REF_SELF_RE = re.compile(r"<ref\b[^>]*/>", re.IGNORECASE)
#: `<ref name="x" />` and a dangling `</ref>` after the block regex ate a body.
_REF_NAME_RE = re.compile(r"<ref\b[^>]*>.*?</ref\s*>", re.IGNORECASE | re.DOTALL)
_STRAGGLING_REF_RE = re.compile(r"</?ref\b[^>]*>", re.IGNORECASE)
_FILE_LINK_RE = re.compile(
    r"\[\[\s*(?:File|Image|Tập tin|Hình|Media|Ảnh)\s*:[^\]]*\]\]", re.IGNORECASE
)
_CATEGORY_RE = re.compile(r"\[\[\s*(?:Category|Thể loại)\s*:[^\]]*\]\]", re.IGNORECASE)
_EXT_LINK_RE = re.compile(r"\[(?:https?|ftp|mailto|news|irc):[^\s\]]+(?:\s+([^\]]*))?\]")
_INTERNAL_RE = re.compile(r"\[\[([^\]\|]+)(?:\|([^\]]*))?\]\]")
_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
_BOLD_ITALIC_RE = re.compile(r"'{2,5}")
_BEHAVIOR_TOGGLE_RE = re.compile(r"__[A-Z]+__")
_HTML_ENTITIES = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&apos;": "'",
    "&ndash;": "–",
    "&mdash;": "—",
    "&hellip;": "…",
    "&#160;": " ",
}


def _strip_templates(text: str) -> str:
    """Remove `{{...}}` including nested ones, innermost first.

    Iterating on the *innermost* pattern (`{{` with no further `{{` before the
    closing `}}`) is what makes `{{quote|outer {{cite|inner}}}}` disappear
    completely instead of leaving a husk behind.
    """
    innermost = re.compile(r"\{\{(?:[^{}]|\{[^{}]*\})*\}\}")
    for _ in range(40):
        new = innermost.sub("", text)
        if new == text:
            break
        text = new
    return re.sub(r"\{\{[^{}]*\}\}", "", text)  # any pathological remnant


def strip_wikitext(text: str) -> str:
    """Remove every trace of wiki markup, keeping prose and `==` headings.

    Deliberately *not* removed: `==` / `===` headings -- they are the chapter
    boundaries CONTRACT.md needs.
    """
    if not text:
        return ""
    s = text.replace("\r\n", "\n").replace("\r", "\n")

    # 1. comments and non-prose blocks
    s = _COMMENT_RE.sub("", s)
    s = _DROP_BLOCK_RE.sub("", s)
    s = _DROP_EMPTY_RE.sub("", s)

    # 2. footnotes / references (whole callout, then any straggler tag)
    s = _REF_BLOCK_RE.sub("", s)
    s = _REF_SELF_RE.sub("", s)
    s = _REF_NAME_RE.sub("", s)
    s = _STRAGGLING_REF_RE.sub("", s)

    # 3. tables: drop the `{| ... |}` block including nested cells
    s = _drop_tables(s)

    # 4. templates, behaviour switches, categories, files
    s = _BEHAVIOR_TOGGLE_RE.sub("", s)
    s = _FILE_LINK_RE.sub("", s)
    s = _CATEGORY_RE.sub("", s)
    s = _strip_templates(s)

    # 5. inline markup -> plain text
    s = _INTERNAL_RE.sub(lambda m: m.group(2) if m.group(2) else m.group(1), s)
    s = _EXT_LINK_RE.sub(lambda m: (m.group(1) or "").strip(), s)
    s = _BOLD_ITALIC_RE.sub("", s)
    s = _TAG_RE.sub("", s)
    for ent, rep in _HTML_ENTITIES.items():
        s = s.replace(ent, rep)
    s = s.replace("&amp;", "&")

    # 6. per-line tidy: kill leftover list markers and horizontal rules
    out: list[str] = []
    for line in s.split("\n"):
        if _TABLE_START_RE.match(line) or re.match(r"^[ \t]*[|!][ \t]*$", line):
            continue
        if re.match(r"^[ \t]*-{4,}[ \t]*$", line):
            continue
        stripped = _LIST_LINE_RE.sub("", line).rstrip()
        out.append(stripped)
    s = "\n".join(out)

    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip("\n")


def _drop_tables(s: str) -> str:
    """Remove `{| ... |}` wikitext tables, honouring nesting."""
    out: list[str] = []
    depth = 0
    for line in s.split("\n"):
        opens = line.count("{|")
        closes = line.count("|}")
        if depth == 0 and opens:
            depth = 1 + (opens - 1) - closes
            continue
        if depth > 0:
            depth += opens - closes
            if depth <= 0:
                depth = 0
            continue
        out.append(line)
    return "\n".join(out)


def split_wikitext_sections(
    wikitext: str,
    *,
    min_words: int = 40,
    max_level: int = 3,
    strip_first_heading: bool = True,
) -> list[Section]:
    """Turn stripped wikitext into chapters at `==` and `===`.

    `max_level=3` means `==` (level 1) and `===` (level 2) both start a chapter,
    as the extract contract requires; `====` and deeper stay inside the chapter
    as ordinary prose. Front/back matter (`Mục lục`, `Ghi chú`, `Bản quyền`, ...)
    is dropped entirely, apparatus and all.
    """
    lines = strip_wikitext(wikitext).split("\n")

    marks: list[tuple[int, int, str]] = []
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            marks.append((i, len(m.group(1)), m.group(2).strip()))

    if not marks:
        body = sanitize_prose("\n".join(lines))
        return [Section(title="Nội dung", text=body)] if body.strip() else []

    sections: list[Section] = []
    for idx, (line_no, level, title) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(lines)
        if level > max_level:
            # Too deep to be a chapter: fold its text into the running chapter.
            if sections:
                extra = "\n".join(lines[line_no + 1 : end]).strip()
                if extra:
                    sections[-1].text = (sections[-1].text + "\n\n" + extra).strip()
            continue
        if _APPARATUS_RE.match(re.sub(r"^[\d\W_]+", "", title).strip()):
            continue
        body = "\n".join(lines[line_no + 1 : end]).strip()
        # A `== Chương 4 ==` immediately followed by `=== Chương 4 ===` is a
        # duplicate; drop the shallower one.
        chapters = [s for s in sections if s.title.strip() and not _APPARATUS_RE.match(s.title.strip())]
        if chapters and strip_first_heading and _same_title(chapters[-1].title, title):
            chapters[-1].text = (chapters[-1].text + "\n\n" + body).strip()
            continue
        sections.append(Section(title=title, text=body, level=min(level, 2)))

    cleaned = [Section(title=s.title, text=sanitize_prose(s.text), level=s.level) for s in sections]
    cleaned = [s for s in cleaned if not s.is_empty()]

    # A trailing apparatus section (e.g. a stub "Ghi chú" that survived) can
    # leave a 3-word chapter; drop stubs and merge the rest into the previous.
    kept: list[Section] = []
    for s in cleaned:
        if s.word_count < 5:
            if kept and s.word_count:
                kept[-1].text = (kept[-1].text + "\n\n" + s.text).strip()
            continue
        kept.append(s)
    if min_words > 0 and len(kept) > 1:
        merged: list[Section] = []
        buf: list[Section] = []
        for s in kept:
            buf.append(s)
            if s.word_count >= min_words:
                merged.append(_join(buf))
                buf = []
        if buf:
            merged[-1] = _join([merged[-1], *buf]) if merged else _join(buf)
        kept = merged
    return kept


def _join(parts: Sequence[Section]) -> Section:
    if len(parts) == 1:
        return parts[0]
    return Section(
        title=parts[0].title,
        text="\n\n".join(p.text.strip() for p in parts if p.text.strip()),
        level=1,
    )


def _same_title(a: str, b: str) -> bool:
    na = re.sub(r"^[\d\W_]+", "", a).strip().lower()
    nb = re.sub(r"^[\d\W_]+", "", b).strip().lower()
    if not na or not nb:
        return False
    return na == nb or difflib.SequenceMatcher(None, na, nb).ratio() >= 0.9


# --------------------------------------------------------------------------
# Public entrypoints
# --------------------------------------------------------------------------


@dataclass
class ProbeResult:
    """Outcome of looking for a usable source, for `books.json` diagnostics."""

    slug: str = ""
    available: bool = False
    detail: str = ""
    title: str = ""
    pageid: int = 0
    language: str = ""
    revision: Revision | None = None
    chapters: int = 0
    words: int = 0
    tried: list[str] = field(default_factory=list)


def _candidate_titles(entry: dict[str, Any], language: str) -> list[str]:
    """URLs/languages to try, in preference order, with catalog typos noted."""
    out: list[str] = []
    for key in ("vi_source_url", "source_url"):
        url = entry.get(key) or ""
        if not url:
            continue
        if f"//{language}.wikisource.org/" in url or (language != "vi" and key == "vi_source_url"):
            continue
        out.append(url)
    # Any *other* language URL is still better than nothing when asked for.
    for key in ("vi_source_url", "source_url"):
        url = entry.get(key) or ""
        if url and url not in out:
            out.append(url)
    return out


def probe(entry: dict[str, Any], *, languages: Sequence[str] = ("vi",)) -> ProbeResult:
    """Check whether a catalog entry has a fetchable, non-trivial source.

    Never raises for a missing page -- a miss is data the build reports, not an
    error. Network failures are reported as unavailable with the reason.
    """
    res = ProbeResult(slug=entry.get("slug", ""))
    for lang in languages:
        for url in _candidate_titles(entry, lang):
            res.tried.append(f"{lang}:{url}")
            try:
                with MediaWikiClient(lang) as mw:
                    title = mw.resolve(title_from_url(url))
                    if not title:
                        res.detail = f"khong tim thay trang tren {lang}.wikisource.org"
                        continue
                    _, wiki = mw.wikitext(title)
                    sections = split_wikitext_sections(wiki)
                    words = sum(s.word_count for s in sections)
                    if not sections or words < 200:
                        res.detail = (
                            f"trang {title!r} ton tai nhung rong/ngan ({words} chu)"
                            if not sections
                            else f"trang {title!r} chi co {words} chu"
                        )
                        continue
                    res.available = True
                    res.title = title
                    res.language = lang
                    res.chapters = len(sections)
                    res.words = words
                    res.revision = mw.revision(title)
                    res.detail = f"{title} [{res.revision.describe()}] {len(sections)} chuong, {words} chu"
                    return res
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                res.detail = f"loi mang: {exc}"
                continue
    if not res.detail:
        res.detail = "khong co URL nguon"
    return res


def extract(
    url: str,
    *,
    translator: str = "",
    language: str = "vi",
    fallback_language: str = "",
    min_words: int = 40,
    client: MediaWikiClient | None = None,
    include_subpages: bool = True,
) -> Document:
    """Fetch a Wikisource page (and its subpages) and return clean chapters.

    Raises `LookupError` when the page cannot be found, `ValueError` when it
    exists but yields no usable prose. `fallback_language` is opt-in and never
    silent: if it is used, `Document.language` and `Document.notes` say so, so
    a caller cannot mistake English text for Vietnamese.
    """
    notes: list[str] = []
    doc_lang = language
    for lang in ([language] + ([fallback_language] if fallback_language else [])):
        try:
            doc = _extract_one(
                url,
                language=lang,
                min_words=min_words,
                client=client,
                include_subpages=include_subpages,
            )
        except LookupError as exc:
            notes.append(f"{lang}: {exc}")
            continue
        doc.notes = notes
        doc.language = doc_lang
        if lang != language:
            doc.notes.append(
                f"VIETNAMESE EDITION NOT FOUND -- extracted the {lang} edition instead. "
                f"language={lang}; this is NOT narratable as Vietnamese and must be "
                f"translated by a human before TTS."
            )
            doc.language = lang
        doc.translator = translator
        return doc
    raise LookupError("; ".join(notes) or f"cannot fetch {url}")


def _extract_one(
    url: str,
    *,
    language: str,
    min_words: int,
    client: MediaWikiClient | None,
    include_subpages: bool,
) -> Document:
    mw = client or MediaWikiClient(language)
    owns = client is None
    try:
        want = title_from_url(url)
        title = mw.resolve(want)
        if not title:
            raise LookupError(f"page {want!r} not found on {language}.wikisource.org")
        _, wiki = mw.wikitext(title)
        rev = mw.revision(title)
        parts: list[tuple[str, str]] = [(title, wiki)]

        if include_subpages:
            for sub in mw.subpages(title):
                _, sub_wiki = mw.wikitext(sub)
                parts.append((sub, sub_wiki))

        sections: list[Section] = []
        for page_title, page_wiki in parts:
            found = split_wikitext_sections(page_wiki, min_words=min_words)
            for sec in found:
                # For a multi-page work the subpage name is the better title.
                if page_title != title and not re.match(
                    r"^\s*(chương|chuong|phần|phan|chapter|part)\b", sec.title, re.IGNORECASE
                ):
                    sec.title = f"{page_title.split('/')[-1]}: {sec.title}" if sec.title else page_title.split("/")[-1]
                sections.append(sec)

        if not sections:
            # A page with no `==` structure at all still has prose; split it
            # with the shared heading/keyword detector rather than giving up.
            from . import drop_page_number_lines

            flat = drop_page_number_lines(strip_wikitext(wiki))
            sections = detect_chapters(sanitize_prose(flat), min_words=min_words)
        if not sections:
            raise ValueError(f"page {title!r} has no usable prose")

        return Document(
            sections=sections,
            title=title,
            source_format="wikisource",
            source_url=f"https://{language}.wikisource.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")),
            language=language,
            extra={
                "page_title": title,
                "pageid": rev.pageid,
                "revision": rev.revid,
                "revision_timestamp": rev.timestamp,
                "revision_user": rev.user,
                "pages": [p for p, _ in parts],
            },
        )
    finally:
        if owns:
            mw.close()
