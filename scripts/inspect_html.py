"""Debug helper: show the rendered HTML structure of a page, for extractor work.

    python3 scripts/inspect_html.py "Truyện Kiều (bản Trương Vĩnh Ký 1911)/CHUNG"
    python3 scripts/inspect_html.py "Truyện Kiều (bản Trương Vĩnh Ký 1911)/Tích Túy Kiều" --classes
    python3 scripts/inspect_html.py "Truyện Kiều" --dump
"""

from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from sachnoi.extract.wikisource import MediaWikiClient, title_from_url  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("title")
    ap.add_argument("--classes", action="store_true", help="count every class/id used")
    ap.add_argument("--dump", type=int, default=0, help="print N chars of raw HTML")
    ap.add_argument("--tags", default="", help="comma-separated tags to pretty-print")
    args = ap.parse_args()

    with MediaWikiClient("vi") as mw:
        title = mw.resolve(title_from_url(args.title)) or args.title
        html = mw.rendered_html(title)
    print(f"title={title!r}  html={len(html)} bytes")

    if args.classes:
        counts = collections.Counter(re.findall(r'class="([^"]+)"', html))
        for name, n in counts.most_common(40):
            print(f"  {n:>5}  {name}")
        print("--- ids ---")
        for name, n in collections.Counter(re.findall(r'id="([^"]+)"', html)).most_common(20):
            print(f"  {n:>5}  {name}")

    if args.tags:
        for tag in args.tags.split(","):
            tag = tag.strip()
            for m in re.finditer(rf"<{tag}\b[^>]*>.*?</{tag}>", html, re.S | re.I):
                snippet = re.sub(r"\s+", " ", m.group(0))
                print(f"  <{tag}> {snippet[:400]}")

    if args.dump:
        print(html[: args.dump])
    return 0


if __name__ == "__main__":
    sys.exit(main())
