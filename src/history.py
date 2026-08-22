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

# Cap on search terms taken from one query. Each term costs three LIKE
# comparisons, so a pasted paragraph would otherwise build a statement with
# hundreds of them against every row.
MAX_QUERY_TERMS = 10

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


_COLUMNS = "sent_at, digest_title, digest_slug, title, link, source, summary, category"

# Searched fields: the three that carry meaning to someone looking for a past
# item. `link` is deliberately excluded -- a URL fragment matching would return
# items whose visible text has nothing to do with the term.
_SEARCH_COLUMNS = ("title", "summary", "source")


def _like_escape(term: str) -> str:
    """Neutralise LIKE wildcards in a user's term.

    Without this a search for "50%" matches "50" followed by anything, and "a_b"
    matches "axb" -- results the person typing them would call wrong. The
    backslash must be doubled first, or it would escape the escapes.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _filters(digest_slug: str | None, query: str | None) -> tuple[str, list[Any]]:
    """Build the WHERE clause shared by the count and the page query.

    Every term must appear in at least one searched column, and terms are
    AND-ed, so "naver saudi" finds the story whichever way round the outlet
    wrote the headline. An empty or whitespace-only query contributes nothing,
    which is what keeps the unfiltered path identical to what it was.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if digest_slug:
        clauses.append("digest_slug = ?")
        params.append(digest_slug)
    for term in (query or "").split()[:MAX_QUERY_TERMS]:
        clauses.append(
            "(" + " OR ".join(f"{col} LIKE ? ESCAPE '\\'" for col in _SEARCH_COLUMNS) + ")"
        )
        params += [f"%{_like_escape(term)}%"] * len(_SEARCH_COLUMNS)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def load_entries(
    config: dict[str, Any] | None = None,
    limit: int = 200,
    offset: int = 0,
    digest_slug: str | None = None,
    query: str | None = None,
    db_path: Path | str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Return newest-first slice and total matching count.

    `total` is the count *after* filtering, which is what lets the page's
    "Showing N of M" and its Load more button walk the filtered set."""
    where, params = _filters(digest_slug, query)
    conn = get_connection(db_path)
    try:
        _ensure_schema(conn)
        # `where` is assembled from literal fragments only -- every user-supplied
        # value reaches SQLite through a ? placeholder, never through the f-string.
        total = conn.execute(f"SELECT COUNT(*) FROM history{where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM history{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    finally:
        conn.close()

    return [dict(row) for row in rows], total
