"""`TranslateConfig.mode`: all four modes, and the guarantees around them.

The default is `off` and it has to stay that way. Every catalog entry is
already a Vietnamese edition, and CONTRACT.md forbids machine-translating a
public-domain work to make it look narratable.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

from sachnoi.config import TranslateConfig
from sachnoi.extract import Document, Section
from sachnoi.translate import (
    MODES,
    CommandEngine,
    GlossEngine,
    HttpEngine,
    OffEngine,
    TranslationError,
    batch_text,
    build_engine,
    translate_document,
    translate_text,
)


def _doc(text: str = "Câu một. Câu hai.") -> Document:
    return Document(
        sections=[Section(title="Chương 1", text=text)],
        language="vi",
        source_format="wikisource",
    )


# --------------------------------------------------------------------------
# Mode dispatch
# --------------------------------------------------------------------------


def test_the_four_modes_exist() -> None:
    assert set(MODES) == {"off", "gloss", "command", "http"}


def test_the_default_is_off() -> None:
    assert TranslateConfig().mode == "off"
    assert isinstance(build_engine(), OffEngine)


@pytest.mark.parametrize("mode", ["off", "gloss"])
def test_off_and_gloss_need_no_configuration(mode: str) -> None:
    cfg = dataclasses.replace(TranslateConfig(), mode=mode)
    assert build_engine(cfg).name == mode


def test_command_mode_needs_a_command() -> None:
    cfg = dataclasses.replace(TranslateConfig(), mode="command", command="")
    with pytest.raises(TranslationError) as exc:
        build_engine(cfg)
    assert "SACHNOI_MT_COMMAND" in str(exc.value)


def test_http_mode_needs_a_url() -> None:
    cfg = dataclasses.replace(TranslateConfig(), mode="http", url="")
    with pytest.raises(TranslationError) as exc:
        build_engine(cfg)
    assert "SACHNOI_MT_URL" in str(exc.value)


def test_an_unknown_mode_is_refused_at_build_time() -> None:
    cfg = dataclasses.replace(TranslateConfig(), mode="deepseek")
    with pytest.raises(TranslationError) as exc:
        build_engine(cfg)
    assert "off, gloss, command, http" in str(exc.value)


def test_mode_is_case_insensitive() -> None:
    assert build_engine(dataclasses.replace(TranslateConfig(), mode="GLOSS")).name == "gloss"


# --------------------------------------------------------------------------
# off
# --------------------------------------------------------------------------


def test_off_passes_vietnamese_through_unchanged() -> None:
    text = "Cô bé nghĩ một cuốn sách không có hình vẽ."
    assert OffEngine().translate(text) == text


def test_off_only_removes_invisible_characters() -> None:
    # Not translation -- just the invisible characters that would reach the TTS.
    # They are deleted, not replaced by a space, so "a<ZWSP>b" stays one word.
    assert OffEngine().translate("\ufeffa\u200bb") == "ab"
    assert OffEngine().translate("a\u00a0b") == "a b"  # a non-breaking space is a space


def test_off_still_collapses_paragraphs() -> None:
    assert OffEngine().translate("A.\n\n\n\nB.") == "A.\n\nB."


# --------------------------------------------------------------------------
# gloss
# --------------------------------------------------------------------------


def test_gloss_normalises_without_translating() -> None:
    assert GlossEngine().translate("Chương I: Mở đầu.") == "Chương một. Mở đầu."


def test_gloss_is_byte_identical_across_runs() -> None:
    engine = GlossEngine()
    text = "TP.HCM & QLĐT → 3 người: 10:30, 12/09, 5%."
    assert engine.translate(text) == engine.translate(text)


def test_gloss_titles_keep_their_digits() -> None:
    assert GlossEngine().title("## Chương 3") == "Chương 3"


# --------------------------------------------------------------------------
# command
# --------------------------------------------------------------------------


def test_command_mode_round_trip() -> None:
    engine = CommandEngine(command=f"{sys.executable} -c \"import sys;print(sys.stdin.read().upper())\"")
    assert engine.translate("abc") == "ABC"


def test_command_mode_receives_the_batch_on_stdin() -> None:
    marker = "XINH-CHAO-1234"
    engine = CommandEngine(command=f"{sys.executable} -c \"import sys;print(sys.stdin.read() + '{marker}')\"")
    assert engine.translate("Văn bản.").endswith(marker)


def test_command_mode_fails_loudly_on_a_missing_binary() -> None:
    with pytest.raises(TranslationError) as exc:
        CommandEngine(command="khong-ton-tai-12345").translate("x")
    assert "not found" in str(exc.value)


def test_command_mode_fails_loudly_on_a_nonzero_exit() -> None:
    engine = CommandEngine(command=f"{sys.executable} -c \"import sys;sys.exit(3)\"")
    with pytest.raises(TranslationError) as exc:
        engine.translate("x")
    assert "exited 3" in str(exc.value)


def test_command_mode_refuses_to_silently_pass_the_original_through() -> None:
    # An empty response from a broken MT binary must not become a "translated"
    # book that still contains the source language.
    engine = CommandEngine(command=f"{sys.executable} -c \"pass\"")
    with pytest.raises(TranslationError) as exc:
        engine.translate("Văn bản gốc.")
    assert "empty" in str(exc.value)


def test_batches_stay_whole_paragraphs() -> None:
    # batch_text decides the split, so assert on it directly rather than on the
    # subprocess plumbing.
    para = "chữ " * 100  # 400 characters
    text = "\n\n".join(f"Đoạn {i}. " + para for i in range(6))
    batches = batch_text(text, 500)
    assert len(batches) == 6
    for original in text.split("\n\n"):
        assert any(original in b for b in batches)


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------


class _Response:
    def __init__(self, payload=None, status: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status_code = status
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeHTTP:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.sent: list[dict] = []

    def post(self, url, *, json=None, headers=None):
        self.sent.append({"url": url, "json": json, "headers": headers})
        return self.response

    def close(self) -> None:
        pass


@pytest.mark.parametrize("key", ["translation", "translatedText", "text", "output"])
def test_http_mode_accepts_the_common_response_shapes(key: str) -> None:
    engine = HttpEngine(url="https://mt.example/translate")
    engine._client = _FakeHTTP(_Response({key: "Xin chào"}))
    assert engine.translate("Văn bản.") == "Xin chào"


def test_http_mode_sends_the_documented_payload() -> None:
    engine = HttpEngine(url="https://mt.example/translate", model="vi-1")
    fake = _FakeHTTP(_Response({"translation": "ok"}))
    engine._client = fake
    engine.translate("Văn bản.")
    assert fake.sent[0]["json"] == {"text": "Văn bản.", "target_lang": "vi", "model": "vi-1"}


def test_http_mode_fails_loudly_on_a_server_error() -> None:
    engine = HttpEngine(url="https://mt.example/translate")
    engine._client = _FakeHTTP(_Response(None, status=502, text="bad gateway"))
    with pytest.raises(TranslationError) as exc:
        engine.translate("x")
    assert "502" in str(exc.value)


def test_http_mode_fails_loudly_on_a_non_json_body() -> None:
    engine = HttpEngine(url="https://mt.example/translate")
    engine._client = _FakeHTTP(_Response(None, status=200, text="<html>oops</html>"))
    with pytest.raises(TranslationError) as exc:
        engine.translate("x")
    assert "non-JSON" in str(exc.value)


def test_http_mode_fails_loudly_when_the_field_is_missing() -> None:
    engine = HttpEngine(url="https://mt.example/translate")
    engine._client = _FakeHTTP(_Response({"error": "quota"}))
    with pytest.raises(TranslationError) as exc:
        engine.translate("x")
    assert "no translation field" in str(exc.value)


def test_http_mode_fails_loudly_when_unreachable() -> None:
    engine = HttpEngine(url="https://mt.example/translate")

    class _Boom:
        def post(self, *_a, **_k):
            raise OSError("connection refused")

    engine._client = _Boom()
    with pytest.raises(TranslationError) as exc:
        engine.translate("x")
    assert "unreachable" in str(exc.value)


# --------------------------------------------------------------------------
# Batching
# --------------------------------------------------------------------------


def test_batching_never_splits_a_paragraph() -> None:
    text = "\n\n".join(f"Đoạn {i}. " + "chữ " * 30 for i in range(10))
    batches = batch_text(text, 400)
    assert len(batches) > 1
    for original in text.split("\n\n"):
        assert any(original in b for b in batches), "a paragraph was cut in half"


def test_batching_respects_the_limit_where_it_can() -> None:
    para = "chữ " * 100  # 100 words
    text = "\n\n".join([para] * 5)
    batches = batch_text(text, 250)
    assert len(batches) > 1
    assert all(len(b.split()) <= 400 for b in batches)


def test_zero_or_negative_batch_size_is_one_batch() -> None:
    assert batch_text("A.\n\nB.", 0) == ["A.\n\nB."]


# --------------------------------------------------------------------------
# Whole documents
# --------------------------------------------------------------------------


def test_translate_document_normalises_titles_in_every_mode() -> None:
    for mode in ("off", "gloss"):
        doc = _doc()
        translate_document(doc, config=dataclasses.replace(TranslateConfig(), mode=mode))
        assert doc.sections[0].title == "Chương 1"
        assert doc.extra["translate_mode"] == mode


def test_machine_translation_is_recorded_as_a_rights_risk() -> None:
    notes: list[str] = []
    doc = _doc()
    engine = CommandEngine(command=f"{sys.executable} -c \"import sys;print(sys.stdin.read().upper())\"")
    translate_document(doc, engine=engine, notes=notes)
    assert any("derivative work" in n for n in notes)


def test_default_mode_leaves_no_rights_note() -> None:
    notes: list[str] = []
    translate_document(_doc(), notes=notes)
    assert notes == []


def test_translate_document_rejects_an_engine_with_a_bogus_mode() -> None:
    class _Impostor:
        name = "translate-with-a-vibe"

        def translate(self, text: str) -> str:
            return text

    with pytest.raises(TranslationError):
        translate_document(_doc(), engine=_Impostor())  # type: ignore[arg-type]


def test_empty_text_is_a_no_op() -> None:
    assert translate_text("", build_engine()) == ""
    assert translate_text("   ", build_engine()) == ""


def test_http_client_is_closed_after_a_document_run() -> None:
    closed: list[bool] = []

    class _Client:
        def post(self, *_a, **_k):
            return _Response({"translation": "ok"})

        def close(self) -> None:
            closed.append(True)

    engine = HttpEngine(url="https://mt.example/translate")
    engine._client = _Client()
    translate_document(_doc(), engine=engine)
    assert closed == [True]
