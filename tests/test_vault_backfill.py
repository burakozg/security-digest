"""Rebuilding the vault from the history table, and the limits of doing so."""

import sqlite3

import pytest

from src.history import iter_all, record_sent
from src.vault import backfill

CONFIG = {
    "vault": {"enabled": False, "min_mentions": 2},
    "llm": {"extract_entities": True},
    "digests": [
        {"title": "Security Digest", "domain": "security", "sections": ["news"]},
        {"title": "AI News", "domain": "ai_ml", "sections": ["news"]},
    ],
}


@pytest.fixture
def history(tmp_path):
    db = tmp_path / "d.db"
    record_sent([
        {"title": "Fortinet RCE", "link": "https://a/1", "source": "Krebs",
         "summary": "A flaw.", "category": "news"},
        {"title": "Okta breach", "link": "https://a/2", "source": "BleepingComputer",
         "summary": "A breach.", "category": "news"},
    ], "Security Digest", CONFIG, db)
    record_sent([
        {"title": "Model launch", "link": "https://a/3", "source": "The Verge",
         "summary": "A model.", "category": "news"},
    ], "AI News", CONFIG, db)
    return db


@pytest.fixture
def stub(monkeypatch):
    """Entities without a model. Records the calls so a test can assert none."""
    calls = []

    def fake(rows, client, config):
        calls.append(len(rows))
        return [["Fortinet"] if "Fortinet" in r["title"] else ["Okta"] for r in rows]

    monkeypatch.setattr(backfill, "entities_for", fake)
    monkeypatch.setattr(backfill, "_get_client", lambda cfg: None)
    return calls


def test_history_iterates_oldest_first(history):
    rows = iter_all(db_path=history)
    assert [r["title"] for r in rows] == ["Fortinet RCE", "Okta breach", "Model launch"]


def test_since_filters_by_date(history):
    assert iter_all(since="2999-01-01", db_path=history) == []
    assert len(iter_all(since="2000-01-01", db_path=history)) == 3


def test_dry_run_spends_nothing_and_writes_nothing(history, stub, tmp_path):
    base = tmp_path / "vault"
    result = backfill.run(dry_run=True, base=base, db_path=history, config=CONFIG)

    assert result == {"rows": 3, "todo": 3, "batches": 1,
                      "written": 0, "projected": 0, "skipped": 0}
    assert stub == []                       # no model call
    assert not list(base.rglob("*.md"))     # nothing on disk


def test_backfill_writes_one_story_note_per_row(history, stub, tmp_path):
    base = tmp_path / "vault"
    backfill.run(base=base, db_path=history, config=CONFIG)

    assert len(list(base.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/*.md"))) == 3
    # Filed by month, not in one flat folder.
    assert not (base / "stories").exists()
    # No per-day index notes: they carried no information and no inbound links.
    assert not (base / "digests").exists()


def test_backfilled_notes_are_marked_and_have_no_raw_content(history, stub, tmp_path):
    base = tmp_path / "vault"
    backfill.run(base=base, db_path=history, config=CONFIG)

    note = next(base.glob("*/*/*fortinet-rce.md")).read_text()
    assert "backfilled: true" in note
    # The history table never stored `description`; inventing a Raw content
    # section from the summary would be a lie the note could not be told from a
    # real one.
    assert "## Raw content" not in note
    assert "A flaw." in note


def test_domain_is_recovered_from_the_digest(history, stub, tmp_path):
    base = tmp_path / "vault"
    backfill.run(base=base, db_path=history, config=CONFIG)

    assert 'domain: "security"' in next(base.glob("*/*/*fortinet-rce.md")).read_text()
    assert 'domain: "ai_ml"' in next(base.glob("*/*/*model-launch.md")).read_text()


def test_a_second_run_calls_no_model_and_writes_nothing(history, stub, tmp_path):
    base = tmp_path / "vault"
    backfill.run(base=base, db_path=history, config=CONFIG)
    before = {p: p.read_text() for p in base.rglob("*.md")}
    stub.clear()

    result = backfill.run(base=base, db_path=history, config=CONFIG)

    assert stub == []
    assert result["todo"] == 0
    assert {p: p.read_text() for p in base.rglob("*.md")} == before


def test_limit_stops_early_and_the_rest_are_picked_up_next_time(history, stub, tmp_path):
    base = tmp_path / "vault"
    assert backfill.run(base=base, limit=1, db_path=history, config=CONFIG)["todo"] == 1
    assert len(list(base.glob("*/*/*.md"))) == 1

    assert backfill.run(base=base, db_path=history, config=CONFIG)["todo"] == 2
    assert len(list(base.glob("*/*/*.md"))) == 3


def test_a_failed_entity_batch_leaves_stories_untagged_rather_than_aborting(
    history, monkeypatch, tmp_path
):
    """A batch the model fluffed costs those stories their topic links. Aborting
    would cost every remaining story the same thing."""
    def explode(*args, **kwargs):
        raise RuntimeError("model said no")

    monkeypatch.setattr(backfill, "_call_llm", explode)
    monkeypatch.setattr(backfill, "_get_client", lambda cfg: None)

    base = tmp_path / "vault"
    result = backfill.run(base=base, db_path=history, config=CONFIG)

    assert result["written"] > 0
    assert len(list(base.glob("*/*/*.md"))) == 3
    assert not list((base / "topics").glob("*.md"))


def test_topic_notes_accumulate_across_the_whole_corpus(history, stub, tmp_path):
    """The point of backfilling at all: a thing named twice over four months has
    a note on day one, instead of waiting for it to be named twice again."""
    record_sent([
        {"title": "Fortinet again", "link": "https://a/9", "source": "Krebs",
         "summary": "More.", "category": "news"},
    ], "Security Digest", CONFIG, history)

    base = tmp_path / "vault"
    backfill.run(base=base, db_path=history, config=CONFIG)

    note = (base / "topics" / "fortinet.md").read_text()
    assert "security_mentions: 2" in note
    assert "fortinet-rce" in note and "fortinet-again" in note
