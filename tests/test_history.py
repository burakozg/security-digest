"""Tests for src.history: SQLite-backed digest delivery log (task 3.4)."""

import json

from src.history import load_entries, record_sent


def test_load_entries_empty_returns_nothing(tmp_path):
    db_path = tmp_path / "test.db"
    entries, total = load_entries(db_path=db_path)
    assert entries == []
    assert total == 0


def test_record_sent_then_load_entries_roundtrips(tmp_path):
    db_path = tmp_path / "test.db"
    items = [
        {"title": "Story A", "link": "https://x.com/a", "source": "Krebs", "summary": "S1", "category": "news"},
        {"title": "Story B", "link": "https://x.com/b", "source": "Krebs", "summary": "S2", "category": "ai"},
    ]
    record_sent(items, "Security Digest", db_path=db_path)

    entries, total = load_entries(db_path=db_path)
    assert total == 2
    assert len(entries) == 2
    assert entries[0]["digest_slug"] == "security-digest"
    assert entries[0]["digest_title"] == "Security Digest"


def test_load_entries_newest_first(tmp_path):
    db_path = tmp_path / "test.db"
    record_sent([{"title": "First", "link": "https://x.com/1"}], "Digest", db_path=db_path)
    record_sent([{"title": "Second", "link": "https://x.com/2"}], "Digest", db_path=db_path)

    entries, _ = load_entries(db_path=db_path)
    assert [e["title"] for e in entries] == ["Second", "First"]


def test_load_entries_pagination(tmp_path):
    db_path = tmp_path / "test.db"
    for i in range(5):
        record_sent([{"title": f"Story {i}", "link": f"https://x.com/{i}"}], "Digest", db_path=db_path)

    page1, total = load_entries(db_path=db_path, limit=2, offset=0)
    page2, _ = load_entries(db_path=db_path, limit=2, offset=2)
    assert total == 5
    assert len(page1) == 2
    assert len(page2) == 2
    # Newest-first, non-overlapping pages
    assert {e["title"] for e in page1}.isdisjoint({e["title"] for e in page2})


def test_load_entries_filters_by_digest_slug(tmp_path):
    db_path = tmp_path / "test.db"
    record_sent([{"title": "A", "link": "https://x.com/a"}], "Security Digest", db_path=db_path)
    record_sent([{"title": "B", "link": "https://x.com/b"}], "AI News", db_path=db_path)

    entries, total = load_entries(db_path=db_path, digest_slug="ai-news")
    assert total == 1
    assert entries[0]["title"] == "B"


def test_record_sent_trims_to_max_entries(tmp_path):
    db_path = tmp_path / "test.db"
    config = {"history": {"max_entries": 3}}
    for i in range(5):
        record_sent([{"title": f"Story {i}", "link": f"https://x.com/{i}"}], "Digest", config=config, db_path=db_path)

    entries, total = load_entries(db_path=db_path, limit=100)
    assert total == 3
    # Keeps the most recent 3
    assert {e["title"] for e in entries} == {"Story 2", "Story 3", "Story 4"}


def test_record_sent_with_no_items_is_a_noop(tmp_path):
    db_path = tmp_path / "test.db"
    record_sent([], "Digest", db_path=db_path)
    entries, total = load_entries(db_path=db_path)
    assert entries == []
    assert total == 0


def test_migrates_legacy_history_json(tmp_path, monkeypatch):
    import src.history as history_module

    legacy_path = tmp_path / "digest_history.json"
    legacy_path.write_text(json.dumps([
        {
            "sent_at": "2026-01-01T00:00:00",
            "digest_title": "Security Digest",
            "digest_slug": "security-digest",
            "title": "Legacy Story",
            "link": "https://legacy.com/1",
            "source": "Test",
            "summary": "Summary",
            "category": "news",
        }
    ]))
    monkeypatch.setattr(history_module, "_LEGACY_JSON_PATH", legacy_path)

    db_path = tmp_path / "test.db"
    entries, total = load_entries(db_path=db_path)
    assert total == 1
    assert entries[0]["title"] == "Legacy Story"
