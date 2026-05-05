"""Concatenate per-line FLACs into a chapter M4B + SRT (PLAN.md Phase 6).

Pipeline for one chapter:

1. Walk the chapter's parsed JSON files (in stage order).
2. For each Line, resolve its cache FLAC via ``cache.compute_cache_key``.
3. Insert silence between lines per the rules table (Phase 6).
4. Build a single FFmpeg concat-demuxer list and let FFmpeg produce the M4B
   with embedded chapter markers.
5. Emit a parallel SRT with timestamps accumulated from FLAC durations + silence.

Silence is generated once per unique duration into ``data/cache/silence/`` and
reused. This avoids re-encoding silence on every build.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from . import cache, router
from .parser import Line
from .tts import _ffmpeg_bin, _read_duration_ms

logger = logging.getLogger(__name__)

SILENCE_DIR = cache.CACHE_DIR / "silence"

# Silence durations (ms) live in ``preset_config.json``'s ``pacing_ms`` block
# (see ``router.DEFAULT_PACING_MS`` for the keys + defaults). Access them via
# ``preset.pace("<key>")``.


# -- silence generation --------------------------------------------------------


def ensure_silence(duration_ms: int, *, ffmpeg: str | None = None) -> Path:
    SILENCE_DIR.mkdir(parents=True, exist_ok=True)
    out = SILENCE_DIR / f"silence_{duration_ms}.flac"
    if out.exists():
        return out
    seconds = duration_ms / 1000.0
    cmd = [
        ffmpeg or _ffmpeg_bin(), "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono",
        "-t", f"{seconds}", "-c:a", "flac", str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg silence gen failed: {proc.stderr.strip()}")
    return out


# -- concat planning -----------------------------------------------------------


@dataclass
class ConcatItem:
    flac: Path
    duration_ms: int
    line: Line | None  # None for silence
    chapter_marker: str | None = None  # set on the first item of a chapter section
    text_for_srt: str | None = None


@dataclass
class StageInfo:
    stage_id: str
    kind: str  # "battle" / "story" / "recap"
    parts: list[str]  # e.g. ["beg", "end"] or ["story"]


def _load_chapter_lines(
    chapter_id: str,
    parsed_dir: Path,
    chapter_index: dict,
) -> list[tuple[StageInfo, str, list[Line]]]:
    """Return [(stage_info, part, lines), ...] in narrative order."""
    chapter = next(
        (c for c in chapter_index["chapters"] if c["chapter_id"] == chapter_id), None
    )
    if chapter is None:
        raise KeyError(chapter_id)
    out: list[tuple[StageInfo, str, list[Line]]] = []
    for stage in chapter["stages"]:
        parts: list[str] = []
        if stage["kind"] == "battle":
            if stage["beg"]:
                parts.append("beg")
            if stage["end"]:
                parts.append("end")
        else:
            parts.append(stage["kind"])
        info = StageInfo(stage_id=stage["id"], kind=stage["kind"], parts=parts)
        for part in parts:
            json_path = parsed_dir / chapter_id / f"{stage['id']}_{part}.json"
            if not json_path.exists():
                logger.warning("missing parsed JSON: %s", json_path)
                continue
            data = json.loads(json_path.read_text(encoding="utf-8"))
            lines = [Line(**ln) for ln in data["lines"]]
            out.append((info, part, lines))
    return out


def _silence_between(
    prev: Line | None,
    curr: Line,
    *,
    at_part_boundary: bool,
    preset: router.PresetSpec,
) -> int:
    if at_part_boundary:
        return 0  # part-boundary silence is added explicitly by caller
    if prev is None:
        return 0
    if prev.kind == "pause":
        return prev.pause_ms or 0
    if curr.kind == "pause":
        return 0  # the pause line itself contributes
    if prev.kind == "narration" or curr.kind == "narration":
        return preset.pace("narration_dialogue")
    if prev.kind == "dialogue" and curr.kind == "dialogue":
        if prev.speaker == curr.speaker:
            return preset.pace("same_speaker")
        return preset.pace("speaker_switch")
    return preset.pace("same_speaker")


def _stage_label(info: StageInfo, part: str) -> str:
    """Human-readable chapter marker label, e.g. ``1-1 戦闘前``."""
    short_id = info.stage_id.replace("main_", "").replace("st_", "S-")
    # Trim leading zero for display: 01-01 -> 1-1
    if "-" in short_id:
        a, b = short_id.split("-", 1)
        try:
            short_id = f"{int(a)}-{int(b) if b.isdigit() else b}"
        except ValueError:
            pass
    if info.kind == "battle":
        suffix = "戦闘前" if part == "beg" else "戦闘後"
        return f"{short_id} {suffix}"
    if info.kind == "recap":
        return f"{short_id} 振り返り"
    return f"{short_id} ストーリー"


def plan_chapter(
    chapter_id: str,
    *,
    parsed_dir: Path,
    chapter_index: dict,
    preset: router.PresetSpec,
) -> list[ConcatItem]:
    """Build the ordered ConcatItem list for one chapter (no FFmpeg yet)."""
    stages = _load_chapter_lines(chapter_id, parsed_dir, chapter_index)
    items: list[ConcatItem] = []

    prev_stage_id: str | None = None
    prev_part: str | None = None

    with cache.open_index() as conn:
        for info, part, lines in stages:
            # --- inter-stage / inter-part silence ---
            if prev_stage_id is None:
                pass  # no leading silence
            elif info.stage_id != prev_stage_id:
                _emit_silence(items, preset.pace("stage_gap"))
            elif prev_part == "beg" and part == "end":
                _emit_silence(items, preset.pace("beg_end"))

            # mark first audible item with this stage's chapter label
            chapter_marker = _stage_label(info, part)
            first_item_index = len(items)

            prev_line: Line | None = None
            for line in lines:
                if line.kind == "pause":
                    pause = line.pause_ms or 0
                    if pause > 0:
                        _emit_silence(items, pause)
                    prev_line = line
                    continue

                gap = _silence_between(prev_line, line, at_part_boundary=False, preset=preset)
                if gap > 0:
                    _emit_silence(items, gap)

                text = _render_line_text(line)
                if not text:
                    prev_line = line
                    continue
                key = cache.compute_cache_key(text, preset.cache_preset_label, preset.style)
                flac = cache.cache_path_for(key)
                if not flac.exists():
                    logger.warning(
                        "no cache flac for %s line: %r (key=%s)",
                        info.stage_id, text[:40], key,
                    )
                    prev_line = line
                    continue
                # duration may be unknown if previously not recorded
                row = conn.execute(
                    "SELECT duration_ms FROM wav_cache WHERE hash = ?", (key,),
                ).fetchone()
                duration = (row["duration_ms"] if row else None) or _read_duration_ms(flac)

                items.append(ConcatItem(
                    flac=flac,
                    duration_ms=duration,
                    line=line,
                    text_for_srt=text,
                ))
                prev_line = line

            # promote chapter marker onto first item of this stage section
            if first_item_index < len(items):
                items[first_item_index].chapter_marker = chapter_marker

            prev_stage_id = info.stage_id
            prev_part = part

    return items


def _emit_silence(items: list[ConcatItem], duration_ms: int) -> None:
    if duration_ms <= 0:
        return
    silence = ensure_silence(duration_ms)
    items.append(ConcatItem(flac=silence, duration_ms=duration_ms, line=None))


def _render_line_text(line: Line) -> str:
    # Mirror tts.TTSDriver._render_text (no speaker prefix).
    if line.kind in ("dialogue", "narration"):
        return (line.text or "").strip()
    return ""


# -- FFmpeg invocation ---------------------------------------------------------


def build_chapter(
    chapter_id: str,
    *,
    parsed_dir: Path,
    chapter_index: dict,
    preset: router.PresetSpec,
    output_dir: Path,
    chapter_title: str | None = None,
    aac_bitrate: str = "96k",
    ffmpeg: str | None = None,
) -> tuple[Path, Path]:
    """Render one chapter to ``<chapter_id>.m4b`` + ``<chapter_id>.srt``.

    Returns (m4b_path, srt_path).
    """
    items = plan_chapter(
        chapter_id,
        parsed_dir=parsed_dir,
        chapter_index=chapter_index,
        preset=preset,
    )
    if not items:
        raise RuntimeError(f"chapter {chapter_id}: no audible items")

    output_dir.mkdir(parents=True, exist_ok=True)
    m4b_path = output_dir / f"{chapter_id}.m4b"
    srt_path = output_dir / f"{chapter_id}.srt"

    with tempfile.TemporaryDirectory() as td:
        list_path = Path(td) / "concat.list"
        meta_path = Path(td) / "chapters.txt"
        _write_concat_list(items, list_path)
        _write_metadata(items, meta_path, chapter_title=chapter_title or chapter_id)

        cmd = [
            ffmpeg or _ffmpeg_bin(), "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-i", str(meta_path),
            "-map_metadata", "1",
            "-map_chapters", "1",
            "-c:a", "aac", "-b:a", aac_bitrate,
            "-ac", "1",
            "-f", "mp4",
            str(m4b_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg concat failed: {proc.stderr.strip()}")

    _write_srt(items, srt_path)
    return m4b_path, srt_path


def _write_concat_list(items: list[ConcatItem], path: Path) -> None:
    lines = []
    for item in items:
        flac_str = str(item.flac.resolve()).replace("\\", "/").replace("'", r"\'")
        lines.append(f"file '{flac_str}'")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_metadata(
    items: list[ConcatItem], path: Path, *, chapter_title: str
) -> None:
    """Write FFMETADATA1 with ;album, ;title, and per-stage chapter markers."""
    lines = [";FFMETADATA1", f"title={chapter_title}", "album=アークナイツ メインストーリー"]

    cursor_ms = 0
    chap_starts: list[tuple[int, str]] = []  # (start_ms, label)
    for item in items:
        if item.chapter_marker:
            chap_starts.append((cursor_ms, item.chapter_marker))
        cursor_ms += item.duration_ms
    total_ms = cursor_ms

    for i, (start_ms, label) in enumerate(chap_starts):
        end_ms = chap_starts[i + 1][0] if i + 1 < len(chap_starts) else total_ms
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={start_ms}")
        lines.append(f"END={end_ms}")
        lines.append(f"title={label}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_srt(items: list[ConcatItem], path: Path) -> None:
    cursor_ms = 0
    entries: list[str] = []
    n = 0
    for item in items:
        start_ms = cursor_ms
        end_ms = cursor_ms + item.duration_ms
        cursor_ms = end_ms
        if item.line is None or not item.text_for_srt:
            continue
        n += 1
        entries.append(
            f"{n}\n"
            f"{_format_srt_ts(start_ms)} --> {_format_srt_ts(end_ms)}\n"
            f"{item.text_for_srt}\n"
        )
    path.write_text("\n".join(entries) + "\n", encoding="utf-8")


def _format_srt_ts(ms: int) -> str:
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms_rem = divmod(rem, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms_rem:03}"
