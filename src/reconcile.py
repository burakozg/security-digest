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
base file is now a strict superset of it, so nothing is lost by dropping it,
and the base file is authoritative again until the next admin-panel save
recreates an override.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml


def merge_sources(base_path: Path, override_path: Path) -> list[str]:
    """Merge override_path's rss list into base_path's rss list, in place.
    Override entries win on name collision (an admin-panel edit to an
    existing feed's URL is kept); entries unique to either side are kept.
    Returns a list of human-readable change summary lines; empty if no
    override file existed or nothing changed."""
    if not override_path.exists():
        return []

    base_doc = yaml.safe_load(base_path.read_text()) or {}
    override_doc = yaml.safe_load(override_path.read_text()) or {}

    base_rss = base_doc.get("rss") or []
    override_rss = override_doc.get("rss") if isinstance(override_doc, dict) else None
    if not isinstance(override_rss, list) or not override_rss:
        return []

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
        else:
            changes.append(f"  added:   {name} ({feed.get('url')})")
            merged.append(dict(feed))
            by_name[name] = len(merged) - 1

    if not changes:
        return []

    base_doc["rss"] = merged
    base_path.write_text(
        yaml.dump(base_doc, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
    return changes


def merge_llm(config_path: Path, override_path: Path) -> list[str]:
    """Merge override_path's llm: dict into config_path's llm: block, in
    place. Override keys win (only the ones the admin panel actually sets --
    provider, model -- so temperature/batch_size normally come from the base
    file untouched). Returns a list of change summary lines."""
    if not override_path.exists():
        return []

    config_doc = yaml.safe_load(config_path.read_text()) or {}
    override_doc = yaml.safe_load(override_path.read_text()) or {}
    override_llm = override_doc.get("llm", override_doc) if isinstance(override_doc, dict) else None
    if not isinstance(override_llm, dict) or not override_llm:
        return []

    base_llm = config_doc.get("llm") or {}
    changes: list[str] = []
    merged = dict(base_llm)
    for key, value in override_llm.items():
        if merged.get(key) != value:
            changes.append(f"  {key}: {merged.get(key)!r} -> {value!r}")
            merged[key] = value

    if not changes:
        return []

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
    if len(sys.argv) != 4 or sys.argv[1] not in _MERGERS:
        print("Usage: python -m src.reconcile sources|llm <base_path> <override_path>",
              file=sys.stderr)
        return 2

    kind, base_arg, override_arg = sys.argv[1], sys.argv[2], sys.argv[3]
    base_path, override_path = Path(base_arg), Path(override_arg)

    changes = _MERGERS[kind](base_path, override_path)

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
