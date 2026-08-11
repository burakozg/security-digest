"""Shared pytest fixtures."""

import pytest


@pytest.fixture(autouse=True)
def isolate_legacy_json_migration(tmp_path, monkeypatch):
    """src.dedupe, src.status, and src.history each have a module-level
    _LEGACY_JSON_PATH pointing at this project's real data/*.json files, used
    for the one-time migration into SQLite. Without this fixture, any test
    using an isolated tmp_path db (but not explicitly testing migration)
    would still trigger a real migration FROM this project's actual
    data/seen.json, data/status.json, data/digest_history.json -- which
    genuinely exist and hold real history from running this app -- polluting
    the test's "isolated" database with production data.

    Point all three at paths that don't exist by default; tests that
    specifically exercise migration override this themselves via
    monkeypatch.setattr(<module>, "_LEGACY_JSON_PATH", ...).
    """
    import src.dedupe
    import src.history
    import src.status

    monkeypatch.setattr(src.dedupe, "_LEGACY_JSON_PATH", tmp_path / "_no_legacy_seen.json")
    monkeypatch.setattr(src.status, "_LEGACY_JSON_PATH", tmp_path / "_no_legacy_status.json")
    monkeypatch.setattr(src.history, "_LEGACY_JSON_PATH", tmp_path / "_no_legacy_history.json")
