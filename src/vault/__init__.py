"""Projecting the delivered digest into an Obsidian vault over LiveSync.

The email is the product; this is the record. After a digest goes out, every
story it carried becomes a note of its own -- summary, byline, links, the feed
text the fetcher already held -- and every named thing the corpus has now seen
twice gets a
contribution to its note under the vault's shared topics folder. That last part
is the point: the topic notes are the ones `podcast-digest` also writes into, so
"Fortinet" accumulates evidence from both corpora on one page.

Notes are written to disk under ``output/vault/`` first and projected from there.
The disk copy is the source and CouchDB is the projection, which is what makes
retry, backfill after an outage and `POST /admin/vault/resync` all the same cheap
idempotent pass rather than three mechanisms.

**Nothing here may fail a run.** By the time it is called the email has been sent
and `record_sent` has recorded it; a database asleep on another machine must not
turn that into a failed pipeline. Every error is logged and swallowed, and the
next run re-projects what was missed.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from src.utils import PROJECT_ROOT
from src.vault.livesync import LiveSyncVault, VaultUnavailable
from src.vault.render import (
    claimed_stems,
    relink,
    story_dir,
    story_note,
    story_note_name,
    story_rel,
    today,
)
from src.vault.topics import TOPICS_DIR, min_mentions, note_body, note_name

log = logging.getLogger(__name__)

VAULT_DIR = PROJECT_ROOT / "output" / "vault"

__all__ = ["project_digest", "resync", "enabled_for", "setting",
           "existing_topics", "VaultUnavailable"]


def _cfg(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("vault", {}) or {}


#: Every vault setting is deployment topology, so each one can be given in the
#: environment and the environment wins -- the same precedence
#: `delivery._resolve_email_config` uses, and for the same reason: config.yaml is
#: git-tracked and is pushed over the target's copy on every deploy, so the real
#: values cannot live there. Which database receives your notes is exactly the
#: kind of value that must not be published or overwritten.
#:
#: Reading all of them, not just the URL and password, is deliberate. A variable
#: named VAULT_DB sitting in .env and being silently ignored is a trap: it reads
#: as configured, and the projection quietly goes somewhere else.
_ENV_KEYS = {
    "enabled": "VAULT_ENABLED",
    "couchdb_url": "VAULT_COUCHDB_URL",
    "db": "VAULT_DB",
    "user": "VAULT_USER",
    "folder": "VAULT_FOLDER",
    "topics_folder": "VAULT_TOPICS_FOLDER",
}

_TRUE = {"1", "true", "yes", "on"}


def setting(config: dict[str, Any], key: str, default: Any = None) -> Any:
    """One vault setting: the environment if it names it, else config.yaml."""
    raw = os.environ.get(_ENV_KEYS.get(key, ""))
    if raw is not None and raw.strip():
        value = raw.strip()
        return value.lower() in _TRUE if key == "enabled" else value
    return _cfg(config).get(key, default)


def enabled_for(config: dict[str, Any], digest_title: str) -> bool:
    """Whether this digest projects into the vault.

    An empty `vault.digests` means every digest this instance sends -- which is
    what an instance that turned the vault on wants. Naming digests is for the
    case where only some of them belong in the vault.
    """
    cfg = _cfg(config)
    if not setting(config, "enabled", False):
        return False
    wanted = cfg.get("digests") or []
    return not wanted or digest_title in wanted


def build_vault(config: dict[str, Any]) -> LiveSyncVault:
    """The client, from config plus the environment.

    The URL and password come from the environment first: `config.yaml` is
    git-tracked and is pushed over the target's copy on every deploy, so a real
    address written there is both published and fragile. Same reasoning as
    `delivery._resolve_email_config`.
    """
    cfg = _cfg(config)
    url = setting(config, "couchdb_url") or ""
    if not url:
        raise VaultUnavailable(
            "vault.enabled is true but no CouchDB URL is set. Put VAULT_COUCHDB_URL "
            "in .env (preferred -- .env is neither committed nor overwritten by a "
            "deploy), or vault.couchdb_url in config.yaml."
        )
    db = setting(config, "db")
    if not db:
        # No default. The vault database is whichever one your LiveSync clients
        # replicate against, and guessing a name means either a 404 or -- worse --
        # writing notes into some other application's database.
        raise VaultUnavailable(
            "No vault database named. Set VAULT_DB in .env (or vault.db in "
            "config.yaml) to the database your LiveSync clients replicate against."
        )
    password = os.environ.get("VAULT_COUCHDB_PASSWORD")
    if not password:
        raise VaultUnavailable("VAULT_COUCHDB_PASSWORD must be set in .env for vault projection")
    return LiveSyncVault(
        url,
        str(db),
        str(setting(config, "user", "security_digest")),
        password,
        folder=str(setting(config, "folder", "12 daily-digest")),
        topics_folder=str(setting(config, "topics_folder", "99 topics")),
        topics_dir=TOPICS_DIR,
        timeout_s=float(cfg.get("timeout_s", 30)),
    )


def existing_topics(config: dict[str, Any]) -> set[str]:
    """Topic-note filenames already in the vault, or an empty set.

    Read before naming anything so a thing another writer has already given a
    note keeps that one name -- see `src.vault.topics.note_name`. Never raises:
    not knowing is a reason to fall back to choosing our own name, never a reason
    to fail a digest that has already been delivered.
    """
    try:
        vault = build_vault(config)
    except VaultUnavailable:
        return set()
    try:
        return vault.topic_stems()
    except VaultUnavailable:
        return set()
    finally:
        vault.close()


def _write(relative: str, markdown: str, base: Path) -> Path:
    path = base / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return path


def write_notes(
    items: list[dict[str, Any]],
    digest_cfg: dict[str, Any],
    config: dict[str, Any],
    *,
    date: str | None = None,
    base: Path | None = None,
    db_path: Path | str | None = None,
    backfilled: bool = False,
    existing_topics: set[str] | None = None,
) -> list[str]:
    """Render one digest's notes to disk. Returns the paths written, relative to `base`.

    Three passes, in this order for a reason: mentions have to be recorded before
    the threshold is evaluated (today's second sighting of a thing is what earns
    it a note), and the story notes have to know which topics resolved before
    they can link them.
    """
    from src.vault import topics as topics_mod

    base = base or VAULT_DIR
    date = date or today()
    digest_title = str(digest_cfg.get("title", "Digest"))
    # One folder per month. `claimed_stems` only has to scan this run's own,
    # since a stem carries its date and nothing outside the month can collide.
    stories = base / story_dir(date)
    stories.mkdir(parents=True, exist_ok=True)

    # 1. Name every story, then record what it mentions.
    taken = claimed_stems(stories, date)
    names: dict[str, str] = {}
    mentions: list[dict[str, Any]] = []
    for item in items:
        link = str(item.get("link", ""))
        if not link:
            continue
        name = story_note_name(date, str(item.get("title", "")), link, taken)
        taken[name] = link
        names[link] = name
        for surface in item.get("entities") or []:
            mentions.append({
                "surface": surface,
                "link": link,
                "story_note": name,
                "story_title": item.get("title", ""),
                "digest_title": digest_title,
                "published": str(item.get("published") or date)[:19],
            })
    touched = topics_mod.record(mentions, config, db_path)
    written_relinked: list[str] = []

    # 2. Which of those things now clear the bar, and what note each one is on.
    threshold = min_mentions(config)
    counts = topics_mod.counts(touched, db_path)
    resolved: dict[str, str] = {}
    topic_rows: dict[str, list[dict[str, Any]]] = {}
    for key, count in sorted(counts.items()):
        if count < threshold:
            continue
        rows = topics_mod.mentions_of(key, db_path)
        topic_rows[key] = rows
        stem = note_name(rows, key, db_path, existing_topics)
        for row in rows:
            resolved[str(row.get("surface") or "")] = stem

    # 3. Bring older stories' topic lines up to date. A thing named for the
    #    second time today earns its note today, and the story that first named
    #    it months ago still has it as plain text -- so the topic note links to
    #    that story and the story does not link back. One line per note, rewritten
    #    in place; nothing else in an existing note is touched.
    stale = set(topics_mod.stories_of(list(topic_rows), db_path)) - set(names.values())
    for stem in sorted(stale):
        # From the stem, not from this run's folder: a story whose topic only
        # now crossed the threshold is routinely months older than the run
        # relinking it, and lives under its own month.
        rel = story_rel(stem)
        if rel is None:
            continue
        path = base / rel
        if not path.is_file():
            continue
        try:
            before = path.read_text(encoding="utf-8")
        except OSError:
            continue
        after = relink(before, topics_mod.surfaces_for(stem, db_path), resolved)
        if after != before:
            path.write_text(after, encoding="utf-8")
            written_relinked.append(rel)

    # 4. Write the story notes.
    written: list[str] = []
    for item in items:
        link = str(item.get("link", ""))
        if link not in names:
            continue
        relative = story_rel(names[link])
        if relative is None:
            continue
        _write(relative, story_note(
            item, digest_title, date, resolved_topics=resolved, backfilled=backfilled
        ), base)
        written.append(relative)

    for key, rows in sorted(topic_rows.items()):
        relative = f"{TOPICS_DIR}/{note_name(rows, key, db_path, existing_topics)}.md"
        _write(relative, note_body(key, rows), base)
        written.append(relative)

    return written + written_relinked


def project(paths: list[str], config: dict[str, Any], *, base: Path | None = None) -> dict[str, Any]:
    """Push the named notes into the vault. Raises VaultUnavailable."""
    base = base or VAULT_DIR
    vault = build_vault(config)
    projected: list[str] = []
    skipped = 0
    try:
        for relative in paths:
            path = base / relative
            if not path.is_file():
                continue
            written = vault.project(
                relative,
                path.read_text(encoding="utf-8"),
                mtime_ms=int(path.stat().st_mtime * 1000),
                # Topic notes are shared with the reader and with whatever else
                # writes to the vault; story notes are ours alone.
                merge=relative.startswith(f"{TOPICS_DIR}/"),
            )
            if written:
                projected.append(written)
            else:
                skipped += 1
    except VaultUnavailable as exc:
        raise VaultUnavailable(
            f"{exc} (projected {len(projected)} of {len(paths)} before stopping)"
        ) from exc
    finally:
        vault.close()
    return {"considered": len(paths), "projected": len(projected), "skipped": skipped}


def project_digest(
    items: list[dict[str, Any]],
    digest_cfg: dict[str, Any],
    config: dict[str, Any],
    *,
    date: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Write and project one delivered digest. Never raises.

    The single call the pipeline makes. See the module docstring for why it
    swallows everything: the digest has already been emailed and recorded, and a
    sync target being down is an operator problem, not a failed run.
    """
    digest_title = str(digest_cfg.get("title", "Digest"))
    if not enabled_for(config, digest_title):
        return None
    try:
        written = write_notes(
            items, digest_cfg, config, date=date, db_path=db_path,
            existing_topics=existing_topics(config),
        )
    except (OSError, ValueError):
        log.exception("Could not write vault notes for '%s'", digest_title)
        return None
    try:
        result = project(written, config)
    except VaultUnavailable as exc:
        log.error(
            "Vault projection deferred for '%s': %s. The notes are on disk under "
            "output/vault/; the next run or POST /admin/vault/resync will catch up.",
            digest_title, exc,
        )
        return {"written": len(written), "projected": 0, "error": str(exc)}
    except Exception:
        log.exception("Vault projection failed unexpectedly for '%s'", digest_title)
        return {"written": len(written), "projected": 0, "error": "unexpected"}
    log.info(
        "Vault: %d note(s) written, %d projected, %d already current",
        len(written), result["projected"], result["skipped"],
    )
    return {"written": len(written), **result}


def resync(
    config: dict[str, Any], *, base: Path | None = None, prune: bool = False
) -> dict[str, Any]:
    """Re-project everything under output/vault/. Raises VaultUnavailable.

    Idempotent -- an unchanged note costs one GET and is skipped -- so this is
    both how you catch up after the CouchDB was down and how old stories' links
    start resolving once a topic has crossed the threshold and earned its note.

    `prune` additionally removes notes the vault still holds under our own folder
    that we no longer produce, which is what makes a rename or a reorganisation
    finish rather than leave the old copies behind. Off by default: it is the
    only operation in this package that deletes anything.
    """
    base = base or VAULT_DIR
    paths = sorted(
        str(p.relative_to(base)) for p in base.rglob("*.md") if p.is_file()
    )
    result = project(paths, config, base=base)
    if prune:
        result["pruned"] = _prune(paths, config, base)
    return result


def _prune(paths: list[str], config: dict[str, Any], base: Path) -> list[str]:
    """Soft-delete notes under our folder that no longer exist on disk.

    Three guards, and the first is the one that must never bend:

    1. **Only under `vault.folder`.** The topics folder is shared with
       `podcast-digest` and with the reader's own prose, and our disk copy of a
       topic note is only ever *our section* of it -- so "not on disk" says
       nothing about whether it should exist. Pruning there would delete another
       writer's work from their own vault.
    2. **Never from an empty disk.** A container started with the `output/` mount
       missing presents as "we produce nothing", which would otherwise delete
       every note we have ever written. Refuse and say so.
    3. **Every removal is logged and returned**, so a surprising prune is visible
       afterwards instead of silent.
    """
    if not paths:
        log.error(
            "Refusing to prune: nothing on disk under %s. That is what a missing "
            "output mount looks like, not an empty vault.", base,
        )
        return []

    vault = build_vault(config)
    try:
        folder = vault.vault_path("x").rsplit("/", 1)[0] + "/"
        ours = {vault.vault_path(p) for p in paths}
        # Only paths that route into our own folder are candidates; topic notes
        # route elsewhere and are excluded by construction, not by filtering.
        stale = sorted(p for p in vault.entries_under(folder) if p not in ours)
        removed = [p for p in stale if vault.soft_delete(p)]
    finally:
        vault.close()

    if removed:
        log.info("Pruned %d note(s) the vault held but we no longer produce", len(removed))
    return removed
