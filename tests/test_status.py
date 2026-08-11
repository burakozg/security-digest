"""Tests for src.status: SQLite-backed pipeline run status (task 3.4)."""

import json

from src.status import get, update


def test_get_returns_idle_defaults_when_never_updated(tmp_path):
    db_path = tmp_path / "test.db"
    assert get(db_path) == {
        "status": "idle",
        "last_run": None,
        "items_processed": 0,
        "previous_delivered": False,
    }


def test_update_then_get_roundtrips(tmp_path):
    db_path = tmp_path / "test.db"
    update("success", items=5, previous_delivered=False, db_path=db_path)
    result = get(db_path)
    assert result["status"] == "success"
    assert result["items_processed"] == 5
    assert result["previous_delivered"] is False
    assert result["last_run"] is not None
    assert "error" not in result


def test_update_includes_error_when_provided(tmp_path):
    db_path = tmp_path / "test.db"
    update("failure", error="boom", db_path=db_path)
    result = get(db_path)
    assert result["status"] == "failure"
    assert result["error"] == "boom"


def test_update_overwrites_previous_status(tmp_path):
    """Only ever one row (singleton) -- the latest update wins."""
    db_path = tmp_path / "test.db"
    update("running", db_path=db_path)
    update("success", items=3, db_path=db_path)
    result = get(db_path)
    assert result["status"] == "success"
    assert result["items_processed"] == 3


def test_migrates_legacy_status_json(tmp_path, monkeypatch):
    import src.status as status_module

    legacy_path = tmp_path / "status.json"
    legacy_path.write_text(json.dumps({
        "status": "success",
        "last_run": "2026-01-01T00:00:00",
        "items_processed": 7,
        "previous_delivered": True,
    }))
    monkeypatch.setattr(status_module, "_LEGACY_JSON_PATH", legacy_path)

    db_path = tmp_path / "test.db"
    result = get(db_path)
    assert result["status"] == "success"
    assert result["items_processed"] == 7
    assert result["previous_delivered"] is True


def test_migration_runs_only_once(tmp_path, monkeypatch):
    """Idempotent: once the table has a row (from migration or otherwise),
    the legacy file is never re-imported, even if it's still present and
    has since changed."""
    import src.status as status_module

    legacy_path = tmp_path / "status.json"
    legacy_path.write_text(json.dumps({"status": "success", "items_processed": 1}))
    monkeypatch.setattr(status_module, "_LEGACY_JSON_PATH", legacy_path)

    db_path = tmp_path / "test.db"
    first = get(db_path)  # triggers the one-time migration
    assert first["status"] == "success"
    assert first["items_processed"] == 1

    # Legacy file changes after migration already ran -- must be ignored.
    legacy_path.write_text(json.dumps({"status": "failure", "items_processed": 999}))
    second = get(db_path)
    assert second["status"] == "success"
    assert second["items_processed"] == 1
