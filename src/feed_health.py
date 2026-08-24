"""Per-feed fetch health, so a dead feed is visible instead of silent.

fetch_feed() has always swallowed a failed feed: it logs one WARNING and returns
an empty list, and the run carries on. That is the right behaviour for the
pipeline -- one publisher being down must not cost the whole digest -- but it
means a feed can stop working and nothing says so anywhere a person looks.
Threatpost and CSO Online sat dead in sources.yaml for months exactly that way.

Two kinds of dead, and the second is the one that hides:

  * BROKEN -- the fetch or the parse failed, or the feed had no entries at all.
    Visible in the log, at least, if anyone reads it.
  * QUIET -- HTTP 200, valid XML, entries present, and the newest of them is
    weeks old. Nothing errors, nothing warns, and the feed contributes nothing.
    A publisher that shuts down usually leaves this behind.

Health is recorded by the real run rather than probed on a timer: it costs no
extra traffic, and it reports what the pipeline actually experienced rather than
what a separate request happens to get. `check_now` exists for the other case --
adding a feed, or testing one you have just fixed, where waiting for tomorrow's
run is no use.
"""

from __future__ import annotations

import datetime
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from src.db import get_connection

log = logging.getLogger(__name__)

OK = "ok"
EMPTY = "empty"
ERROR = "error"
QUIET = "quiet"
UNKNOWN = "unknown"

# How stale the newest entry has to be before a feed that fetches perfectly well
# is called quiet. Three weeks: long enough that a low-volume blog or a holiday
# gap does not trip it, short enough to notice a publisher that has stopped.
QUIET_AFTER_DAYS = 21

# Probing every feed from the admin page has to be quick enough to wait for, so
# it runs them in parallel and does NOT retry -- a retry storm across a dozen
# dead feeds is a minute of spinner. The daily run still retries properly.
CHECK_WORKERS = 6

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS feed_health (
        url           TEXT PRIMARY KEY,
        name          TEXT NOT NULL,
        checked_at    TEXT NOT NULL,
        status        TEXT NOT NULL,
        detail        TEXT,
        items         INTEGER NOT NULL DEFAULT 0,
        newest        TEXT,
        ok_at         TEXT,
        failing_since TEXT
    )
    """,
)


def _ensure_schema(conn) -> None:
    for statement in _SCHEMA:
        conn.execute(statement)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse(ts: str | None) -> datetime.datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def error_detail(exc: BaseException) -> str:
    """A short phrase naming why a fetch failed, for a table cell.

    str(exc) on an httpx error is a multi-line paragraph with a documentation
    URL in it -- true, and useless in a column. The status code is the part that
    tells you whether to fix the URL or wait."""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    if isinstance(exc, httpx.TooManyRedirects):
        return "too many redirects"
    if isinstance(exc, httpx.TransportError):
        # ConnectError, ReadError, and the DNS failures underneath them.
        return type(exc).__name__.replace("Error", " error").strip().lower()
    text = " ".join(str(exc).split())
    return (text[:60] + "…") if len(text) > 60 else (text or type(exc).__name__)


# --- recording --------------------------------------------------------------


def record(results: list[dict[str, Any]], db_path: Path | str | None = None) -> None:
    """Store one run's per-feed outcomes.

    `failing_since` is carried forward rather than recomputed: the length of a
    failure is the single most useful thing in the column ("failing 12d" is what
    makes you go and look), and it is only knowable by remembering when the
    streak started."""
    if not results:
        return
    checked_at = _now().isoformat(timespec="seconds")
    conn = get_connection(db_path)
    try:
        _ensure_schema(conn)
        previous = {
            row["url"]: row
            for row in conn.execute("SELECT url, status, ok_at, failing_since FROM feed_health")
        }
        rows = []
        for r in results:
            url = str(r.get("url", ""))
            if not url:
                continue
            status = str(r.get("status") or ERROR)
            prev = previous.get(url)
            if status == OK:
                ok_at, failing_since = checked_at, None
            else:
                ok_at = (prev["ok_at"] if prev else None)
                # Keep the start of the streak if one is already running.
                failing_since = (
                    prev["failing_since"] if prev and prev["failing_since"] else checked_at
                )
            rows.append((
                url, str(r.get("name") or url), checked_at, status,
                r.get("detail"), int(r.get("items") or 0), r.get("newest") or None,
                ok_at, failing_since,
            ))
        conn.executemany(
            "INSERT INTO feed_health "
            "(url, name, checked_at, status, detail, items, newest, ok_at, failing_since) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET "
            "name=excluded.name, checked_at=excluded.checked_at, status=excluded.status, "
            "detail=excluded.detail, items=excluded.items, newest=excluded.newest, "
            "ok_at=excluded.ok_at, failing_since=excluded.failing_since",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def prune(urls: list[str], db_path: Path | str | None = None) -> None:
    """Forget feeds that are no longer configured.

    Rows are keyed by URL so that renaming a feed in the admin panel keeps its
    history; the cost is that *changing* a URL leaves the old row behind, which
    would otherwise accumulate for ever and show a stale red row against a feed
    nobody has any more."""
    if not urls:
        return
    conn = get_connection(db_path)
    try:
        _ensure_schema(conn)
        placeholders = ",".join("?" * len(urls))
        conn.execute(f"DELETE FROM feed_health WHERE url NOT IN ({placeholders})", urls)
        conn.commit()
    finally:
        conn.close()


def load(db_path: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Everything recorded, keyed by feed URL."""
    conn = get_connection(db_path)
    try:
        _ensure_schema(conn)
        rows = conn.execute("SELECT * FROM feed_health").fetchall()
    finally:
        conn.close()
    return {row["url"]: dict(row) for row in rows}


# --- presentation -----------------------------------------------------------


def _days_since(ts: str | None, now: datetime.datetime) -> int | None:
    dt = _parse(ts)
    return None if dt is None else max(0, (now - dt).days)


def _ago(ts: str | None, now: datetime.datetime) -> str:
    dt = _parse(ts)
    if dt is None:
        return "never"
    seconds = max(0, int((now - dt).total_seconds()))
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def describe(
    row: dict[str, Any] | None,
    now: datetime.datetime | None = None,
    quiet_after_days: int = QUIET_AFTER_DAYS,
) -> dict[str, Any]:
    """Turn a stored row into what the column shows.

    `quiet` is derived here rather than stored so that changing the threshold
    re-reads the existing data instead of needing a fresh run against every feed.
    A feed whose entries carry no dates at all is never called quiet -- that is
    a feed that omits a field, not a publisher that has stopped, and crying wolf
    about it would train you to ignore the column."""
    now = now or _now()
    if not row:
        return {"status": UNKNOWN, "summary": "never checked", "detail": None,
                "items": 0, "checked_at": None, "failing_days": None}

    status = str(row.get("status") or UNKNOWN)
    items = int(row.get("items") or 0)
    failing_days = _days_since(row.get("failing_since"), now)
    stale_days = _days_since(row.get("newest"), now)

    if status == OK and stale_days is not None and stale_days >= quiet_after_days:
        status = QUIET

    if status == ERROR:
        head = str(row.get("detail") or "fetch failed")
    elif status == EMPTY:
        head = "no entries"
    elif status == QUIET:
        head = f"{items} item{'' if items == 1 else 's'}, newest {stale_days}d old"
    else:
        head = f"OK · {items} item{'' if items == 1 else 's'}"

    if status in (ERROR, EMPTY) and failing_days is not None:
        tail = "failing today" if failing_days == 0 else f"failing {failing_days}d"
    else:
        tail = f"checked {_ago(row.get('checked_at'), now)}"

    return {
        "status": status,
        "summary": f"{head} · {tail}",
        "detail": row.get("detail"),
        "items": items,
        "checked_at": row.get("checked_at"),
        "failing_days": failing_days,
        "stale_days": stale_days,
    }


# --- on-demand probe --------------------------------------------------------


def check_now(
    feeds: list[dict[str, Any]], config: dict[str, Any] | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Fetch these feeds right now, record the outcome, and return it.

    For the case the recorded data cannot serve: a feed you have just added or
    just fixed, where "wait until tomorrow morning" is not an answer."""
    from src.fetcher import fetch_feed

    # No retries and no backoff -- see CHECK_WORKERS.
    probe_config = {**(config or {}), "retry": {"max_retries": 0, "initial_delay": 0}}

    def probe(feed: dict[str, Any]) -> dict[str, Any]:
        health: dict[str, Any] = {"url": feed["url"], "name": feed.get("name") or feed["url"]}
        try:
            fetch_feed(feed["url"], health["name"], limit=1, config=probe_config, health=health)
        except Exception as e:                      # pragma: no cover - defensive
            health.update(status=ERROR, detail=error_detail(e), items=0)
        return health

    if not feeds:
        return []
    with ThreadPoolExecutor(max_workers=min(CHECK_WORKERS, len(feeds))) as pool:
        results = list(pool.map(probe, feeds))
    record(results, db_path)
    return results
