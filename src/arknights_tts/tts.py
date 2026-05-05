"""VOICEVOX engine driver: HTTP API client, line synthesis, FLAC conversion.

Synthesis is per-line: each line goes through ``audio_query`` -> ``synthesis``
-> WAV bytes, which are then re-encoded to FLAC via FFmpeg. The resulting
FLAC + duration is stored in the cache (see ``cache.py``).

The engine is expected to be running locally (default: 127.0.0.1:50021).
Start it manually with::

    & "%LOCALAPPDATA%/Microsoft/WinGet/Packages/...VOICEVOX.../vv-engine/run.exe"

We don't manage the engine lifecycle from Python -- if it isn't reachable,
``connect()`` raises and the user must start it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

import requests

from . import cache
from .parser import Line
from .router import PresetSpec

logger = logging.getLogger(__name__)


def _ffmpeg_bin() -> str:
    """Resolve the ffmpeg executable. Override via ARKNIGHTS_TTS_FFMPEG."""
    return _resolve_binary("ffmpeg", "ARKNIGHTS_TTS_FFMPEG")


def _ffprobe_bin() -> str:
    """Resolve ffprobe. Falls back to the ffmpeg dir if ffprobe isn't on PATH."""
    return _resolve_binary("ffprobe", env_var=None, sibling_of=_ffmpeg_bin())


def _resolve_binary(name: str, env_var: str | None, *, sibling_of: str | None = None) -> str:
    """Locate a Windows-friendly binary path.

    Search order:
    1. ``${env_var}`` if set
    2. ``shutil.which(name)`` -- standard PATH lookup
    3. Sibling of an already-resolved binary (e.g. ffprobe next to ffmpeg)
    4. Common Windows winget package install paths (Gyan.FFmpeg layout) --
       useful when ``ffmpeg`` was installed but the existing shell's PATH
       wasn't refreshed
    5. The bare name (lets subprocess raise FileNotFoundError with context)
    """
    if env_var:
        override = os.environ.get(env_var)
        if override:
            return override

    found = shutil.which(name)
    if found:
        return found

    if sibling_of:
        sibling = Path(sibling_of).with_name(
            f"{name}.exe" if os.name == "nt" else name
        )
        if sibling.exists():
            return str(sibling)

    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        winget_links = local / "Microsoft" / "WinGet" / "Links" / f"{name}.exe"
        if winget_links.exists():
            return str(winget_links)
        winget_pkgs = local / "Microsoft" / "WinGet" / "Packages"
        if winget_pkgs.exists():
            for pkg in winget_pkgs.glob("Gyan.FFmpeg*"):
                for build in pkg.glob("ffmpeg-*-full_build"):
                    candidate = build / "bin" / f"{name}.exe"
                    if candidate.exists():
                        return str(candidate)

    return name


@dataclass
class SynthResult:
    line: Line
    cache_key: str
    flac_path: Path
    duration_ms: int
    cached: bool


class VoicevoxClient:
    """Thin wrapper around the VOICEVOX HTTP API."""

    def __init__(self, base_url: str, *, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()

    def ping(self) -> str:
        r = self._session.get(f"{self.base_url}/version", timeout=self.timeout)
        r.raise_for_status()
        return r.text.strip().strip('"')

    def audio_query(self, text: str, *, style_id: int) -> dict:
        r = self._session.post(
            f"{self.base_url}/audio_query",
            params={"text": text, "speaker": style_id},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()

    def synthesis(self, query: dict, *, style_id: int) -> bytes:
        r = self._session.post(
            f"{self.base_url}/synthesis",
            params={"speaker": style_id, "enable_interrogative_upspeak": "true"},
            json=query,
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.content


class TTSDriver:
    """High-level driver: synthesize a sequence of Lines into the FLAC cache."""

    def __init__(
        self,
        preset: PresetSpec,
        *,
        ffmpeg: str | None = None,
    ) -> None:
        self.preset = preset
        self.ffmpeg = ffmpeg or _ffmpeg_bin()
        self._client = VoicevoxClient(preset.engine_url)

    def __enter__(self) -> "TTSDriver":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # nothing to release
        return None

    def connect(self) -> None:
        try:
            version = self._client.ping()
        except requests.RequestException as exc:
            raise RuntimeError(
                f"VOICEVOX engine not reachable at {self.preset.engine_url}: {exc}\n"
                f"Start the engine first (vv-engine/run.exe)."
            ) from exc
        logger.info("VOICEVOX engine version: %s", version)

    def synthesize_lines(
        self,
        lines: Sequence[Line],
        *,
        chapter: str,
        stage: str,
        dict_version: str | None,
        max_retries: int = 3,
    ) -> Iterator[SynthResult]:
        with cache.open_index() as conn:
            for i, line in enumerate(lines):
                if line.kind not in ("dialogue", "narration"):
                    continue
                text = self._render_text(line)
                if not text:
                    continue
                key = cache.compute_cache_key(
                    text, self.preset.cache_preset_label, self.preset.style
                )
                flac = cache.cache_path_for(key)

                if cache.exists(conn, key) and flac.exists():
                    yield SynthResult(line, key, flac, _read_duration_ms(flac), cached=True)
                    continue

                last_exc: Exception | None = None
                for attempt in range(1, max_retries + 1):
                    try:
                        duration = self._synthesize_one(text, flac)
                        cache.upsert(
                            conn,
                            key=key,
                            text=text,
                            preset=self.preset.cache_preset_label,
                            style=self.preset.style,
                            chapter=chapter,
                            stage=stage,
                            line_no=i,
                            duration_ms=duration,
                            dict_version=dict_version,
                        )
                        yield SynthResult(line, key, flac, duration, cached=False)
                        last_exc = None
                        break
                    except Exception as exc:
                        last_exc = exc
                        logger.warning(
                            "synth attempt %d/%d failed for %s line %d: %s",
                            attempt, max_retries, stage, i, exc,
                        )
                        time.sleep(min(2**attempt, 10))
                if last_exc is not None:
                    logger.error(
                        "synth permanently failed for %s line %d (%r): %s",
                        stage, i, text[:40], last_exc,
                    )

    @staticmethod
    def _render_text(line: Line) -> str:
        # Speaker prefix from parser is not rendered (decision overturned after
        # listening to main_00.m4b -- prefixes felt redundant). Parser still
        # annotates ``speaker_prefix`` in case we want to revisit.
        if line.kind in ("dialogue", "narration"):
            return (line.text or "").strip()
        return ""

    def _synthesize_one(self, text: str, flac_path: Path) -> int:
        query = self._client.audio_query(text, style_id=self.preset.style_id)
        # Apply our style overrides; trim leading/trailing silence (we manage
        # silence at the concat layer).
        for k, v in self.preset.style.items():
            query[k] = v
        query["prePhonemeLength"] = 0.0
        query["postPhonemeLength"] = 0.0

        wav_bytes = self._client.synthesis(query, style_id=self.preset.style_id)
        with tempfile.TemporaryDirectory() as td:
            wav_path = Path(td) / "out.wav"
            wav_path.write_bytes(wav_bytes)
            duration = _read_duration_ms(wav_path)
            flac_path.parent.mkdir(parents=True, exist_ok=True)
            _convert_wav_to_flac(wav_path, flac_path, ffmpeg=self.ffmpeg)
        return duration


def _convert_wav_to_flac(wav: Path, flac: Path, *, ffmpeg: str) -> None:
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(wav), "-c:a", "flac", str(flac)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()}")


def _read_duration_ms(audio_path: Path) -> int:
    suffix = audio_path.suffix.lower()
    if suffix == ".wav":
        try:
            with wave.open(str(audio_path), "rb") as w:
                frames = w.getnframes()
                rate = w.getframerate()
                if rate <= 0:
                    return 0
                return int(frames * 1000 / rate)
        except wave.Error:
            return 0
    if suffix == ".flac":
        try:
            out = subprocess.run(
                [
                    _ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path),
                ],
                capture_output=True, text=True, check=True,
            )
            return int(float(out.stdout.strip()) * 1000)
        except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
            return 0
    return 0
