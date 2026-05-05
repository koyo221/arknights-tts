"""VOICEVOX smoke test (Phase 0.5 / 0.6).

Requires the VOICEVOX engine to be running at the URL in
``config/preset_config.json`` (default 127.0.0.1:50021). Start it with::

    & "%LOCALAPPDATA%/Microsoft/WinGet/Packages/HiroshibaKazuyuki.VOICEVOX.../VOICEVOX/vv-engine/run.exe"

Outputs ``data/smoke_out.flac`` and prints metadata.

Usage:
    uv run python scripts/smoke_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from arknights_tts.parser import Line
from arknights_tts.router import load_preset_config
from arknights_tts.tts import TTSDriver


def main() -> int:
    preset = load_preset_config(Path("config/preset_config.json"))
    print(f"engine: {preset.engine_url}")
    print(f"speaker: {preset.speaker_name} (style_id={preset.style_id})")
    print(f"style: {preset.style}")

    line = Line(
        kind="dialogue",
        speaker="アーミヤ",
        speaker_prefix="アーミヤ、",
        text="ドクター、起きてください。",
    )
    with TTSDriver(preset) as drv:
        for r in drv.synthesize_lines(
            [line],
            chapter="smoke",
            stage="smoke",
            dict_version="smoke",
        ):
            print(f"  flac: {r.flac_path}")
            print(f"  duration: {r.duration_ms} ms")
            print(f"  cached: {r.cached}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
