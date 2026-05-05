"""CLI entry point. Subcommands are wired up in their respective phases (PLAN.md Phase 7b)."""

from __future__ import annotations

import json
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from . import cache, concat, dict_loader, fetcher, parser, router

console = Console()

CONFIG_DIR = Path("config")
DATA_DIR = Path("data")
LOGS_DIR = Path("logs")
PARSED_DIR = DATA_DIR / "parsed"
DECISION_LOG = LOGS_DIR / "skipped_decisions.jsonl"
SPEAKER_ALIASES_PATH = CONFIG_DIR / "speaker_aliases.json"
CHAPTER_INDEX_PATH = CONFIG_DIR / "chapter_index.json"
PRESET_CONFIG_PATH = CONFIG_DIR / "preset_config.json"
WORD_DICT_PATH = CONFIG_DIR / "word_dict.csv"
OUTPUT_DIR = DATA_DIR / "output"


def _load_chapter_index() -> dict:
    if not CHAPTER_INDEX_PATH.exists():
        raise click.ClickException(
            f"{CHAPTER_INDEX_PATH} not found. Run `arknights-tts index` first."
        )
    return json.loads(CHAPTER_INDEX_PATH.read_text(encoding="utf-8"))


def _resolve_chapters(idx: dict, chapters: tuple[str, ...]) -> list[str]:
    all_ids = [c["chapter_id"] for c in idx["chapters"]]
    if not chapters:
        return []
    resolved: list[str] = []
    for spec in chapters:
        if spec == "all":
            return list(all_ids)
        if ".." in spec:
            lo, hi = spec.split("..", 1)
            try:
                i_lo = all_ids.index(lo)
                i_hi = all_ids.index(hi)
            except ValueError as exc:
                raise click.ClickException(f"unknown chapter in range: {spec}") from exc
            if i_lo > i_hi:
                i_lo, i_hi = i_hi, i_lo
            resolved.extend(all_ids[i_lo : i_hi + 1])
            continue
        if spec not in all_ids:
            raise click.ClickException(f"unknown chapter: {spec}")
        resolved.append(spec)
    seen: set[str] = set()
    out = []
    for cid in resolved:
        if cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


@click.group()
@click.version_option()
def cli() -> None:
    """arknights-tts: Arknights main story audiobook pipeline."""


@cli.command()
def index() -> None:
    """Regenerate config/chapter_index.json from data/upstream."""
    idx = fetcher.build_chapter_index()
    fetcher.write_chapter_index(idx)
    n_chapters = len(idx["chapters"])
    n_stages = sum(len(c["stages"]) for c in idx["chapters"])
    console.print(
        f"[green]wrote[/green] {fetcher.OUTPUT_PATH} "
        f"({n_chapters} chapters, {n_stages} stages, "
        f"upstream commit {(idx['upstream_commit'] or 'unknown')[:8]})"
    )


@cli.command(name="list")
def list_cmd() -> None:
    """Show chapter index summary."""
    idx = _load_chapter_index()
    table = Table(title=f"upstream commit {(idx['upstream_commit'] or 'unknown')[:8]}")
    table.add_column("chapter_id")
    table.add_column("title")
    table.add_column("battle", justify="right")
    table.add_column("story", justify="right")
    table.add_column("recap", justify="right")
    table.add_column("parsed", justify="right")
    for c in idx["chapters"]:
        kinds: dict[str, int] = {}
        for s in c["stages"]:
            kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
        parsed_dir = PARSED_DIR / c["chapter_id"]
        parsed_n = len(list(parsed_dir.glob("*.json"))) if parsed_dir.exists() else 0
        table.add_row(
            c["chapter_id"],
            c.get("title") or "-",
            str(kinds.get("battle", 0)),
            str(kinds.get("story", 0)),
            str(kinds.get("recap", 0)),
            str(parsed_n),
        )
    console.print(table)


@cli.command()
@click.argument("chapters", nargs=-1)
@click.option("--all", "all_", is_flag=True, help="Parse every chapter.")
def parse(chapters: tuple[str, ...], all_: bool) -> None:
    """Parse one or more chapters into data/parsed/<chapter>/*.json."""
    idx = _load_chapter_index()
    if all_:
        target_ids = [c["chapter_id"] for c in idx["chapters"]]
    else:
        target_ids = _resolve_chapters(idx, chapters)
    if not target_ids:
        raise click.ClickException("no chapters specified (use --all or pass IDs)")

    aliases = parser.SpeakerAliases.load(SPEAKER_ALIASES_PATH)

    for cid in target_ids:
        console.print(f"[cyan]parsing[/cyan] {cid}")
        stats_by_stage = parser.parse_chapter(
            idx,
            cid,
            upstream_dir=fetcher.UPSTREAM_DIR,
            aliases=aliases,
            output_dir=PARSED_DIR,
            decision_log=DECISION_LOG,
        )
        total = parser.ParseStats()
        for stats in stats_by_stage.values():
            total.lines_in += stats.lines_in
            total.narration += stats.narration
            total.dialogue += stats.dialogue
            total.pauses += stats.pauses
        console.print(
            f"  {len(stats_by_stage)} stage parts, "
            f"narration={total.narration}, dialogue={total.dialogue}, "
            f"pauses={total.pauses}"
        )


@cli.command(name="discover-speakers")
@click.argument("chapters", nargs=-1)
@click.option("--all", "all_", is_flag=True)
def discover_speakers(chapters: tuple[str, ...], all_: bool) -> None:
    """List speakers found in parsed chapters with their resolution kind."""
    if all_:
        target_dirs = sorted(PARSED_DIR.glob("*"))
    else:
        if not chapters:
            raise click.ClickException("pass chapter IDs or --all")
        target_dirs = [PARSED_DIR / c for c in chapters]

    counts: dict[tuple[str, str, str], int] = {}
    for d in target_dirs:
        if not d.exists():
            console.print(f"[yellow]not parsed yet:[/yellow] {d.name}")
            continue
        for jp in sorted(d.glob("*.json")):
            data = json.loads(jp.read_text(encoding="utf-8"))
            for ln in data["lines"]:
                if ln.get("kind") != "dialogue":
                    continue
                speaker = ln.get("speaker") or ""
                raw = ln.get("meta", {}).get("raw_speaker", speaker)
                kind = ln.get("meta", {}).get("speaker_kind", "named")
                counts[(speaker, raw, kind)] = counts.get((speaker, raw, kind), 0) + 1

    table = Table()
    table.add_column("speaker (normalized)")
    table.add_column("raw")
    table.add_column("kind")
    table.add_column("count", justify="right")
    for (sp, raw, kind), n in sorted(counts.items(), key=lambda kv: -kv[1]):
        table.add_row(sp, raw if raw != sp else "", kind, str(n))
    console.print(table)


@cli.command()
@click.argument("chapters", nargs=-1)
@click.option("--all", "all_", is_flag=True)
def synth(chapters: tuple[str, ...], all_: bool) -> None:
    """Synthesize one or more parsed chapters into FLAC cache."""
    from . import tts

    idx = _load_chapter_index()
    target_ids = (
        [c["chapter_id"] for c in idx["chapters"]]
        if all_
        else _resolve_chapters(idx, chapters)
    )
    if not target_ids:
        raise click.ClickException("no chapters specified (use --all or pass IDs)")

    preset = router.load_preset_config(PRESET_CONFIG_PATH)
    dict_ver = cache.dict_version(WORD_DICT_PATH)

    with tts.TTSDriver(preset) as driver:
        for cid in target_ids:
            stage_dir = PARSED_DIR / cid
            if not stage_dir.exists():
                console.print(f"[yellow]skip[/yellow] {cid}: not parsed yet")
                continue
            console.print(f"[cyan]synth[/cyan] {cid}")
            for stage_json in sorted(stage_dir.glob("*.json")):
                data = json.loads(stage_json.read_text(encoding="utf-8"))
                lines = [parser.Line(**ln) for ln in data["lines"]]
                cached_n = synth_n = 0
                for r in driver.synthesize_lines(
                    lines,
                    chapter=cid,
                    stage=data["stage_id"],
                    dict_version=dict_ver,
                ):
                    if r.cached:
                        cached_n += 1
                    else:
                        synth_n += 1
                console.print(
                    f"  {stage_json.name}: cached={cached_n}, synth={synth_n}"
                )


@cli.command()
@click.argument("chapters", nargs=-1)
@click.option("--all", "all_", is_flag=True)
@click.option("--bitrate", default="96k", help="AAC bitrate, e.g. 64k, 96k, 128k.")
def build(chapters: tuple[str, ...], all_: bool, bitrate: str) -> None:
    """Concatenate cached FLACs into one M4B + SRT per chapter."""
    idx = _load_chapter_index()
    target_ids = (
        [c["chapter_id"] for c in idx["chapters"]]
        if all_
        else _resolve_chapters(idx, chapters)
    )
    if not target_ids:
        raise click.ClickException("no chapters specified (use --all or pass IDs)")

    preset = router.load_preset_config(PRESET_CONFIG_PATH)
    for cid in target_ids:
        title = next(
            (c.get("title") for c in idx["chapters"] if c["chapter_id"] == cid), None,
        )
        try:
            m4b, srt = concat.build_chapter(
                cid,
                parsed_dir=PARSED_DIR,
                chapter_index=idx,
                preset=preset,
                output_dir=OUTPUT_DIR,
                chapter_title=title or cid,
                aac_bitrate=bitrate,
            )
        except RuntimeError as exc:
            console.print(f"[red]build failed[/red] {cid}: {exc}")
            continue
        console.print(f"[green]built[/green] {m4b.name}, {srt.name}")


@cli.command(name="all")
@click.argument("chapters", nargs=-1)
@click.option("--all", "all_", is_flag=True)
def run_all(chapters: tuple[str, ...], all_: bool) -> None:
    """Run parse + synth + build for the given chapters."""
    ctx = click.get_current_context()
    ctx.invoke(parse, chapters=chapters, all_=all_)
    ctx.invoke(synth, chapters=chapters, all_=all_)
    ctx.invoke(build, chapters=chapters, all_=all_, bitrate="96k")


@cli.command()
def pick() -> None:
    """Interactive chapter picker (questionary), runs parse+synth+build on selection."""
    import questionary

    idx = _load_chapter_index()
    choices = [
        questionary.Choice(
            title=f"{c['chapter_id']:<8} {c.get('title') or '-'} "
                  f"({len(c['stages'])} stages)",
            value=c["chapter_id"],
        )
        for c in idx["chapters"]
    ]
    selected = questionary.checkbox("Select chapters", choices=choices).ask()
    if not selected:
        return
    ctx = click.get_current_context()
    ctx.invoke(parse, chapters=tuple(selected), all_=False)
    ctx.invoke(synth, chapters=tuple(selected), all_=False)
    ctx.invoke(build, chapters=tuple(selected), all_=False, bitrate="96k")


@cli.group()
def cache_grp() -> None:
    """Cache management commands."""


cli.add_command(cache_grp, name="cache")


@cli.group()
def dict_grp() -> None:
    """Word dictionary management."""


cli.add_command(dict_grp, name="dict")


@dict_grp.command(name="diff")
@click.option(
    "--old",
    type=click.Path(path_type=Path, exists=True),
    required=True,
    help="Previous word_dict.csv to compare against.",
)
def dict_diff(old: Path) -> None:
    """Show added/modified/removed entries between an old CSV and the current one."""
    added, modified, removed = dict_loader.diff_csv_vs_csv(old, WORD_DICT_PATH)
    table = Table(title="word_dict.csv diff")
    table.add_column("change")
    table.add_column("word")
    table.add_column("reading")
    for e in added:
        table.add_row("[green]+[/green]", e.word, e.reading)
    for old_e, new_e in modified:
        table.add_row("[yellow]~[/yellow]", new_e.word, f"{old_e.reading} -> {new_e.reading}")
    for e in removed:
        table.add_row("[red]-[/red]", e.word, e.reading)
    console.print(table)
    console.print(f"added={len(added)} modified={len(modified)} removed={len(removed)}")


@dict_grp.command(name="sync")
def dict_sync() -> None:
    """Push word_dict.csv to VOICEVOX's user dictionary (managed entries only).

    Existing entries you added manually in the VOICEVOX GUI are left alone --
    only entries marked with ``[arknights_tts]`` are touched.
    """
    preset = router.load_preset_config(PRESET_CONFIG_PATH)
    entries = dict_loader.load_csv(WORD_DICT_PATH)
    if not entries:
        raise click.ClickException(f"{WORD_DICT_PATH} is empty or missing")
    counts = dict_loader.sync_to_voicevox(entries, engine_url=preset.engine_url)
    console.print(
        f"[green]synced[/green] added={counts['added']} "
        f"updated={counts['updated']} removed={counts['removed']}"
    )


@dict_grp.command(name="import")
def dict_import() -> None:
    """List managed entries currently in the VOICEVOX engine (for verification)."""
    preset = router.load_preset_config(PRESET_CONFIG_PATH)
    entries = dict_loader.import_from_voicevox(engine_url=preset.engine_url)
    table = Table(title=f"{len(entries)} managed entries in VOICEVOX")
    table.add_column("word")
    table.add_column("reading")
    table.add_column("priority", justify="right")
    for e in entries:
        table.add_row(e.word, e.reading, str(e.priority))
    console.print(table)


@cache_grp.command(name="stats")
def cache_stats() -> None:
    """Show cache size and hit metrics."""
    with cache.open_index() as conn:
        s = cache.stats(conn)
    table = Table(title="cache stats")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("entries", str(s["total_entries"]))
    for preset, n in s["by_preset"].items():
        table.add_row(f"  preset={preset}", str(n))
    table.add_row("flac files on disk", str(s["flac_count"]))
    table.add_row("flac total size", f"{s['flac_total_mb']:.1f} MB")
    table.add_row("audio total length", f"{s['total_duration_hours']:.2f} hours")
    console.print(table)


@cache_grp.command(name="invalidate-by-dict")
@click.option(
    "--old",
    type=click.Path(path_type=Path, exists=True),
    help="Previous word_dict.csv to diff against. If omitted, all dict surfaces are considered changed.",
)
@click.option(
    "--yes", "auto_yes", is_flag=True, help="Skip confirmation prompt."
)
def cache_invalidate_by_dict(old: Path | None, auto_yes: bool) -> None:
    """Invalidate cache entries whose text contains words changed in word_dict.csv."""
    if not WORD_DICT_PATH.exists():
        raise click.ClickException(f"{WORD_DICT_PATH} not found")
    surfaces = cache.extract_dict_surfaces(old, WORD_DICT_PATH)
    if not surfaces:
        console.print("[green]no dict changes detected[/green]")
        return
    with cache.open_index() as conn:
        affected = cache.find_by_text_contains(conn, surfaces)
        console.print(
            f"surfaces changed: {len(surfaces)}, affected cache rows: {len(affected)}"
        )
        if not affected:
            return
        sample = ", ".join(sorted(set(s for s in surfaces[:10])))
        console.print(f"[dim]sample surfaces: {sample}[/dim]")
        if not auto_yes:
            click.confirm("Delete those entries (file + index)?", abort=True)
        for entry in affected:
            cache.delete(conn, entry.hash)
        console.print(f"[red]invalidated[/red] {len(affected)} entries")


if __name__ == "__main__":
    cli()
