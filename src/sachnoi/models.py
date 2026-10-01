"""Data contracts shared by every pipeline stage.

Both the extract/translate side (may1) and the audio/package side (may2)
import from here. Treat this file as frozen: change it only deliberately and
update BOTH branches in the same commit.

Every stage communicates through JSON on disk, never through Python objects
passed in memory, because the two sides run on different machines.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Directories, relative to the repo root.
SOURCE_DIR = "books"  # books/<slug>/source/...
TEXT_DIR = "books"  # books/<slug>/text/<slug>.md
AUDIO_DIR = "books"  # books/<slug>/audio/<slug>/ch01.mp3
DIST_DIR = "books"  # books/<slug>/dist/<slug>.m4b
CATALOG_FILE = "catalog/library.json"


@dataclass
class Chapter:
    """One audio chapter. `index` is 1-based and defines playback order."""

    index: int
    slug: str  # stable, ASCII, lowercase, e.g. "chuong-01"
    title: str  # shown in the M4B chapter track
    source_path: str  # repo-relative path to the markdown/text segment
    audio_path: str | None = None  # filled by the TTS stage
    duration_sec: float | None = None  # filled by the TTS stage
    word_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Chapter":
        return cls(**{k: v for k, v in data.items() if k in {f.name for f in dataclasses.fields(cls)}})


@dataclass
class Book:
    """A single audiobook. `slug` is the primary key everywhere."""

    slug: str
    title: str  # Vietnamese title
    author: str
    translator: str = ""
    language: str = "vi"
    source_language: str = "en"
    source_format: str = "pdf"  # pdf | epub | txt | md
    source_path: str = ""  # repo-relative path to the original file
    source_url: str = ""  # provenance; required for public-domain books
    license: str = ""  # e.g. "public-domain", "CC-BY-SA-4.0"
    license_url: str = ""
    rights_note: str = ""  # short human-readable rights statement
    summary: str = ""
    narrator: str = ""  # TTS voice name
    text_path: str = ""  # repo-relative path to the assembled Vietnamese markdown
    chapters: list[Chapter] = field(default_factory=list)
    cover_path: str = ""  # repo-relative path to the generated cover image
    m4b_path: str = ""  # repo-relative path to the final audiobook
    status: str = "draft"  # draft | extracted | translated | narrated | packaged

    @property
    def dir(self) -> str:
        return f"books/{self.slug}"

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["chapters"] = [c.to_dict() for c in self.chapters]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Book":
        payload = {k: v for k, v in data.items() if k in {f.name for f in dataclasses.fields(cls)}}
        payload["chapters"] = [Chapter.from_dict(c) for c in payload.get("chapters", [])]
        return cls(**payload)

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Book":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def meta_path(self) -> str:
        return f"{self.dir}/book.json"

    @property
    def chapter_dir(self) -> str:
        return f"{self.dir}/audio/{self.slug}"


@dataclass
class Manifest:
    """Job contract handed between machines.

    The extract/translate stage writes it; the audio stage reads it. Keep it
    flat JSON so a human can read it when debugging a half-finished build.
    """

    book: Book
    stage: str = "extract"  # which stage produced this manifest
    created_at: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"book": self.book.to_dict(), "stage": self.stage, "created_at": self.created_at, "notes": self.notes}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Manifest":
        return cls(
            book=Book.from_dict(data["book"]),
            stage=data.get("stage", "extract"),
            created_at=data.get("created_at", ""),
            notes=data.get("notes", []),
        )


def load_books(books_dir: str | Path = "books") -> list[Book]:
    """Load every books/<slug>/book.json under `books_dir`, sorted by slug."""
    root = Path(books_dir)
    if not root.is_dir():
        return []
    books: list[Book] = []
    for meta in sorted(root.glob("*/book.json")):
        books.append(Book.load(meta))
    return books


def iter_chapter_files(manifest: Manifest) -> Iterable[Path]:
    for chapter in manifest.book.chapters:
        yield Path(chapter.source_path)


def slugify(text: str) -> str:
    """ASCII-safe slug. Vietnamese diacritics are stripped, not transliterated."""
    import re
    import unicodedata

    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = text.lower().replace("đ", "d").replace("Đ", "d")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return re.sub(r"-{2,}", "-", text) or "sach"