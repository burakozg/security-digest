"""Track pipeline run status for web UI. Backed by SQLite (see src/db.py) --
migrated from a single JSON file that was rewritten wholesale on every update
with no locking, from both the web /run thread and the scheduled APScheduler
job."""

import datetime
import json
import logging
from pathlib import Path
from typing import Any

from src.db import get_connection
from src.utils import PROJECT_ROOT

log = logging.getLogger(__name__)

_LEGACY_JSON_PATH = PROJECT_ROOT / "data" / "status.json"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL,
    last_run TEXT,
    items_processed INTEGER NOT NULL DEFAULT 0,
    previous_delivered INTEGER NOT NULL DEFAULT 0,
    error TEXT
)
"""


def _ensure_schema(conn) -> None:
    conn.execute(_SCHEMA)
    # One-time migration from the legacy status.json: only runs while the
    # table is still empty, so it's a no-op on every call after the first.
    if _LEGACY_JSON_PATH.exists():
        row = conn.execute("SELECT 1 FROM status WHERE id = 1").fetchone()
        if row is None:
            try:
                data = json.loads(_LEGACY_JSON_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                log.warning("Could not read legacy %s for migration", _LEGACY_JSON_PATH)
                return
            conn.execute(
                "INSERT INTO status (id, status, last_run, items_processed, previous_delivered, error) "
                "VALUES (1, ?, ?, ?, ?, ?)",
                (
                    data.get("status", "idle"),
                    data.get("last_run"),
                    int(data.get("items_processed", 0) or 0),
                    1 if data.get("previous_delivered") else 0,
                    data.get("error"),
                ),
            )
            conn.commit()
            log.info("Migrated %s into digest.db", _LEGACY_JSON_PATH)


def update(
    status: str,
    items: int = 0,
    error: str | None = None,
    previous_delivered: bool = False,
    db_path: Path | str | None = None,
) -> None:
    """Update run status. status: running | success | failure."""
    conn = get_connection(db_path)
    try:
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO status (id, status, last_run, items_processed, previous_delivered, error) "
            "VALUES (1, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "status=excluded.status, last_run=excluded.last_run, "
            "items_processed=excluded.items_processed, "
            "previous_delivered=excluded.previous_delivered, error=excluded.error",
            (
                status,
                datetime.datetime.now().isoformat(),
                items,
                1 if previous_delivered else 0,
                str(error) if error else None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get(db_path: Path | str | None = None) -> dict[str, Any]:
    """Read current status."""
    conn = get_connection(db_path)
    try:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT status, last_run, items_processed, previous_delivered, error FROM status WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return {"status": "idle", "last_run": None, "items_processed": 0, "previous_delivered": False}
    result: dict[str, Any] = {
        "status": row["status"],
        "last_run": row["last_run"],
        "items_processed": row["items_processed"],
        "previous_delivered": bool(row["previous_delivered"]),
    }
    if row["error"]:
        result["error"] = row["error"]
    return result
