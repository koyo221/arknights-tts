from __future__ import annotations

from pathlib import Path

import pytest

from arknights_tts.parser import SpeakerAliases, parse_file

FIXTURE = Path(__file__).parent / "fixtures" / "sample_story.txt"
ALIASES = Path(__file__).parent.parent / "config" / "speaker_aliases.json"


@pytest.fixture(scope="module")
def aliases() -> SpeakerAliases:
    return SpeakerAliases.load(ALIASES)


@pytest.fixture(scope="module")
def parsed(aliases: SpeakerAliases, tmp_path_factory):
    decision_log = tmp_path_factory.mktemp("logs") / "skipped_decisions.jsonl"
    lines, stats = parse_file(
        FIXTURE,
        aliases,
        chapter_id="main_test",
        stage_id="main_test-01",
        decision_log=decision_log,
    )
    return lines, stats, decision_log


class TestParser:
    def test_dialogue_extracted(self, parsed):
        lines, _, _ = parsed
        dialogues = [ln for ln in lines if ln.kind == "dialogue"]
        assert any(d.speaker == "アーミヤ" and "起きてください" in d.text for d in dialogues)
        assert any(d.speaker == "ケルシー" for d in dialogues)
        assert any(d.speaker == "ホルン" for d in dialogues)

    def test_doctor_substitution(self, parsed):
        lines, _, _ = parsed
        decisions = [ln for ln in lines if ln.kind == "dialogue" and "決断" in (ln.text or "")]
        assert decisions and "ドクター" in decisions[0].text
        assert "{@nickname}" not in decisions[0].text

    def test_decision_skipped(self, parsed):
        lines, _, decision_log = parsed
        assert not any("どうする？" in (ln.text or "") for ln in lines if ln.kind == "dialogue")
        assert decision_log.exists()
        assert "どうする？" in decision_log.read_text(encoding="utf-8")

    def test_subtitle_routing(self, parsed):
        lines, _, _ = parsed
        narration_texts = [ln.text for ln in lines if ln.kind == "narration"]
        assert any(t.startswith("場所、ロドス・アイランド") for t in narration_texts)
        assert any("お前は知るだろう" in t for t in narration_texts)

    def test_speaker_prefix_changes_only_on_switch(self, parsed):
        lines, _, _ = parsed
        dialogues = [ln for ln in lines if ln.kind == "dialogue"]
        # First Amiya line: prefix
        amiya_first = next(d for d in dialogues if d.speaker == "アーミヤ")
        assert amiya_first.speaker_prefix == "アーミヤ、"
        # Second consecutive Amiya: no prefix
        amiya_lines = [d for d in dialogues if d.speaker == "アーミヤ" and "時間" in (d.text or "")]
        assert amiya_lines and amiya_lines[0].speaker_prefix is None

    def test_anonymous_no_prefix(self, parsed):
        lines, _, _ = parsed
        anon = [
            ln for ln in lines
            if ln.kind == "dialogue" and ln.meta.get("speaker_kind") == "anonymous"
        ]
        assert anon
        for ln in anon:
            assert ln.speaker_prefix is None

    def test_mob_normalization(self, parsed):
        lines, _, _ = parsed
        mobs = [
            ln for ln in lines
            if ln.kind == "dialogue" and ln.meta.get("speaker_kind") == "mob"
        ]
        assert any(ln.speaker == "兵士" for ln in mobs)

    def test_emotion_suffix_stripped(self, parsed):
        lines, _, _ = parsed
        # アーミヤ？ -> アーミヤ
        amiya_q = [
            ln for ln in lines
            if ln.kind == "dialogue" and ln.meta.get("raw_speaker") == "アーミヤ？"
        ]
        assert amiya_q and amiya_q[0].speaker == "アーミヤ"

    def test_multiline_captured(self, parsed):
        lines, _, _ = parsed
        ml = [ln for ln in lines if ln.meta.get("tag") == "multiline"]
        assert len(ml) == 2
        assert all(ln.speaker == "ホルン" for ln in ml)

    def test_blocker_pause(self, parsed):
        lines, _, _ = parsed
        pauses = [ln for ln in lines if ln.kind == "pause" and ln.meta.get("tag") == "blocker"]
        assert pauses and pauses[0].pause_ms == 1500

    def test_narration_plain_text(self, parsed):
        lines, _, _ = parsed
        plain_narr = [
            ln.text for ln in lines
            if ln.kind == "narration" and "暗闇に消えていった" in (ln.text or "")
        ]
        assert plain_narr
