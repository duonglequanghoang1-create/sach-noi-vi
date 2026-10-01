"""Rights gate: a hard failure, never a warning.

CONTRACT.md: "The build refuses to narrate or package a book whose `license` is
not one of `public-domain`, `CC0`, `CC-BY-4.0`, `CC-BY-SA-4.0`. ... This is not
optional and not a warning -- it is a hard failure."
"""

from __future__ import annotations

import json

import pytest

from sachnoi.extract import RightsError, check_license, normalize_license
from sachnoi.extract import extract_file
from sachnoi.models import Book


ALLOWED = ["public-domain", "CC0", "CC-BY-4.0", "CC-BY-SA-4.0"]
REFUSED = [
    "all-rights-reserved",
    "CC-BY-NC-4.0",
    "CC-BY-ND-4.0",
    "CC-BY-NC-SA-4.0",
    "in-copyright",
    "fair-use",
    "proprietary",
    "CC-BY-3.0",  # wrong version, must not be waved through
    "CC-BY-SA-3.0",
    "public-domain-ish",  # near miss on purpose
    "unknown",
]


@pytest.mark.parametrize("license_id", ALLOWED)
def test_allowed_licences_pass(license_id: str) -> None:
    assert check_license(license_id) == license_id.lower()


@pytest.mark.parametrize("license_id", REFUSED)
def test_refused_licences_raise(license_id: str) -> None:
    with pytest.raises(RightsError) as exc:
        check_license(license_id, slug="some-book")
    assert license_id in str(exc.value)
    assert "some-book" in str(exc.value)


def test_empty_licence_is_a_failure_not_a_pass() -> None:
    # A book with no provenance must never reach the TTS.
    for value in ("", "   ", None):
        with pytest.raises(RightsError):
            check_license(value)  # type: ignore[arg-type]


def test_case_and_spacing_are_forgiving() -> None:
    assert normalize_license("  Public-Domain ") == "public-domain"
    assert normalize_license("CC BY 4.0") == "cc-by-4.0"
    assert normalize_license("CC-BY-SA-4.0") == "cc-by-sa-4.0"
    assert check_license("CC BY SA 4.0") == "cc-by-sa-4.0"


def test_error_is_its_own_type_so_callers_can_catch_it() -> None:
    # Agent B and the CLI need to print one line, not a traceback.
    from sachnoi.extract import ALLOWED_LICENSES

    assert issubclass(RightsError, Exception)
    assert not issubclass(RightsError, KeyError)
    assert "public-domain" in ALLOWED_LICENSES


def test_extract_file_refuses_before_reading(tmp_path) -> None:
    source = tmp_path / "book.txt"
    source.write_text("# Chương 1\n\nVăn bản.", encoding="utf-8")
    with pytest.raises(RightsError):
        extract_file(source, license_id="CC-BY-NC-4.0")


def test_extract_file_refuses_a_missing_licence_before_reading(tmp_path) -> None:
    # The file does not even exist: the gate must still fire first.
    missing = tmp_path / "nope.pdf"
    with pytest.raises(RightsError):
        extract_file(missing, license_id="")


def test_extract_url_refuses_a_bad_licence() -> None:
    from sachnoi.extract import extract_url

    with pytest.raises(RightsError):
        extract_url("https://vi.wikisource.org/wiki/T%E1%BA%AFt_%C4%91%C3%A8n", license_id="in-copyright")


def test_assemble_book_refuses_a_bad_licence(tmp_path, catalog_entry) -> None:
    from sachnoi.extract import Document, Section, assemble_book

    entry = dict(catalog_entry, license="all-rights-reserved")
    doc = Document(sections=[Section(title="Chương 1", text="Chữ. " * 60)])
    with pytest.raises(RightsError):
        assemble_book(entry, doc, write_files=False)


def test_catalog_licences_all_pass_the_gate() -> None:
    """The frozen catalog must stay narratable, or the build is stuck."""
    from pathlib import Path

    from sachnoi.config import REPO_ROOT

    path = Path(REPO_ROOT) / "catalog" / "books.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    books = data["books"]
    assert books, "catalog is empty"
    for entry in books:
        assert check_license(entry["license"], slug=entry["slug"]) in {
            "public-domain",
            "cc0",
            "cc-by-4.0",
            "cc-by-sa-4.0",
        }


def test_generated_book_json_survives_the_gate() -> None:
    """Whatever we wrote to disk must still be narratable by agent B."""
    from pathlib import Path

    from sachnoi.config import REPO_ROOT
    from sachnoi.models import load_books

    books = load_books(Path(REPO_ROOT) / "books")
    if not books:
        pytest.skip("no books/ output yet; run scripts/extract_book.py")
    for book in books:
        check_license(book.license, slug=book.slug)
        if book.status == "blocked-no-vi-source":
            # A blocked book must say so and must not pretend to have text.
            assert book.chapters == []
            assert book.text_path == ""
            assert "VIETNAMESE EDITION" in book.rights_note
        else:
            assert book.chapters, f"{book.slug}: status={book.status} but no chapters"
            assert book.text_path
