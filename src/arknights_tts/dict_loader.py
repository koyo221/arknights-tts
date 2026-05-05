"""Word-dict CSV loader and VOICEVOX user-dict synchronizer (PLAN.md Phase 5).

Source of truth: ``config/word_dict.csv``.
VOICEVOX side: managed via the engine's ``/user_dict`` HTTP API. We tag every
entry we own with the marker ``[arknights_tts]`` in its ``surface`` field's
metadata so we can identify and replace just our entries on sync, leaving
manually-added entries from the GUI alone.

VOICEVOX user-dict entry shape (from ``/user_dict`` JSON):

    {
        "<uuid>": {
            "surface": "アーミヤ",
            "priority": 5,                # 0..10
            "context_id": ...,
            "part_of_speech": "固有名詞",
            "yomi": "アーミヤ",
            "pronunciation": "アーミヤ",
            "accent_type": 1,
            "mora_count": 4,
            "accent_associative_rule": "*"
        },
        ...
    }
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import requests

logger = logging.getLogger(__name__)

MANAGED_TAG_PREFIX = "[arknights_tts]"  # written into pronunciation_label, see notes


@dataclass
class WordEntry:
    word: str
    reading: str
    priority: int = 5
    pos: str = "固有名詞"

    @classmethod
    def from_row(cls, row: dict) -> "WordEntry":
        return cls(
            word=(row.get("word") or "").strip(),
            reading=(row.get("reading") or "").strip(),
            priority=int(row.get("priority") or 5),
            pos=(row.get("pos") or "固有名詞").strip(),
        )


def load_csv(path: Path) -> list[WordEntry]:
    if not path.exists():
        return []
    entries: list[WordEntry] = []
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            entry = WordEntry.from_row(row)
            if entry.word:
                entries.append(entry)
    return entries


def diff_csv_vs_csv(
    old: Path, new: Path
) -> tuple[list[WordEntry], list[tuple[WordEntry, WordEntry]], list[WordEntry]]:
    old_map = {e.word: e for e in load_csv(old)} if old.exists() else {}
    new_map = {e.word: e for e in load_csv(new)} if new.exists() else {}

    added = [e for word, e in new_map.items() if word not in old_map]
    removed = [e for word, e in old_map.items() if word not in new_map]
    modified = [
        (old_map[w], new_map[w])
        for w in new_map
        if w in old_map and (
            old_map[w].reading != new_map[w].reading
            or old_map[w].priority != new_map[w].priority
            or old_map[w].pos != new_map[w].pos
        )
    ]
    return added, modified, removed


# -- VOICEVOX user-dict sync --------------------------------------------------


@dataclass
class _ManagedEntry:
    """A user-dict entry recognised as managed by us. We mark managed entries
    by appending ``MANAGED_TAG_PREFIX`` to their ``surface`` field. The sync
    logic strips the marker on read and re-applies on write."""

    uuid: str
    word: str  # surface without the marker
    reading: str
    priority: int

    @property
    def marked_surface(self) -> str:
        return f"{self.word} {MANAGED_TAG_PREFIX}"


def fetch_user_dict(engine_url: str) -> dict:
    r = requests.get(f"{engine_url.rstrip('/')}/user_dict", timeout=30)
    r.raise_for_status()
    return r.json()


def list_managed(user_dict: dict) -> dict[str, _ManagedEntry]:
    """Return our managed entries keyed by their UUID."""
    out: dict[str, _ManagedEntry] = {}
    for uuid, entry in user_dict.items():
        surface = entry.get("surface") or ""
        if MANAGED_TAG_PREFIX not in surface:
            continue
        word = surface.replace(MANAGED_TAG_PREFIX, "").strip()
        out[uuid] = _ManagedEntry(
            uuid=uuid,
            word=word,
            reading=entry.get("pronunciation") or entry.get("yomi") or "",
            priority=int(entry.get("priority") or 5),
        )
    return out


def sync_to_voicevox(
    entries: Iterable[WordEntry],
    *,
    engine_url: str,
) -> dict[str, int]:
    """Push ``entries`` into the engine's user dict (managed area only).

    Returns a count breakdown ``{"added": N, "updated": M, "removed": K}``.
    Existing non-managed user-dict entries (added manually in the GUI) are not
    touched.
    """
    base = engine_url.rstrip("/")
    current = fetch_user_dict(engine_url)
    managed = list_managed(current)
    by_word = {e.word: e for e in managed.values()}

    target = list(entries)
    target_words = {e.word for e in target}

    counts = {"added": 0, "updated": 0, "removed": 0}

    # delete managed entries no longer present in CSV
    for uuid, m in list(managed.items()):
        if m.word not in target_words:
            r = requests.delete(f"{base}/user_dict_word/{uuid}", timeout=30)
            r.raise_for_status()
            counts["removed"] += 1
            del managed[uuid]
            del by_word[m.word]

    # add or update
    for entry in target:
        existing = by_word.get(entry.word)
        if existing and existing.reading == entry.reading and existing.priority == entry.priority:
            continue
        if existing is not None:
            r = requests.put(
                f"{base}/user_dict_word/{existing.uuid}",
                params={
                    "surface": f"{entry.word} {MANAGED_TAG_PREFIX}",
                    "pronunciation": _to_katakana(entry.reading),
                    "accent_type": _guess_accent_type(entry.reading),
                    "priority": entry.priority,
                },
                timeout=30,
            )
            r.raise_for_status()
            counts["updated"] += 1
        else:
            r = requests.post(
                f"{base}/user_dict_word",
                params={
                    "surface": f"{entry.word} {MANAGED_TAG_PREFIX}",
                    "pronunciation": _to_katakana(entry.reading),
                    "accent_type": _guess_accent_type(entry.reading),
                    "priority": entry.priority,
                },
                timeout=30,
            )
            r.raise_for_status()
            counts["added"] += 1

    return counts


def import_from_voicevox(*, engine_url: str) -> list[WordEntry]:
    """Read managed entries from the engine into a list (round-trip helper)."""
    user_dict = fetch_user_dict(engine_url)
    managed = list_managed(user_dict)
    return [
        WordEntry(word=m.word, reading=m.reading, priority=m.priority)
        for m in managed.values()
    ]


def _guess_accent_type(reading: str) -> int:
    """Best-effort default. VOICEVOX requires an accent type per word; for
    proper-noun readings, "drop after first mora" (1) is a sane default
    that the user can override later via the GUI per word."""
    return 1


def _to_katakana(reading: str) -> str:
    """Convert any hiragana characters in ``reading`` to katakana.

    VOICEVOX's user-dict API requires katakana for the pronunciation field
    (422 otherwise). The CSV may contain hiragana for readability, so we
    normalize on the way out.
    """
    out_chars: list[str] = []
    for ch in reading:
        cp = ord(ch)
        if 0x3041 <= cp <= 0x3096:
            out_chars.append(chr(cp + 0x60))
        else:
            out_chars.append(ch)
    return "".join(out_chars)
