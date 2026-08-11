"""Append-only log of digest items that were delivered. Backed by SQLite (see
src/db.py) -- migrated from a JSON array rewritten wholesale on every append,
which meant load_entries() had to load the entire history into memory to
paginate it. Pagination is now a real LIMIT/OFFSET query."""

import datetime
import json
import logging
from pathlib import Path
from typing import Any

from src.db import get_connection
from src.utils import PROJECT_ROOT, slug

log = logging.getLogger(__name__)

DEFAULT_MAX_ENTRIES = 10000

_LEGACY_JSON_PATH = PROJECT_ROOT / "data" / "digest_history.json"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at TEXT NOT NULL,
    digest_title TEXT NOT NULL,
    digest_slug TEXT NOT NULL,
    title TEXT NOT NULL,
    link TEXT NOT NULL,
    source TEXT NOT NULL,
    summary TEXT NOT NULL,
    category TEXT NOT NULL
)
"""
_SCHEMA_INDEX = "CREATE INDEX IF NOT EXISTS idx_history_digest_slug ON history(digest_slug)"


def _max_entries(config: dict[str, Any] | None) -> int:
    cfg = (config or {}).get("history", {}) if config else {}
    return int(cfg.get("max_entries", DEFAULT_MAX_ENTRIES))


def _ensure_schema(conn) -> None:
    conn.execute(_SCHEMA)
    conn.execute(_SCHEMA_INDEX)
    # One-time migration from the legacy digest_history.json: only runs while
    # the table is still empty, so it's a no-op on every call after the first.
    # Insertion order is preserved (oldest first) so `id` ordering matches the
    # original chronological order.
    if _LEGACY_JSON_PATH.exists():
        row = conn.execute("SELECT 1 FROM history LIMIT 1").fetchone()
        if row is None:
            try:
                raw = _LEGACY_JSON_PATH.read_text(encoding="utf-8")
                rows = json.loads(raw) if raw.strip() else []
            except (json.JSONDecodeError, OSError):
                log.warning("Could not read legacy %s for migration", _LEGACY_JSON_PATH)
                return
            if isinstance(rows, list) and rows:
                conn.executemany(
                    "INSERT INTO history (sent_at, digest_title, digest_slug, title, link, source, summary, category) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            r.get("sent_at", ""),
                            r.get("digest_title", ""),
                            r.get("digest_slug", ""),
                            r.get("title", ""),
                            r.get("link", ""),
                            r.get("source", ""),
                            r.get("summary", ""),
                            r.get("category", ""),
                        )
                        for r in rows
                        if isinstance(r, dict)
                    ],
                )
                conn.commit()
                log.info("Migrated %d history rows from %s into digest.db", len(rows), _LEGACY_JSON_PATH)


def record_sent(
    items: list[dict[str, Any]],
    digest_title: str,
    config: dict[str, Any] | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Record each item as delivered in the digest (for history UI)."""
    if not items:
        return
    max_entries = _max_entries(config)
    sent_at = datetime.datetime.now().isoformat(timespec="seconds")
    digest_slug = slug(digest_title)

    conn = get_connection(db_path)
    try:
        _ensure_schema(conn)
        conn.executemany(
            "INSERT INTO history (sent_at, digest_title, digest_slug, title, link, source, summary, category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    sent_at,
                    digest_title,
                    digest_slug,
                    item.get("title", ""),
                    item.get("link", ""),
                    item.get("source", ""),
                    item.get("summary", ""),
                    item.get("category", ""),
                )
                for item in items
            ],
        )
        # Trim to max_entries, keeping the most recently inserted rows.
        conn.execute(
            "DELETE FROM history WHERE id NOT IN "
            "(SELECT id FROM history ORDER BY id DESC LIMIT ?)",
            (max_entries,),
        )
        conn.commit()
    finally:
        conn.close()


def load_entries(
    config: dict[str, Any] | None = None,
    limit: int = 200,
    offset: int = 0,
    digest_slug: str | None = None,
    db_path: Path | str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Return newest-first slice and total matching count."""
    conn = get_connection(db_path)
    try:
        _ensure_schema(conn)
        if digest_slug:
            total = conn.execute(
                "SELECT COUNT(*) FROM history WHERE digest_slug = ?", (digest_slug,)
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT sent_at, digest_title, digest_slug, title, link, source, summary, category "
                "FROM history WHERE digest_slug = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                (digest_slug, limit, offset),
            ).fetchall()
        else:
            total = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
            rows = conn.execute(
                "SELECT sent_at, digest_title, digest_slug, title, link, source, summary, category "
                "FROM history ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
    finally:
        conn.close()

    return [dict(row) for row in rows], total
