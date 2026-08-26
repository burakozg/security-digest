"""What the corpus knows about each named thing, and the topic note that says so.

The digest pipeline decides three things about a story -- its section
(`category`), its subject area (`domain`) and which feed it came from (`source`).
None of those is a *topic*: "news" and "security" are beats, and a beat is not
what a second brain is organised around. The summariser now also names the things
a story is actually about (see `llm.extract_entities`), and this module is where
those accumulate into something with a shape.

One row per (thing, story). Aggregated across runs, that answers the question no
single digest can: Fortinet has been in six stories since June, and here they
are. The rows outlive the `history` table's trimming, because a topic's timeline
losing its early half silently is worse than the table growing.

A topic earns a note at `vault.min_mentions` mentions. Below that it stays plain
text in the story note: one mention is a detail, not a thread through the corpus,
and a vault with four thousand single-use CVE notes is a worse graph than none.
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import Any

from src.db import get_connection
from src.vault.notes import KEY_PREFIX, wrap
from src.vault.text import (
    MAX_ENTITY_CHARS,
    canonical,
    display_name,
    md_escape_inline,
    slugify,
    wikilink,
)

log = logging.getLogger(__name__)

DEFAULT_MIN_MENTIONS = 2
DEFAULT_MAX_MENTIONS = 20000

#: Directory under `output/vault/` holding one note per topic. Named once here
#: because the projection has to recognise it to route those notes to a different
#: vault folder than the digests.
TOPICS_DIR = "topics"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entity_mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_key   TEXT NOT NULL,
    surface      TEXT NOT NULL,
    link         TEXT NOT NULL,
    story_note   TEXT NOT NULL,
    story_title  TEXT NOT NULL,
    digest_title TEXT NOT NULL,
    published    TEXT NOT NULL,
    seen_at      TEXT NOT NULL
)
"""
_SCHEMA_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_entity_mentions_key ON entity_mentions(entity_key)"
)
# The identity of a mention is (thing, story). Re-running a day, re-projecting
# after an outage, or backfilling over ground the live path already covered must
# not count the same story twice -- this index plus INSERT OR IGNORE is what
# makes every writer here idempotent.
_SCHEMA_UNIQUE = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_mentions_id "
    "ON entity_mentions(entity_key, link)"
)

# The filename a topic's note was FIRST given, pinned forever after.
#
# It cannot be recomputed each run. The name is slugify(display_name(surfaces)),
# and the display name is the most common spelling -- which moves as mentions
# accumulate: one story saying "Fortinet" and a second saying "Fortinet Inc."
# tie, the longer wins, and the note silently becomes `fortinet-inc.md`. The old
# file does not go away. It stays in the vault holding podcast-digest's section
# and the reader's own prose, orphaned, while every link now points at a new note
# that has neither -- which is precisely the failure src/vault/notes.py exists to
# prevent, arrived at from the other direction.
_SCHEMA_NOTES = """
CREATE TABLE IF NOT EXISTS topic_notes (
    entity_key TEXT PRIMARY KEY,
    note_name  TEXT NOT NULL,
    named_at   TEXT NOT NULL
)
"""


def _ensure_schema(conn) -> None:
    conn.execute(_SCHEMA)
    conn.execute(_SCHEMA_INDEX)
    conn.execute(_SCHEMA_UNIQUE)
    conn.execute(_SCHEMA_NOTES)


def _max_mentions(config: dict[str, Any] | None) -> int:
    cfg = (config or {}).get("vault", {}) if config else {}
    return int(cfg.get("max_mentions", DEFAULT_MAX_MENTIONS))


def min_mentions(config: dict[str, Any] | None) -> int:
    cfg = (config or {}).get("vault", {}) if config else {}
    return int(cfg.get("min_mentions", DEFAULT_MIN_MENTIONS))


def record(
    mentions: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
    db_path: Path | str | None = None,
) -> set[str]:
    """Store one row per (entity, story). Returns the entity keys touched.

    Each mention is ``{surface, link, story_note, story_title, digest_title,
    published}``. The caller has already decided what the entities are; this only
    canonicalises them, so that "Mandiant" and "Mandiant Inc." land on one key.
    """
    if not mentions:
        return set()
    seen_at = datetime.datetime.now().isoformat(timespec="seconds")
    rows = []
    touched: set[str] = set()
    for mention in mentions:
        surface = str(mention.get("surface", "")).strip()[:MAX_ENTITY_CHARS]
        key = canonical(surface)
        if not key or not mention.get("link"):
            continue
        touched.add(key)
        rows.append((
            key,
            surface,
            str(mention.get("link", "")),
            str(mention.get("story_note", "")),
            str(mention.get("story_title", "")),
            str(mention.get("digest_title", "")),
            str(mention.get("published", "")),
            seen_at,
        ))
    if not rows:
        return set()

    conn = get_connection(db_path)
    try:
        _ensure_schema(conn)
        conn.executemany(
            "INSERT OR IGNORE INTO entity_mentions "
            "(entity_key, surface, link, story_note, story_title, digest_title, published, seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.execute(
            "DELETE FROM entity_mentions WHERE id NOT IN "
            "(SELECT id FROM entity_mentions ORDER BY id DESC LIMIT ?)",
            (_max_mentions(config),),
        )
        conn.commit()
    finally:
        conn.close()
    return touched


def counts(
    keys: list[str] | set[str] | None = None, db_path: Path | str | None = None
) -> dict[str, int]:
    """Mentions per entity key, for the given keys or for all of them."""
    conn = get_connection(db_path)
    try:
        _ensure_schema(conn)
        if keys is None:
            rows = conn.execute(
                "SELECT entity_key, COUNT(*) AS n FROM entity_mentions GROUP BY entity_key"
            ).fetchall()
        else:
            keys = list(keys)
            if not keys:
                return {}
            placeholders = ",".join("?" * len(keys))
            rows = conn.execute(
                f"SELECT entity_key, COUNT(*) AS n FROM entity_mentions "
                f"WHERE entity_key IN ({placeholders}) GROUP BY entity_key",
                keys,
            ).fetchall()
    finally:
        conn.close()
    return {row["entity_key"]: row["n"] for row in rows}


def mentions_of(key: str, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    """Every stored mention of one entity, newest first."""
    conn = get_connection(db_path)
    try:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT surface, link, story_note, story_title, digest_title, published "
            "FROM entity_mentions WHERE entity_key = ? ORDER BY published DESC, id DESC",
            (key,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _adopt(surfaces: dict[str, int], key: str, existing: set[str] | None) -> str | None:
    """A filename already in the vault that this thing plainly belongs to.

    Tried in order of confidence: every spelling the corpus has used for it, most
    common first, then the canonical key. Deliberately conservative -- only an
    exact filename match counts, because adopting the wrong note would merge two
    unrelated topics into one page, which is worse than the split it is meant to
    prevent and much harder to notice.
    """
    if not existing:
        return None
    ordered = sorted(surfaces.items(), key=lambda kv: (-kv[1], kv[0]))
    for candidate in [slugify(s) for s, _ in ordered] + [slugify(key)]:
        if candidate in existing:
            return candidate
    return None


def _surfaces(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        surface = str(row.get("surface") or "").strip()
        if surface:
            counts[surface] = counts.get(surface, 0) + 1
    return counts


def note_name(
    rows: list[dict[str, Any]],
    key: str,
    db_path: Path | str | None = None,
    existing: set[str] | None = None,
) -> str:
    """The topic note's filename stem, chosen once and then never changed.

    Three sources, in order:

    1. **Our pin.** Whatever was chosen for this key before, read back from
       `topic_notes`. See that table's comment for what recomputing it costs.
    2. **A name another writer already chose**, when `existing` -- the filenames
       already in the vault's topics folder -- contains one this key could
       plausibly be. This is what stops two applications pinning two different
       names for one thing. Both of us pin now, but each into a private store
       neither can read: podcast-digest into a `control:topic_names` document in
       its own database, us into SQLite. The *vault* is the only store both can
       see, so it is what we consult before inventing a name. Whoever gets there
       second adopts; only a genuine simultaneous first write can still split.
    3. **Our own choice**, ``slugify(display_name(surfaces))`` -- the same rule
       podcast-digest uses, so unprompted we still usually agree (see
       :mod:`src.vault.text` for why matching that rule matters more than
       improving on it).

    Whatever is chosen is pinned, so the answer never moves again.
    """
    conn = get_connection(db_path)
    try:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT note_name FROM topic_notes WHERE entity_key = ?", (key,)
        ).fetchone()
        if row:
            return str(row["note_name"])
        surfaces = _surfaces(rows)
        name = _adopt(surfaces, key, existing) or slugify(
            display_name(surfaces) if surfaces else key
        )
        conn.execute(
            "INSERT OR IGNORE INTO topic_notes (entity_key, note_name, named_at) "
            "VALUES (?, ?, ?)",
            (key, name, datetime.datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        # Re-read rather than returning `name`: another process may have claimed
        # this key between the SELECT and the INSERT, and its name is the one on
        # disk.
        row = conn.execute(
            "SELECT note_name FROM topic_notes WHERE entity_key = ?", (key,)
        ).fetchone()
        return str(row["note_name"]) if row else name
    finally:
        conn.close()


def note_body(key: str, rows: list[dict[str, Any]]) -> str:
    """This writer's contribution to one topic note.

    A complete note as it would be created fresh -- frontmatter, a title, and a
    single region marked as ours. When the note already exists in the vault only
    the marked region and the prefixed frontmatter keys are taken from this; see
    :mod:`src.vault.notes`, which is the contract a second application writing to
    the same file has to follow.
    """
    surfaces = _surfaces(rows)
    name = display_name(surfaces) if surfaces else key
    digests = {str(row.get("digest_title") or "") for row in rows if row.get("digest_title")}
    dates = sorted((str(row.get("published") or ""))[:10] for row in rows if row.get("published"))
    first, last = (dates[0], dates[-1]) if dates else ("", "")

    # Escaped for the heading, quoted for the frontmatter. These strings are
    # model output over untrusted feed text: unquoted, a name containing a colon
    # or bracket breaks the YAML, and unescaped it renders as a link in the
    # heading.
    safe = md_escape_inline(name, max_chars=MAX_ENTITY_CHARS)
    front = [
        "type: topic",
        f"title: {_yaml_str(name)}",
        "tags: [topic]",
        f"{KEY_PREFIX}mentions: {len(rows)}",
        f"{KEY_PREFIX}digests: {len(digests)}",
        f"{KEY_PREFIX}first_seen: {first}",
        f"{KEY_PREFIX}last_seen: {last}",
    ]
    section = [
        "## From security digests",
        "",
        f"*{len(rows)} stor{'ies' if len(rows) != 1 else 'y'} across "
        f"{len(digests)} digest{'s' if len(digests) != 1 else ''} · {first} → {last}*",
        "",
    ]
    for row in rows:
        date = str(row.get("published") or "")[:10]
        digest = md_escape_inline(str(row.get("digest_title") or ""), max_chars=80)
        # Shorter than the story note's own title: this is a scannable list of
        # up to `max_mentions` lines, and a few feeds publish 280-character
        # headlines that would each wrap three times.
        link = wikilink(
            str(row.get("story_note") or ""), str(row.get("story_title") or ""), max_chars=110
        )
        section.append(f"- {date} · **{digest}** — {link}")

    return "---\n" + "\n".join(front) + "\n---\n\n" + f"# {safe}\n\n" + wrap("\n".join(section)) + "\n"


def _yaml_str(value: str) -> str:
    """A double-quoted YAML scalar. json.dumps produces exactly this for a str,
    and YAML's double-quoted style is a superset of JSON's."""
    import json

    return json.dumps(value, ensure_ascii=False)


def surfaces_for(story_note: str, db_path: Path | str | None = None) -> list[str]:
    """The entity surfaces recorded for one story note, in the order the model
    named them (which is `id` order, since they were inserted together)."""
    conn = get_connection(db_path)
    try:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT surface FROM entity_mentions WHERE story_note = ? ORDER BY id",
            (story_note,),
        ).fetchall()
    finally:
        conn.close()
    return [str(row["surface"]) for row in rows]


def stories_of(
    keys: list[str] | set[str], db_path: Path | str | None = None
) -> list[str]:
    """Every story note that mentions any of these entities."""
    keys = list(keys)
    if not keys:
        return []
    placeholders = ",".join("?" * len(keys))
    conn = get_connection(db_path)
    try:
        _ensure_schema(conn)
        rows = conn.execute(
            f"SELECT DISTINCT story_note FROM entity_mentions "
            f"WHERE entity_key IN ({placeholders}) AND story_note != ''",
            keys,
        ).fetchall()
    finally:
        conn.close()
    return [str(row["story_note"]) for row in rows]
