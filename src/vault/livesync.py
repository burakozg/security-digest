"""Writing files into an Obsidian vault's CouchDB, in Self-hosted LiveSync's
own document format.

The notes are already files on disk under ``output/vault/``. This is the hop that
makes them readable in Obsidian on a phone, and it is deliberately not a file
copy: LiveSync replicates a vault against a CouchDB database, so writing
LiveSync's *own* document shape into that database materialises the file on every
client that syncs. Nothing has to be awake but the NAS, and no folder has to be
mounted anywhere.

The wire format is not documented by the plugin; it was reverse-engineered from
documents a live LiveSync v0.25 client wrote (E2EE off) and has been in
production in the taster and podcast-digest projects since. Two documents per
file:

* a **chunk**, ``{_id: "h:t<hash>", data: <markdown>, type: "leaf"}``, holding the
  text and content-addressed so identical content is stored once;
* an **entry**, keyed by the lowercased vault path, carrying
  ``{path, children: [chunk ids], ctime, mtime, size, type: "plain", eden: {}}``.

LiveSync fetches children strictly by id, so using our own ``h:t`` namespace
rather than matching its internal xxhash scheme costs at most a duplicate chunk.

**A deleted file stays deleted.** A digest is generated output; if you prune last
month's stories from the vault, re-projecting them would be the software arguing
with you, so a deleted entry is left alone and reported. The precise scope of
that promise: deleting a note in Obsidian is a **soft** delete -- LiveSync keeps
the document and sets ``deleted: true`` -- and that is what is honoured. A **hard**
CouchDB tombstone is a different matter: ``PUT`` with no ``_rev`` over one returns
201, not 409, so the write succeeds and the file comes back. That is a limit
rather than a decision, because once compaction has run a purged tombstone is
indistinguishable from a document that never existed. Hard deletes come from
direct database operations, not from anything Obsidian does.

Every failure raises :class:`VaultUnavailable`. The digest has already been
emailed and recorded by the time anything here runs, so a sync target being down
is an operator problem and must never fail the run -- see
:func:`src.vault.project_digest`, which swallows it.

**Ported from podcast-digest** (`podcast_agent/vault.py`), itself a port of
taster's `backend/app/couchdb_client.py`. Converted from ``httpx.AsyncClient`` to
``httpx.Client`` because this pipeline is synchronous; the document handling is
otherwise unchanged, and should stay that way.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

#: Ids LiveSync treats as chunks live in the ``h:`` namespace; ``h:t`` is the
#: sub-namespace taster claimed for content it writes itself, and sharing it is
#: correct rather than a collision -- both sides address chunks by content, so
#: identical text legitimately resolves to one document.
_CHUNK_PREFIX = "h:t"


class VaultUnavailable(Exception):
    """The vault database cannot be reached, or refused the write."""


def chunk_id(content: str) -> str:
    return _CHUNK_PREFIX + hashlib.sha1(content.encode("utf-8")).hexdigest()[:24]  # noqa: S324


def _q(doc_id: str) -> str:
    # Entry ids are vault paths and contain "/" -- left unencoded, CouchDB parses
    # them as db/doc/attachment segments and the write lands somewhere else.
    return quote(doc_id, safe="")


class LiveSyncVault:
    """Writes files into the vault's CouchDB in LiveSync's document format."""

    def __init__(
        self,
        url: str,
        db: str,
        user: str,
        password: str | None,
        *,
        folder: str,
        topics_folder: str,
        topics_dir: str = "topics",
        timeout_s: float = 30.0,
    ) -> None:
        self._db = db
        self._base = (url or "").rstrip("/")
        self._folder = folder
        self._topics_folder = topics_folder
        self._topics_dir = topics_dir
        self._client = (
            httpx.Client(base_url=self._base, auth=(user, password or ""), timeout=timeout_s)
            if self._base
            else None
        )

    @property
    def name(self) -> str:
        return f"vault:{self._base}/{self._db}"

    def vault_path(self, relative: Path | str) -> str:
        """Where a file written under ``output/vault/`` lands in the vault.

        Topic notes are routed out of the digest folder and filed flat: they are
        what every story's links point at, and burying them under this app's own
        output would make the graph read as more digest output rather than as
        subjects the corpus keeps returning to. It is also how they end up in the
        same folder podcast-digest writes its topic notes to.
        """
        rel = Path(relative)
        if rel.parts and rel.parts[0] == self._topics_dir:
            return f"{self._topics_folder}/{Path(*rel.parts[1:]).as_posix()}"
        return f"{self._folder}/{rel.as_posix()}"

    def existing_markdown(self, vault_path: str) -> str | None:
        """The note as the vault currently holds it, reassembled from its chunks.

        None when there is no live note -- including a soft-deleted one, so a
        topic the reader threw away is not quietly rebuilt by the merge.
        """
        entry = self._get(vault_path.lower())
        if entry is None or entry.get("deleted"):
            return None
        parts = []
        for cid in entry.get("children") or []:
            chunk = self._get(str(cid))
            if chunk is None:
                return None  # torn note; safer to rewrite than to merge into half
            parts.append(str(chunk.get("data") or ""))
        return "".join(parts)

    def project(
        self,
        relative: Path | str,
        markdown: str,
        *,
        mtime_ms: int,
        merge: bool = False,
    ) -> str | None:
        """Write one file into the vault. Returns its vault path, or None if skipped.

        Skipped means the file is already there byte for byte, or a human deleted
        it and that deletion is being respected.
        """
        if self._client is None:
            raise VaultUnavailable("vault.couchdb_url is not set")

        path = self.vault_path(relative)

        if merge:
            from src.vault.notes import merge_owned_section

            # Against the vault, not against our own file: nothing syncs back, so
            # our copy on disk cannot know what a person -- or another
            # application -- wrote into this note.
            current = self.existing_markdown(path)
            markdown = merge_owned_section(current, markdown)
            if current is not None and markdown == current:
                return None  # our section already says exactly this

        cid = chunk_id(markdown)
        self._put_chunk(cid, markdown)

        entry: dict[str, Any] = {
            "_id": path.lower(),  # LiveSync keys entries by lowercased path
            "path": path,
            "children": [cid],
            "ctime": mtime_ms,
            "mtime": mtime_ms,
            "size": len(markdown.encode("utf-8")),
            "type": "plain",
            "eden": {},
        }
        written = self._put_entry(entry)
        if written:
            log.info("Projected %s (%d bytes)", path, entry["size"])
        return path if written else None

    def _put_chunk(self, cid: str, markdown: str) -> None:
        """Ensure the chunk exists.

        Content-addressed, so a live chunk with this id already holds this exact
        text and is correct as it stands. A soft-deleted one is revived
        unconditionally -- unlike an entry, a chunk carries no intent: an entry
        whose children cannot be fetched is a file that renders empty, which is
        worse than either keeping it or deleting it.

        The tombstone branch is for a race (the chunk was live when we wrote and
        deleted before we looked); an ordinary hard-deleted chunk never gets here,
        because a PUT with no ``_rev`` over a tombstone returns 201.
        """
        body = {"_id": cid, "data": markdown, "type": "leaf"}
        response = self._put(cid, body)
        if response.status_code != 409:
            return

        existing = self._get(cid)
        if existing is not None and not existing.get("deleted"):
            return
        rev = existing.get("_rev") if existing else self._tombstone_rev(cid)
        if rev is None:
            raise VaultUnavailable(f"conflict on chunk {cid} with no revision to take over")
        self._put_or_raise(cid, {**body, "_rev": rev})

    def _put_entry(self, entry: dict[str, Any]) -> bool:
        """Write the file entry. False when nothing needed writing."""
        entry_id = str(entry["_id"])
        response = self._put(entry_id, entry)
        if response.status_code != 409:
            return True

        existing = self._get(entry_id)
        if existing is None:
            # Raced: the document was live when we tried to write and gone by the
            # time we looked. Not the ordinary hard-delete path -- a PUT over a
            # tombstone returns 201 and never reaches here. Leaving it alone is
            # the same call as below, and the loser of a race should not win it.
            log.info("Skipping %s: deleted while writing", entry["path"])
            return False
        if existing.get("deleted"):
            # What deleting a note in Obsidian produces: LiveSync keeps the
            # document and flags it. Respected -- see the module docstring.
            log.info("Skipping %s: deleted in the vault", entry["path"])
            return False
        if list(existing.get("children") or []) == entry["children"]:
            return False  # already present, byte for byte

        self._put_or_raise(
            entry_id,
            {**entry, "_rev": existing["_rev"], "ctime": existing.get("ctime") or entry["ctime"]},
        )
        return True

    def _put(self, doc_id: str, body: dict[str, Any]) -> httpx.Response:
        assert self._client is not None
        try:
            response = self._client.put(f"/{self._db}/{_q(doc_id)}", json=body)
        except httpx.HTTPError as exc:
            raise VaultUnavailable(f"{self.name} unreachable: {type(exc).__name__}: {exc}") from exc
        if response.status_code in (201, 202, 409):
            return response
        # Credentials never reach the message -- only what was attempted.
        raise VaultUnavailable(
            f"{self.name} refused a write: HTTP {response.status_code} {response.text[:200]}"
        )

    def _put_or_raise(self, doc_id: str, body: dict[str, Any]) -> None:
        response = self._put(doc_id, body)
        if response.status_code == 409:
            raise VaultUnavailable(f"{self.name}: repeated conflict writing {doc_id}")

    def _get(self, doc_id: str) -> dict[str, Any] | None:
        assert self._client is not None
        try:
            response = self._client.get(f"/{self._db}/{_q(doc_id)}")
        except httpx.HTTPError as exc:
            raise VaultUnavailable(f"{self.name} unreachable: {type(exc).__name__}: {exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise VaultUnavailable(
                f"{self.name} refused a read: HTTP {response.status_code} {response.text[:200]}"
            )
        doc: dict[str, Any] = response.json()
        return doc

    def _tombstone_rev(self, doc_id: str) -> str | None:
        """The revision of a hard-deleted document's leaf, so it can be written
        over. A plain GET 404s for these, so the deleted leaf is asked for
        explicitly."""
        assert self._client is not None
        try:
            response = self._client.get(
                f"/{self._db}/{_q(doc_id)}",
                params={"open_revs": "all"},
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise VaultUnavailable(f"{self.name} unreachable: {type(exc).__name__}: {exc}") from exc
        if response.status_code != 200:
            return None
        for row in response.json():
            ok = row.get("ok") if isinstance(row, dict) else None
            if isinstance(ok, dict) and ok.get("_rev"):
                rev: str = ok["_rev"]
                return rev
        return None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def entries_under(self, prefix: str) -> set[str]:
        """Live entry paths beginning with `prefix`.

        One ranged `_all_docs`, and a failure returns nothing rather than raising:
        this feeds a deletion, so knowing less must mean deleting less.

        `include_docs`, unlike `topic_stems`'s ids-only listing, because a
        LiveSync deletion is a `deleted: true` FIELD on a document that still
        exists -- `_all_docs` omits CouchDB tombstones but happily returns these.
        Without reading the documents, every note ever pruned would come back in
        this set forever and cost a GET on every later prune. Entry documents
        carry no text (the markdown lives in separate chunks), so this stays one
        small request.
        """
        if self._client is None:
            return set()
        try:
            response = self._client.get(
                f"/{self._db}/_all_docs",
                params={
                    "startkey": f'"{prefix}"',
                    "endkey": f'"{prefix}\ufff0"',
                    "include_docs": "true",
                },
            )
            if response.status_code != 200:
                return set()
            rows = response.json().get("rows") or []
        except (httpx.HTTPError, ValueError):
            log.warning("Could not list %s; nothing will be pruned", prefix)
            return set()
        return {
            str(row["id"]) for row in rows
            if str(row.get("id", "")).startswith(prefix)
            and str(row["id"]).endswith(".md")
            and not (row.get("doc") or {}).get("deleted")
        }

    def soft_delete(self, path: str) -> bool:
        """Mark a note deleted, the way Obsidian itself does. True if it changed.

        A flag on the kept document, never a hard `DELETE`: clients propagate it
        as a deletion, it is reversible, and it leaves a document our own writer
        recognises -- a purged tombstone is indistinguishable from a note that
        never existed, so a later write would silently recreate the file.
        """
        doc = self._get(path.lower())
        if doc is None or doc.get("deleted"):
            return False
        doc["deleted"] = True
        self._put_or_raise(str(doc["_id"]), doc)
        log.info("Removed %s from the vault", path)
        return True

    def topic_stems(self) -> set[str]:
        """Filenames (without .md) of every note already in the topics folder.

        One ranged `_all_docs` request, ids only -- no chunk reads. Used to adopt
        a name another writer already chose for a thing, rather than pinning a
        second one for it; see `src.vault.topics.note_name`.

        An empty set on any failure, deliberately: not knowing what is there is a
        reason to fall back to choosing our own name, never a reason to fail a
        run that has already been delivered and recorded.
        """
        if self._client is None:
            return set()
        prefix = f"{self._topics_folder}/"
        try:
            response = self._client.get(
                f"/{self._db}/_all_docs",
                params={"startkey": f'"{prefix}"', "endkey": f'"{prefix}\ufff0"'},
            )
            if response.status_code != 200:
                return set()
            rows = response.json().get("rows") or []
        except (httpx.HTTPError, ValueError):
            log.warning("Could not list %s; falling back to naming topics ourselves", prefix)
            return set()
        return {
            str(row["id"])[len(prefix):-3]
            for row in rows
            if str(row.get("id", "")).startswith(prefix) and str(row["id"]).endswith(".md")
        }
