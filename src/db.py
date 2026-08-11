"""Shared SQLite connection for the seen-store, status, and history tables that
used to be three separate JSON files (data/seen.json, data/status.json,
data/digest_history.json), each rewritten wholesale on every write with no
locking -- a real risk given the web /run thread and APScheduler's scheduled
job can both trigger a pipeline run that writes these.

One file, WAL mode (readers don't block writers and vice versa) plus a
generous busy_timeout (so a momentary write conflict retries instead of
raising "database is locked") replaces all three. Connection-per-call, opened
and closed within a single function call on whichever thread calls it --
never shared across threads, so no check_same_thread=False is needed.
"""

import sqlite3
from pathlib import Path

from src.utils import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "data" / "digest.db"


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with WAL mode and a busy timeout. Caller is
    responsible for closing it (use as a context manager)."""
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn
