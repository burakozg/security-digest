"""Filter out items that have already been processed. Content deduplication for same story across feeds."""

import json
import logging
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from src.db import get_connection
from src.utils import PROJECT_ROOT

log = logging.getLogger(__name__)

_LEGACY_JSON_PATH = PROJECT_ROOT / "data" / "seen.json"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    link TEXT PRIMARY KEY,
    seen_at REAL NOT NULL
)
"""


def _normalize_title(title: str) -> str:
    """Normalize title for similarity comparison."""
    s = (title or "").lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _title_similarity(a: str, b: str) -> float:
    """Return similarity ratio 0-1. Uses SequenceMatcher on normalized titles."""
    na, nb = _normalize_title(a), _normalize_title(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


# Words too common to distinguish one story from another.
_STOPWORDS = frozenset("""
a an the of for with and or to in on at by from via as is are was were be been
its it this that these those new after over into out up down about amid than
""".split())

# Below this, an overlap is coincidence rather than evidence: two headlines about
# unrelated events routinely share two ordinary words.
_MIN_SHARED_TOKENS = 3

# Each headline must also contribute at least this many words the other lacks.
#
# This is what separates "same story, different newsroom" from "same template,
# different subject". Two outlets covering one event rewrite the framing --
# "unveils deal structure" vs "secures funding" -- so each side carries several
# words of its own. Two different events reported in the same house style differ
# by exactly one word: Fortinet/Ivanti, Taiwan/Japan. Requiring two independent
# differences keeps the first and rejects the second, where every symmetric
# overlap measure ranks them the wrong way round.
#
# The cost is missing a merge when one headline is a strict shortening of
# another. That is the right way to be wrong: a surviving duplicate is visible
# and mildly annoying, whereas a wrongly merged story is information the reader
# silently never sees.
_MIN_DISTINCT_TOKENS = 2


def _significant_tokens(title: str, exclude: set[str]) -> set[str]:
    """Distinctive words in a title: stopwords, very short words and the tracked
    topic's own name removed.

    The topic name is excluded because it appears in every result for that topic,
    so leaving it in inflates the similarity of items that share nothing else."""
    return {
        w for w in _normalize_title(title).split()
        if len(w) > 2 and w not in _STOPWORDS and w not in exclude
    }


def _same_story(
    a: dict[str, Any], b: dict[str, Any], containment_threshold: float
) -> bool:
    """Whether two search results are the same story reported twice.

    Character similarity can't see this: four outlets covering one deal write
    "Naver Unveils AI Factory Deal Structure with Nvidia, Brookfield" and "Naver
    secures Nvidia, Brookfield for AI factory funding", which share almost no
    character runs (ratio 0.50) but nearly all their proper nouns. Comparing the
    sets of distinctive words catches it; comparing strings never can.

    Containment against the *shorter* title, not Jaccard: a terse headline and a
    detailed one describing the same event legitimately differ in length, and
    Jaccard punishes that.

    Deliberately limited to two items from the same topic. Within one topic the
    same story genuinely does arrive from many outlets, so this is the case worth
    merging. Across topics -- and on publisher feeds, where duplication is rare --
    bag-of-words matching is too blunt: "CISA adds Fortinet flaw to KEV catalog"
    and "CISA adds Ivanti flaw to KEV catalog" share five of six words and are
    different stories."""
    source = a.get("source")
    if not source or source != b.get("source"):
        return False

    exclude = _significant_tokens(str(source), set())
    ta = _significant_tokens(a.get("title", ""), exclude)
    tb = _significant_tokens(b.get("title", ""), exclude)
    if not ta or not tb:
        return False

    shared = ta & tb
    if len(shared) < _MIN_SHARED_TOKENS:
        return False
    if len(shared) / min(len(ta), len(tb)) < containment_threshold:
        return False
    return min(len(ta - tb), len(tb - ta)) >= _MIN_DISTINCT_TOKENS


def dedupe_content(
    items: list[dict[str, Any]],
    similarity_threshold: float = 0.85,
    story_dedupe: bool = False,
    story_containment_threshold: float = 0.6,
) -> list[dict[str, Any]]:
    """Remove items that describe the same story (similar titles). Keeps first occurrence by publish date.

    story_dedupe additionally merges same-topic items whose distinctive words
    overlap (see _same_story) -- needed for news search, where one story arrives
    from a dozen outlets under a dozen different headlines."""
    if not items or (similarity_threshold >= 1.0 and not story_dedupe):
        return items

    # Sort by published date (newest first) so we keep the freshest version
    sorted_items = sorted(items, key=lambda x: x.get("published") or "", reverse=True)

    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for item in sorted_items:
        title = item.get("title", "")
        is_duplicate = any(
            _title_similarity(title, k.get("title", "")) >= similarity_threshold
            for k in kept
        )
        if not is_duplicate and story_dedupe:
            # Compare against already-dropped items too (single-linkage): with a
            # story covered by four outlets, the fourth headline may resemble the
            # third -- which was itself removed -- without resembling the first
            # one still standing. Comparing only against survivors leaves it in.
            is_duplicate = any(
                _same_story(item, k, story_containment_threshold)
                for k in kept + dropped
            )
        (dropped if is_duplicate else kept).append(item)

    removed = len(items) - len(kept)
    if removed:
        log.info("Content deduplication: removed %d similar items (%d -> %d)", removed, len(items), len(kept))
    return kept


def _ensure_schema(conn) -> None:
    conn.execute(_SCHEMA)
    # One-time migration from the legacy seen.json: only runs while the table
    # is still empty, so it's a no-op on every call after the first.
    if _LEGACY_JSON_PATH.exists():
        row = conn.execute("SELECT 1 FROM seen LIMIT 1").fetchone()
        if row is None:
            try:
                data = json.loads(_LEGACY_JSON_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                log.warning("Could not read legacy %s for migration", _LEGACY_JSON_PATH)
                return
            if isinstance(data, dict) and data:
                conn.executemany(
                    "INSERT OR REPLACE INTO seen (link, seen_at) VALUES (?, ?)",
                    list(data.items()),
                )
                conn.commit()
                log.info("Migrated %d seen links from %s into digest.db", len(data), _LEGACY_JSON_PATH)


def filter_new(
    items: list[dict[str, Any]],
    db_path: Path | str | None = None,
    retention_days: int = 14,
) -> list[dict[str, Any]]:
    """Return only items whose link we haven't seen before (or whose seen record has
    expired past retention). Read-only -- does not update the store. Call mark_seen()
    once the item has actually been delivered, so a failed run doesn't permanently
    lose items that were never sent."""
    cutoff = time.time() - (retention_days * 86400)

    conn = get_connection(db_path)
    try:
        _ensure_schema(conn)
        rows = conn.execute("SELECT link, seen_at FROM seen").fetchall()
    finally:
        conn.close()
    seen = {row["link"]: row["seen_at"] for row in rows}

    new_items = []
    for item in items:
        link = item.get("link", "")
        if not link:
            continue
        seen_at = seen.get(link)
        if seen_at is not None and seen_at > cutoff:
            continue
        new_items.append(item)

    return new_items


def mark_seen(
    items: list[dict[str, Any]],
    db_path: Path | str | None = None,
    retention_days: int = 14,
) -> None:
    """Record items' links as seen. Call after successful delivery, not before --
    marking on fetch meant a failed summarisation or delivery permanently dropped
    those items, since the next run would treat them as already handled."""
    cutoff = time.time() - (retention_days * 86400)
    now = time.time()

    conn = get_connection(db_path)
    try:
        _ensure_schema(conn)
        conn.execute("DELETE FROM seen WHERE seen_at <= ?", (cutoff,))
        links = [(item.get("link", ""), now) for item in items if item.get("link")]
        if links:
            conn.executemany("INSERT OR REPLACE INTO seen (link, seen_at) VALUES (?, ?)", links)
        conn.commit()
    finally:
        conn.close()


def clear_seen(db_path: Path | str | None = None) -> None:
    """Clear the seen store (admin "flush" action) so previously processed
    items can be included again in the next run."""
    conn = get_connection(db_path)
    try:
        _ensure_schema(conn)
        conn.execute("DELETE FROM seen")
        conn.commit()
    finally:
        conn.close()
