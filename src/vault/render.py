"""Rendering a delivered story as an Obsidian note.

Deliberately not `src.digest.render_markdown`. That one builds one document for
an email, and its `_safe_text` neutralises `[` and `]` wholesale -- correct there,
fatal here, where the whole point is that `[[wikilinks]]` work. So the vault gets
its own renderer with its own escaping (:mod:`src.vault.text`), which still has
to stop an untrusted headline closing our link and opening its own.

Two note shapes:

* a **story note**, one per delivered item: what it is, who reported it, the
  summary, the topics it names, and the feed text the fetcher already held. This
  is the atom -- it is what topic notes link to and what backlinks accumulate on;
* the **topic note** lives in :mod:`src.vault.topics`, because its content comes
  from the whole corpus rather than from one run.

**There is deliberately no per-day index note.** One was written for a while and
earned nothing: measured against the live vault, all 64 of them had zero inbound
links, while every one of the 427 story notes had at least one. Their entire
content was `[[story]] -- *publisher*` lines, and a story note already carries
`date`, `digest`, `category` and `publisher` in its frontmatter -- so they added
64 disconnected hubs to the graph and no information at all. The digest as an
*edition* is what the email and the History page are for; the vault is for the
stories and what connects them.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from src.vault.text import as_quote, md_escape_inline, sanitize_md_block, slugify, wikilink

#: Where a story note lives under `output/vault/`, and therefore under
#: `vault.folder` once projected: one folder per month, inside one per year.
#:
#: A single flat folder does not survive growth -- this instance adds ~15 stories
#: a day -- and `podcast-digest` already nests under a year in the same vault
#: (`11 podcasts/2026/`). Nesting costs nothing in Obsidian: it resolves
#: `[[wikilinks]]` by FILENAME, not by path, which is why moving these notes
#: broke no link and why nothing that stores a bare stem (topic notes, the
#: `entity_mentions` table) had to change.
_DATED_STEM = re.compile(r"^(\d{4})-(\d{2})-\d{2}-")


def story_dir(date: str) -> str:
    """``"2026/08"`` for a note dated 2026-08-25."""
    return f"{date[:4]}/{date[5:7]}"


def story_rel(stem: str) -> str | None:
    """A story note's path relative to `output/vault/`, from its name alone.

    `story_note_name` always date-prefixes the stem, so a note's location is
    derivable from the note's own name with no lookup and no state carried
    around. That is what lets the relink pass reach into a month it is not
    currently writing -- the stale story it needs to touch is routinely older
    than the run touching it.

    None for a stem that is not date-prefixed: better to skip a note we cannot
    place than to build a path out of a slice of a slug.
    """
    match = _DATED_STEM.match(stem)
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}/{stem}.md"

#: Marks a note as machine-managed, the convention `homelab/vault-sync.sh`
#: established and checks for. A reader opening a note in the vault can tell at a
#: glance that editing it is pointless -- the next run rewrites these wholesale,
#: because unlike a topic note they have no other writer.
MANAGED_BY = "security-digest"

#: Cap on the feed text carried into a story note. `sources.max_description_chars`
#: is 5000 on the security instance and the note holds one block per outlet, so a
#: heavily-clustered story is otherwise a wall.
MAX_RAW_CHARS = 6000


def _yaml(value: Any) -> str:
    """A YAML scalar for a value that came from a feed or a model.

    Always quoted. An unquoted title containing a colon, a leading `-`, or the
    word `yes` is either a YAML parse error or a value that silently changes
    type, and every string in this frontmatter is untrusted.
    """
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


def story_note_name(
    date: str, title: str, link: str, taken: dict[str, str] | None = None
) -> str:
    """The story note's filename stem, which is also its wikilink target.

    Date-prefixed so the folder sorts chronologically and two stories about the
    same product months apart cannot collide. A same-day collision -- two feeds
    whose titles slug identically -- gets a short hash of the link appended,
    because a collision here would point every link at whichever note was written
    last, silently.

    `taken` maps a stem already assigned to the link that owns it, so the answer
    is stable across re-runs: the same story keeps its name, and only the second
    claimant on a stem is pushed onto a hashed one. Callers build it from the
    `link:` line the notes already on disk carry -- which is why that line is in
    the frontmatter.
    """
    stem = f"{date[:10]}-{slugify(title, max_len=70)}"
    owner = (taken or {}).get(stem)
    if owner is None or owner == link:
        return stem
    return f"{stem}-{hashlib.sha1(link.encode('utf-8')).hexdigest()[:6]}"  # noqa: S324


_LINK_LINE = re.compile(r"^link: (.*)$", re.MULTILINE)


def claimed_stems(stories_dir: Path, date: str) -> dict[str, str]:
    """stem -> link, for story notes already written for `date`.

    Only that day's notes are read: a stem carries its date, so nothing outside
    it can collide, and scanning the whole folder would mean opening every note
    ever written on every run.
    """
    taken: dict[str, str] = {}
    for path in sorted(stories_dir.glob(f"{date[:10]}-*.md")):
        try:
            match = _LINK_LINE.search(path.read_text(encoding="utf-8")[:2000])
        except OSError:
            continue
        if match:
            taken[path.stem] = json.loads(match.group(1)) if match.group(1).startswith('"') \
                else match.group(1).strip()
    return taken


def _byline(item: dict[str, Any]) -> str:
    """Every outlet that reported the story, each linked.

    Mirrors `digest._render_sources`: a clustered item merges several reports of
    one event, and crediting only the first would hide that the others exist.
    """
    links = item.get("links") or []
    if len(links) < 2:
        fallback = md_escape_inline(item.get("publisher") or item.get("source", ""), max_chars=120)
        link = _safe_url(item.get("link", ""))
        return f"*[{fallback}]({link})*" if link and fallback else f"*{fallback}*"
    parts = []
    for entry in links:
        publisher = md_escape_inline(entry.get("publisher", "") or "source", max_chars=120)
        href = _safe_url(entry.get("link", ""))
        parts.append(f"[{publisher}]({href})" if href else publisher)
    return "*" + " · ".join(parts) + "*"


def _safe_url(url: str) -> str:
    """http(s) only, and nothing that would break out of a Markdown link target."""
    candidate = (url or "").strip()
    if not candidate.lower().startswith(("http://", "https://")):
        return ""
    if any(ch in candidate for ch in (" ", "(", ")", "<", ">", '"', "'", "\\")):
        return ""
    return candidate


def topic_links(entities: list[str], resolved: dict[str, str]) -> str:
    """The story's `**Topics:**` line.

    `resolved` maps a surface to the topic note it should link to. A surface that
    is absent has not reached `vault.min_mentions` yet and renders as plain text,
    so the vault gains no dangling links -- and the next time it is named, the
    topic note appears and a resync makes this line a link.
    """
    if not entities:
        return ""
    parts = [wikilink(resolved.get(surface, ""), surface, max_chars=80) for surface in entities]
    return "**Topics:** " + ", ".join(parts)


def story_note(
    item: dict[str, Any],
    digest_title: str,
    date: str,
    *,
    resolved_topics: dict[str, str] | None = None,
    backfilled: bool = False,
) -> str:
    """One delivered story as a note."""
    title = md_escape_inline(item.get("title", "Untitled") or "Untitled", max_chars=200)
    front = [
        "type: security-story",
        f"title: {_yaml(item.get('title'))}",
        f"date: {date[:10]}",
        f"digest: {_yaml(digest_title)}",
        f"category: {_yaml(item.get('category'))}",
    ]
    if item.get("domain"):
        front.append(f"domain: {_yaml(item.get('domain'))}")
    # `publisher`, not `source`. `source:` is a cross-project convention in this
    # vault meaning "this note is machine-managed, and here is where it came
    # from" -- homelab/vault-sync.sh refuses to overwrite a note that lacks one.
    # Using the same key for the news outlet would make a generated note claim to
    # have been synced from "BleepingComputer".
    front.append(f"publisher: {_yaml(item.get('publisher') or item.get('source'))}")
    front.append(f"source: {MANAGED_BY}")
    # Not decoration: `claimed_stems` reads this back to keep note names stable
    # across re-runs when two same-day titles slug identically.
    front.append(f"link: {_yaml(item.get('link'))}")
    front.append("tags: [security-story]")
    if backfilled:
        # Says so on the note rather than in a changelog: a backfilled note has
        # no raw content and a single-outlet byline because the history table
        # never stored either, and six months from now that has to read as a
        # known limit rather than as a note that lost something.
        front.append("backfilled: true")

    parts = ["---", "\n".join(front), "---", "", f"# {title}", "", _byline(item), ""]

    summary = sanitize_md_block(item.get("summary", ""), max_chars=4000)
    if summary:
        parts += [summary, ""]

    topics = topic_links(list(item.get("entities") or []), resolved_topics or {})
    if topics:
        parts += [topics, ""]

    raw = _raw_content(item)
    if raw:
        parts += ["## Raw content", "", raw, ""]

    return "\n".join(parts).rstrip() + "\n"


def _raw_content(item: dict[str, Any]) -> str:
    """The feed text the fetcher already stored, quoted per outlet.

    Not a re-fetch of the article: `sources.max_description_chars` of feed text is
    what this app has ever held, and going to the web for more would be a
    different feature with different failure modes. A clustered story pools every
    member's text (see `summariser._combined_description`), so the `[Publisher]`
    prefixes that pooling adds are what the per-outlet split below reads.
    """
    text = sanitize_md_block(item.get("description", ""), max_chars=MAX_RAW_CHARS)
    if not text:
        return ""
    blocks = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        # `_combined_description` prefixes each member's text with "[Publisher] ".
        # Promoting that to the callout's title is both more readable and safer:
        # left inline it renders as a broken Markdown link.
        match = _POOLED_PREFIX.match(block)
        if match:
            title = md_escape_inline(match.group(1), max_chars=80)
            blocks.append(f"> [!quote] {title}\n" + as_quote(match.group(2).strip()))
        else:
            blocks.append("> [!quote]\n" + as_quote(block))
    return "\n\n".join(blocks)


#: The "[Publisher] " prefix `summariser._combined_description` writes ahead of
#: each outlet's text when it pools a clustered story's descriptions.
_POOLED_PREFIX = re.compile(r"^\[([^\]\n]{1,80})\]\s+(.*)$", re.DOTALL)


def today() -> str:
    return datetime.date.today().isoformat()


#: The line `story_note` writes for a story's topics. Anchored to the start of a
#: line so nothing in a summary or a quoted description can be mistaken for it.
TOPICS_LINE = re.compile(r"^\*\*Topics:\*\* .*$", re.MULTILINE)


def relink(markdown: str, entities: list[str], resolved: dict[str, str]) -> str:
    """Rewrite a story note's `**Topics:**` line against the current topic notes.

    A story is written with the topics that existed the day it was delivered, and
    a thing named for the second time months later earns its note then. Without
    this, the story that first named it keeps the surface as plain text forever:
    the topic note links to the story, but the story does not link back, and the
    graph shows one edge where there are two.

    Only that one line is touched -- the note is otherwise left exactly as it was.
    """
    line = topic_links(entities, resolved)
    if not line:
        return markdown
    if TOPICS_LINE.search(markdown):
        return TOPICS_LINE.sub(lambda _: line, markdown, count=1)
    return markdown
