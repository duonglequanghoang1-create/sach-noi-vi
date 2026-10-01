#!/usr/bin/env python3
"""Hard rights gate for the repository.

Fails the build if any committed book is not demonstrably public domain or
explicitly redistributable. Run in CI on every push and available locally
before committing.

    python3 tools/check_rights.py [--json]

Exit codes: 0 = clean, 1 = at least one book violates the policy.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ALLOWED = {"public-domain", "CC0", "CC-BY-4.0", "CC-BY-SA-4.0"}

REPO_ROOT = Path(__file__).resolve().parents[1]

# Life + 70 is the shortest copyright term in play (Vietnam and the US). For a
# work to be public domain under it, the author must have died at least 70
# years ago. This gate is deliberately stricter than strictly necessary: it
# refuses anything newer than 1955, because a book claiming public domain with
# an author who died after 1955 is far more likely to be a factual mistake
# than a real exception.
LATEST_SAFE_DEATH_YEAR = 1955

# Works the operator asked us not to touch. Matched case-insensitively against
# slug, title, author and source_url. This is a second, independent layer: the
# license field alone has already been proven capable of asserting anything.
DENIED_AUTHOR_PATTERNS = (
    "lewis carroll",
    "arthur conan doyle",
    "jules verne",
    "robert louis stevenson",
    "charles dickens",
    "h. g. wells",
    "h.g. wells",
    "herbert schildt",
    "mark allen weiss",
    "barbara liscov",
    "clifford s. clifton",
    "thomas h. cormen",
    "leislie lamport",
    "ronald l. rivest",
    "ngo tat to",
    "ngô tất tố",
    "bruce eckels",
    "rudyard kipling",
    "mary shelley",
)

REQUIRED_FIELDS = (
    "slug",
    "title",
    "author",
    "license",
    "license_url",
    "rights_note",
)


def _death_year(book: dict, blob: str) -> int | None:
    """Extract the author's death year from whatever the book declares.

    Deliberately looks in more than one place, because the failure mode this
    gate exists to catch is a plausible-looking year in prose that happens to
    be wrong. If we cannot find a death year at all, we say so rather than
    assuming the best.
    """
    import re

    for key in ("author_death_year", "author_dates"):
        value = book.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            match = re.search(r"(1[6-9]\d\d|20[0-2]\d)\s*[–\-—]", value)
            if match:
                return int(match.group(1))

    # Fall back to prose: "d. 1945", "died 1945", "(1894-1945)".
    match = re.search(r"(?:d\.?|died|mất)\s*(1[6-9]\d\d|20[0-2]\d)", blob, re.I)
    if match:
        return int(match.group(1))
    match = re.search(r"\(1[6-9]\d\d\s*[–\-—]\s*(1[6-9]\d\d|20[0-2]\d)\)", blob)
    if match:
        return int(match.group(1))
    return None


def check_one(meta: Path) -> list[str]:
    problems: list[str] = []
    rel = meta.relative_to(REPO_ROOT)

    try:
        raw = meta.read_text(encoding="utf-8")
        book = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [f"{rel}: not valid JSON ({exc})"]

    for field in REQUIRED_FIELDS:
        if not str(book.get(field, "")).strip():
            problems.append(f"{rel}: missing required field '{field}'")

    lic = str(book.get("license", "")).strip()
    if lic and lic not in ALLOWED:
        problems.append(
            f"{rel}: licence '{lic}' is not allowed; "
            f"expected one of {sorted(ALLOWED)}"
        )

    # Layer 1: denied-author denylist, independent of the declared licence.
    haystack = " ".join(
        str(book.get(k, ""))
        for k in ("slug", "title", "author", "translator", "source_url")
    ).lower()
    for pattern in DENIED_AUTHOR_PATTERNS:
        if pattern in haystack:
            problems.append(
                f"{rel}: matches denied-author list ('{pattern}'); "
                f"this work is not approved for this repository"
            )

    # Layer 2: a public-domain claim must be arithmetically supportable.
    if lic == "public-domain":
        year = _death_year(book, raw)
        if year is None:
            problems.append(
                f"{rel}: claims public-domain but declares no author death "
                f"year; add 'author_death_year' so the claim is checkable"
            )
        elif year > LATEST_SAFE_DEATH_YEAR:
            problems.append(
                f"{rel}: claims public-domain but the author died in {year}. "
                f"life+70 does not expire until {year + 70}, so this work is "
                f"still under copyright"
            )

    # A book.json with a text/ sibling is the shape that actually leaks
    # copyrighted prose into git, so check for it explicitly.
    text_dir = meta.parent / "text"
    if text_dir.is_dir():
        md_files = sorted(text_dir.glob("*.md"))
        total = sum(f.stat().st_size for f in md_files)
        if lic not in ALLOWED and total > 0:
            problems.append(
                f"{rel}: {total} bytes of prose in text/ but licence is '{lic}'"
            )
        if not md_files:
            problems.append(f"{rel}: text/ exists but contains no .md")

    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    metas = sorted(REPO_ROOT.glob("books/*/book.json"))
    problems: list[str] = []
    for meta in metas:
        problems.extend(check_one(meta))

    report = {
        "books_scanned": len(metas),
        "allowed_licenses": sorted(ALLOWED),
        "problems": problems,
        "ok": not problems,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"scanned {len(metas)} book(s) under books/")
        if problems:
            print("\nRIGHTS VIOLATIONS:")
            for p in problems:
                print(f"  - {p}")
        else:
            print("all books clear")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())