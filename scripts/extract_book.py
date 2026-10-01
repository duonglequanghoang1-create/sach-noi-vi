#!/usr/bin/env python3
"""Build `books/<slug>/text/<slug>.md` and `books/<slug>/book.json` from the catalog.

    python3 scripts/extract_book.py --all
    python3 scripts/extract_book.py --slug alice-luc-tiec-vung-nguon-roi
    python3 scripts/extract_book.py --all --translate-mode gloss
    python3 scripts/extract_book.py --all --dry-run

What it does, in order:

1. rights gate -- refuses a book whose `license` is not public-domain or an
   explicitly redistributable Creative Commons licence, before any fetch;
2. fetch the source (Wikisource, or a local file under `books/<slug>/source/`);
3. run the translate stage (mode `off` by default -- the Wikisource text is
   already Vietnamese, and this project never machine-translates a public-domain
   book without an explicit operator decision);
4. write `text/<slug>.md` plus one `text/chNN.md` per chapter;
5. write `book.json` from `models.Book`, copying every provenance field
   verbatim from the catalog;
6. write a `Manifest` for the other machine.

Exit codes: 0 all books built, 1 a hard failure, 2 at least one book had no
usable source (the run still succeeded for the others).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
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
    normalize_license,
    write_manifest,
)
from sachnoi.extract import wikisource  # noqa: E402
from sachnoi.models import Book  # noqa: E402
from sachnoi.translate import TranslationError, build_engine, translate_document  # noqa: E402
from sachnoi.translate.gloss import normalize_title  # noqa: E402

CATALOG = CONFIG_ROOT / "catalog" / "books.json"
BOOKS_DIR = CONFIG_ROOT / "books"

#: Written into `book.json` when a book has no usable Vietnamese source. The
#: brief is explicit: do NOT machine-translate in this case.
NO_VI_SOURCE_STATUS = "blocked-no-vi-source"
NO_VI_SOURCE_TRANSLATOR = (
    "Khong tim thay ban dich tieng Viet public-domain tren vi.wikisource.org. "
    "KHONG dung may dich: ban dich may la tac pham phai sinh va co the khong "
    "duoc phan phoi. Can nguoi dich tay va ghi ten dich gia tai day truoc khi doc."
)


def load_catalog(path: Path = CATALOG) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    books = data.get("books", [])
    if not books:
        raise SystemExit(f"{path} has no books")
    return books


def pick(entry: dict[str, Any], slugs: Sequence[str]) -> list[dict[str, Any]]:
    if not slugs:
        return entry
    return [e for e in entry if e["slug"] in set(slugs)]


def local_source(book_dir: Path) -> Path | None:
    """A hand-supplied file in `books/<slug>/source/`, if the operator added one.

    Checked before the network so an offline build is always possible.
    """
    src = book_dir / "source"
    if not src.is_dir():
        return None
    for pattern in ("*.txt", "*.md", "*.wikitext", "*.epub", "*.pdf"):
        hits = sorted(src.glob(pattern))
        if hits:
            return max(hits, key=lambda p: p.stat().st_size)
    return None


def blocked_book(
    entry: dict[str, Any],
    *,
    reason: str,
    status: str = NO_VI_SOURCE_STATUS,
    translator: str = "",
    extra: dict[str, Any] | None = None,
) -> Book:
    """A `book.json` that is honest about having no Vietnamese text yet.

    Still written, and still carries the full rights provenance from the
    catalog, so agent B can see exactly which book is missing what.
    """
    license_id = normalize_license(entry.get("license", ""))
    rights = entry.get("rights_note", "")
    book = Book(
        slug=entry["slug"],
        title=entry.get("title", ""),
        author=entry.get("author", ""),
        translator=translator or NO_VI_SOURCE_TRANSLATOR,
        language=entry.get("language", "vi"),
        source_language=entry.get("source_language", "en"),
        source_format="wikisource",
        source_path="",
        source_url=entry.get("source_url", ""),
        license=license_id,
        license_url=entry.get("license_url", ""),
        rights_note=(
            f"{rights} | VIETNAMESE EDITION: {reason} No machine translation was "
            f"applied. This book is NOT narratable until a human translation with "
            f"clear redistribution rights is added."
        ).strip(),
        narrator=entry.get("narrator", ""),
        text_path="",
        chapters=[],
        status=status,
    )
    return book


def build_one(
    entry: dict[str, Any],
    *,
    translate_mode: str,
    offline: bool = False,
    dry_run: bool = False,
    fallback_language: str = "",
    report: list[str] | None = None,
) -> tuple[Book | None, str]:
    """Build one book. Returns `(book_or_None, status_word)`."""
    slug = entry["slug"]
    book_dir = BOOKS_DIR / slug
    notes: list[str] = []
    say = report.append if report is not None else (lambda _m: None)

    # ---- 1. rights gate, before anything is fetched or written ----------
    try:
        check_license(entry.get("license", ""), slug=slug)
    except RightsError as exc:
        say(f"  [rights] BLOCKED {slug}: {exc}")
        return None, "rights-blocked"

    # ---- 2. source ------------------------------------------------------
    local = local_source(book_dir)
    if local is not None:
        say(f"  [source] {slug}: local file {local.relative_to(CONFIG_ROOT)}")
        from sachnoi.extract import extract_file

        document = extract_file(
            local,
            license_id=entry.get("license", ""),
            source_url=entry.get("source_url", ""),
        )
    elif offline:
        say(f"  [source] {slug}: offline, no local file")
        book = blocked_book(entry, reason="Offline run with no local source file.")
        if not dry_run:
            _finish(book, entry, say=say)
        return book, "offline"
    else:
        vi_url = entry.get("vi_source_url") or ""
        try:
            document = wikisource.extract(
                vi_url or entry.get("source_url", ""),
                translator=entry.get("translator", ""),
                language="vi",
                fallback_language=fallback_language,
            )
            say(
                f"  [source] {slug}: {document.extra.get('page_title')} "
                f"({len(document.sections)} chapters, "
                f"{sum(s.word_count for s in document.sections)} words)"
            )
        except (LookupError, ValueError, RuntimeError) as exc:
            say(f"  [source] MISS {slug}: {exc}")
            book = blocked_book(
                entry,
                reason=(
                    "Khong co ban dich tieng Viet public-domain tren "
                    f"vi.wikisource.org: khong tai duoc trang "
                    f"{vi_url or entry.get('source_url', '')} ({exc})."
                ),
            )
            if not dry_run:
                _finish(book, entry, say=say)
            return book, "no-source"

    if not document.sections:
        book = blocked_book(entry, reason="Extraction produced no chapters.")
        if not dry_run:
            _finish(book, entry, say=say)
        return book, "empty"

    # ---- 3. translate stage --------------------------------------------
    from sachnoi.config import Config

    cfg = Config.load()
    # TranslateConfig is a frozen dataclass, so swap the mode with a copy rather
    # than mutating the shared config object.
    translate_cfg = _with_mode(cfg.translate, translate_mode)
    try:
        engine = build_engine(translate_cfg)
    except TranslationError as exc:
        say(f"  [mode] {slug}: {exc}")
        return None, "bad-mode"
    try:
        translate_document(document, config=translate_cfg, engine=engine, notes=notes)
    except TranslationError as exc:
        say(f"  [translate] {slug}: {exc}")
        return None, "translate-failed"

    if document.language != "vi":
        # Only reachable with an explicit --fallback-language. Say so loudly
        # rather than shipping English text in a book marked `language: vi`.
        say(f"  [translate] {slug}: NOT Vietnamese (got {document.language})")
        book = blocked_book(
            entry,
            reason=(
                f"Khong co ban tieng Viet public-domain; chi tai duoc ban "
                f"{document.language} tu {document.source_url}."
            ),
            status=f"{NO_VI_SOURCE_STATUS} (fallback {document.language})",
            translator=(
                f"Ban goc {document.language} cua {entry.get('author', '')}. "
                f"CHUA CO BAN TIENG VIET -- can nguoi dich tay."
            ),
        )
        if not dry_run:
            _finish(book, entry, say=say)
        return book, "fallback-non-vi"

    # ---- 4/5. text + book.json -----------------------------------------
    from sachnoi.extract import assemble_book

    if dry_run:
        words = sum(count_words(s.text) for s in document.sections)
        say(f"  [dry-run] {slug}: {len(document.sections)} chapters, {words} words")
        return None, "dry-run"

    book = assemble_book(entry, document)
    book.status = "translated" if translate_mode != "off" else "extracted"
    return _finish(book, entry, notes=notes, document=document, say=say), "ok"


def _finish(
    book: Book,
    entry: dict[str, Any],
    *,
    notes: Sequence[str],
    document: Any = None,
    say=lambda _m: None,
) -> Book:
    """Write `book.json` + the cross-machine manifest, and report the paths."""
    book_dir = BOOKS_DIR / entry["slug"]
    out = book_dir / "book.json"
    book.save(out)
    extra_notes = list(notes) + list(getattr(document, "notes", []) or [])
    write_manifest(book, book_dir / "manifest.json", notes=extra_notes)
    say(
        f"  [done] {entry['slug']}: {len(book.chapters)} chapters -> "
        f"{book.text_path} + {out.relative_to(CONFIG_ROOT)} + manifest.json"
    )
    return book


def _with_mode(base, mode: str):
    import dataclasses

    return dataclasses.replace(base, mode=mode)


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
        help="default off: the Wikisource source is already Vietnamese",
    )
    parser.add_argument(
        "--fallback-language",
        default="",
        help="try this Wikisource language if no Vietnamese edition exists. "
        "The result is marked NOT narratable as Vietnamese.",
    )
    parser.add_argument("--offline", action="store_true", help="never hit the network")
    parser.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing")
    args = parser.parse_args(argv)

    entries = pick(load_catalog(args.catalog), args.slug or [])
    if not entries:
        print("no matching books", file=sys.stderr)
        return 1

    report: list[str] = []
    tally: dict[str, int] = {}
    books: list[Book] = []
    for entry in entries:
        print(f"[{entry['slug']}]")
        try:
            book, status = build_one(
                entry,
                translate_mode=args.translate_mode,
                offline=args.offline,
                dry_run=args.dry_run,
                fallback_language=args.fallback_language,
                report=report,
            )
        except Exception as exc:  # noqa: BLE001 - one bad book must not kill the run
            print(f"  [error] {entry['slug']}: {type(exc).__name__}: {exc}", file=sys.stderr)
            status = "error"
            book = None
        tally[status] = tally.get(status, 0) + 1
        if book is not None:
            books.append(book)
        for line in report:
            print(line)
        report.clear()

    print("\n=== summary ===")
    for status, count in sorted(tally.items()):
        print(f"  {status:<18} {count}")
    if not args.dry_run and books:
        print(f"  wrote {len(books)} book.json file(s) under {BOOKS_DIR}")
    if "rights-blocked" in tally or "error" in tally or "bad-mode" in tally:
        return 1
    return 2 if tally.get("no-source") or tally.get("empty") or tally.get("fallback-non-vi") else 0


if __name__ == "__main__":
    sys.exit(main())
