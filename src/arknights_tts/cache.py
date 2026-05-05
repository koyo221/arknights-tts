"""FLAC cache + SQLite index for synthesized lines (PLAN.md Phase 4).

Cache key: ``sha256(text + preset_name + json.dumps(style, sort_keys=True))``
truncated to 32 hex chars (still effectively collision-free for our scale).

Layout:

* ``data/cache/<hash>.flac`` -- audio data
* ``data/cache/index.db``    -- SQLite database with rich per-line metadata
  used for selective invalidation (per-text, per-preset, per-style).

The ``dict_version_at_generation`` column records the SHA-256 of
``config/word_dict.csv`` at generation time so we can detect when the dictionary
has changed since a cache entry was produced.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

CACHE_DIR = Path("data/cache")
INDEX_DB_PATH = CACHE_DIR / "index.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS wav_cache (
    hash                       TEXT PRIMARY KEY,
    text                       TEXT NOT NULL,
    preset                     TEXT NOT NULL,
    style_json                 TEXT NOT NULL,
    generated_at               INTEGER NOT NULL,
    dict_version_at_generation TEXT,
    chapter                    TEXT,
    stage                      TEXT,
    line_no                    INTEGER,
    duration_ms                INTEGER
);

CREATE INDEX IF NOT EXISTS idx_chapter_stage ON wav_cache(chapter, stage, line_no);
CREATE INDEX IF NOT EXISTS idx_text          ON wav_cache(text);
CREATE INDEX IF NOT EXISTS idx_preset        ON wav_cache(preset);
"""


def compute_cache_key(text: str, preset: str, style: dict[str, float]) -> str:
    style_canonical = json.dumps(style, sort_keys=True, ensure_ascii=False)
    raw = f"{text}|{preset}|{style_canonical}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def cache_path_for(key: str) -> Path:
    return CACHE_DIR / f"{key}.flac"


@dataclass
class CacheEntry:
    hash: str
    text: str
    preset: str
    style_json: str
    generated_at: int
    dict_version_at_generation: str | None
    chapter: str | None
    stage: str | None
    line_no: int | None
    duration_ms: int | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "CacheEntry":
        return cls(**{k: row[k] for k in row.keys()})


@contextmanager
def open_index(db_path: Path = INDEX_DB_PATH) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def exists(conn: sqlite3.Connection, key: str) -> bool:
    cur = conn.execute("SELECT 1 FROM wav_cache WHERE hash = ?", (key,))
    return cur.fetchone() is not None


def upsert(
    conn: sqlite3.Connection,
    *,
    key: str,
    text: str,
    preset: str,
    style: dict[str, float],
    chapter: str | None,
    stage: str | None,
    line_no: int | None,
    duration_ms: int | None,
    dict_version: str | None,
) -> None:
    now = int(datetime.now(timezone.utc).timestamp())
    conn.execute(
        """
        INSERT OR REPLACE INTO wav_cache
            (hash, text, preset, style_json, generated_at,
             dict_version_at_generation, chapter, stage, line_no, duration_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            key,
            text,
            preset,
            json.dumps(style, sort_keys=True, ensure_ascii=False),
            now,
            dict_version,
            chapter,
            stage,
            line_no,
            duration_ms,
        ),
    )


def delete(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM wav_cache WHERE hash = ?", (key,))
    flac = cache_path_for(key)
    flac.unlink(missing_ok=True)


def find_by_text_contains(conn: sqlite3.Connection, surfaces: list[str]) -> list[CacheEntry]:
    """Return cache entries whose text contains any of the given surface forms.

    Uses substring matching (LIKE '%surface%'); for our scale (up to a few
    hundred thousand rows) this is fast enough without full-text search.
    """
    entries: list[CacheEntry] = []
    seen: set[str] = set()
    for surface in surfaces:
        if not surface:
            continue
        cur = conn.execute(
            "SELECT * FROM wav_cache WHERE text LIKE ? ESCAPE '\\'",
            (f"%{_escape_like(surface)}%",),
        )
        for row in cur:
            if row["hash"] in seen:
                continue
            seen.add(row["hash"])
            entries.append(CacheEntry.from_row(row))
    return entries


def find_by_preset(conn: sqlite3.Connection, preset: str) -> list[CacheEntry]:
    cur = conn.execute("SELECT * FROM wav_cache WHERE preset = ?", (preset,))
    return [CacheEntry.from_row(r) for r in cur]


def stats(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) AS n FROM wav_cache").fetchone()["n"]
    by_preset = {
        row["preset"]: row["n"]
        for row in conn.execute(
            "SELECT preset, COUNT(*) AS n FROM wav_cache GROUP BY preset"
        )
    }
    total_duration = conn.execute(
        "SELECT COALESCE(SUM(duration_ms),0) AS s FROM wav_cache"
    ).fetchone()["s"]
    flac_files = list(CACHE_DIR.glob("*.flac"))
    flac_bytes = sum(p.stat().st_size for p in flac_files)
    return {
        "total_entries": total,
        "by_preset": by_preset,
        "total_duration_ms": total_duration,
        "total_duration_hours": total_duration / 1000 / 3600,
        "flac_count": len(flac_files),
        "flac_total_bytes": flac_bytes,
        "flac_total_mb": flac_bytes / 1024 / 1024,
    }


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# -- dictionary versioning -----------------------------------------------------

def dict_version(csv_path: Path) -> str:
    if not csv_path.exists():
        return ""
    return hashlib.sha256(csv_path.read_bytes()).hexdigest()[:16]


def extract_dict_surfaces(old_csv: Path | None, new_csv: Path) -> list[str]:
    """Return surface forms (the "word" column) added or changed between two CSVs.

    Used to scope cache invalidation to lines that actually contain an updated
    word. If ``old_csv`` is None or missing, every entry in ``new_csv`` is
    treated as new.
    """
    new_entries = _read_dict_csv(new_csv)
    if old_csv is None or not old_csv.exists():
        return list(new_entries.keys())
    old_entries = _read_dict_csv(old_csv)
    changed: list[str] = []
    for word, reading in new_entries.items():
        if old_entries.get(word) != reading:
            changed.append(word)
    return changed


def _read_dict_csv(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    if not path.exists():
        return entries
    import csv
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            word = (row.get("word") or "").strip()
            reading = (row.get("reading") or "").strip()
            if word:
                entries[word] = reading
    return entries
