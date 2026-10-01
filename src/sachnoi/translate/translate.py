"""The translate stage: four modes, defaulting to doing nothing.

`vi.wikisource.org` already serves Vietnamese editions of these public-domain
works, so the default mode is ``off``: the text is copied through untouched.
Machine translation is deliberately *not* a default, for two reasons that are
bigger than quality:

1. **Rights.** A machine translation of a public-domain original is a new
   derivative work. Depending on the engine's terms it may not be redistributable
   at all, and a bad MT edition is not something to put in a public audiobook.
2. **Correctness.** This pipeline exists to produce a *narration*. A wrong
   proper noun is worse than an untranslated sentence, because nobody can tell
   it is wrong.

The other three modes exist so the choice is the operator's, and is recorded in
`book.json`:

``gloss``
    Deterministic regex normalisation (:mod:`sachnoi.translate.gloss`). No model,
    no network, byte-identical every run. This is what you want for a source
    that is already Vietnamese but messy.
``command``
    Shell out to an MT binary the operator configures via
    ``SACHNOI_MT_COMMAND``. The command receives the batch on stdin and must
    print the translation on stdout.
``http``
    POST to ``SACHNOI_MT_URL``. Used only when the operator has explicitly
    accepted the rights question for that endpoint.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, Sequence

from ..config import Config, TranslateConfig
from ..extract import Document, Section
from .gloss import Gloss, collapse_spaces_keep_lines, normalize_title

__all__ = [
    "MODES",
    "TranslationError",
    "Engine",
    "OffEngine",
    "GlossEngine",
    "CommandEngine",
    "HttpEngine",
    "build_engine",
    "translate_document",
    "translate_text",
    "batch_text",
]

MODES = ("off", "gloss", "command", "http")


class TranslationError(RuntimeError):
    """Raised when a configured MT backend cannot be used."""


class Engine(Protocol):
    """What every mode implements."""

    name: str

    def translate(self, text: str) -> str:  # pragma: no cover - protocol
        ...


@dataclass
class OffEngine:
    """`off`: pass Vietnamese through unchanged.

    This is the correct mode for a Vietnamese source and the safe default. The
    only thing it does is remove the invisible characters that would otherwise
    reach the TTS, which is not translation.
    """

    name: str = "off"

    def translate(self, text: str) -> str:
        return collapse_spaces_keep_lines(text.strip())


@dataclass
class GlossEngine:
    """`gloss`: the full deterministic reading-text normaliser."""

    name: str = "gloss"
    keep_heading_numbers: bool = False

    def __post_init__(self) -> None:
        self._gloss = Gloss(keep_heading_numbers=self.keep_heading_numbers)

    def translate(self, text: str) -> str:
        return self._gloss.script(text)

    def title(self, title: str) -> str:
        return self._gloss.title(title)


@dataclass
class CommandEngine:
    """`command`: pipe each batch through a configured MT binary.

    The command is a shell-style string from ``SACHNOI_MT_COMMAND``; the batch
    arrives on stdin and the translation must come back on stdout. A non-zero
    exit, or output that comes back empty, is a hard error -- silently passing
    the original through would hide a broken pipeline.
    """

    command: str
    name: str = "command"
    timeout_sec: int = 120
    env: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if not self.command.strip():
            raise TranslationError("SACHNOI_MT_COMMAND is empty; cannot use mode=command")
        self._argv = shlex.split(self.command)

    def translate(self, text: str) -> str:
        if not text.strip():
            return text
        try:
            proc = subprocess.run(
                self._argv,
                input=text,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
                env=self.env,
                check=False,
            )
        except FileNotFoundError as exc:
            raise TranslationError(f"MT command not found: {self._argv[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise TranslationError(f"MT command timed out after {self.timeout_sec}s") from exc
        if proc.returncode != 0:
            raise TranslationError(
                f"MT command exited {proc.returncode}: {proc.stderr.strip()[:300]}"
            )
        out = proc.stdout.strip()
        if not out:
            raise TranslationError("MT command returned empty output")
        return out


@dataclass
class HttpEngine:
    """`http`: POST each batch as JSON to ``SACHNOI_MT_URL``.

    Request:  ``{"text": ..., "target_lang": "vi", "model": ...}``
    Response: ``{"translation": ...}`` (also accepts ``{"translatedText": ...}``
    and ``{"text": ...}``).

    Non-200 and an unparseable body are both hard errors for the same reason as
    in :class:`CommandEngine`: a silent fallback would produce an untranslated
    book that claims to be translated.
    """

    url: str
    name: str = "http"
    model: str = ""
    timeout_sec: int = 120
    target_lang: str = "vi"
    headers: dict[str, str] | None = None
    _client: Any = None

    def __post_init__(self) -> None:
        if not self.url.strip():
            raise TranslationError("SACHNOI_MT_URL is empty; cannot use mode=http")

    def _get_client(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=self.timeout_sec, follow_redirects=True)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def translate(self, text: str) -> str:
        if not text.strip():
            return text
        payload: dict[str, Any] = {"text": text, "target_lang": self.target_lang}
        if self.model:
            payload["model"] = self.model
        headers = {"Content-Type": "application/json"}
        headers.update(self.headers or {})
        try:
            resp = self._get_client().post(self.url, json=payload, headers=headers)
        except Exception as exc:  # noqa: BLE001 - re-raised with context
            raise TranslationError(f"MT endpoint unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise TranslationError(
                f"MT endpoint returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise TranslationError(f"MT endpoint returned non-JSON: {resp.text[:200]}") from exc
        for key in ("translation", "translatedText", "text", "output"):
            value = data.get(key) if isinstance(data, dict) else None
            if isinstance(value, str) and value.strip():
                return value
        raise TranslationError(
            f"MT response has no translation field; keys={sorted(data) if isinstance(data, dict) else type(data).__name__}"
        )


def build_engine(cfg: TranslateConfig | None = None) -> Engine:
    """Instantiate the engine named by ``TranslateConfig.mode``.

    Raises :class:`TranslationError` for an unknown mode or for `command`/`http`
    with no endpoint configured -- both are operator mistakes that should fail
    at build time, not halfway through a 45-chapter book.
    """
    cfg = cfg or Config.load().translate
    mode = (cfg.mode or "off").strip().lower()
    if mode == "off":
        return OffEngine()
    if mode == "gloss":
        return GlossEngine()
    if mode == "command":
        return CommandEngine(command=cfg.command, timeout_sec=cfg.timeout_sec)
    if mode == "http":
        return HttpEngine(url=cfg.url, model=cfg.model, timeout_sec=cfg.timeout_sec)
    raise TranslationError(
        f"unknown translate mode {cfg.mode!r}; expected one of {', '.join(MODES)}"
    )


def batch_text(text: str, batch_chars: int) -> list[str]:
    """Split text into MT batches on paragraph boundaries.

    Batching is paragraph-aligned on purpose: a translator (human or machine)
    handles a whole paragraph far better than an arbitrary character cut, and a
    paragraph never straddles two requests.
    """
    if batch_chars <= 0:
        return [text] if text.strip() else []
    batches: list[str] = []
    buf: list[str] = []
    size = 0
    for para in text.split("\n\n"):
        piece = para if not buf else "\n\n" + para
        if buf and size + len(piece) > batch_chars:
            batches.append("".join(buf))
            buf, size = [para], len(para)
        else:
            buf.append(piece)
            size += len(piece)
    if buf:
        batches.append("".join(buf))
    return [b for b in batches if b.strip()]


def translate_text(text: str, engine: Engine | None = None, *, batch_chars: int = 3000) -> str:
    """Run `text` through `engine`, batching on paragraph boundaries."""
    if engine is None:
        engine = build_engine()
    if not text.strip():
        return text.strip()
    if isinstance(engine, (OffEngine, GlossEngine)):
        return engine.translate(text)
    out: list[str] = []
    for batch in batch_text(text, batch_chars):
        out.append(engine.translate(batch))
    return "\n\n".join(out)


def translate_document(
    document: Document,
    *,
    config: TranslateConfig | None = None,
    engine: Engine | None = None,
    notes: list[str] | None = None,
) -> Document:
    """Translate/normalise every section of `document` in place and return it.

    Chapter titles always go through :func:`normalize_title` regardless of mode:
    a title is a label in the M4B track list, not prose, so it is never sent to
    a translator and never has its digits spelled out.
    """
    cfg = config or Config.load().translate
    eng = engine or build_engine(cfg)
    if getattr(eng, "name", "") not in MODES:
        raise TranslationError(f"engine {eng!r} does not report a valid mode")

    for section in document.sections:
        section.text = translate_text(section.text, eng, batch_chars=cfg.batch_chars)
        section.title = normalize_title(section.title) or "Chương"
    document.extra["translate_mode"] = getattr(eng, "name", "off")
    if notes is not None and getattr(eng, "name", "") in ("command", "http"):
        notes.append(
            f"machine translation was applied via mode={eng.name!r}; the resulting "
            f"text is a derivative work and its redistribution rights must be "
            f"confirmed before publication"
        )
    if isinstance(eng, HttpEngine):
        eng.close()
    return document
