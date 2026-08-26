"""The LiveSync wire format, and the promises made about deletion."""

import json
from urllib.parse import unquote

import httpx
import pytest

from src.vault.livesync import LiveSyncVault, VaultUnavailable, chunk_id


class FakeCouch:
    """A CouchDB just real enough to exercise the document handling.

    Records every request so a test can assert on what was actually sent -- the
    point of most of these tests is the shape of the write, not the reply.
    """

    def __init__(self, docs=None, put_status=None):
        self.docs = dict(docs or {})
        self.requests = []
        self.puts = []
        self.put_status = put_status or {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        # The client percent-encodes the whole id (entry ids are vault paths and
        # contain "/"), so decode it back to the key the test wrote.
        doc_id = unquote(request.url.path.split("/", 2)[-1])
        if request.method == "PUT":
            body = json.loads(request.content)
            self.puts.append(body)
            forced = self.put_status.get(doc_id)
            if forced:
                return httpx.Response(forced, json={"error": "conflict"})
            existing = self.docs.get(doc_id)
            if existing is not None and existing.get("_rev") != body.get("_rev"):
                return httpx.Response(409, json={"error": "conflict"})
            self.docs[doc_id] = {**body, "_rev": "2-x"}
            return httpx.Response(201, json={"ok": True})
        doc = self.docs.get(doc_id)
        if doc is None:
            return httpx.Response(404, json={"error": "not_found"})
        return httpx.Response(200, json=doc)


def build(couch, **kwargs):
    vault = LiveSyncVault(
        "http://couch.test", "vault", "u", "p",
        folder="12 daily-digest", topics_folder="99 topics", **kwargs,
    )
    vault._client = httpx.Client(
        transport=httpx.MockTransport(couch.handler), base_url="http://couch.test"
    )
    return vault


def test_writes_a_chunk_and_an_entry_in_livesync_shape():
    couch = FakeCouch()
    vault = build(couch)

    assert vault.project("stories/a.md", "# Hello", mtime_ms=1700) == "12 daily-digest/stories/a.md"

    chunk, entry = couch.puts
    assert chunk == {"_id": chunk_id("# Hello"), "data": "# Hello", "type": "leaf"}
    assert chunk["_id"].startswith("h:t")
    assert entry["_id"] == "12 daily-digest/stories/a.md"  # already lowercase
    assert entry["path"] == "12 daily-digest/stories/a.md"
    assert entry["children"] == [chunk_id("# Hello")]
    assert entry["type"] == "plain"
    assert entry["eden"] == {}
    assert entry["ctime"] == entry["mtime"] == 1700


def test_entry_id_is_the_lowercased_path():
    couch = FakeCouch()
    vault = build(couch)
    vault.project("stories/Mixed Case.md", "x", mtime_ms=1)
    entry = couch.puts[1]
    assert entry["_id"] == "12 daily-digest/stories/mixed case.md"
    # The readable path keeps its case; only the key is folded.
    assert entry["path"] == "12 daily-digest/stories/Mixed Case.md"


def test_size_counts_utf8_bytes_not_characters():
    couch = FakeCouch()
    vault = build(couch)
    vault.project("stories/a.md", "héllo →", mtime_ms=1)
    assert couch.puts[1]["size"] == len("héllo →".encode("utf-8"))


def test_slashes_are_percent_encoded_in_the_request_path():
    couch = FakeCouch()
    vault = build(couch)
    vault.project("stories/a.md", "x", mtime_ms=1)
    entry_request = couch.requests[-1]
    # Unencoded, CouchDB reads the id as db/doc/attachment and the write lands
    # somewhere else entirely.
    assert "%2F" in str(entry_request.url)
    assert "/vault/12%20daily-digest%2Fstories%2Fa.md" in str(entry_request.url)


def test_topic_notes_route_to_the_shared_topics_folder():
    vault = build(FakeCouch())
    assert vault.vault_path("topics/fortinet.md") == "99 topics/fortinet.md"
    assert vault.vault_path("stories/x.md") == "12 daily-digest/stories/x.md"


def test_unchanged_note_is_not_rewritten():
    markdown = "# Hello"
    entry_id = "12 daily-digest/stories/a.md"
    couch = FakeCouch({
        entry_id: {"_id": entry_id, "_rev": "1-a", "children": [chunk_id(markdown)]},
        chunk_id(markdown): {"_id": chunk_id(markdown), "_rev": "1-a", "data": markdown},
    })
    vault = build(couch)

    assert vault.project("stories/a.md", markdown, mtime_ms=1) is None


def test_a_note_deleted_in_obsidian_stays_deleted():
    """Soft delete -- what deleting a note in Obsidian actually produces."""
    entry_id = "12 daily-digest/stories/a.md"
    couch = FakeCouch({entry_id: {"_id": entry_id, "_rev": "1-a", "deleted": True}})
    vault = build(couch)

    assert vault.project("stories/a.md", "# Back from the dead", mtime_ms=1) is None
    # The chunk may be written (it carries no intent); the entry must not be.
    assert not [p for p in couch.puts if p.get("_id") == entry_id and p.get("_rev")]


def test_changed_note_is_rewritten_keeping_its_original_ctime():
    entry_id = "12 daily-digest/stories/a.md"
    couch = FakeCouch({entry_id: {"_id": entry_id, "_rev": "1-a", "children": ["h:told"], "ctime": 100}})
    vault = build(couch)

    assert vault.project("stories/a.md", "new text", mtime_ms=999) == entry_id
    rewrite = couch.puts[-1]
    assert rewrite["_rev"] == "1-a"
    assert rewrite["ctime"] == 100      # when the note was created
    assert rewrite["mtime"] == 999      # when it last changed


def test_unreachable_database_raises_vault_unavailable():
    def refuse(request):
        raise httpx.ConnectError("no route to host")

    vault = LiveSyncVault("http://couch.test", "vault", "u", "p",
                          folder="f", topics_folder="t")
    vault._client = httpx.Client(transport=httpx.MockTransport(refuse), base_url="http://couch.test")
    with pytest.raises(VaultUnavailable, match="unreachable"):
        vault.project("stories/a.md", "x", mtime_ms=1)


def test_credentials_never_appear_in_an_error_message():
    def deny(request):
        return httpx.Response(401, text="unauthorized")

    vault = LiveSyncVault("http://couch.test", "vault", "someuser", "hunter2",
                          folder="f", topics_folder="t")
    vault._client = httpx.Client(transport=httpx.MockTransport(deny), base_url="http://couch.test")
    with pytest.raises(VaultUnavailable) as excinfo:
        vault.project("stories/a.md", "x", mtime_ms=1)
    assert "hunter2" not in str(excinfo.value)


def test_merge_reads_the_vault_copy_not_ours():
    """The whole point of merge=True: nothing syncs back, so our disk copy cannot
    know what a person wrote into the note."""
    entry_id = "99 topics/fortinet.md"
    theirs = "Human prose.\n\n<!-- begin:security-digest -->\nold\n<!-- end:security-digest -->\n"
    couch = FakeCouch({
        entry_id: {"_id": entry_id, "_rev": "1-a", "children": ["h:tprev"]},
        "h:tprev": {"_id": "h:tprev", "_rev": "1-a", "data": theirs},
    })
    vault = build(couch)

    ours = "<!-- begin:security-digest -->\nnew\n<!-- end:security-digest -->\n"
    vault.project("topics/fortinet.md", ours, mtime_ms=1, merge=True)

    written = couch.puts[0]["data"]
    assert "Human prose." in written
    assert "new" in written and "old" not in written


# --- pruning ----------------------------------------------------------------

def test_entries_under_lists_live_notes_with_that_prefix():
    couch = FakeCouch()
    couch.docs["_all_docs"] = None  # unused; handler special-cases below
    vault = build(couch)

    def handler(request):
        if request.url.path.endswith("_all_docs"):
            assert request.url.params.get("include_docs") == "true"
            return httpx.Response(200, json={"rows": [
                {"id": "12 daily-digest/2026/08/a.md", "doc": {}},
                {"id": "12 daily-digest/stories/old.md", "doc": {}},
                {"id": "12 daily-digest/2026/08/not-a-note", "doc": {}},
                # A LiveSync deletion is a field on a live document, so this is
                # still returned by _all_docs -- and must not be offered up for
                # deleting all over again on every later prune.
                {"id": "12 daily-digest/stories/gone.md", "doc": {"deleted": True}},
            ]})
        return couch.handler(request)

    vault._client = httpx.Client(transport=httpx.MockTransport(handler),
                                 base_url="http://couch.test")
    found = vault.entries_under("12 daily-digest/")
    assert found == {"12 daily-digest/2026/08/a.md", "12 daily-digest/stories/old.md"}


def test_a_listing_failure_prunes_nothing():
    """Knowing less must mean deleting less."""
    vault = LiveSyncVault("http://couch.test", "vault", "u", "p",
                          folder="f", topics_folder="t")
    vault._client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")),
        base_url="http://couch.test",
    )
    assert vault.entries_under("f/") == set()


def test_soft_delete_flags_the_document_rather_than_removing_it():
    doc_id = "12 daily-digest/stories/old.md"
    couch = FakeCouch({doc_id: {"_id": doc_id, "_rev": "1-a", "children": ["h:tx"]}})
    vault = build(couch)

    assert vault.soft_delete(doc_id) is True
    written = couch.puts[-1]
    assert written["deleted"] is True
    assert written["_rev"] == "1-a"          # kept, not tombstoned
    assert written["children"] == ["h:tx"]   # the document survives intact


def test_soft_deleting_twice_is_a_no_op():
    doc_id = "12 daily-digest/stories/old.md"
    couch = FakeCouch({doc_id: {"_id": doc_id, "_rev": "1-a", "deleted": True}})
    vault = build(couch)
    assert vault.soft_delete(doc_id) is False
    assert couch.puts == []
