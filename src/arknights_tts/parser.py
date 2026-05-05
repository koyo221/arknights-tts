"""Story script parser. Converts upstream tagged ``.txt`` into a Line list (JSON).

Tag formats observed in upstream data (verified 2026-05-05):

* ``[name="X"]text`` -- speaker tag followed by dialogue text on the same line.
  ``X`` may contain quotes, spaces, etc.
* ``[TagName(arg1=val1, arg2=val2)]`` -- control tag with arguments.
* ``[TagName]`` -- bare control tag (often a "reset" or end-of-effect marker).
* Plain text (no brackets) -- narration.

Tag names are case-insensitive (the upstream mixes ``[Dialog]`` and
``[stopmusic]`` etc.). Argument values may be quoted (``key="value"``) or
unquoted (``time=2``); commas inside quoted strings are not expected.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Literal

logger = logging.getLogger(__name__)

LineKind = Literal["narration", "dialogue", "pause"]

# Order matters: try [name="X"] first, then bracketed control tags, then plain text.
NAME_RE = re.compile(r'^\s*\[name="([^"]*)"\]\s*(.*)$')
CONTROL_RE = re.compile(r"^\s*\[([A-Za-z][A-Za-z0-9_]*)(?:\(([^)]*)\))?\]\s*(.*)$")
ARG_RE = re.compile(r'(\w+)\s*=\s*("[^"]*"|[^,)\s]+)')

PAUSE_BLOCKER_MS = 1500
PAUSE_DELAY_DEFAULT_MS = 500  # for tags like [Character], [Image] without explicit time

# Subtitle routing (Phase 2: PLAN.md Subtitle handling)
SUBTITLE_LOCATION_MAX_CHARS = 12  # below this -> "場所、" template


@dataclass
class Line:
    kind: LineKind
    speaker: str | None = None  # normalized speaker (post-aliases) for dialogue
    speaker_prefix: str | None = None  # e.g. "アーミヤ、" if speaker just changed
    text: str | None = None
    pause_ms: int | None = None
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in (None, {})}


@dataclass
class SpeakerAliases:
    aliases: dict[str, str]
    anonymous: set[str]
    voice_only: set[str]
    mob_pattern: re.Pattern | None

    @classmethod
    def load(cls, path: Path) -> "SpeakerAliases":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            aliases=data.get("aliases", {}),
            anonymous=set(data.get("anonymous", [])),
            voice_only=set(data.get("voice_only", [])),
            mob_pattern=re.compile(data["mob_pattern"]) if data.get("mob_pattern") else None,
        )

    def resolve(self, raw_name: str) -> tuple[str, str]:
        """Return (normalized_name, kind) where kind in
        ``{"named", "anonymous", "voice_only", "mob"}``.

        Also strips emotional-uncertainty suffixes (``？``, ``?``, ``……``, ``…``)
        on a second pass when the original form is unknown — this folds
        ``アーミヤ？`` into ``アーミヤ`` automatically without requiring a manual
        alias entry. Real names that end in those characters would be misfiled,
        but we have no examples in the upstream main story.
        """
        name = raw_name.strip()
        if name in self.anonymous:
            return name, "anonymous"
        if name in self.voice_only:
            return name, "voice_only"
        if name in self.aliases:
            return self.aliases[name], "named"
        if self.mob_pattern and (m := self.mob_pattern.match(name)):
            return m.group(1), "mob"

        stripped = re.sub(r"[？\?][！\!]*$|[…]+$", "", name).strip()
        if stripped and stripped != name:
            if stripped in self.aliases:
                return self.aliases[stripped], "named"
            if self.mob_pattern and (m := self.mob_pattern.match(stripped)):
                return m.group(1), "mob"
            return stripped, "named"

        return name, "named"


@dataclass
class ParseStats:
    lines_in: int = 0
    narration: int = 0
    dialogue: int = 0
    pauses: int = 0
    decisions_skipped: int = 0
    unknown_tags: dict[str, int] = field(default_factory=dict)


# -- helpers --------------------------------------------------------------------------

def _parse_args(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    args = {}
    for m in ARG_RE.finditer(raw):
        key, val = m.group(1), m.group(2)
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        args[key] = val
    return args


def _strip_inline_substitutions(text: str) -> str:
    """Replace ``Dr.{@nickname}`` and similar templates with sane defaults.

    Game's runtime substitutes the player's chosen nickname here. For audio
    purposes we want a stable string — use ``"ドクター"`` (the in-universe
    canonical name).
    """
    text = re.sub(r"Dr\.\{@nickname\}", "ドクター", text)
    text = re.sub(r"\{@nickname\}", "ドクター", text)
    text = re.sub(r"\{@cn=([^}]*)\}", r"\1", text)  # CN-name override -> use as-is
    text = re.sub(r"\{@[^}]+\}", "", text)  # other unknown templates -> drop
    return text


def _normalize_text(text: str) -> str:
    text = _strip_inline_substitutions(text)
    return text.strip()


# -- core parser ----------------------------------------------------------------------

def parse_file(
    path: Path,
    aliases: SpeakerAliases,
    *,
    chapter_id: str | None = None,
    stage_id: str | None = None,
    decision_log: Path | None = None,
) -> tuple[list[Line], ParseStats]:
    """Parse a single .txt into a list of Lines + stats.

    ``chapter_id`` / ``stage_id`` are recorded in skipped-decision entries for
    cross-referencing. They do not influence the Lines themselves.
    """
    raw = path.read_text(encoding="utf-8")
    lines = list(_iter_lines(raw, aliases, chapter_id, stage_id, decision_log))
    lines = _apply_speaker_prefix(lines, aliases)

    stats = ParseStats(lines_in=len(raw.splitlines()))
    for ln in lines:
        if ln.kind == "narration":
            stats.narration += 1
        elif ln.kind == "dialogue":
            stats.dialogue += 1
        elif ln.kind == "pause":
            stats.pauses += 1
    return lines, stats


def _iter_lines(
    raw: str,
    aliases: SpeakerAliases,
    chapter_id: str | None,
    stage_id: str | None,
    decision_log: Path | None,
) -> Iterator[Line]:
    for raw_line in raw.splitlines():
        if not raw_line.strip():
            continue

        # 1) [name="X"]text -- dialogue
        m = NAME_RE.match(raw_line)
        if m:
            speaker_raw, text = m.group(1), m.group(2)
            text = _normalize_text(text)
            if not text:
                continue
            normalized, kind = aliases.resolve(speaker_raw)
            yield Line(
                kind="dialogue",
                speaker=normalized,
                text=text,
                meta={"raw_speaker": speaker_raw, "speaker_kind": kind},
            )
            continue

        # 2) [TagName(args)] or [TagName]
        m = CONTROL_RE.match(raw_line)
        if m:
            tag_raw = m.group(1)
            args_raw = m.group(2)
            tail = m.group(3)
            tag = tag_raw.lower()
            args = _parse_args(args_raw)

            if tag == "decision":
                _log_decision(args, chapter_id, stage_id, decision_log)
                continue

            if tag == "predicate":
                # companion to Decision; no audio implication
                continue

            if tag == "subtitle":
                # bare [Subtitle] = end-of-subtitle marker, ignore
                if not args.get("text"):
                    continue
                yield from _emit_subtitle(args["text"])
                continue

            if tag == "blocker":
                yield Line(kind="pause", pause_ms=PAUSE_BLOCKER_MS, meta={"tag": tag})
                continue

            if tag in {"delay", "daley", "delau"}:  # typos in upstream
                seconds = args.get("time")
                ms = int(float(seconds) * 1000) if seconds else PAUSE_DELAY_DEFAULT_MS
                yield Line(kind="pause", pause_ms=ms, meta={"tag": "delay"})
                continue

            if tag == "multiline":
                # Same shape as [name="X"]text but the speaker is in the args.
                # We treat each multiline call as its own dialogue line; the
                # speaker-prefix logic naturally suppresses the prefix on
                # consecutive same-speaker lines.
                speaker_raw = args.get("name", "")
                text_clean = _normalize_text(tail)
                if not text_clean or not speaker_raw:
                    continue
                normalized, kind = aliases.resolve(speaker_raw)
                yield Line(
                    kind="dialogue",
                    speaker=normalized,
                    text=text_clean,
                    meta={
                        "raw_speaker": speaker_raw,
                        "speaker_kind": kind,
                        "tag": "multiline",
                    },
                )
                continue

            if tag == "sticker":
                # Stickers are used for two distinct purposes:
                # 1. On-screen captions / narration (chapter 13's intro etc.)
                # 2. Bare ``[Sticker(id="st1")]`` end-of-effect markers
                # The text-bearing ones are story content.
                raw_text = args.get("text", "")
                if raw_text.startswith("\\n"):  # continuation marker
                    raw_text = raw_text[2:]
                text_clean = _normalize_text(raw_text)
                if text_clean:
                    yield Line(
                        kind="narration",
                        text=text_clean,
                        meta={"tag": "sticker"},
                    )
                continue

            if tag in {"playmusic", "stopmusic", "playsound", "stopsound", "image",
                       "background", "imagetween", "character", "charslot",
                       "stickerclear", "dialog", "header"}:
                # Visual / audio-effect tags. No story content.
                continue

            # Unknown tag; if the line had trailing text after the tag (e.g.
            # something like [???]some text), treat as narration.
            tail_clean = _normalize_text(tail)
            if tail_clean:
                yield Line(
                    kind="narration",
                    text=tail_clean,
                    meta={"unknown_tag": tag},
                )
            else:
                logger.debug("unknown tag skipped: %s", raw_line.strip()[:80])
                yield Line(kind="pause", pause_ms=0, meta={"unknown_tag": tag})
            continue

        # 3) plain text -- narration
        text = _normalize_text(raw_line)
        if text:
            yield Line(kind="narration", text=text)


def _emit_subtitle(text: str) -> Iterator[Line]:
    """Subtitle routing per PLAN.md Phase 2 (5).

    Short text -> "場所、{text}" templated narration.
    Long text  -> raw narration.
    """
    text = text.strip().strip('"').strip("「」『』")
    if not text:
        return
    if len(text) <= SUBTITLE_LOCATION_MAX_CHARS:
        yield Line(
            kind="narration",
            text=f"場所、{text}",
            meta={"subtitle": "location"},
        )
    else:
        yield Line(
            kind="narration",
            text=text,
            meta={"subtitle": "scene"},
        )


def _log_decision(
    args: dict,
    chapter_id: str | None,
    stage_id: str | None,
    decision_log: Path | None,
) -> None:
    if decision_log is None:
        return
    decision_log.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "chapter": chapter_id,
        "stage": stage_id,
        "options": args.get("options"),
        "values": args.get("values"),
    }
    with decision_log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# -- speaker prefix logic -------------------------------------------------------------

def _apply_speaker_prefix(lines: list[Line], aliases: SpeakerAliases) -> list[Line]:
    """Insert speaker_prefix on dialogue lines per the b-rule (PLAN.md Phase 2).

    Rule:
    * Prefix only when the speaker just changed.
    * Always prefix the first dialogue of a stage.
    * Always prefix after a narration insertion.
    * Always prefix after a scene-change pause (Blocker / long delay >= 1000ms).
    * anonymous and voice_only speakers: never prefix.
    """
    last_speaker: str | None = None
    reset_pending = True  # prefix the first dialogue
    for ln in lines:
        if ln.kind == "narration":
            reset_pending = True
            continue
        if ln.kind == "pause":
            if (ln.pause_ms or 0) >= 1000:
                reset_pending = True
            continue
        if ln.kind != "dialogue":
            continue

        speaker_kind = ln.meta.get("speaker_kind", "named")
        if speaker_kind in {"anonymous", "voice_only"}:
            ln.speaker_prefix = None
            last_speaker = None
            reset_pending = True
            continue

        speaker = ln.speaker
        if reset_pending or speaker != last_speaker:
            ln.speaker_prefix = f"{speaker}、"
        else:
            ln.speaker_prefix = None
        last_speaker = speaker
        reset_pending = False
    return lines


# -- public entry ---------------------------------------------------------------------

def parse_chapter(
    chapter_index: dict,
    chapter_id: str,
    *,
    upstream_dir: Path,
    aliases: SpeakerAliases,
    output_dir: Path,
    decision_log: Path,
) -> dict[str, ParseStats]:
    """Parse all stages of a single chapter, write per-stage JSON files."""
    chapter = next(
        (c for c in chapter_index["chapters"] if c["chapter_id"] == chapter_id), None
    )
    if chapter is None:
        raise KeyError(chapter_id)

    out_dir = output_dir / chapter_id
    out_dir.mkdir(parents=True, exist_ok=True)
    stats_by_stage: dict[str, ParseStats] = {}

    for stage in chapter["stages"]:
        sources: list[tuple[str, str | None]] = []
        if stage["kind"] == "battle":
            if stage["beg"]:
                sources.append(("beg", stage["beg"]))
            if stage["end"]:
                sources.append(("end", stage["end"]))
        else:
            if stage["story"]:
                sources.append((stage["kind"], stage["story"]))

        for tag, rel in sources:
            if rel is None:
                continue
            src = upstream_dir / rel
            lines, stats = parse_file(
                src,
                aliases,
                chapter_id=chapter_id,
                stage_id=stage["id"],
                decision_log=decision_log,
            )
            out_path = out_dir / f"{stage['id']}_{tag}.json"
            out_path.write_text(
                json.dumps(
                    {
                        "chapter_id": chapter_id,
                        "stage_id": stage["id"],
                        "stage_kind": stage["kind"],
                        "source": rel,
                        "part": tag,
                        "lines": [ln.to_dict() for ln in lines],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            stats_by_stage[f"{stage['id']}_{tag}"] = stats

    return stats_by_stage
