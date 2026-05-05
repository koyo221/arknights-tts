"""Voice router. Maps a parsed Line to a VOICEVOX speaker + style.

Per the Phase 3 design (single voice, character identity carried by the
speaker prefix in the text), this resolver returns the same
``(style_id, style)`` for every line. It exists as its own module so future
expansion (per-kind or per-speaker overrides) can be added without touching
synthesis.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .parser import Line


# Default silence durations (ms) inserted between audio segments. Override in
# preset_config.json under a ``pacing_ms`` block. See ``concat.plan_chapter``
# for where each value is applied.
DEFAULT_PACING_MS: dict[str, int] = {
    "same_speaker": 500,
    "speaker_switch": 600,
    "narration_dialogue": 750,
    "scene": 1500,
    "beg_end": 2500,
    "stage_gap": 4500,
}


@dataclass(frozen=True)
class PresetSpec:
    engine: str
    engine_url: str
    speaker_name: str
    style_id: int
    style: dict[str, float]
    pacing_ms: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_PACING_MS))

    @property
    def cache_preset_label(self) -> str:
        """Stable string used in the cache key. Tied to the voice identity, not
        the URL, so re-pointing the engine doesn't invalidate the cache."""
        return f"{self.engine}:{self.style_id}"

    @property
    def style_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.style, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]

    def pace(self, key: str) -> int:
        """Return the silence duration (ms) for ``key``, with default fallback."""
        return int(self.pacing_ms.get(key, DEFAULT_PACING_MS[key]))


def load_preset_config(path: Path) -> PresetSpec:
    data = json.loads(path.read_text(encoding="utf-8"))
    pacing = dict(DEFAULT_PACING_MS)
    pacing.update({k: int(v) for k, v in (data.get("pacing_ms") or {}).items()})
    return PresetSpec(
        engine=data.get("engine", "voicevox"),
        engine_url=data.get("engine_url", "http://127.0.0.1:50021"),
        speaker_name=data["speaker_name"],
        style_id=int(data["style_id"]),
        style=dict(data["style"]),
        pacing_ms=pacing,
    )


def resolve_preset(line: Line, config: PresetSpec) -> PresetSpec:
    """Currently a constant function; ``line`` is unused but accepted to keep
    the signature future-proof for per-line overrides."""
    del line
    return config
