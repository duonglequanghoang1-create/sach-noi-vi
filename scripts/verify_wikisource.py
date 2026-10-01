"""Manual validation: run the Wikisource backend against a real public-domain
Vietnamese page, to prove the markup stripping works outside the unit tests.

Not part of the test suite (it needs the network). Run:
    python3 scripts/verify_wikisource.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from sachnoi.extract import render_chapter_markdown  # noqa: E402
from sachnoi.extract import wikisource  # noqa: E402
from sachnoi.translate import translate_document, build_engine  # noqa: E402
from sachnoi.translate.gloss import normalize_script  # noqa: E402

# Nguyễn Bính (1894-1945): the author has been dead for over 75 years, so the
# work is public domain in Vietnam (70 years post-mortem) and in the US/UK.
PD_VIETNAMESE_PAGES = [
    ("Tắt đèn", "https://vi.wikisource.org/wiki/T%E1%BA%AFt_%C4%91%C3%A8n"),
    ("Ba ngày luân lạc/Chương 4", "https://vi.wikisource.org/wiki/Ba_ng%C3%A0y_lu%C3%A2n_l%E1%BA%A1c/Ch%C6%B0%C6%A1ng_4"),
]


def main() -> int:
    rc = 0
    for label, url in PD_VIETNAMESE_PAGES:
        print(f"\n{'=' * 70}\n{label}\n{url}")
        try:
            doc = wikisource.extract(url, language="vi")
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED: {type(exc).__name__}: {exc}")
            rc = 1
            continue
        words = sum(s.word_count for s in doc.sections)
        print(f"  page      : {doc.title}")
        print(f"  chapters  : {len(doc.sections)}   words: {words}")
        print(f"  revision  : {doc.extra.get('revision')} by {doc.extra.get('revision_user')}")
        print(f"  subpages  : {len(doc.extra.get('pages', [])) - 1}")
        for sec in doc.sections[:3]:
            print(f"    - [{sec.word_count:>6}w] {sec.title!r}")
            print(f"      {sec.text[:160]!r}")
        md = render_chapter_markdown(doc.sections[0])
        print("  --- markdown head ---")
        print("\n".join("    " + ln for ln in md.split("\n")[:10]))
        # Prove the residual markup is gone.
        for bad in ("{{", "}}", "[[", "]]", "<ref", "</ref", "'''", "{|", "http://", "https://"):
            if bad in "\n".join(s.text for s in doc.sections):
                print(f"  !! residual markup {bad!r} in body")
                rc = 1
        if words < 500:
            print("  !! suspiciously little text")
            rc = 1
    print("\nOK" if rc == 0 else "\nPROBLEMS FOUND")
    return rc


if __name__ == "__main__":
    sys.exit(main())
