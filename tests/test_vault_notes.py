"""The multi-writer contract for shared topic notes.

These are the tests that matter most in this package: getting them wrong means
silently eating another application's work, or a person's, in their own vault.
"""

import re
import unicodedata
from pathlib import Path

import pytest

from src.vault.text import slugify as ours_slugify
from src.vault.notes import (
    KEY_PREFIX,
    OWNER,
    merge_frontmatter,
    merge_owned_section,
    split_frontmatter,
    wrap,
)

EXISTING = """---
type: topic
title: "Fortinet"
tags: [topic, vendor, my-own-tag]
podcasts_mentions: 3
podcasts_first_seen: 2026-01-04
---

My own thinking about Fortinet, written by hand.

<!-- begin:podcast-digest -->
## From podcasts
- 2026-05-01 · **Risky Business** — an episode
<!-- end:podcast-digest -->
"""

OURS = (
    "---\ntype: topic\ntitle: \"Fortinet\"\ntags: [topic]\n"
    f"{KEY_PREFIX}mentions: 6\n{KEY_PREFIX}first_seen: 2026-06-01\n---\n\n"
    "# Fortinet\n\n" + wrap("## From security digests\n\n- 2026-08-24 — [[a|A]]") + "\n"
)


def test_a_new_note_is_written_as_is():
    assert merge_owned_section(None, OURS) == OURS
    assert merge_owned_section("   ", OURS) == OURS


def test_human_prose_survives():
    merged = merge_owned_section(EXISTING, OURS)
    assert "My own thinking about Fortinet, written by hand." in merged


def test_another_writers_section_is_untouched():
    merged = merge_owned_section(EXISTING, OURS)
    assert "<!-- begin:podcast-digest -->" in merged
    assert "- 2026-05-01 · **Risky Business** — an episode" in merged
    assert "<!-- end:podcast-digest -->" in merged


def test_another_writers_frontmatter_keys_are_untouched():
    merged = merge_owned_section(EXISTING, OURS)
    front, _ = split_frontmatter(merged)
    assert "podcasts_mentions: 3" in front
    assert "podcasts_first_seen: 2026-01-04" in front


def test_shared_keys_are_not_overwritten():
    """`tags` describes the note, not our part of it. Overwriting [topic, vendor,
    my-own-tag] with our own [topic] would silently drop two tags, and YAML would
    not complain -- the last duplicate key just wins."""
    merged = merge_owned_section(EXISTING, OURS)
    front, _ = split_frontmatter(merged)
    assert "tags: [topic, vendor, my-own-tag]" in front
    assert "tags: [topic]" not in front


def test_our_keys_are_replaced_not_appended():
    once = merge_owned_section(EXISTING, OURS)
    front, _ = split_frontmatter(once)
    assert front.count(f"{KEY_PREFIX}mentions: 6") == 1

    newer = OURS.replace(f"{KEY_PREFIX}mentions: 6", f"{KEY_PREFIX}mentions: 7")
    twice = merge_owned_section(once, newer)
    front, _ = split_frontmatter(twice)
    assert f"{KEY_PREFIX}mentions: 7" in front
    assert f"{KEY_PREFIX}mentions: 6" not in front


def test_our_region_is_replaced_in_place():
    once = merge_owned_section(EXISTING, OURS)
    newer = OURS.replace("- 2026-08-24 — [[a|A]]", "- 2026-08-25 — [[b|B]]")
    twice = merge_owned_section(once, newer)

    assert twice.count("<!-- begin:security-digest -->") == 1
    assert "[[b|B]]" in twice and "[[a|A]]" not in twice
    # Position is kept: the human's prose is still above the podcast section,
    # which is still above ours.
    assert twice.index("My own thinking") < twice.index("begin:podcast-digest")
    assert twice.index("begin:podcast-digest") < twice.index("begin:security-digest")


def test_merging_is_idempotent():
    once = merge_owned_section(EXISTING, OURS)
    assert merge_owned_section(once, OURS) == once


def test_a_backslash_in_our_section_is_not_treated_as_a_regex_escape():
    """md_escape_inline emits backslashes constantly; a naive re.sub replacement
    string would eat them or raise."""
    ours = "---\ntype: topic\n---\n\n" + wrap(r"- an item with \[brackets\] in it") + "\n"
    once = merge_owned_section(EXISTING, ours)
    twice = merge_owned_section(once, ours)
    assert r"\[brackets\]" in twice


def test_note_with_no_frontmatter_at_all():
    merged = merge_owned_section("Just prose, no YAML.\n", OURS)
    assert "Just prose, no YAML." in merged
    assert "<!-- begin:security-digest -->" in merged
    front, _ = split_frontmatter(merged)
    assert "type: topic" in front  # ours seeded it


def test_merge_frontmatter_drops_our_stale_keys():
    existing = [f"{KEY_PREFIX}mentions: 3", f"{KEY_PREFIX}gone: yes", "type: topic"]
    merged = merge_frontmatter(existing, [f"{KEY_PREFIX}mentions: 9"])
    assert merged == ["type: topic", f"{KEY_PREFIX}mentions: 9"]


def test_owner_differs_from_the_other_writers():
    """A shared owner tag would make each application overwrite the other's
    section, which is the exact failure this contract exists to prevent."""
    assert OWNER == "security-digest"
    assert KEY_PREFIX == "security_"


def test_our_slug_rule_matches_podcast_digests():
    """Both applications file a topic note under slugify(name). If these two ever
    disagree, one thing gets two notes in `99 topics/` and the shared folder --
    the entire reason for writing into someone else's vault -- is pointless.

    Their module is not imported: it pulls in `bleach`, which is their dependency
    and not ours. Its `slugify` and the one regex it uses are extracted from the
    source and executed in an empty namespace instead, so this asserts against
    the canonical text rather than against a copy of it that could drift.
    """
    source = (
        Path.home() / "projects" / "podcast-digest" / "podcast_agent" / "sanitize.py"
    )
    if not source.is_file():
        pytest.skip("podcast-digest is not checked out next to this repo")

    text = source.read_text(encoding="utf-8")
    match = re.search(r"^def slugify\(.*?(?=\n\n\ndef |\Z)", text, re.DOTALL | re.MULTILINE)
    assert match, "podcast-digest's slugify could not be located -- has it moved?"
    strip = re.search(r"^_SLUG_STRIP = .*$", text, re.MULTILINE)
    assert strip, "podcast-digest's _SLUG_STRIP could not be located"

    namespace: dict = {"re": re, "unicodedata": unicodedata}
    exec(strip.group(0), namespace)  # noqa: S102 -- our own repo's source
    exec(match.group(0), namespace)  # noqa: S102
    theirs = namespace["slugify"]

    for name in (
        "CVE-2026-1234", "Volt Typhoon", "Mandiant Inc.", "Müller GmbH",
        "FortiOS / FortiProxy", "中文 name", "", "A" * 80, "Lazarus  Group",
        "APT-29", "$pecial &chars!", "Anthropic", "NIS2", "MITRE ATT&CK",
    ):
        assert ours_slugify(name) == theirs(name), name
