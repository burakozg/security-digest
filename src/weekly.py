"""Weekly delivery: gather every day, send once a week.

A recipient can ask for one email a week instead of seven. The coverage is the
same -- the pipeline still runs daily and still fetches, summarises and routes
their items -- but the send is held until their day, and what arrives is one
consolidated edition rather than a replay of seven.

Why the items are QUEUED rather than the send simply skipped: src/main.py marks
an item seen only once some digest routed it, so a weekly digest that is skipped
on a Tuesday leaves its items unseen, and they are re-fetched and re-summarised
at full token cost every day until the send day. Worse, where a topic is shared
with a daily reader the opposite happens -- the item IS routed, IS marked seen,
and has expired from the seen store by the time the weekly digest is built. The
weekly digest therefore takes its normal turn in the routing loop every day, and
only the delivery is deferred.

Why a queue table rather than the existing `history` table, which holds nearly
the right columns: record_sent() means "this was emailed". Writing rows there
daily for someone who has not been emailed makes the History page lie, and those
rows would be subject to history.max_entries trimming. Queuing separately also
keeps the whole item dict -- including the `links` list clustering produced --
which the weekly re-clustering pass uses and the history columns would flatten.
On the send day record_sent() is called once, with the consolidated items, so
History shows exactly what landed in the inbox.
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.db import get_connection
from src.delivery import deliver
from src.digest import build_digest
from src.history import record_sent
from src.utils import PROJECT_ROOT, render_template, slug
from src.vault import project_digest

log = logging.getLogger(__name__)

WEEKLY = "weekly"
DAILY = "daily"
FREQUENCIES = (DAILY, WEEKLY)

WEEKLY_PROMPT_PATH = PROJECT_ROOT / "prompts" / "weekly.txt"
WEEKLY_INTRO_PROMPT_PATH = PROJECT_ROOT / "prompts" / "weekly_intro.txt"

# Saturday. A weekly reader is catching up rather than keeping up, so the
# default lands where there is time to read it.
DEFAULT_SEND_WEEKDAY = 5

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAYS = {name: i for i, name in enumerate(DAY_NAMES)}

# Ceiling on how many queued items are consolidated in one send. Only reachable
# if sends have been failing for weeks; the point is that the edition degrades
# loudly instead of building a prompt too large to answer.
MAX_CONSOLIDATED_ITEMS = 600

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS weekly_pending (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        digest_slug TEXT NOT NULL,
        queued_at   TEXT NOT NULL,
        item        TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_weekly_pending_slug ON weekly_pending(digest_slug)",
    """
    CREATE TABLE IF NOT EXISTS weekly_sends (
        digest_slug TEXT PRIMARY KEY,
        last_sent   TEXT NOT NULL
    )
    """,
)


def _ensure_schema(conn) -> None:
    for statement in _SCHEMA:
        conn.execute(statement)


# --- time -------------------------------------------------------------------
#
# Stored timestamps are UTC and carry their offset. The containers set no TZ and
# run UTC while schedule.txt says Europe/Stockholm, so a naive local timestamp
# would be right only by accident of the run time -- and comparing one against a
# timezone-aware `now` raises.


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse(ts: str) -> datetime.datetime:
    """Read a stored timestamp. A value without an offset (hand-edited row) is
    read as UTC rather than raising when compared against an aware `now`."""
    dt = datetime.datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def schedule_tz(config: dict[str, Any]) -> datetime.tzinfo:
    """The timezone the digest's day is measured in -- the one the reader set the
    run time in, not the container's."""
    name = str((config.get("schedule") or {}).get("timezone") or "UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("Unknown schedule timezone %r, using UTC for the weekly send day", name)
        return datetime.timezone.utc


# --- digest fields ----------------------------------------------------------


def is_weekly(digest: dict[str, Any]) -> bool:
    return str(digest.get("frequency") or DAILY).strip().lower() == WEEKLY


def send_weekday(digest: dict[str, Any]) -> int:
    """0 = Monday .. 6 = Sunday. Accepts 'sat' or 'saturday'; anything else falls
    back to the default rather than silently never sending."""
    raw = str(digest.get("send_day") or "").strip().lower()[:3]
    if raw and raw not in DAYS:
        log.warning("Unknown send_day %r on digest %r, using the default",
                    digest.get("send_day"), digest.get("title"))
    return DAYS.get(raw, DEFAULT_SEND_WEEKDAY)


def send_day_label(digest: dict[str, Any]) -> str:
    return ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday")[send_weekday(digest)]


# --- queue ------------------------------------------------------------------


def queue(
    items: list[dict[str, Any]],
    digest_title: str,
    db_path: Path | str | None = None,
) -> None:
    """Hold items for a weekly digest's next send."""
    if not items:
        return
    queued_at = _utcnow().isoformat(timespec="seconds")
    digest_slug = slug(digest_title)
    conn = get_connection(db_path)
    try:
        _ensure_schema(conn)
        conn.executemany(
            "INSERT INTO weekly_pending (digest_slug, queued_at, item) VALUES (?, ?, ?)",
            [(digest_slug, queued_at, json.dumps(item, default=str)) for item in items],
        )
        conn.commit()
    finally:
        conn.close()


def pending(
    digest_slug: str, db_path: Path | str | None = None
) -> tuple[list[int], list[dict[str, Any]]]:
    """Queued items for one digest, oldest first, with their row ids.

    The ids come back so the caller can delete exactly the rows it sent -- a
    blanket DELETE by slug would also discard anything queued by a concurrent
    run between the read and the send."""
    conn = get_connection(db_path)
    try:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT id, item FROM weekly_pending WHERE digest_slug = ? ORDER BY id",
            (digest_slug,),
        ).fetchall()
    finally:
        conn.close()

    ids: list[int] = []
    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            item = json.loads(row["item"])
        except json.JSONDecodeError:
            log.warning("Discarding unreadable queued item %s for %s", row["id"], digest_slug)
            ids.append(row["id"])
            continue
        if isinstance(item, dict):
            ids.append(row["id"])
            items.append(item)
    return ids, items


def oldest_queued(digest_slug: str, db_path: Path | str | None = None) -> datetime.datetime | None:
    conn = get_connection(db_path)
    try:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT MIN(queued_at) AS first FROM weekly_pending WHERE digest_slug = ?",
            (digest_slug,),
        ).fetchone()
    finally:
        conn.close()
    return _parse(row["first"]) if row and row["first"] else None


def clear(ids: list[int], db_path: Path | str | None = None) -> None:
    if not ids:
        return
    conn = get_connection(db_path)
    try:
        _ensure_schema(conn)
        conn.executemany("DELETE FROM weekly_pending WHERE id = ?", [(i,) for i in ids])
        conn.commit()
    finally:
        conn.close()


def last_sent(digest_slug: str, db_path: Path | str | None = None) -> datetime.datetime | None:
    conn = get_connection(db_path)
    try:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT last_sent FROM weekly_sends WHERE digest_slug = ?", (digest_slug,)
        ).fetchone()
    finally:
        conn.close()
    return _parse(row["last_sent"]) if row else None


def mark_sent(
    digest_slug: str,
    when: datetime.datetime | None = None,
    db_path: Path | str | None = None,
) -> None:
    conn = get_connection(db_path)
    try:
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO weekly_sends (digest_slug, last_sent) VALUES (?, ?) "
            "ON CONFLICT(digest_slug) DO UPDATE SET last_sent = excluded.last_sent",
            (digest_slug, (when or _utcnow()).isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()


# --- due check --------------------------------------------------------------


def _send_boundary(now_local: datetime.datetime, weekday: int) -> datetime.datetime:
    """Midnight starting the most recent occurrence of `weekday` at or before now."""
    return (now_local - datetime.timedelta(days=(now_local.weekday() - weekday) % 7)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def is_due(
    digest: dict[str, Any],
    config: dict[str, Any],
    now: datetime.datetime | None = None,
    db_path: Path | str | None = None,
) -> bool:
    """Whether this weekly digest should go out on this run.

    One rule: the reader's send day has come round since the last time they were
    sent anything. Everything the cadence has to cope with falls out of it --

      * on the day itself, the last send was before this morning, so it goes;
      * a second run the same day finds the send already inside the window, so
        nothing is sent twice;
      * a container down on Saturday still sends on Sunday, because Saturday has
        passed and no send happened in it;
      * a send that FAILED on Saturday is retried on the very next run, for the
        same reason -- which a "seven days since the last send" rule would get
        wrong, making a refused connection cost the whole week.

    Before the first ever send there is nothing to measure from, so the queue's
    own age stands in: a reader added on Monday is not owed the Saturday that
    happened before they existed, and waits for the next one.
    """
    now = now or _utcnow()
    digest_slug = slug(str(digest.get("title", "")))

    reference = last_sent(digest_slug, db_path) or oldest_queued(digest_slug, db_path)
    if reference is None:
        return False

    tz = schedule_tz(config)
    return reference.astimezone(tz) < _send_boundary(now.astimezone(tz), send_weekday(digest))


# --- consolidation ----------------------------------------------------------


def _intro_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"intro": {"type": "string"}},
        "required": ["intro"],
        "additionalProperties": False,
    }


def _as_cluster_input(item: dict[str, Any]) -> dict[str, Any]:
    """A queued item is already summarised, so its prose lives in `summary` --
    but the cluster formatter reads `description`. Map it across rather than
    handing the model an item that reads as having no content."""
    return {**item, "description": item.get("summary") or item.get("description") or ""}


def consolidate(
    items: list[dict[str, Any]], config: dict[str, Any]
) -> list[dict[str, Any]]:
    """Merge a week of already-summarised items into one edition's entries.

    One LLM call per topic, as src.summariser._cluster_all does -- here for size
    and readability rather than routing correctness, since every queued item
    already belongs to this one digest. The pass merges a Monday story with its
    Thursday follow-up into a single entry keeping both outlets' links, and
    re-ranks the categories against the week rather than against one day.

    A group whose call fails is kept exactly as it was queued: the items are
    already summarised, so falling through with them costs the reader some
    duplication and nothing else. Re-summarising them would be worse.
    """
    from src.summariser import (
        _assign_clusters, _call_llm, _cluster_schema, _format_item_for_cluster,
        _get_client, _unwrap_list, categories, domains,
    )

    if not items:
        return []

    if len(items) > MAX_CONSOLIDATED_ITEMS:
        log.warning(
            "%d items queued for one weekly edition, consolidating the most recent %d. "
            "Sends have probably been failing -- check the log for delivery errors.",
            len(items), MAX_CONSOLIDATED_ITEMS,
        )
        items = items[-MAX_CONSOLIDATED_ITEMS:]

    client = _get_client(config)

    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(str(item.get("source", "")), []).append(_as_cluster_input(item))

    entries: list[dict[str, Any]] = []
    for source, group in groups.items():
        log.info("Consolidating %d item(s) of the week for '%s'", len(group), source)
        try:
            items_text = "\n".join(
                _format_item_for_cluster(item, i) for i, item in enumerate(group)
            )
            content = _call_llm(
                client, config, render_template(WEEKLY_PROMPT_PATH, items=items_text),
                _cluster_schema(categories(config), domains(config)), kind="weekly",
            )
            clusters = _unwrap_list(content, "clusters")
            if clusters is None:
                raise ValueError("no usable clusters in the response")
            entries.extend(_assign_clusters(group, clusters, config))
        except Exception as e:
            log.warning(
                "Weekly consolidation failed for '%s', keeping its items as they were "
                "summarised: %s", source, e,
            )
            entries.extend(group)

    if len(entries) < len(items):
        log.info("Consolidated the week's %d items into %d entries", len(items), len(entries))
    return entries


def write_intro(entries: list[dict[str, Any]], config: dict[str, Any]) -> str:
    """A short opening paragraph over the whole edition.

    Returns "" on any failure. This is the one part of the email that is purely
    nice to have, so it must never be the reason a week of news goes undelivered.
    """
    from src.summariser import _call_llm, _get_client, _strip_delimiters

    if not entries:
        return ""
    try:
        lines = "\n".join(
            f"- [{_strip_delimiters(str(e.get('category', '')))}] "
            f"{_strip_delimiters(str(e.get('title', '')))}: "
            f"{_strip_delimiters(str(e.get('summary', '')))}"
            for e in entries
        )
        content = _call_llm(
            _get_client(config), config,
            render_template(WEEKLY_INTRO_PROMPT_PATH, entries=lines),
            _intro_schema(), kind="weekly_intro",
        )
        return str(json.loads(content).get("intro") or "").strip()
    except Exception as e:
        log.warning("Weekly intro failed, sending the digest without one: %s", e)
        return ""


# --- sending ----------------------------------------------------------------


def _sections(digest: dict[str, Any]) -> list[str]:
    """Which sections the weekly edition carries.

    Narrower than the daily one by default -- a week of `mention` items is a long
    tail nobody reads, and they are all in the History page anyway. Applied HERE,
    after consolidation, and never by narrowing the digest's own `sections`:
    that field is what main.py routes on, so trimming it would leave every
    `mention` item unrouted, unseen, and re-summarised daily. Filtering
    afterwards is also the right order editorially, since the week's context can
    promote a Monday `mention` into `notable`."""
    return [str(s) for s in (digest.get("weekly_sections") or digest.get("sections") or [])]


def _window_label(start: datetime.datetime | None, end: datetime.datetime) -> str:
    if start is None or start.date() >= end.date():
        return f"week to {end.day} {end:%b}"
    return f"week of {start.day} {start:%b} – {end.day} {end:%b}"


def send_one(
    digest: dict[str, Any],
    config: dict[str, Any],
    now: datetime.datetime | None = None,
    db_path: Path | str | None = None,
) -> bool:
    """Consolidate and send one weekly digest's queue. True if an email went out.

    The order is deliberate: deliver first, then record, clear and stamp. deliver
    raises after its retries are exhausted, so a mail server that is refusing
    leaves the queue exactly as it was and the week arrives on the next run
    instead of evaporating.
    """
    now = now or _utcnow()
    title = str(digest.get("title", "Digest"))
    digest_slug = slug(title)

    ids, items = pending(digest_slug, db_path)
    if not items:
        # Nothing to say. An email with nothing in it is worse than no email
        # (see recipients.derive_digests). The send day is still stamped: this
        # week HAS been considered and found empty, and without the stamp every
        # run for the rest of the week would find itself still owed a send and
        # fire one off the moment a single item arrived.
        log.info("Weekly digest '%s' is due but has nothing queued; not sending", title)
        mark_sent(digest_slug, now, db_path)
        return False

    entries = consolidate(items, config)

    wanted = set(_sections(digest))
    if wanted:
        kept = [e for e in entries if str(e.get("category", "")) in wanted]
        if len(kept) < len(entries):
            log.info(
                "Weekly '%s': %d of %d entries are outside %s and are left out of the email",
                title, len(entries) - len(kept), len(entries), sorted(wanted),
            )
        entries = kept

    if not entries:
        # Everything the week produced fell outside the weekly sections. The
        # queue is still cleared and the send stamped: those items HAVE been
        # considered, and holding them would send them again next week.
        log.info("Weekly digest '%s': nothing in the week met the bar; not sending", title)
        clear(ids, db_path)
        mark_sent(digest_slug, now, db_path)
        return False

    tz = schedule_tz(config)
    start = last_sent(digest_slug, db_path) or oldest_queued(digest_slug, db_path)
    label = _window_label(start.astimezone(tz) if start else None, now.astimezone(tz))

    content = build_digest(
        entries, config, digest, intro=write_intro(entries, config), date_label=label
    )
    log.info("Delivering weekly digest '%s' (%s, %d entries)", title, label, len(entries))
    deliver(content, config, title=title, digest_cfg=digest, subject_note=label)

    record_sent(entries, title, config, db_path)
    project_digest(entries, digest, config, db_path=db_path)
    clear(ids, db_path)
    mark_sent(digest_slug, now, db_path)
    return True


def send_due(
    config: dict[str, Any],
    digests: list[dict[str, Any]],
    now: datetime.datetime | None = None,
    db_path: Path | str | None = None,
) -> int:
    """Send every weekly digest whose day has come. Returns how many went out.

    One digest failing must not stop the others: they are separate readers.
    """
    now = now or _utcnow()
    sent = 0
    for digest in digests:
        if not is_weekly(digest):
            continue
        try:
            if not is_due(digest, config, now, db_path):
                continue
            if send_one(digest, config, now, db_path):
                sent += 1
        except Exception:
            log.exception(
                "Weekly digest %r failed to send. Its queue is kept intact and the "
                "send is still owed, so the next run retries it", digest.get("title"),
            )
    return sent
