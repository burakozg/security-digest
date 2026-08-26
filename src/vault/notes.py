"""One topic note, many writers.

A topic note in the vault -- ``99 topics/fortinet.md`` -- is written by more than
one thing. This app contributes what the security corpus knows about an entity;
`podcast-digest` does the same for the podcast corpus; and the person whose vault
it is writes their own thinking at the top. All three want the *same* file,
because splitting them would put two ``fortinet.md`` in the vault, leave links
resolving to whichever Obsidian picked first, and produce a graph that quietly
lies about how many things connect.

So the file is divided by ownership rather than by author:

* everything outside a marked region belongs to whoever wrote it, and is never
  touched -- that includes the human's prose and any other application's section;
* a region between ``<!-- begin:<owner> -->`` and ``<!-- end:<owner> -->`` is
  owned wholly by that writer, replaced on every run;
* frontmatter keys are namespaced per owner, so two writers can both describe the
  same entity without either having to know the other's schema.

HTML comments rather than heading text as the delimiter: a heading is something a
person may rename or another writer may pick by coincidence, and getting that
wrong means silently eating someone else's work. A comment is explicit, invisible
in reading view, and belongs to nobody by accident.

**The merge happens against what is in the vault, not against the file on disk.**
Our copy under ``output/vault/`` never sees a human's edit -- nothing syncs back
-- so merging into it would rewrite the vault from a source that cannot know what
the vault contains. See :func:`merge_owned_section`, whose caller passes the note
as the vault currently holds it.

**Ported from podcast-digest** (`podcast_agent/notes.py`), which is the canonical
copy and the first implementation of this contract. Only :data:`OWNER` and
:data:`KEY_PREFIX` differ, plus the absence of its legacy-adoption machinery:
this app has never written a topic note in any other format, so there is nothing
here to adopt. Fix bugs in both copies or neither -- a divergence here corrupts
notes another application also writes to.
"""

from __future__ import annotations

import re

#: This application's owner tag. A second application picks its own; anything it
#: writes outside its own markers is not ours to touch, and vice versa.
OWNER = "security-digest"

#: Frontmatter keys this writer owns are prefixed, so two writers describing one
#: entity cannot collide on `mentions` meaning two different counts.
KEY_PREFIX = "security_"


def begin_marker(owner: str = OWNER) -> str:
    return f"<!-- begin:{owner} -->"


def end_marker(owner: str = OWNER) -> str:
    return f"<!-- end:{owner} -->"


def _region(owner: str) -> re.Pattern[str]:
    return re.compile(
        rf"[ \t]*{re.escape(begin_marker(owner))}.*?{re.escape(end_marker(owner))}[ \t]*",
        re.DOTALL,
    )


def wrap(body: str, owner: str = OWNER) -> str:
    """Mark a block as owned, so a later run can replace exactly this much."""
    return f"{begin_marker(owner)}\n{body.strip()}\n{end_marker(owner)}"


_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def split_frontmatter(text: str) -> tuple[list[str], str]:
    """``(frontmatter lines, body)``. No YAML parse: these notes are line-per-key
    by construction, and a real parser would reformat a human's frontmatter as
    the price of reading it."""
    match = _FRONTMATTER.match(text)
    if not match:
        return [], text
    return match.group(1).split("\n"), text[match.end() :]


def _key(line: str) -> str:
    return line.split(":", 1)[0].strip()


def merge_frontmatter(
    existing: list[str], ours: list[str], *, prefix: str = KEY_PREFIX
) -> list[str]:
    """Replace our prefixed keys; leave every other line exactly as it was.

    Two classes of key, and the difference matters:

    * **Prefixed** (``security_mentions``) are ours. Replaced every run, and
      dropped when this run no longer produces them.
    * **Unprefixed** (``type``, ``title``, ``tags``) describe the note as a
      whole, so they belong to whoever created it. We supply them when the key is
      absent and never otherwise -- overwriting ``tags: [topic, ai]`` with our own
      ``tags: [topic]`` would silently drop a tag the reader added, and YAML would
      not even complain, because the last duplicate key wins.
    """
    kept: list[str] = []
    for line in existing:
        if not line.strip() or line.startswith(prefix):
            continue
        # Drop an EXACT duplicate of a line already kept. Duplicate keys are
        # invalid YAML: Obsidian gives up on the whole block and shows raw text
        # instead of the properties panel, so the note looks broken to a reader.
        #
        # Not hypothetical — 21 topic notes in this vault acquired a second copy
        # of another writer's `security_*` block. No sequence of this function can
        # produce that (it always appends the owner's keys last, and these sat
        # either side of another writer's block), and some of the affected notes
        # had no region from the writer whose keys were duplicated. The likely
        # source is LiveSync's own line-level merge of two revisions during
        # concurrent writes, which duplicates identical lines rather than
        # collapsing them.
        #
        # Only EXACT duplicates. Two lines with the same key and different values
        # are a real disagreement between writers, and quietly picking one would
        # be inventing an answer; they are left for a human, who can at least see
        # both.
        if line in kept:
            continue
        kept.append(line)
    seen = {_key(line) for line in kept}
    owned = [line for line in ours if line.startswith(prefix)]
    seeds = [line for line in ours if not line.startswith(prefix) and _key(line) not in seen]
    return kept + seeds + owned


def merge_owned_section(existing: str | None, ours: str, *, owner: str = OWNER) -> str:
    """Our section written into ``existing``, leaving everything else alone.

    ``existing`` is the note as the vault currently holds it, or None when there
    is no note yet. ``ours`` is a complete note as we would write it fresh --
    frontmatter, a title, and one marked region.

    Three cases, in the order they are checked:

    1. **No existing note** -- ours becomes the file.
    2. **A marked region of ours is present** -- it is replaced in place, so the
       human's prose above it and any other writer's section below it keep their
       position on the page.
    3. **No marked region** -- our section is appended, below whatever is already
       there.
    """
    if not existing or not existing.strip():
        return ours

    our_front, our_body = split_frontmatter(ours)
    our_region = _region(owner).search(our_body)
    our_section = our_region.group(0).strip() if our_region else wrap(our_body.strip(), owner)

    front, body = split_frontmatter(existing)
    merged_front = merge_frontmatter(front, our_front)

    if _region(owner).search(body):
        # A plain replacement string would treat backslashes in our section as
        # regex escapes; md_escape_inline emits plenty of those.
        body = _region(owner).sub(lambda _: our_section, body, count=1)
    else:
        body = body.rstrip() + "\n\n" + our_section + "\n"

    head = "---\n" + "\n".join(merged_front) + "\n---\n" if merged_front else ""
    return head + body
