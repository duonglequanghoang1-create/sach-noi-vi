"""Translate stage: get the text into natural, speakable Vietnamese.

Import order matters for readers: `gloss` is the deterministic normaliser,
`translate` picks a backend from :class:`sachnoi.config.TranslateConfig`.
"""

from __future__ import annotations

from .gloss import (
    Gloss,
    int_to_viet,
    normalize_script,
    normalize_title,
    roman_to_int,
    split_sentences,
)
from .translate import (
    MODES,
    CommandEngine,
    Engine,
    GlossEngine,
    HttpEngine,
    OffEngine,
    TranslationError,
    batch_text,
    build_engine,
    translate_document,
    translate_text,
)

__all__ = [
    "Gloss",
    "int_to_viet",
    "roman_to_int",
    "normalize_script",
    "normalize_title",
    "split_sentences",
    "MODES",
    "Engine",
    "OffEngine",
    "GlossEngine",
    "CommandEngine",
    "HttpEngine",
    "TranslationError",
    "build_engine",
    "translate_document",
    "translate_text",
    "batch_text",
]
