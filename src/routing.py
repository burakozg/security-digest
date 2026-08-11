"""Which feed reaches which digest.

Delivery is an AND of two independent filters (see src/main.py): the LLM picks
an item's category, and the config picks which feeds a digest accepts. An item
is delivered only where both agree, so the real routing key is the *pair*
(feed x category) -- and for a long time nothing checked that the two axes
lined up.

They did not. On 2026-08-11 the security instance summarised 46 items, marked 6
as `exclude`, delivered 20, and silently discarded the other 20: every security
feed produced `ai` items that Security Digest did not accept and AI Security
Digest did not list. Nothing logged it, and mark_seen() then consumed those
items for a fortnight.

The fix is to stop writing the mapping twice. A feed names the digests it feeds,
in sources.yaml where the feed is defined, and each digest's `sources` list is
derived from that. One place to edit, next to the thing being edited, and a feed
that belongs nowhere is visible in the file you are already looking at.

An explicit `sources:` on a digest still wins, so an instance that has not
migrated behaves exactly as before.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# The category meaning "do not deliver this". It is a decision, not a gap, so it
# is excluded from the routing matrix and never reported as a dropped item.
EXCLUDE = "exclude"


def all_feeds(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Every feed this instance fetches: hand-written RSS plus topic searches."""
    sources = config.get("sources") or {}
    feeds = list(sources.get("rss") or []) + list(sources.get("topic_feeds") or [])
    return [f for f in feeds if isinstance(f, dict) and f.get("name")]


# Set on a digest whose `sources` was derived from feed declarations. It exists
# because an empty `sources` list already means "accept every feed", so a
# derived-and-empty list would say the exact opposite of what it means: a digest
# no feed routes to would quietly take everything instead of nothing.
DERIVED_FLAG = "sources_derived"


def _declared(config: dict[str, Any]) -> dict[str, set[str]]:
    """digest title -> feed names that named it, for feeds using `digests:`."""
    out: dict[str, set[str]] = {}
    for feed in all_feeds(config):
        if "digests" not in feed:
            continue
        for title in feed.get("digests") or []:
            out.setdefault(str(title).strip(), set()).add(str(feed["name"]).strip())
    return out


def uses_feed_routing(config: dict[str, Any]) -> bool:
    """True once any feed carries a `digests:` key. A feed that names no digest
    (`digests: []`) still counts -- "delivered nowhere" is a decision, and it is
    the case that most needs to survive."""
    return any("digests" in f for f in all_feeds(config))


def accepts_feed(digest: dict[str, Any], feed: str) -> bool:
    """Whether a digest takes items from this feed, ignoring category."""
    sources = {str(s).strip() for s in (digest.get("sources") or [])}
    if sources:
        return feed in sources
    # No sources at all: historically "any feed", but under derived routing it
    # means no feed named this digest, so it should receive nothing.
    return not digest.get(DERIVED_FLAG)


def apply_feed_routing(config: dict[str, Any]) -> list[str]:
    """Derive each digest's `sources` from the feeds that named it.

    Mutates config in place and returns warnings. A digest carrying its own
    explicit `sources:` is left untouched -- but if feeds also name it, that is
    two sources of truth for one mapping and the config file wins, loudly."""
    digests = config.get("digests") or []
    if not digests:
        return []

    if not uses_feed_routing(config):
        return []

    declared = _declared(config)
    titles = {str(d.get("title", "")).strip() for d in digests}
    messages: list[str] = []

    for digest in digests:
        title = str(digest.get("title", "")).strip()
        if digest.get("sources"):
            if title in declared:
                messages.append(
                    f"Digest {title!r} lists its own sources, so the feeds naming it in "
                    f"sources.yaml are ignored. Remove one of the two."
                )
            continue
        digest["sources"] = sorted(declared.get(title, ()))
        digest[DERIVED_FLAG] = True

    for title in sorted(set(declared) - titles):
        feeds = ", ".join(sorted(declared[title]))
        messages.append(
            f"Feed(s) {feeds} route to digest {title!r}, which does not exist -- "
            f"their items will be fetched but never delivered"
        )

    # A feed with no `digests:` key at all, in a file where other feeds have one,
    # contributes to nobody's derived list and so reaches nothing. That reads as
    # an omission rather than a decision, which is precisely the failure this
    # module exists to end -- `digests: []` says the same thing on purpose.
    silent = sorted(str(f["name"]).strip() for f in all_feeds(config) if "digests" not in f)
    if silent:
        messages.append(
            f"Feed(s) {', '.join(silent)} have no digests: key while other feeds do, "
            f"so they are delivered nowhere. Give them a list, or an explicit [] if "
            f"that is intended."
        )

    return messages


def accepts_domain(digest: dict[str, Any], domain: str | None) -> bool:
    """Whether a digest carries this subject area.

    A digest with no `domain` takes any, which is what every instance did before
    domains existed. Once digests share a section list, this is the field that
    keeps them apart: without it a feed routed to two digests delivers each of
    its items to both, as two separate emails."""
    wanted = digest.get("domain")
    if not wanted:
        return True
    return domain == wanted


def accepting_digests(
    config: dict[str, Any], feed: str, category: str, domain: str | None = None
) -> list[str]:
    """Digest titles that would deliver an item from `feed` categorised
    `category` in `domain` -- the same AND that src/main.py applies."""
    out = []
    for digest in config.get("digests") or []:
        if (
            category in set(digest.get("sections") or [])
            and accepts_feed(digest, feed)
            and accepts_domain(digest, domain)
        ):
            out.append(str(digest.get("title", "")))
    return out


def routing_matrix(config: dict[str, Any]) -> dict[str, Any]:
    """The feed x category grid, plus the pairs that reach no digest.

    This is the view that would have made the 20 vanishing items obvious: the
    per-feed warning could only ever say "this feed goes nowhere", which was
    true of 2 feeds while 9 others were losing a category each.

    The columns are whichever field actually routes. Where domains are defined
    the digests share one section list, so the section can no longer send an
    item anywhere in particular and the domain decides -- showing categories
    there would be a grid of identical cells. Without domains, the category is
    still the routing field and stays the column."""
    from src.summariser import categories, domains

    doms = domains(config)
    columns = doms or [c for c in categories(config) if c != EXCLUDE]
    axis = "domain" if doms else "category"

    # Every section must be reachable, whichever axis routes -- a section listed
    # by no digest loses its items just as quietly.
    sections = sorted({s for d in (config.get("digests") or []) for s in (d.get("sections") or [])})

    rows = []
    unroutable: list[dict[str, str]] = []

    for feed in sorted(all_feeds(config), key=lambda f: str(f["name"]).lower()):
        name = str(feed["name"])
        if doms:
            cells = {
                col: sorted({t for s in sections
                             for t in accepting_digests(config, name, s, col)})
                for col in columns
            }
        else:
            cells = {col: accepting_digests(config, name, col) for col in columns}
        rows.append({"feed": name, "cells": cells})
        for col, titles in cells.items():
            if not titles:
                unroutable.append({"feed": name, axis: col})

    return {
        "axis": axis,
        "categories": columns,
        "feeds": rows,
        "unroutable": unroutable,
        "orphan_feeds": [r["feed"] for r in rows if not any(r["cells"].values())],
    }
