"""Wikisource backend: markup stripping, chapter splitting, resolution, errors."""

from __future__ import annotations

import json

import pytest

from sachnoi.extract import Section, chapter_title, count_words, render_chapter_markdown
from sachnoi.extract import wikisource as ws


# --------------------------------------------------------------------------
# Markup removal
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "markup",
    [
        "{{chú thích}}",
        "{{đầu đề|tựa đề=X}}",
        "[[Thể loại:Văn học]]",
        "[[File:hinh.jpg|thumb|caption]]",
        "[[Tập tin:a.ogg]]",
        "<ref>Ghi chú nguồn</ref>",
        "<ref name='x'/>",
        "<!-- chú thích ẩn -->",
        "{{{1}}}",
        "<nowiki>{{không phải template}}</nowiki>",
        "__NOTOC__",
        "{|\n|-\n| a || b\n|}",
        "'''in đậm'''",
        "''nghiêng''",
        "<div class='mw-parser-output'>",
        "===",
    ],
)
def test_markup_is_removed(markup: str) -> None:
    out = ws.strip_wikitext(f"Trước {markup} sau.")
    assert "{{" not in out
    assert "}}" not in out
    assert "[[" not in out
    assert "]]" not in out
    assert "<ref" not in out
    assert "<" not in out.replace("Trước", "").replace("sau.", "")
    assert "|" not in out


def test_templates_that_hold_the_prose_are_unwrapped_not_deleted(vi_wikitext: str) -> None:
    # Regression: deleting {{văn|...}} as an ordinary template removes the whole
    # chapter, and {{drop cap|B}} eats the first letter of the first word.
    out = ws.strip_wikitext(vi_wikitext)
    assert "Bắt đầu từ gà gáy" in out
    assert "trâu bò lục tục" in out
    assert "Gà gáy giục" in out
    assert "Số liệu theo sổ địa chính" not in out  # the <ref> went


def test_nested_content_templates_are_unwrapped_recursively() -> None:
    raw = "{{văn|{{drop cap|X}}in đầu tiên của chương.}}"
    assert "Xin đầu tiên của chương." in ws.strip_wikitext(raw)


def test_header_template_becomes_a_heading() -> None:
    raw = "{{đầu đề|tựa đề=[[../]]|tác giả=X|phần=Chương 3}}\n{{văn|Nội dung.}}\n"
    out = ws.strip_wikitext(raw)
    assert "== Chương 3 ==" in out
    assert "Nội dung." in out
    assert "tựa đề" not in out
    assert "Nội dung.\n" not in out.split("== Chương 3 ==")[0].strip()  # nothing before the heading


def test_pipe_inside_a_nested_template_does_not_split_parameters() -> None:
    raw = "{{đầu đề|phần={{nowrap|Chương 4}}|tác giả=A|B}}"
    assert "== Chương 4 ==" in ws.strip_wikitext(raw)


def test_wikilinks_keep_the_display_text(vi_wikitext: str) -> None:
    raw = "Xem [[Tắt đèn/II|chương sau]] và [[mục lục]]."
    out = ws.strip_wikitext(raw)
    assert "chương sau" in out
    assert "mục lục" in out
    assert "[[" not in out


def test_external_links_keep_the_label_only() -> None:
    out = ws.strip_wikitext("Xem [https://example.vn/bai-viet bài viết này] nhé.")
    assert "bài viết này" in out
    assert "example.vn" not in out


def test_headings_are_kept(vi_wikitext: str) -> None:
    out = ws.strip_wikitext(vi_wikitext)
    assert "== Ghi chú ==" in out


# --------------------------------------------------------------------------
# Chapter splitting
# --------------------------------------------------------------------------


def test_chapters_split_on_equals_headings() -> None:
    raw = "\n\n".join(
        [
            "== Chương 1 ==\n\n" + ("Câu một. " * 30),
            "== Chương 2 ==\n\n" + ("Câu hai. " * 30),
            "== Chương 3 ==\n\n" + ("Câu ba. " * 30),
        ]
    )
    sections = ws.split_wikitext_sections(raw, min_words=20)
    assert [s.title for s in sections] == ["Chương 1", "Chương 2", "Chương 3"]
    assert all(s.word_count >= 20 for s in sections)


def test_triple_equals_also_marks_a_chapter() -> None:
    raw = "\n\n".join(
        [
            "== Chương 1 ==\n\n" + ("A " * 40),
            "=== Mục 1 ===\n\n" + ("B " * 40),
            "=== Mục 2 ===\n\n" + ("C " * 40),
        ]
    )
    titles = [s.title for s in ws.split_wikitext_sections(raw, min_words=20)]
    assert "Mục 1" in titles and "Mục 2" in titles


def test_quadruple_equals_stays_inside_the_chapter() -> None:
    raw = "== Chương 1 ==\n\n" + ("A " * 60) + "\n\n==== Tiểu mục sâu ====\n\n" + ("B " * 30)
    sections = ws.split_wikitext_sections(raw, min_words=20)
    assert len(sections) == 1
    assert sections[0].title == "Chương 1"
    assert "Tiểu mục sâu" in sections[0].text


def test_apparatus_sections_are_dropped_entirely() -> None:
    raw = "\n\n".join(
        [
            "== Chương 1 ==\n\n" + ("A " * 60),
            "== Chương 2 ==\n\n" + ("B " * 60),
            "== Ghi chú ==\n\n" + ("C " * 60),
            "== Tham khảo ==\n\n" + ("D " * 60),
            "== Bản quyền ==\n\n" + ("E " * 60),
        ]
    )
    titles = [s.title for s in ws.split_wikitext_sections(raw, min_words=20)]
    assert titles == ["Chương 1", "Chương 2"]


def test_duplicate_nested_heading_does_not_create_a_stub_chapter() -> None:
    raw = "== Chương 1 ==\n\n" + ("A " * 60) + "\n\n=== Chương 1 ===\n\n" + ("B " * 60)
    sections = ws.split_wikitext_sections(raw, min_words=20)
    assert len(sections) == 1
    assert "B" in sections[0].text


def test_tiny_sections_are_merged_forward() -> None:
    raw = "\n\n".join(
        [
            "== Chương 1 ==\n\n" + ("A " * 60),
            "== Chương 2 ==\n\n" + ("Vài chữ ngắn. " * 3),
            "== Chương 3 ==\n\n" + ("B " * 60),
        ]
    )
    sections = ws.split_wikitext_sections(raw, min_words=40)
    # The stub is folded into the next real chapter, which keeps its own title.
    assert [s.title for s in sections] == ["Chương 1", "Chương 3"]
    assert "Vài chữ ngắn." in sections[-1].text
    assert "B" in sections[-1].text


def test_page_with_no_headings_still_yields_a_chapter() -> None:
    sections = ws.split_wikitext_sections("Chỉ có văn xuôi. " * 30, min_words=5)
    assert len(sections) == 1
    assert sections[0].word_count > 20


def test_empty_wikitext_yields_nothing() -> None:
    assert ws.split_wikitext_sections("") == []
    assert ws.split_wikitext_sections("{{chỉ có template}}") == []


# --------------------------------------------------------------------------
# Title / URL helpers
# --------------------------------------------------------------------------


def test_title_from_url() -> None:
    assert ws.title_from_url("https://vi.wikisource.org/wiki/T%E1%BA%AFt_%C4%91%C3%A8n") == "Tắt đèn"
    assert ws.title_from_url("https://vi.wikisource.org/wiki/A_B?action=edit#s1") == "A B"
    assert ws.title_from_url("A_B") == "A B"


def test_natural_key_orders_numerals() -> None:
    titles = ["Chương 10", "Chương 2", "Chương 1"]
    assert sorted(titles, key=ws.natural_key) == ["Chương 1", "Chương 2", "Chương 10"]


def test_norm_title_folds_case_and_accents() -> None:
    assert ws._norm_title("Tắt Đèn") == ws._norm_title("tat den")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("I", "Chương một"),
        ("IV", "Chương bốn"),
        ("XIV", "Chương mười bốn"),
        ("12", "Chương mười hai"),
        ("Chương 4", "Chương 4"),
        ("Phần mở đầu", "Phần mở đầu"),
        ("", "Chương"),
    ],
)
def test_chapter_title_expands_bare_numerals(raw: str, expected: str) -> None:
    assert chapter_title(raw) == expected


# --------------------------------------------------------------------------
# API client behaviour, with the network stubbed out
# --------------------------------------------------------------------------


class _FakeResponse:
    """Stands in for `httpx.Response`."""

    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload, ensure_ascii=False)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Dispatch by request shape: `handler(params) -> response payload`.

    Keying on the request rather than on the action name matters, because one
    action (`query`) is used for three different things here: page lookup, page
    listing and revision lookup.
    """

    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[dict] = []

    def get(self, _url: str, *, params: dict):
        self.calls.append(dict(params))
        return _FakeResponse(self.handler(params))


def test_resolve_returns_the_canonical_title() -> None:
    fake = _FakeClient(
        lambda p: {"query": {"pages": [{"pageid": 1, "title": "Tắt đèn"}]}}
    )
    mw = ws.MediaWikiClient("vi", client=fake)  # type: ignore[arg-type]
    assert mw.resolve("Tat den") == "Tắt đèn"
    assert [c["action"] for c in fake.calls] == ["query"]


def test_resolve_falls_back_to_search_for_a_mistyped_title() -> None:
    """catalog/books.json really does contain `Gi%E1%BA%A5c_m%C6%A1o` (sic), so the
    fuzzy fallback has to be exercised rather than hoped for."""

    def handler(p: dict) -> dict:
        if "titles" in p:
            return {"query": {"pages": [{"missing": True}]}}
        return {"query": {"search": [{"title": "Giấc mơ đêm giáng sinh"}]}}

    mw = ws.MediaWikiClient("vi", client=_FakeClient(handler))  # type: ignore[arg-type]
    assert mw.resolve("Giấc mơo đêm giáng sinh") == "Giấc mơ đêm giáng sinh"


def test_resolve_rejects_an_unrelated_search_hit() -> None:
    def handler(p: dict) -> dict:
        if "titles" in p:
            return {"query": {"pages": [{"missing": True}]}}
        return {"query": {"search": [{"title": "Luật Đất đai nước Cộng hòa"}]}}

    mw = ws.MediaWikiClient("vi", client=_FakeClient(handler))  # type: ignore[arg-type]
    assert mw.resolve("Frankenstein hay là người nhân tính thứ nhất") is None


def test_revision_is_read_from_the_revisions_api() -> None:
    def handler(p: dict) -> dict:
        return {
            "query": {
                "pages": [
                    {
                        "pageid": 9,
                        "title": "Tắt đèn",
                        "revisions": [
                            {"revid": 141060, "timestamp": "2024-05-01T10:00:00Z", "user": "Someone"}
                        ],
                    }
                ]
            }
        }

    mw = ws.MediaWikiClient("vi", client=_FakeClient(handler))  # type: ignore[arg-type]
    rev = mw.revision("Tắt đèn")
    assert (rev.revid, rev.pageid, rev.user) == (141060, 9, "Someone")
    assert "2024-05-01" in rev.describe()


def test_subpages_are_listed_in_natural_order() -> None:
    def handler(p: dict) -> dict:
        if p.get("list") == "allpages":
            kids = ["Tắt đèn/X", "Tắt đèn/II", "Tắt đèn/I"]
            return {"query": {"allpages": [{"title": t} for t in kids]}}
        return {"query": {"allpages": []}}

    mw = ws.MediaWikiClient("vi", client=_FakeClient(handler))  # type: ignore[arg-type]
    assert mw.subpages("Tắt đèn") == ["Tắt đèn/I", "Tắt đèn/II", "Tắt đèn/X"]


def test_extract_raises_lookup_error_for_a_missing_page() -> None:
    def handler(p: dict) -> dict:
        if "titles" in p:
            return {"query": {"pages": [{"missing": True}]}}
        return {"query": {"search": []}}

    mw = ws.MediaWikiClient("vi", client=_FakeClient(handler))  # type: ignore[arg-type]
    with pytest.raises(LookupError):
        ws.extract("https://vi.wikisource.org/wiki/Khong_Ton_Tai", client=mw)


def test_extract_refuses_an_unsupported_language() -> None:
    with pytest.raises(ValueError):
        ws.MediaWikiClient("klingon")


# --------------------------------------------------------------------------
# End-to-end on a stubbed client, including subpages
# --------------------------------------------------------------------------


def test_extract_collects_subpages_in_order() -> None:
    pages = {
        "Tắt đèn": "== Mục lục ==\n[[../I]]\n[[../II]]",
        # Over MIN_TOTAL_WORDS combined, or the extract stage would reject the
        # pair as a stub.
        "Tắt đèn/I": "{{đầu đề|phần=I}}{{văn|" + ("Chữ một. " * 200) + "}}",
        "Tắt đèn/II": "{{đầu đề|phần=II}}{{văn|" + ("Chữ hai. " * 200) + "}}",
    }

    def handler(p: dict) -> dict:
        if p["action"] == "parse":
            title = p["page"]
            return {"parse": {"title": title, "wikitext": pages.get(title, "")}}
        if p.get("list") == "allpages":
            prefix = p.get("apprefix", "")
            kids = [t for t in pages if t.startswith(prefix) and t != prefix]
            return {"query": {"allpages": [{"title": t} for t in kids]}}
        if p.get("list") == "search":
            return {"query": {"search": []}}
        if p.get("prop") == "revisions":
            title = p["titles"]
            return {
                "query": {
                    "pages": [
                        {
                            "pageid": 3,
                            "title": title,
                            "revisions": [
                                {"revid": 7, "timestamp": "2024-01-01T00:00:00Z", "user": "T"}
                            ],
                        }
                    ]
                }
            }
        if "titles" in p:
            if p["titles"] in pages:
                return {"query": {"pages": [{"pageid": 1, "title": p["titles"]}]}}
            return {"query": {"pages": [{"missing": True}]}}
        return {"query": {}}

    mw = ws.MediaWikiClient("vi", client=_FakeClient(handler))  # type: ignore[arg-type]
    doc = ws.extract("https://vi.wikisource.org/wiki/T%E1%BA%AFt_%C4%91%C3%A8n", client=mw)
    assert [chapter_title(s.title) for s in doc.sections] == ["Chương một", "Chương hai"]
    assert doc.extra["revision"] == 7
    assert doc.extra["pageid"] == 3
    assert doc.language == "vi"
    # The `== Mục lục ==` index page contributes no prose of its own.
    assert all("Mục lục" not in s.title for s in doc.sections)


class _StubWiki:
    """Language-aware stand-in for `MediaWikiClient`.

    `vi` has no page for the title; `en` does. Injected by monkeypatching the
    class so the real `extract()` loop is exercised, including the per-language
    client construction.
    """

    def __init__(self, language: str = "vi", exists: bool = True, **_kwargs) -> None:
        self.language = language
        self.exists = exists

    def resolve(self, title: str) -> str | None:
        return title if self.exists else None

    def wikitext(self, title: str) -> tuple[str, str]:
        # Over MIN_TOTAL_WORDS, or the extract stage would (correctly) reject it
        # as a stub.
        return title, "== A ==\n\n" + ("Word. " * 300)

    def subpages(self, title: str) -> list[str]:
        return []

    def linked_pages(self, title: str) -> list[str]:
        return []

    def rendered_html(self, title: str) -> str:
        return ""

    def revision(self, title: str) -> ws.Revision:
        return ws.Revision(revid=1, title=title, pageid=1)

    def close(self) -> None:
        pass


def test_extract_never_labels_a_fallback_as_vietnamese(monkeypatch) -> None:
    # The Vietnamese edition does not exist; the English one does.
    monkeypatch.setattr(ws, "MediaWikiClient", lambda lang, **kw: _StubWiki(lang, exists=lang != "vi"))
    doc = ws.extract(
        "https://en.wikisource.org/wiki/Alice",
        language="vi",
        fallback_language="en",
    )
    # A fallback must never be silently passed off as Vietnamese.
    assert doc.language == "en"
    assert any("VIETNAMESE EDITION NOT FOUND" in n for n in doc.notes)
    assert any("NOT narratable" in n for n in doc.notes)


def test_extract_prefers_the_requested_language(monkeypatch) -> None:
    monkeypatch.setattr(ws, "MediaWikiClient", _StubWiki)
    doc = ws.extract("https://vi.wikisource.org/wiki/Alice", language="vi", fallback_language="en")
    assert doc.language == "vi"
    assert doc.notes == []
