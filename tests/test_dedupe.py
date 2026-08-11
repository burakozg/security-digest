"""Tests for src.dedupe: content deduplication and the seen-store filter/mark split.
The seen store is SQLite (task 3.4) -- tests use an isolated tmp_path db file."""

import json
import time

from src.db import get_connection
from src.dedupe import clear_seen, dedupe_content, filter_new, mark_seen


def _seed_seen(db_path, link: str, seen_at: float) -> None:
    """Directly insert a seen row with a specific timestamp, bypassing
    mark_seen()'s use of time.time(), for testing expiry behavior."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS seen (link TEXT PRIMARY KEY, seen_at REAL NOT NULL)"
        )
        conn.execute("INSERT OR REPLACE INTO seen (link, seen_at) VALUES (?, ?)", (link, seen_at))
        conn.commit()
    finally:
        conn.close()


def test_dedupe_content_removes_similar_titles():
    items = [
        {"title": "Major Ransomware Attack Hits Hospital", "published": "2026-01-02T00:00:00"},
        {"title": "Major Ransomware Attack Hits Hospital!!", "published": "2026-01-01T00:00:00"},
        {"title": "Completely Different Story About VPNs", "published": "2026-01-01T00:00:00"},
    ]
    kept = dedupe_content(items, similarity_threshold=0.85)
    assert len(kept) == 2
    titles = {i["title"] for i in kept}
    assert "Completely Different Story About VPNs" in titles
    assert "Major Ransomware Attack Hits Hospital" in titles


def test_dedupe_content_keeps_all_when_threshold_is_one():
    items = [{"title": "A", "published": ""}, {"title": "A", "published": ""}]
    assert dedupe_content(items, similarity_threshold=1.0) == items


def test_dedupe_content_empty_list():
    assert dedupe_content([]) == []


def test_filter_new_returns_unseen_items_without_recording_them(tmp_path):
    db_path = tmp_path / "test.db"
    items = [{"link": "https://a.com/1"}, {"link": "https://a.com/2"}]
    new = filter_new(items, db_path, retention_days=14)
    assert len(new) == 2
    # filter_new is read-only: it must not have recorded any item as seen,
    # even though opening the db creates the (empty) schema.
    assert filter_new(items, db_path, retention_days=14) == items


def test_filter_new_skips_already_seen(tmp_path):
    db_path = tmp_path / "test.db"
    _seed_seen(db_path, "https://a.com/1", time.time())
    items = [{"link": "https://a.com/1"}, {"link": "https://a.com/2"}]
    new = filter_new(items, db_path, retention_days=14)
    assert [i["link"] for i in new] == ["https://a.com/2"]


def test_filter_new_treats_expired_entries_as_new(tmp_path):
    db_path = tmp_path / "test.db"
    old_timestamp = time.time() - (30 * 86400)
    _seed_seen(db_path, "https://a.com/1", old_timestamp)
    items = [{"link": "https://a.com/1"}]
    new = filter_new(items, db_path, retention_days=14)
    assert len(new) == 1


def test_filter_new_skips_items_without_link(tmp_path):
    db_path = tmp_path / "test.db"
    items = [{"title": "no link here"}]
    assert filter_new(items, db_path, retention_days=14) == []


def test_mark_seen_writes_store(tmp_path):
    db_path = tmp_path / "test.db"
    mark_seen([{"link": "https://a.com/1"}], db_path, retention_days=14)
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT link FROM seen WHERE link = ?", ("https://a.com/1",)).fetchone()
    finally:
        conn.close()
    assert row is not None


def test_mark_seen_prunes_expired_entries(tmp_path):
    db_path = tmp_path / "test.db"
    old_timestamp = time.time() - (30 * 86400)
    _seed_seen(db_path, "https://old.com/x", old_timestamp)
    mark_seen([{"link": "https://new.com/y"}], db_path, retention_days=14)
    conn = get_connection(db_path)
    try:
        rows = {r["link"] for r in conn.execute("SELECT link FROM seen").fetchall()}
    finally:
        conn.close()
    assert "https://old.com/x" not in rows
    assert "https://new.com/y" in rows


def test_filter_new_then_mark_seen_full_cycle(tmp_path):
    """The scenario task 2.4 fixed: items stay eligible until explicitly marked."""
    db_path = tmp_path / "test.db"
    items = [{"link": "https://a.com/1"}]

    new = filter_new(items, db_path, retention_days=14)
    assert new == items

    # Not marked seen yet -- a second filter_new should still return it (e.g. a
    # crashed run before delivery, retried on the next run).
    assert filter_new(items, db_path, retention_days=14) == items

    mark_seen(new, db_path, retention_days=14)
    assert filter_new(items, db_path, retention_days=14) == []


def test_clear_seen_removes_all_entries(tmp_path):
    """Regression check for the /admin/flush endpoint (task 3.4)."""
    db_path = tmp_path / "test.db"
    mark_seen([{"link": "https://a.com/1"}, {"link": "https://a.com/2"}], db_path, retention_days=14)
    assert filter_new([{"link": "https://a.com/1"}], db_path, retention_days=14) == []

    clear_seen(db_path)

    assert filter_new([{"link": "https://a.com/1"}], db_path, retention_days=14) == [{"link": "https://a.com/1"}]


def test_migrates_legacy_seen_json(tmp_path, monkeypatch):
    """Task 3.4: existing data/seen.json is imported into SQLite on first
    access, exactly once."""
    import src.dedupe as dedupe_module

    legacy_path = tmp_path / "seen.json"
    legacy_path.write_text(json.dumps({"https://legacy.com/1": time.time()}))
    monkeypatch.setattr(dedupe_module, "_LEGACY_JSON_PATH", legacy_path)

    db_path = tmp_path / "test.db"
    # The legacy link should now be treated as already-seen.
    result = filter_new([{"link": "https://legacy.com/1"}], db_path, retention_days=14)
    assert result == []


# --- same-story merging for news search ------------------------------------


def _search_item(title, published, source="Nvidia"):
    return {"title": title, "link": "https://x/" + title[:16],
            "source": source, "published": published, "description": ""}


# Four real headlines for one story, as returned by news search for the Nvidia
# topic. Item 2 is a genuinely different story about the same company.
NAVER = [
    _search_item("Naver Unveils AI Factory Deal Structure with Nvidia, Brookfield", "2026-08-03T10:00:00"),
    _search_item("Naver Establishes Subsidiary for AI Factory Operation", "2026-08-03T09:00:00"),
    _search_item("Naver secures Nvidia, Brookfield for AI factory funding", "2026-08-03T08:00:00"),
    _search_item('Naver Unveils AI Factory Contract Structure: "Receives NVIDIA GPUs, '
                 'Eases Financial Burden via Brookfield SPV"', "2026-08-03T07:00:00"),
]


def test_story_dedupe_merges_one_story_reported_by_many_outlets():
    """Character similarity peaks at 0.67 across these, so the default check
    cannot catch them at any usable threshold."""
    kept = dedupe_content(NAVER, story_dedupe=True)
    titles = [k["title"] for k in kept]
    assert len(kept) == 2
    assert any("Deal Structure" in t for t in titles)
    # The subsidiary story shares only "naver" and "factory" -- a different event.
    assert any("Establishes Subsidiary" in t for t in titles)


def test_story_dedupe_is_off_by_default():
    """It is too blunt for publisher feeds, so the security instance keeps the
    character-similarity behaviour it has always had."""
    assert len(dedupe_content(NAVER)) == 4


def test_story_dedupe_never_merges_across_topics():
    """Two readers tracking different entities must not have one's item removed
    because the other's looked similar."""
    items = [
        _search_item("Brookfield and partners fund the Naver AI factory buildout",
                     "2026-08-03T10:00:00", "Nvidia"),
        _search_item("Naver AI factory buildout wins Brookfield funding",
                     "2026-08-03T09:00:00", "Vattenfall"),
    ]
    assert len(dedupe_content(items, story_dedupe=True)) == 2


def test_story_dedupe_keeps_announcements_that_differ_by_one_entity():
    """Two different announcements in the same house style differ by exactly one
    word (Taiwan/Japan). similarity_threshold=1.0 disables the character check so
    this exercises the story rule alone -- see the note below on what the
    character check does with this pair."""
    items = [
        _search_item("Nvidia opens a Taiwan research office", "2026-08-03T10:00:00"),
        _search_item("Nvidia opens a Japan research office", "2026-08-03T09:00:00"),
    ]
    assert len(dedupe_content(items, similarity_threshold=1.0, story_dedupe=True)) == 2


def test_character_similarity_still_merges_near_identical_headlines():
    """Pre-existing behaviour, unchanged by story_dedupe and documented here
    because it is a real false positive of its own: these two announcements are
    0.93 similar as strings, so the long-standing 0.85 check merges them
    regardless of the story rule. Lowering that risk means lowering the
    threshold, which trades these away for more duplicates everywhere."""
    items = [
        _search_item("Nvidia opens a Taiwan research office", "2026-08-03T10:00:00"),
        _search_item("Nvidia opens a Japan research office", "2026-08-03T09:00:00"),
    ]
    assert len(dedupe_content(items)) == 1


def test_story_dedupe_needs_more_than_a_couple_of_common_words():
    items = [
        _search_item("Nvidia results beat expectations", "2026-08-03T10:00:00"),
        _search_item("Nvidia chief visits Berlin", "2026-08-03T09:00:00"),
    ]
    assert len(dedupe_content(items, story_dedupe=True)) == 2


def test_story_dedupe_keeps_stories_that_differ_only_by_a_swapped_entity():
    """The case the minimum-distinct-tokens rule exists for. By containment
    alone these score 0.83 -- HIGHER than two genuine duplicates of one story
    (0.60) -- so no symmetric overlap threshold can separate them. What does
    separate them: two outlets rewriting one story each contribute several words
    of their own, while these differ by exactly one (Fortinet/Ivanti)."""
    items = [
        _search_item("CISA adds Fortinet flaw to KEV catalog", "2026-08-03T10:00:00", "CISA"),
        _search_item("CISA adds Ivanti flaw to KEV catalog", "2026-08-03T09:00:00", "CISA"),
    ]
    assert len(dedupe_content(items, similarity_threshold=1.0, story_dedupe=True)) == 2


def test_story_dedupe_clusters_transitively():
    """A story covered by four outlets: the fourth headline can resemble the
    third -- itself already removed -- without resembling the first one still
    standing. Comparing only against survivors would leave it in."""
    kept = dedupe_content(NAVER, similarity_threshold=1.0, story_dedupe=True)
    assert len(kept) == 2
