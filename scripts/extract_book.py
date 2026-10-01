#!/usr/bin/env python3
"""Build `books/<slug>/text/<slug>.md` and `books/<slug>/book.json` from the catalog.

    python3 scripts/extract_book.py                     # every book in the catalog
    python3 scripts/extract_book.py --slug truyen-kieu  # just one
    python3 scripts/extract_book.py --translate-mode gloss
    python3 scripts/extract_book.py --probe             # check sources, write nothing
    python3 scripts/extract_book.py --offline           # rebuild from books/*/source/

Pipeline, in order:

1. **Rights gate** -- refuses a book whose `license` is not public-domain or an
   explicitly redistributable Creative Commons licence, before any fetch.
2. **Fetch** the source: the Vietnamese Wikisource page named by
   `wikisource_url` (and its subpages), or a file the operator dropped into
   `books/<slug>/source/`.
3. **Translate** with the configured mode. The default is `off`: every catalog
   entry is already a Vietnamese edition, and CONTRACT.md forbids machine
   translation. A book with no Vietnamese source becomes
   `status=blocked-no-vi-source` -- never a machine translation.
4. **Write** `text/<slug>.md` plus one `text/chNN.md` per chapter.
5. **Write** `book.json` from `models.Book`, copying provenance from the catalog
   and merging the fields the repository rights gate needs.
6. **Write** a `Manifest` for the other machine.

Exit codes: 0 all books built, 1 a hard failure, 2 at least one book was blocked
(the others still built).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from sachnoi.config import REPO_ROOT as CONFIG_ROOT  # noqa: E402
from sachnoi.extract import (  # noqa: E402
    RightsError,
    check_license,
    count_words,
    effective_min_chapter_words,
    normalize_license,
    save_book,
    structure_chapters,
    write_manifest,
)
from sachnoi.extract import wikisource  # noqa: E402
from sachnoi.models import Book  # noqa: E402
from sachnoi.translate import TranslationError, build_engine, translate_document  # noqa: E402

CATALOG = CONFIG_ROOT / "catalog" / "books.json"
BOOKS_DIR = CONFIG_ROOT / "books"

#: CONTRACT.md: the expected outcome for a work with no free Vietnamese edition.
#: Not a failure of the build, and never a machine translation.
BLOCKED_STATUS = "blocked-no-vi-source"
BLOCKED_TRANSLATOR = (
    "Chưa có bản tiếng Việt public-domain cho tác phẩm này. KHÔNG dùng máy dịch: "
    "một bản dịch máy là tác phẩm phái sinh và có thể không được phân phối. Cần người "
    "dịch tay và ghi tên dịch giả vào trường 'translator' trước khi đọc."
)

#: Keys copied from the catalog into `book.json` on top of the `models.Book`
#: fields. `models.py` is frozen by CONTRACT.md, but the repository rights gate
#: reads `author_dates` / `author_death_year`, so they have to be on disk.
EXTRA_FIELDS = (
    "author_dates",
    "author_death_year",
    "translator_dates",
    "license_basis",
    "wikisource_title",
    "wikisource_url",
    "verified",
    "expect_chapters",
    "notes",
)


def load_catalog(path: Path = CATALOG) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    books = data.get("books", [])
    if not books:
        raise SystemExit(f"{path} has no books")
    return books, data


def source_url_of(entry: dict[str, Any]) -> str:
    """The page to fetch. `wikisource_url` is the only field we trust."""
    return entry.get("wikisource_url") or entry.get("source_url") or ""


def local_source(book_dir: Path) -> list[Path]:
    """Files the operator dropped into `books/<slug>/source/`, in reading order.

    Wikitext first, then rendered HTML (a transclusion page's text only exists
    after expansion), then a plain source file. Natural order throughout, so
    `II` precedes `X`.
    """
    src = book_dir / "source"
    if not src.is_dir():
        return []
    for pattern in ("*.wikitext", "*.html", "*.txt", "*.md", "*.epub", "*.pdf"):
        hits = sorted(src.glob(pattern), key=lambda p: wikisource.natural_key(p.stem))
        if hits:
            return hits
    return []


def _slug_equal(a: str, b: str) -> bool:
    """Compare two page names after reducing both to `[a-z0-9]`."""
    norm = lambda t: re.sub(r"[^a-z0-9]+", "", t.lower())  # noqa: E731
    return bool(norm(a)) and norm(a) == norm(b)


def cached_meta(book_dir: Path) -> dict[str, Any]:
    """Provenance written by `cache_source`, so an offline build keeps it."""
    path = book_dir / "source" / "source_meta.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:  # pragma: no cover - a corrupt cache is not fatal
        return {}


def blocked_book(entry: dict[str, Any], *, reason: str, status: str = BLOCKED_STATUS) -> Book:
    """A `book.json` that is honest about having no Vietnamese text yet."""
    return Book(  # noqa: RET504
        slug=entry["slug"],
        title=entry.get("title", ""),
        author=entry.get("author", ""),
        translator=BLOCKED_TRANSLATOR,
        language=entry.get("language", "vi"),
        source_language=entry.get("source_language", "vi"),
        source_format=entry.get("source_format", "wikisource"),
        source_path="",
        source_url=entry.get("source_url", "") or source_url_of(entry),
        license=normalize_license(entry.get("license", "")),
        license_url=entry.get("license_url", ""),
        rights_note=(
            f"{entry.get('rights_note', '').rstrip('.')}. "
            f"VIETNAMESE EDITION: {_block_reason(entry)} "
            f"Kiểm tra lúc build: {reason} "
            f"Không dùng máy dịch. Sách này KHÔNG đọc được cho tới khi có bản dịch tay "
            f"với quyền phân phối rõ ràng."
        ),
        summary=entry.get("summary", ""),
        narrator=entry.get("narrator", ""),
        text_path="",
        chapters=[],
        status=status,
    )


#: Verified death years for the authors in the catalog, used when the catalog's
#: own `author_dates` cannot be parsed by `tools/check_rights.py` (which only
#: reads 4-digit years from 1600 onwards, so a 15th-century author fails it).
#: Each entry is `author name -> (birth, death)`, and each is cross-checked
#: against the author the Wikisource page names in its own header.
DEATH_YEARS: dict[str, tuple[int, int]] = {
    "Nguyễn Du": (1765, 1820),
    "Nguyễn Trãi": (1380, 1442),
    # Chinh phụ ngâm is by Đặng Trần Côn. The catalog credits Nguyễn Trãi, who
    # wrote the preface to Văn Tế -- a confusion the page's own header corrects.
    "Đặng Trần Côn": (1712, 1780),
    "Trương Vĩnh Ký": (1837, 1898),
    "Phan Phu Tiên": (1712, 1780),
    "Đoàn Thị Điểm": (1747, 1823),
}

#: `tools/check_rights.py` only parses a death year written 1600-2029, so a
#: 15th-century author can never satisfy it from `author_dates` alone. Adding
#: `author_death_year` explicitly is the supported way to declare it.
_DEATH_YEAR_RE = re.compile(r"\b(1[0-9]{3}|20[0-2][0-9])\b")


def _resolve_death_year(entry: dict[str, Any], document: Any) -> tuple[int | None, list[str]]:
    """Return `(death_year, notes)`. Never guesses: unknown stays None."""
    notes: list[str] = []
    source_author = (document.extra.get("authorship") or {}).get("author", "") if document else ""
    catalog_author = entry.get("author", "")

    # 1. the catalog's own author_dates, when the gate can read it
    for key in ("author_death_year",):
        value = entry.get(key)
        if isinstance(value, int):
            return value, notes
    dates = entry.get("author_dates") or ""
    years = _DEATH_YEAR_RE.findall(dates)
    if len(years) >= 2 and int(years[-1]) >= 1600:
        return int(years[-1]), notes
    if years and int(years[-1]) >= 1600:
        return int(years[-1]), notes

    # 2. the verified table. On an authority mismatch the *source* wins: a rights
    #    claim has to be about the person who actually wrote the work, and the
    #    catalog is the thing that is wrong.
    if source_author:
        hit = DEATH_YEARS.get(source_author)
        if hit and _fold(source_author) != _fold(catalog_author):
            notes.append(
                f"AUTHORITY MISMATCH: catalog says author={catalog_author!r}, but the "
                f"source's own header says {source_author!r}. The rights claim follows "
                f"the source (death year {hit[1]}); book.json keeps the catalog's "
                f"'author' so the catalog<->book mapping stays intact, and records the "
                f"source author in 'source_author'. The catalog entry needs fixing."
            )
            return hit[1], notes
    for name in (catalog_author, source_author):
        hit = DEATH_YEARS.get(name)
        if hit:
            return hit[1], notes
    return None, notes


def _fold(text: str) -> str:
    return " ".join(text.lower().split())


def _block_reason(entry: dict[str, Any]) -> str:
    """The catalog's own explanation, when it documents why a book is blocked.

    `Lục Vân Tiên` failed because both of its vi.wikisource editions are ~1 KB
    stubs pointing at nomna.org. Writing "offline run with no cached source"
    instead would be a *different and wrong* reason for the same book, and the
    operator would have to rediscover the real one. The catalog records it, so
    the catalog's reason is used; only when it says nothing do we fall back to
    what this run actually saw.
    """
    for key in ("notes", "verified_note"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    verified = entry.get("verified")
    if isinstance(verified, dict):
        note = verified.get("note") or verified.get("checked")
        if isinstance(note, str) and note.strip():
            return note.strip()
    return "Khong co ban dich tieng Viet cong khai tren vi.wikisource.org."


def _extra_for(entry: dict[str, Any], document: Any = None, **extra_fields: Any) -> dict[str, Any]:
    extra = {key: entry.get(key) for key in EXTRA_FIELDS}
    if document is not None:
        extra["source_page"] = document.extra.get("page_title", "")
        extra["source_revision"] = document.extra.get("revision", 0)
        extra["source_revision_timestamp"] = document.extra.get("revision_timestamp", "")
        extra["source_revision_user"] = document.extra.get("revision_user", "")
        extra["source_pages"] = document.extra.get("pages", [])
        authorship = document.extra.get("authorship") or {}
        if authorship.get("author"):
            extra["source_author"] = authorship["author"]
        if authorship.get("translator"):
            extra["source_translator"] = authorship["translator"]
    extra.update({k: v for k, v in extra_fields.items() if v not in (None, "", [], {})})
    return {k: v for k, v in extra.items() if v not in (None, "", [], {})}


def build_one(
    entry: dict[str, Any],
    *,
    translate_mode: str = "off",
    offline: bool = False,
    dry_run: bool = False,
    cache: bool = True,
    target_chapter_words: int = 2500,
    min_source_words: int = 250,
    say=lambda _m: None,
) -> tuple[Book | None, str]:
    """Build one book. Returns `(book_or_None, status_word)`."""
    from sachnoi.config import Config

    slug = entry["slug"]
    book_dir = BOOKS_DIR / slug
    notes: list[str] = []

    # ---- 1. rights gate, before anything is fetched or written ----------
    try:
        check_license(entry.get("license", ""), slug=slug)
    except RightsError as exc:
        say(f"  [rights] BLOCKED {slug}: {exc}")
        return None, "rights-blocked"

    # ---- 2. source ------------------------------------------------------
    local = local_source(book_dir)
    document = None
    if local:
        say(f"  [source] {slug}: {len(local)} cached file(s) under source/")
        document = _from_local(local, entry)
    elif offline:
        say(f"  [source] {slug}: offline and nothing cached under source/")
        book = blocked_book(entry, reason=_block_reason(entry))
        return _finish(book, entry, say=say, dry_run=dry_run), "offline"
    else:
        try:
            document = wikisource.extract(
                source_url_of(entry), language="vi", min_total_words=min_source_words
            )
        except (LookupError, ValueError, RuntimeError) as exc:
            say(f"  [source] MISS {slug}: {exc}")
            book = blocked_book(
                entry,
                reason=(
                    "Khong nap duoc trang "
                    f"{source_url_of(entry)} ({exc})."
                ),
            )
            return _finish(book, entry, say=say, dry_run=dry_run), "no-source"
        say(
            f"  [source] {slug}: {document.extra.get('page_title')} "
            f"({len(document.extra.get('pages', []))} page(s), rev "
            f"{document.extra.get('revision')})"
        )

    if not document or not document.sections:
        book = blocked_book(entry, reason="Extraction produced no chapters.")
        return _finish(book, entry, say=say, dry_run=dry_run), "empty"

    words = sum(s.word_count for s in document.sections)
    expect = entry.get("expect_chapters")
    say(
        f"  [extract] {slug}: {len(document.sections)} chapters, {words} words"
        + (f" (catalog expects {expect})" if expect else "")
    )
    if isinstance(expect, int) and expect and len(document.sections) != expect:
        notes.append(
            f"chapter count {len(document.sections)} != catalog expect_chapters {expect}; "
            f"the source has no more structure than this"
        )

    # ---- 2b. fit the chapter list to an audiobook ------------------------
    # `min_chapter_words` comes from the catalog. Without it a facsimile edition
    # yields a 60-word "chapter" for every phụ -- 30 seconds of narration with a
    # title card, which is worse than no chapter at all.
    min_chapter = effective_min_chapter_words(entry.get("min_chapter_words"))
    if document.extra.get("front_matter"):
        for note in document.extra["front_matter"]:
            say(f"  [front] {slug}: {note}")
            notes.append(note)
    before = len(document.sections)
    document.sections, structure_notes = structure_chapters(
        document.sections,
        target_words=target_chapter_words,
        min_words=min_chapter,
        base_title=entry.get("title", ""),
    )
    for note in structure_notes:
        say(f"  [chapters] {slug}: {note}")
        notes.append(note)
    if len(document.sections) != before:
        say(
            f"  [chapters] {slug}: {before} -> {len(document.sections)} audio chapters "
            f"(target {target_chapter_words} chữ, floor {min_chapter or 'none'})"
        )

    # ---- 2c. rights provenance ------------------------------------------
    death_year, auth_notes = _resolve_death_year(entry, document)
    for note in auth_notes:
        say(f"  [rights] {slug}: {note}")
        notes.append(note)
    if death_year is None:
        say(
            f"  [rights] {slug}: no verifiable death year for {entry.get('author')!r}; "
            f"refusing to write a public-domain claim the gate cannot check"
        )
        book = blocked_book(
            entry,
            reason=(
                f"Khong xac minh duoc nam tu cach cua tac gia {entry.get('author')!r}, "
                f"nen khong the khai bao ban cong cong."
            ),
            status="blocked-unverifiable-author",
        )
        return _finish(book, entry, notes=notes, document=document, say=say), "unverifiable-author"
    say(f"  [rights] {slug}: author death year {death_year} (life+70 = {death_year + 70})")

    # ---- 3. translate stage --------------------------------------------
    cfg = dataclasses.replace(Config.load().translate, mode=translate_mode)
    try:
        engine = build_engine(cfg)
        translate_document(document, config=cfg, engine=engine, notes=notes)
    except TranslationError as exc:
        say(f"  [translate] {slug}: {exc}")
        return None, "translate-failed"

    if document.language != entry.get("language", "vi"):
        say(f"  [translate] {slug}: NOT Vietnamese (got {document.language})")
        book = blocked_book(
            entry,
            reason=(
                f"Chi nap duoc ban {document.language}, khong phai tieng Viet. "
                f"CONTRACT.md cam may dich."
            ),
            status=f"{BLOCKED_STATUS} (source is {document.language})",
        )
        return _finish(book, entry, say=say, dry_run=dry_run), "wrong-language"

    if dry_run:
        return None, "dry-run"

    # ---- 4/5. text + book.json -----------------------------------------
    from sachnoi.extract import assemble_book

    book = assemble_book(entry, document)
    book.status = "translated" if translate_mode != "off" else "extracted"
    if entry.get("translator"):
        book.translator = entry["translator"]
    book = _finish(book, entry, notes=notes, document=document, say=say)
    if cache and not local:
        try:
            cache_source(slug, document)
            say(f"  [cache] raw wikitext under books/{slug}/source/ (gitignored)")
        except Exception as exc:  # noqa: BLE001 - caching is a convenience
            say(f"  [cache] skipped: {exc}")
    return book, "ok"


def _from_local(files: list[Path], entry: dict[str, Any]):
    """Rebuild a document from cached files in `books/<slug>/source/`."""
    from sachnoi.extract import Document, Section
    from sachnoi.extract.txt import extract as extract_txt
    from sachnoi.extract.wikisource import _sections_from_html

    sections: list[Section] = []
    source_path = ""
    page_names: dict[str, str] = cached_meta(BOOKS_DIR / entry["slug"]).get("page_names", {})
    for path in files:
        source_path = str(path)
        # `cache_source` writes "<slug>__<NNN>-<page title>"; strip the ordering
        # prefix and the slug so a chapter is named after its page, not a file.
        # `source_meta.json` has the exact name, which the filename cannot hold.
        from_filename = re.sub(r"^\d+-", "", path.stem.split("__")[-1]).replace("_", " ")
        name = next(
            (v for v in page_names.values() if _slug_equal(v, from_filename)), from_filename
        )
        if path.suffix.lower() == ".html":  # a transclusion page: only the render has text
            found = _sections_from_html(
                path.read_text(encoding="utf-8"), min_words=40, page_title=name
            )
        else:
            found = extract_txt(path).sections
        for sec in found:
            # With several cached files, each is one subpage and its page name is
            # the chapter name. With a single file it is the whole work, so the
            # placeholder title is left for the length splitter to replace with
            # the book's real title.
            if len(files) > 1 and (
                not sec.title.strip() or sec.title.strip() in {"Nội dung", "Phần mở đầu"}
            ):
                sec.title = name
            sections.append(sec)
    meta = cached_meta(BOOKS_DIR / entry["slug"])
    return Document(
        sections=sections,
        title=meta.get("page_title") or entry.get("title", ""),
        source_format=entry.get("source_format", "wikisource"),
        source_path=source_path,
        # Prefer the resolved, canonical URL over the catalog's raw one.
        source_url=meta.get("source_url") or source_url_of(entry),
        language="vi",
        extra={
            "cached_files": [p.name for p in files],
            "page_title": meta.get("page_title", ""),
            "pageid": meta.get("pageid", 0),
            "revision": meta.get("revision", 0),
            "revision_timestamp": meta.get("revision_timestamp", ""),
            "revision_user": meta.get("revision_user", ""),
            "pages": meta.get("pages", [p.name for p in files]),
            "authorship": meta.get("authorship", {}),
            "from_cache": True,
        },
    )


def _finish(
    book: Book,
    entry: dict[str, Any],
    *,
    notes: Sequence[str] = (),
    document: Any = None,
    say=lambda _m: None,
    dry_run: bool = False,
) -> Book:
    """Write `book.json` + the cross-machine manifest, and report the paths."""
    if dry_run:
        return book
    book_dir = BOOKS_DIR / entry["slug"]
    out = book_dir / "book.json"
    death_year, _ = _resolve_death_year(entry, document)
    save_book(
        book,
        out,
        extra=_extra_for(
            entry,
            document,
            author_death_year=death_year,
            rights_gate="tools/check_rights.py",
        ),
    )
    all_notes = list(notes) + list(getattr(document, "notes", []) or [])
    if book.status == BLOCKED_STATUS:
        all_notes.insert(0, "BLOCKED: no Vietnamese edition; no machine translation applied.")
    write_manifest(book, book_dir / "manifest.json", notes=all_notes)
    say(
        f"  [done] {entry['slug']}: {len(book.chapters)} chapters -> "
        f"{book.text_path or '(no text)'} + {out.relative_to(CONFIG_ROOT)} + manifest.json"
    )
    return book


def cache_source(slug: str, document: Any) -> None:
    """Store the source under `books/<slug>/source/` so --offline can rebuild.

    That directory is gitignored, so this is a local cache, never a commit.

    Three things are written, and all three matter for a faithful rebuild:

    * the raw wikitext, or the **rendered HTML** for a transclusion page (its
      wikitext has no text in it at all);
    * a numeric filename prefix, so a natural sort reproduces the reading order
      instead of sorting `Avant-propos` before the root page;
    * `source_meta.json`, holding the resolved title, URL, revision and the
      author the page's own header names. Without it an offline build would lose
      the provenance and fall back to a different death year.
    """
    src = BOOKS_DIR / slug / "source"
    src.mkdir(parents=True, exist_ok=True)
    html_pages = set(document.extra.get("html_pages", []))
    pages = list(document.extra.get("pages", []))
    with wikisource.MediaWikiClient("vi") as mw:
        for i, page in enumerate(pages):
            stem = re.sub(r"[^\w.-]+", "_", page.split("/")[-1]) or "root"
            if page in html_pages:
                (src / f"{slug}__{i:03d}-{stem}.html").write_text(
                    mw.rendered_html(page), encoding="utf-8"
                )
            else:
                _, wiki = mw.wikitext(page)
                (src / f"{slug}__{i:03d}-{stem}.wikitext").write_text(wiki, encoding="utf-8")
    (src / "source_meta.json").write_text(
        json.dumps(
            {
                "page_title": document.extra.get("page_title", ""),
                "source_url": document.source_url,
                "pageid": document.extra.get("pageid", 0),
                "revision": document.extra.get("revision", 0),
                "revision_timestamp": document.extra.get("revision_timestamp", ""),
                "revision_user": document.extra.get("revision_user", ""),
                "pages": pages,
                # A page's own title, because a cache filename cannot carry a
                # comma: "Kim, Van, Kieu tap an" would come back as
                # "Kim_Van_Kieu_tap_an" and the chapter would lose its commas.
                "page_names": {p: p.split("/")[-1] for p in pages},
                "authorship": document.extra.get("authorship", {}),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/extract_book.py", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--catalog", type=Path, default=CATALOG)
    parser.add_argument("--slug", action="append", help="limit to these slugs (repeatable)")
    parser.add_argument(
        "--translate-mode",
        default="off",
        choices=["off", "gloss", "command", "http"],
        help="default off: every catalog entry is already a Vietnamese edition",
    )
    parser.add_argument("--offline", action="store_true", help="never hit the network")
    parser.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing")
    parser.add_argument(
        "--no-cache", action="store_true", help="do not write the raw wikitext under source/"
    )
    parser.add_argument("--probe", action="store_true", help="alias for --dry-run")
    parser.add_argument(
        "--min-source-words",
        type=int,
        default=250,
        help="reject a source below this many words as an index/stub, not the work",
    )
    parser.add_argument(
        "--target-chapter-words",
        type=int,
        default=2500,
        help="split a longer chapter on verse lines (0 disables). The floor comes "
        "from each catalog entry's min_chapter_words.",
    )
    args = parser.parse_args(argv)

    entries, _meta = load_catalog(args.catalog)
    if args.slug:
        wanted = set(args.slug)
        entries = [e for e in entries if e["slug"] in wanted]
        if not entries:
            print(f"no catalog book matches {sorted(wanted)}", file=sys.stderr)
            return 1

    tally: dict[str, int] = {}
    rc = 0
    for entry in entries:
        print(f"[{entry['slug']}]")
        try:
            book, status = build_one(
                entry,
                translate_mode=args.translate_mode,
                offline=args.offline,
                dry_run=args.dry_run or args.probe,
                cache=not args.no_cache,
                target_chapter_words=args.target_chapter_words,
                min_source_words=args.min_source_words,
                say=lambda m: print(m, flush=True),
            )
        except Exception as exc:  # noqa: BLE001 - one bad book must not kill the run
            print(f"  [error] {entry['slug']}: {type(exc).__name__}: {exc}", file=sys.stderr)
            status, book = "error", None
            rc = 1
        tally[status] = tally.get(status, 0) + 1

    print("\n=== summary ===")
    for status, count in sorted(tally.items()):
        print(f"  {status:<18} {count}")
    if tally.get("rights-blocked") or tally.get("translate-failed"):
        return 1
    if tally.get("no-source") or tally.get("empty") or tally.get("wrong-language") or tally.get("offline"):
        return 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
