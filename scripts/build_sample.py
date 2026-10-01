#!/usr/bin/env python3
"""Build a real Vietnamese public-domain sample book, outside the catalog.

    python3 scripts/build_sample.py
    python3 scripts/build_sample.py --offline      # reuse books/*/source/
    python3 scripts/extract_book.py                # the 8 catalog books

Why this exists
---------------
`catalog/books.json` lists eight English classics with a `vi_source_url` each.
None of those Vietnamese editions actually exists on vi.wikisource.org (see
docs/sources.md), and the brief forbids machine-translating them. That leaves
the pipeline with nothing to run end to end.

So this script builds **one** book that is unambiguously fine: a Vietnamese
original whose author died more than 70 years ago, so the work is public domain
in Vietnam and in the US/UK, and the text comes straight from vi.wikisource.org
with no translation at all.

The result lives at `books/vi-<author>/`, is **not** in `catalog/books.json`, and
carries a `summary` saying so, so agent B can use it to verify the audio and
packaging stages on real prose while the eight catalog books stay honestly
blocked.

`catalog/books.json` is a frozen file owned by the other side, so this script
never writes to it and never reads the catalog at all.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from sachnoi.config import REPO_ROOT as CONFIG_ROOT  # noqa: E402
from sachnoi.extract import (  # noqa: E402
    RightsError,
    check_license,
    write_manifest,
)
from sachnoi.extract import wikisource  # noqa: E402
from sachnoi.extract.wikisource import title_from_url  # noqa: E402
from sachnoi.models import Book  # noqa: E402
from sachnoi.translate import build_engine, translate_document  # noqa: E402
from sachnoi.translate.gloss import normalize_title  # noqa: E402

BOOKS_DIR = CONFIG_ROOT / "books"

#: Public-domain Vietnamese originals on vi.wikisource.org. Every entry states
#: why it is public domain; the build refuses anything that cannot.
SAMPLES: list[dict[str, Any]] = [
    {
        "slug": "vi-tat-den",
        "title": "Tắt đèn",
        "author": "Ngô Tất Tố",
        "vi_source_url": "https://vi.wikisource.org/wiki/T%E1%BA%AFt_%C4%91%C3%A8n",
        "license": "public-domain",
        "license_url": "https://creativecommons.org/publicdomain/mark/1.0/",
        "rights_note": (
            "Ngô Tất Tố (1894-1945), died 1945. Vietnamese original, not a "
            "translation. Public domain in Vietnam (70 years post-mortem) and in "
            "the US/UK. Text taken verbatim from vi.wikisource.org with no "
            "machine translation."
        ),
        "narrator": "VN-01",
    },
]


def build(
    sample: dict[str, Any],
    *,
    translate_mode: str = "off",
    offline: bool = False,
    dry_run: bool = False,
) -> tuple[Book | None, str]:
    slug = sample["slug"]
    book_dir = BOOKS_DIR / slug
    notes: list[str] = [
        "SAMPLE FIXTURE: not listed in catalog/books.json. Vietnamese original, "
        "public domain, no translation applied. Safe to narrate.",
    ]
    try:
        check_license(sample.get("license", ""), slug=slug)
    except RightsError as exc:
        print(f"  [rights] BLOCKED {slug}: {exc}")
        return None, "rights-blocked"

    local_dir = book_dir / "source"
    local_files: list[Path] = []
    if local_dir.is_dir():
        for pattern in ("*.wikitext", "*.txt", "*.md"):
            hits = sorted(local_dir.glob(pattern), key=lambda p: wikisource.natural_key(p.stem))
            if hits:
                local_files = hits
                break

    if local_files:
        # A work can be cached as several files (one per Wikisource subpage).
        # Read them in natural order so `II` precedes `III`, not `X`.
        print(f"  [source] {slug}: {len(local_files)} cached file(s) under source/")
        from sachnoi.extract import Section
        from sachnoi.extract.txt import extract as extract_txt

        sections: list[Section] = []
        source_path = ""
        for path in local_files:
            found = extract_txt(path)
            source_path = str(path)
            for sec in found.sections:
                # A single subpage has no heading of its own; its filename is
                # the chapter name (`I`, `II`, `Chương 4`).
                if not sec.title.strip() or sec.title.strip() in {"Nội dung", "Phần mở đầu"}:
                    sec.title = path.stem.split("__")[-1]
                sections.append(sec)
        from sachnoi.extract import Document

        document = Document(
            sections=sections,
            title=sample["title"],
            source_format="txt",
            source_path=source_path,
            source_url=sample["vi_source_url"],
            language="vi",
            extra={"cached_files": [p.name for p in local_files]},
        )
    elif offline:
        print(f"  [source] {slug}: offline and no cached file under source/")
        return None, "offline"
    else:
        print(f"  [source] {slug}: {sample['vi_source_url']}")
        document = wikisource.extract(sample["vi_source_url"], language="vi")

    if not local_files:
        print(
            f"  [extract] {len(document.sections)} chapters, "
            f"{sum(s.word_count for s in document.sections)} words from "
            f"{document.extra.get('page_title')!r} (rev {document.extra.get('revision')})"
        )
    else:
        print(
            f"  [extract] {len(document.sections)} chapters, "
            f"{sum(s.word_count for s in document.sections)} words from cache"
        )
    if not document.sections:
        return None, "empty"

    import dataclasses

    from sachnoi.config import Config

    cfg = dataclasses.replace(Config.load().translate, mode=translate_mode)
    translate_document(document, config=cfg, engine=build_engine(cfg), notes=notes)

    if dry_run:
        return None, "dry-run"

    from sachnoi.extract import assemble_book

    entry = {
        "slug": slug,
        "title": sample["title"],
        "author": sample["author"],
        "translator": "",  # a Vietnamese original: nobody translated it
        "language": "vi",
        "source_language": "vi",
        "narrator": sample.get("narrator", ""),
        "license": sample["license"],
        "license_url": sample["license_url"],
        "rights_note": sample["rights_note"],
        "source_url": document.source_url or sample["vi_source_url"],
    }
    book = assemble_book(entry, document)
    book.source_language = "vi"
    book.source_url = document.source_url or sample["vi_source_url"]
    book.status = "extracted"
    book.summary = (
        "BẢN MẪU / SAMPLE FIXTURE — không nằm trong catalog/books.json. Tác phẩm "
        "Việt Nam gốc của Ngô Tất Tố, public domain, lấy nguyên văn từ "
        "vi.wikisource.org, KHÔNG máy dịch. Dùng để kiểm tra stage audio/package."
    )
    book.save(book_dir / "book.json")
    write_manifest(book, book_dir / "manifest.json", notes=notes)

    # Keep a copy of the raw wikitext so the build is reproducible offline.
    # `books/*/source/` is gitignored, so this is a local cache, not a commit.
    if not local_files:
        local_dir.mkdir(parents=True, exist_ok=True)
        try:
            with wikisource.MediaWikiClient("vi") as mw:
                root = document.extra.get("page_title", "")
                if root:
                    _, wiki = mw.wikitext(root)
                    (local_dir / f"{slug}.wikitext").write_text(wiki, encoding="utf-8")
                    for page in document.extra.get("pages", [])[1:]:
                        _, sub = mw.wikitext(page)
                        (local_dir / f"{slug}__{page.split('/')[-1]}.wikitext").write_text(
                            sub, encoding="utf-8"
                        )
            print(f"  [cache] wrote raw wikitext under {local_dir.relative_to(CONFIG_ROOT)}/")
        except Exception as exc:  # noqa: BLE001 - caching is a convenience, not a requirement
            print(f"  [cache] skipped: {exc}")

    print(
        f"  [done] {slug}: {len(book.chapters)} chapters -> {book.text_path} + "
        f"books/{slug}/book.json + manifest.json"
    )
    return book, "ok"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/build_sample.py", description=__doc__.splitlines()[0])
    parser.add_argument("--slug", action="append", help="limit to these samples (repeatable)")
    parser.add_argument("--translate-mode", default="off", choices=["off", "gloss", "command", "http"])
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    samples = [s for s in SAMPLES if not args.slug or s["slug"] in set(args.slug)]
    if not samples:
        print("no matching samples", file=sys.stderr)
        return 1
    tally: dict[str, int] = {}
    for sample in samples:
        print(f"[{sample['slug']}]")
        try:
            _, status = build(
                sample,
                translate_mode=args.translate_mode,
                offline=args.offline,
                dry_run=args.dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  [error] {type(exc).__name__}: {exc}", file=sys.stderr)
            status = "error"
        tally[status] = tally.get(status, 0) + 1
    print("\n=== summary ===")
    for status, count in sorted(tally.items()):
        print(f"  {status:<18} {count}")
    return 0 if tally.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
