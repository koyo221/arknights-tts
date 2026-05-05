"""Generate config/chapter_index.json from the upstream Arknights data clone.

The upstream layout (verified 2026-05-05 against the archived repo):

* ``ja_JP/gamedata/story/obt/main/level_main_<XX>-<YY>_<beg|end>.txt``
  -- battle stage scripts. ``XX`` is a 2-digit episode number, ``YY`` is a
  2-digit stage number. Some stages only have ``beg`` or only ``end``.
* ``ja_JP/gamedata/story/obt/main/level_st_<XX>-<YY>.txt`` -- story-only
  interludes within a chapter (no battle, no beg/end split). Some chapters
  end on these (chapter_table.json's ``chapterEndStageId`` references
  ``st_NN-YY`` for chapters 1-3).
* ``ja_JP/gamedata/story/obt/main/level_main_<X>_chapter_recap.txt`` -- one-off
  recap files (only ``main_9`` so far). Treated as a special stage.
* ``ja_JP/gamedata/excel/zone_table.json`` -- ``zones[main_<N>]`` (no zero
  padding) provides the displayed episode title (``zoneNameSecond``).
* ``ja_JP/gamedata/excel/chapter_table.json`` -- larger story arcs grouping
  multiple zones (4 arcs as of archive). Not used here for indexing.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

UPSTREAM_DIR = Path("data/upstream")
STORY_DIR = UPSTREAM_DIR / "ja_JP" / "gamedata" / "story" / "obt" / "main"
EXCEL_DIR = UPSTREAM_DIR / "ja_JP" / "gamedata" / "excel"
OUTPUT_PATH = Path("config/chapter_index.json")

MAIN_RE = re.compile(r"^level_main_(\d{2})-(\d{2})_(beg|end)\.txt$")
ST_RE = re.compile(r"^level_st_(\d{2})-(\d{2})\.txt$")
RECAP_RE = re.compile(r"^level_main_(\d+)_chapter_recap\.txt$")


@dataclass
class Stage:
    id: str
    kind: str  # "battle" | "story" | "recap"
    beg: str | None = None
    end: str | None = None
    story: str | None = None  # st_* / recap file (single-file stage)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "beg": self.beg,
            "end": self.end,
            "story": self.story,
        }


@dataclass
class Chapter:
    chapter_id: str
    zone_id: str
    title: str | None
    subtitle: str | None
    title_en: str | None
    stages: list[Stage] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "chapter_id": self.chapter_id,
            "zone_id": self.zone_id,
            "title": self.title,
            "subtitle": self.subtitle,
            "title_en": self.title_en,
            "stages": [s.to_dict() for s in self.stages],
        }


def _load_zone_titles() -> dict[str, dict]:
    """Map ``main_<int>`` → zone metadata (title fields) from zone_table.json."""
    path = EXCEL_DIR / "zone_table.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data["zones"].items() if k.startswith("main_")}


def _load_stage_codes() -> dict[str, int]:
    """Map ``main_NN-YY`` / ``st_NN-YY`` → in-game display number (1, 2, ..., 22).

    Sourced from ``stage_table.json``'s ``code`` field (e.g. ``"13-1"``,
    ``"13-22"``). This is the canonical narrative order: ``st_13-01`` precedes
    ``main_13-01``, and ``st_13-02`` / ``st_13-03`` are interleaved with main
    battles -- a constraint not derivable from the filenames alone.
    """
    path = EXCEL_DIR / "stage_table.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    stages = data.get("stages", data)
    out: dict[str, int] = {}
    for stage_id, info in stages.items():
        if not isinstance(info, dict):
            continue
        if not (stage_id.startswith("main_") or stage_id.startswith("st_")):
            continue
        if info.get("stageType") != "MAIN":
            continue
        code = info.get("code") or ""
        if "-" not in code:
            continue
        try:
            out[stage_id] = int(code.split("-", 1)[1])
        except ValueError:
            continue
    return out


def _stage_sort_key(stage: Stage, codes: dict[str, int]) -> tuple[int, str]:
    """Sort within a chapter by the canonical in-game order.

    Stages with no entry in ``stage_table.json`` (e.g. recap files) sort to the
    end so they don't disrupt the narrative flow.
    """
    return (codes.get(stage.id, 9999), stage.id)


def _resolve_upstream_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(UPSTREAM_DIR), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def build_chapter_index() -> dict:
    if not STORY_DIR.exists():
        raise FileNotFoundError(
            f"upstream story dir not found: {STORY_DIR}\n"
            f"Run sparse-checkout setup first (see docs/CHECKLIST.md Phase 1.1)."
        )

    zones = _load_zone_titles()
    codes = _load_stage_codes()

    chapters: dict[str, Chapter] = {}

    def _get_chapter(chap_xx: str) -> Chapter:
        chapter_id = f"main_{chap_xx}"
        if chapter_id not in chapters:
            zone_key = f"main_{int(chap_xx)}"
            zone = zones.get(zone_key, {})
            chapters[chapter_id] = Chapter(
                chapter_id=chapter_id,
                zone_id=zone_key,
                title=zone.get("zoneNameSecond"),
                subtitle=zone.get("zoneNameFirst"),
                title_en=zone.get("zoneNameThird"),
            )
        return chapters[chapter_id]

    for path in sorted(STORY_DIR.iterdir()):
        if not path.is_file():
            continue
        rel = path.relative_to(UPSTREAM_DIR).as_posix()
        name = path.name

        if m := MAIN_RE.match(name):
            chap_xx, stage_yy, kind = m.group(1), m.group(2), m.group(3)
            chapter = _get_chapter(chap_xx)
            stage_id = f"main_{chap_xx}-{stage_yy}"
            stage = next((s for s in chapter.stages if s.id == stage_id), None)
            if stage is None:
                stage = Stage(id=stage_id, kind="battle")
                chapter.stages.append(stage)
            if kind == "beg":
                stage.beg = rel
            else:
                stage.end = rel
            continue

        if m := ST_RE.match(name):
            chap_xx, stage_yy = m.group(1), m.group(2)
            chapter = _get_chapter(chap_xx)
            stage_id = f"st_{chap_xx}-{stage_yy}"
            chapter.stages.append(Stage(id=stage_id, kind="story", story=rel))
            continue

        if m := RECAP_RE.match(name):
            chap_n = m.group(1)
            chap_xx = chap_n.zfill(2)
            chapter = _get_chapter(chap_xx)
            stage_id = f"main_{chap_xx}-recap"
            chapter.stages.append(Stage(id=stage_id, kind="recap", story=rel))
            continue

    for chapter in chapters.values():
        chapter.stages.sort(key=lambda s: _stage_sort_key(s, codes))

    sorted_chapters = sorted(
        chapters.values(),
        key=lambda c: int(c.chapter_id.split("_")[1]),
    )

    return {
        "upstream_commit": _resolve_upstream_commit(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "chapters": [c.to_dict() for c in sorted_chapters],
    }


def write_chapter_index(index: dict, path: Path = OUTPUT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
