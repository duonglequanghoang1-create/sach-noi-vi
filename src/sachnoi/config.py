"""Configuration. Every tunable lives here or in a .env file; no magic values
scattered through the stages.

Values come from, in order of precedence: environment variables, then the
repo-root `.env` file, then the defaults below.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip("'\"")
    return values


_DOTENV = _load_dotenv(REPO_ROOT / ".env")


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key) or _DOTENV.get(key, default)


def _get_int(key: str, default: int) -> int:
    try:
        return int(_get(key, str(default)))
    except ValueError:
        return default


def _get_float(key: str, default: float) -> float:
    try:
        return float(_get(key, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class TTSConfig:
    """VieNeu-TTS settings. See docs/tts.md for model download instructions."""

    backend: str = field(default_factory=lambda: _get("SACHNOI_TTS_BACKEND", "vieneu"))
    speaker_id: str = field(default_factory=lambda: _get("SACHNOI_TTS_SPEAKER", "VN-01"))
    speed: float = field(default_factory=lambda: _get_float("SACHNOI_TTS_SPEED", 1.0))
    sample_rate: int = field(default_factory=lambda: _get_int("SACHNOI_SAMPLE_RATE", 22050))
    # Long inputs must be split at sentence boundaries; this caps each chunk.
    max_chars: int = field(default_factory=lambda: _get_int("SACHNOI_TTS_MAX_CHARS", 400))
    # Where the VieNeu-TTS checkout and its checkpoints live.
    model_dir: str = field(default_factory=lambda: _get("VIENEU_MODEL_DIR", "models/vieneu"))
    device: str = field(default_factory=lambda: _get("SACHNOI_TTS_DEVICE", "auto"))  # auto|cpu|cuda


@dataclass(frozen=True)
class AudioConfig:
    bitrate: str = field(default_factory=lambda: _get("SACHNOI_M4B_BITRATE", "64k"))
    sample_rate: int = field(default_factory=lambda: _get_int("SACHNOI_SAMPLE_RATE", 22050))
    chapter_gap_sec: float = field(default_factory=lambda: _get_float("SACHNOI_CHAPTER_GAP", 0.6))
    cover_width: int = field(default_factory=lambda: _get_int("SACHNOI_COVER_WIDTH", 1400))
    cover_height: int = field(default_factory=lambda: _get_int("SACHNOI_COVER_HEIGHT", 2100))


@dataclass(frozen=True)
class TranslateConfig:
    """Vietnamese translation of public-domain source text.

    `mode` values:
      - `off`    : source is already Vietnamese, copy through unchanged
      - `gloss`  : deterministic glossary + number/abbreviation normalisation,
                   no MT. Safe default for public-domain texts that already
                   have a Vietnamese edition.
      - `command`: shell out to an MT command the operator configures
      - `http`   : POST to a translation endpoint (SACHNOI_MT_URL)
    """

    mode: str = field(default_factory=lambda: _get("SACHNOI_TRANSLATE_MODE", "off"))
    command: str = field(default_factory=lambda: _get("SACHNOI_MT_COMMAND", ""))
    url: str = field(default_factory=lambda: _get("SACHNOI_MT_URL", ""))
    model: str = field(default_factory=lambda: _get("SACHNOI_MT_MODEL", ""))
    batch_chars: int = field(default_factory=lambda: _get_int("SACHNOI_MT_BATCH_CHARS", 3000))
    timeout_sec: int = field(default_factory=lambda: _get_int("SACHNOI_MT_TIMEOUT", 120))


@dataclass(frozen=True)
class Config:
    tts: TTSConfig = field(default_factory=TTSConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    translate: TranslateConfig = field(default_factory=TranslateConfig)

    @classmethod
    def load(cls) -> "Config":
        return cls()


def repo_root() -> Path:
    return REPO_ROOT


def resolve(path: str | Path) -> Path:
    """Resolve a repo-relative path from a manifest against the repo root."""
    p = Path(path)
    return p if p.is_absolute() else REPO_ROOT / p