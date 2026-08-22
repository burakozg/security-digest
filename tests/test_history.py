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


# Search (`query=`) -- matches title, summary and source; every whitespace-
# separated word must appear somewhere, so terms narrow rather than widen.

def _searchable(tmp_path):
    db_path = tmp_path / "test.db"
    record_sent(
        [
            {"title": "Naver wins Saudi approval", "link": "https://x.com/1",
             "source": "KED Global", "summary": "Digital twin platform.", "category": "key"},
            {"title": "Ransomware affiliate poses as recovery firm", "link": "https://x.com/2",
             "source": "Bleeping Computer", "summary": "Impersonation of recovery services.",
             "category": "news"},
            {"title": "Password spraying surges", "link": "https://x.com/3",
             "source": "Krebs", "summary": "Attackers exploit MFA gaps in Saudi banks.",
             "category": "news"},
        ],
        "Security Digest", db_path=db_path,
    )
    return db_path


def test_search_matches_title(tmp_path):
    entries, total = load_entries(query="ransomware", db_path=_searchable(tmp_path))
    assert total == 1
    assert entries[0]["title"].startswith("Ransomware affiliate")


def test_search_matches_summary(tmp_path):
    entries, total = load_entries(query="MFA", db_path=_searchable(tmp_path))
    assert total == 1
    assert entries[0]["title"] == "Password spraying surges"


def test_search_matches_source(tmp_path):
    entries, total = load_entries(query="Bleeping", db_path=_searchable(tmp_path))
    assert total == 1
    assert entries[0]["source"] == "Bleeping Computer"


def test_search_is_case_insensitive(tmp_path):
    _, total = load_entries(query="krebs", db_path=_searchable(tmp_path))
    assert total == 1


def test_search_terms_are_anded_across_different_fields(tmp_path):
    """"saudi krebs" must match only the row where one term is in the summary
    and the other in the source -- an OR would return two rows here."""
    entries, total = load_entries(query="saudi krebs", db_path=_searchable(tmp_path))
    assert total == 1
    assert entries[0]["title"] == "Password spraying surges"


def test_search_word_order_does_not_matter(tmp_path):
    db = _searchable(tmp_path)
    assert load_entries(query="saudi naver", db_path=db)[1] == 1
    assert load_entries(query="naver saudi", db_path=db)[1] == 1


def test_search_with_no_match_is_empty_not_everything(tmp_path):
    entries, total = load_entries(query="kubernetes", db_path=_searchable(tmp_path))
    assert (entries, total) == ([], 0)


def test_search_composes_with_the_digest_filter(tmp_path):
    db_path = tmp_path / "test.db"
    record_sent([{"title": "Shared term here", "link": "https://x.com/a"}], "Alpha", db_path=db_path)
    record_sent([{"title": "Shared term here too", "link": "https://x.com/b"}], "Beta", db_path=db_path)

    _, both = load_entries(query="shared", db_path=db_path)
    entries, scoped = load_entries(query="shared", digest_slug="alpha", db_path=db_path)

    assert both == 2 and scoped == 1
    assert entries[0]["digest_title"] == "Alpha"


def test_like_wildcards_in_a_term_are_literal(tmp_path):
    """Unescaped, "50%" is "50 followed by anything" and matches both rows."""
    db_path = tmp_path / "test.db"
    record_sent(
        [
            {"title": "Costs rose 50% this quarter", "link": "https://x.com/1"},
            {"title": "Costs rose 50 percent this quarter", "link": "https://x.com/2"},
        ],
        "Digest", db_path=db_path,
    )

    entries, total = load_entries(query="50%", db_path=db_path)
    assert total == 1
    assert "50%" in entries[0]["title"]


def test_underscore_in_a_term_is_literal(tmp_path):
    db_path = tmp_path / "test.db"
    record_sent(
        [
            {"title": "field a_b renamed", "link": "https://x.com/1"},
            {"title": "field axb renamed", "link": "https://x.com/2"},
        ],
        "Digest", db_path=db_path,
    )

    entries, total = load_entries(query="a_b", db_path=db_path)
    assert total == 1
    assert "a_b" in entries[0]["title"]


def test_empty_and_whitespace_queries_behave_like_no_query(tmp_path):
    db = _searchable(tmp_path)
    baseline = load_entries(db_path=db)[1]
    assert load_entries(query="", db_path=db)[1] == baseline
    assert load_entries(query="   ", db_path=db)[1] == baseline
    assert load_entries(query=None, db_path=db)[1] == baseline


def test_total_and_pagination_follow_the_filtered_set(tmp_path):
    db_path = tmp_path / "test.db"
    record_sent(
        [{"title": f"Match {i}", "link": f"https://x.com/{i}"} for i in range(5)]
        + [{"title": f"Other {i}", "link": f"https://y.com/{i}"} for i in range(5)],
        "Digest", db_path=db_path,
    )

    page1, total = load_entries(query="match", limit=2, offset=0, db_path=db_path)
    page2, _ = load_entries(query="match", limit=2, offset=2, db_path=db_path)

    assert total == 5, "total must count matches, not the whole table"
    assert len(page1) == 2 and len(page2) == 2
    assert {e["title"] for e in page1}.isdisjoint({e["title"] for e in page2})
    assert all(e["title"].startswith("Match") for e in page1 + page2)


def test_a_very_long_query_is_capped_rather_than_building_a_huge_statement(tmp_path):
    from src.history import MAX_QUERY_TERMS

    db = _searchable(tmp_path)
    terms = " ".join(f"w{i}" for i in range(MAX_QUERY_TERMS + 40))
    assert load_entries(query=terms, db_path=db)[1] == 0
