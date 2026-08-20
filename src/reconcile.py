"""Reconcile admin-panel overrides (written on the deployed NAS) back into the
git-tracked base files (sources.yaml, config.yaml) before a deploy pushes a
new copy of those files up.

Why this exists: sources.yaml and config.yaml's llm: block are mounted
read-only in the container (see docker-compose.yml / deploy.sh /
deploy-native.sh / container-station-app.yaml) -- the admin panel can't write
to them directly, so it writes to a separate file in the persistent data/
directory instead (data/sources_overrides.yaml, data/llm_overrides.yaml).
load_config() then has that override file completely replace the
corresponding section from the base file whenever it's present.

That's simple, but it means once an override file exists, editing the base
file in git and deploying has no effect on runtime behaviour at all -- the
override always wins. This module pulls a copy of each override off the
target (deploy.sh/deploy-native.sh SCP it down before calling this), merges
it into the local base file, and reports what changed. After a successful
deploy, the caller deletes the override on the target -- the freshly-pushed
base file now covers everything the override held, so nothing is lost by
dropping it, and the base file is authoritative again until the next
admin-panel save recreates an override.

The merge cannot be a plain union. "In the override but not in the base" is
ambiguous: it means the panel *added* a feed, or it means the base *deleted*
one since the last deploy. Treating it as always-added made deleting a feed
from the git-tracked file impossible -- the deploy silently put it straight
back, and then deleted the override, leaving no trace of why. Two dead feeds
removed by hand on 2026-08-20 reappeared exactly this way.

So sources reconciliation takes a stamp of the feed names as last deployed
(deploy.sh keeps it on the target at data/.deployed-sources), the same trick
the prompt files already use. A name in the stamp and in the override but no
longer in the base was deliberately deleted, and stays deleted. A name absent
from the stamp is a genuine panel addition and is merged in. With no stamp
yet -- the first deploy after this change -- nothing is assumed deleted,
which keeps the old behaviour rather than dropping feeds on a guess.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml


def _stamped_names(stamp_path: Path | None) -> set[str]:
    """Feed names recorded as deployed last time, or empty if there is no stamp.

    Empty means "assume nothing was deleted": without a record of what went out,
    a missing feed cannot be told from one the panel just added, and dropping a
    feed on that guess is the worse error of the two.
    """
    if stamp_path is None or not stamp_path.exists():
        return set()
    return {line.strip() for line in stamp_path.read_text().splitlines() if line.strip()}


def merge_sources(
    base_path: Path, override_path: Path, stamp_path: Path | None = None
) -> list[str]:
    """Merge override_path's rss list into base_path's rss list, in place.
    Override entries win on name collision (an admin-panel edit to an
    existing feed's URL is kept). A feed only in the override is added --
    unless it appears in the stamp, which makes it a deletion from the base
    to be honoured rather than undone. Returns human-readable change summary
    lines; empty if no override file existed or nothing changed."""
    if not override_path.exists():
        return []

    base_doc = yaml.safe_load(base_path.read_text()) or {}
    override_doc = yaml.safe_load(override_path.read_text()) or {}

    base_rss = base_doc.get("rss") or []
    override_rss = override_doc.get("rss") if isinstance(override_doc, dict) else None
    if not isinstance(override_rss, list) or not override_rss:
        return []

    deployed = _stamped_names(stamp_path)
    changes: list[str] = []
    merged: list[dict[str, Any]] = [dict(f) for f in base_rss if isinstance(f, dict)]
    by_name = {f.get("name"): i for i, f in enumerate(merged) if f.get("name")}

    for feed in override_rss:
        if not isinstance(feed, dict) or not feed.get("name"):
            continue
        name = feed["name"]
        if name in by_name:
            existing = merged[by_name[name]]
            # Compare the whole entry, not just the URL: a feed's `digests:` is
            # its routing, and it is editable in the admin panel. Comparing only
            # the URL meant re-routing a feed there was never folded back into
            # the git-tracked sources.yaml, so the next deploy shipped a seed
            # that disagreed with what the panel was actually running.
            if existing != dict(feed):
                what = (f"-> {feed.get('url')}" if existing.get("url") != feed.get("url")
                        else f"digests={feed.get('digests', [])}")
                changes.append(f"  updated: {name} {what}")
                merged[by_name[name]] = dict(feed)
        elif name in deployed:
            # Was deployed, is in the override, is gone from the base: deleted by
            # hand since the last deploy. Leaving it out of `merged` is what makes
            # the deletion stick; say so, because the override is about to be
            # removed from the target and this is the only record of the decision.
            changes.append(f"  removed: {name} (deleted from {base_path.name} since last deploy)")
        else:
            changes.append(f"  added:   {name} ({feed.get('url')})")
            merged.append(dict(feed))
            by_name[name] = len(merged) - 1

    if not changes:
        return []

    # Report-only changes must not touch the file. A reconcile whose only finding
    # is a removal leaves `merged` identical to what the base already holds -- the
    # feed is gone from it already -- and rewriting would put this file through
    # yaml.dump purely to strip its 19 lines of comments.
    if merged != [dict(f) for f in base_rss if isinstance(f, dict)]:
        base_doc["rss"] = merged
        base_path.write_text(
            yaml.dump(base_doc, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120),
            encoding="utf-8",
        )
    return changes


def _stamped_llm(stamp_path: Path | None) -> dict[str, Any] | None:
    """The llm block as last deployed, or None if there is no stamp.

    None means "assume the panel owns every key it sets", which is the
    behaviour that predates the stamp -- the safe default, since without a
    record there is no way to tell a panel edit from a hand edit.
    """
    if stamp_path is None or not stamp_path.exists():
        return None
    doc = yaml.safe_load(stamp_path.read_text())
    return doc if isinstance(doc, dict) else None


def merge_llm(
    config_path: Path, override_path: Path, stamp_path: Path | None = None
) -> list[str]:
    """Merge override_path's llm: dict into config_path's llm: block, in place.

    Resolved against a stamp of the block as last deployed, the same way
    merge_sources resolves feeds. A key the panel has not touched since that
    deploy (override value still equals the stamp) leaves the base
    authoritative, so editing it in config.yaml -- or deleting it -- survives
    the deploy instead of being silently reverted. A key the panel did change
    wins, and is folded back into git as before. With no stamp, the override
    wins outright, which is what this did before the stamp existed.

    Returns a list of change summary lines."""
    if not override_path.exists():
        return []

    config_doc = yaml.safe_load(config_path.read_text()) or {}
    override_doc = yaml.safe_load(override_path.read_text()) or {}
    override_llm = override_doc.get("llm", override_doc) if isinstance(override_doc, dict) else None
    if not isinstance(override_llm, dict) or not override_llm:
        return []

    base_llm = config_doc.get("llm") or {}
    deployed = _stamped_llm(stamp_path)
    changes: list[str] = []
    merged = dict(base_llm)
    for key, value in override_llm.items():
        # The panel has not touched this key since the last deploy, so whatever
        # the base says now is a deliberate hand edit -- including removing it.
        if deployed is not None and key in deployed and deployed[key] == value:
            if key not in base_llm:
                changes.append(f"  removed: {key} (deleted from {config_path.name} since last deploy)")
            elif base_llm[key] != value:
                changes.append(f"  kept:    {key}={base_llm[key]!r} (edited in {config_path.name}; panel still {value!r})")
            continue
        if merged.get(key) != value:
            changes.append(f"  {key}: {merged.get(key)!r} -> {value!r}")
            merged[key] = value

    if not changes:
        return []

    # Same reason as merge_sources: a reconcile that only reports decisions has
    # nothing to write, and config.yaml carries comments -- including four lines
    # inside the llm: block itself -- that yaml.dump would strip.
    if merged != base_llm:
        config_doc["llm"] = merged
        config_path.write_text(
            yaml.dump(config_doc, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120),
            encoding="utf-8",
        )
    return changes


_MERGERS = {
    "sources": merge_sources,
    "llm": merge_llm,
}


def _main() -> int:
    if len(sys.argv) not in (4, 5) or sys.argv[1] not in _MERGERS:
        print("Usage: python -m src.reconcile sources|llm <base_path> <override_path> [stamp_path]",
              file=sys.stderr)
        return 2

    kind, base_arg, override_arg = sys.argv[1], sys.argv[2], sys.argv[3]
    base_path, override_path = Path(base_arg), Path(override_arg)
    stamp_path = Path(sys.argv[4]) if len(sys.argv) > 4 else None

    if kind == "sources":
        changes = merge_sources(base_path, override_path, stamp_path)
    else:
        changes = merge_llm(base_path, override_path, stamp_path)

    if not override_path.exists():
        print(f"[reconcile:{kind}] no override present, nothing to merge")
    elif not changes:
        print(f"[reconcile:{kind}] override present but already reflected in {base_path.name}, no changes")
    else:
        print(f"[reconcile:{kind}] merged into {base_path.name}:")
        for line in changes:
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
