"""Debug helper: dump raw wikitext and cleaned sections for a page.

    python3 scripts/inspect_page.py "Truyện Kiều"
    python3 scripts/inspect_page.py "Lục Vân Tiên"
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from sachnoi.extract import wikisource as ws  # noqa: E402


def main() -> int:
    title = " ".join(sys.argv[1:])
    if not title:
        print(__doc__)
        return 1
    mw = ws.MediaWikiClient("vi")
    try:
        resolved = mw.resolve(title)
        print(f"resolve({title!r}) -> {resolved!r}")
        if not resolved:
            return 1
        _, wiki = mw.wikitext(resolved)
        print(f"root wikitext: {len(wiki)} bytes")
        print("--- first 700 chars ---")
        print(wiki[:700])

        print("--- raw allpages apprefix ---")
        raw = mw.api(
            action="query",
            list="allpages",
            apnamespace="0",
            apprefix=f"{resolved}/",
            aplimit="max",
        )
        print([p["title"] for p in raw.get("query", {}).get("allpages", [])])
        kids = mw.subpages(resolved)
        print(f"--- subpages() -> {len(kids)}: {kids[:10]}")

        print("--- prop=links (namespace 0) ---")
        links = mw.api(
            action="query", titles=resolved, prop="links", plnamespace="0", pllimit="max"
        )
        titles = [ln["title"] for pg in links.get("query", {}).get("pages", []) for ln in pg.get("links", [])]
        print(len(titles), titles[:12])

        sections = ws.split_wikitext_sections(wiki, min_words=10)
        print(f"--- {len(sections)} section(s) from root page")
        for s in sections[:4]:
            print(f"    [{s.word_count}w] {s.title!r} :: {s.text[:100]!r}")
    finally:
        mw.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
