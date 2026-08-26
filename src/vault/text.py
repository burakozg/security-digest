"""Text handling for vault notes: slugs, escaping, and entity canonicalisation.

**Ported from podcast-digest** (`podcast_agent/sanitize.py` and the top of
`podcast_agent/entities.py`). Keep it that way. Both applications write into the
same `99 topics/` notes, and a topic note's *filename* is `slugify(name)` -- so
if these two functions ever disagree, one thing acquires two notes and the whole
point of a shared topic folder is lost. Changes belong in both copies or
neither.

Deliberately NOT reusing `src.utils.slug`: that one deletes non-ASCII rather
than transliterating it, so "Müller" becomes "mller" there and "muller" in
podcast-digest -- exactly the silent split this module exists to prevent.

`bleach` is not a dependency here and is not needed: feed descriptions are
already flattened by `src.topics.strip_html` at fetch time (see
`fetcher._entry_description`), so this module's block cleaner only has to defend
against Markdown structure, not against markup. It deliberately does NOT reuse
`strip_html` either: that one collapses every whitespace run including newlines,
which is right for a prompt and wrong for a note, where the paragraph breaks are
the only structure the feed text has left.
"""

from __future__ import annotations

import re
import unicodedata

from html import unescape

#: Control characters except tab/newline.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Includes U+00A0 (no-break space), which feeds emit constantly.
_WHITESPACE_RUNS = re.compile("[ \t ]+")
_BLANK_LINE_RUNS = re.compile(r"\n{3,}")

#: Markdown control characters that must not break out of an inline context.
#: `[` and `]` are in here, which is what stops a feed headline of
#: "Evil ]] [[malware" closing our wikilink and opening its own.
_MD_INLINE_SPECIALS = re.compile(r"([\\`*_\[\]<>|#~])")

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")

_TAG = re.compile(r"<[^>]+>")

#: Characters that would break out of `[[target|label]]`. Backslash-escaping is
#: NOT a defence here: Obsidian's wikilink parser does not process escapes inside
#: the brackets, so `\]\]` still closes the link and everything after it lands
#: outside as attacker-chosen Markdown. They have to be removed.
_UNLINKABLE = str.maketrans({"[": "", "]": "", "|": "-", "#": "", "^": ""})

#: A line of only dashes/equals opens YAML frontmatter or turns the line above it
#: into a heading. Neutralised in any block of untrusted text we embed.
_FRONTMATTER_LINE = re.compile(r"^\s{0,3}(-{3,}|={3,}|\.{3,})\s*$", re.MULTILINE)


def strip_control_chars(text: str) -> str:
    return _CONTROL_CHARS.sub("", text)


def slugify(text: str, *, max_len: int = 60, fallback: str = "untitled") -> str:
    """ASCII-only, lowercase, hyphenated slug for use in filenames.

    Byte-for-byte the same rule as podcast-digest's -- see the module docstring
    for why that matters more than the rule being ideal.
    """
    normalized = unicodedata.normalize("NFKD", text)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_STRIP.sub("-", ascii_only).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or fallback


def md_escape_inline(text: str | None, *, max_chars: int = 300) -> str:
    """Escape text destined for an inline Markdown context (headings, list items).

    Newlines are collapsed: a feed title containing one would otherwise break
    heading structure and list items.
    """
    if not text:
        return ""
    flat = " ".join(strip_control_chars(text).split())
    escaped = _MD_INLINE_SPECIALS.sub(r"\\\1", flat)
    if len(escaped) > max_chars:
        escaped = escaped[:max_chars].rstrip() + "…"
    return escaped


def sanitize_md_block(text: str | None, *, max_chars: int = 8000) -> str:
    """Clean a block of untrusted text for embedding in a note.

    Feed descriptions and model prose. Headings are demoted so nothing can
    restructure the note around them, and frontmatter delimiters are removed so a
    stray `---` cannot terminate the note's own YAML.
    """
    if not text:
        return ""
    # Tags out, entities decoded, then tags again in case decoding revealed one.
    # Newlines survive, unlike src.topics.strip_html -- see the module docstring.
    text = unescape(_TAG.sub(" ", text))
    text = _TAG.sub(" ", text)
    text = strip_control_chars(text)
    text = _FRONTMATTER_LINE.sub("", text)
    # Demote any heading to h4, under the note's own h1/h2.
    text = re.sub(r"^(\s{0,3})#{1,6}\s+", r"\1#### ", text, flags=re.MULTILINE)
    text = _WHITESPACE_RUNS.sub(" ", text)
    text = _BLANK_LINE_RUNS.sub("\n\n", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


def as_quote(text: str) -> str:
    """Prefix every line with '> ', for embedding a block inside a callout."""
    return "\n".join(("> " + line).rstrip() for line in text.split("\n"))


# --- Entity canonicalisation (ported from podcast_agent/entities.py) ---------

#: Entity strings longer than this are almost always a sentence fragment the
#: model returned by mistake; they poison the index by never matching anything.
MAX_ENTITY_CHARS = 80

_CVE = re.compile(r"^cve[\s\-_]*(\d{4})[\s\-_]*(\d{4,7})$", re.IGNORECASE)

#: Leading noise words that change nothing about which thing is meant.
_LEADING = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)

#: Trailing corporate suffixes, so "Mandiant" and "Mandiant Inc." agree.
_TRAILING = re.compile(r"[\s,]+(inc|inc\.|llc|ltd|ltd\.|corp|corp\.|gmbh|plc)$", re.IGNORECASE)


def canonical(name: str) -> str:
    """The key two spellings of the same thing must share.

    Deliberately conservative. Over-merging is the worse error: it silently fuses
    two unrelated entities into one timeline that reads as evidence, and nothing
    downstream can tell. Under-merging leaves two rows a reader can see and
    interpret for themselves.

    So this normalises only what is unambiguous -- case, whitespace, punctuation
    noise, an article, a corporate suffix -- plus CVE identifiers, which have a
    canonical form and are written every possible way.
    """
    text = " ".join(str(name).split()).strip(" .,;:—-")
    if not text:
        return ""
    if match := _CVE.match(text):
        return f"cve-{match.group(1)}-{int(match.group(2)):04d}"
    text = _LEADING.sub("", text)
    text = _TRAILING.sub("", text)
    return text.casefold().strip(" .,;:—-")


def display_name(surfaces: dict[str, int]) -> str:
    """The spelling to show: the most common, ties broken by the longest.

    Length as the tiebreak because the longer form is usually the more
    informative one -- "Volt Typhoon" over "Volt", "CVE-2026-1234" over
    "2026-1234".
    """
    return max(surfaces.items(), key=lambda item: (item[1], len(item[0])))[0]


def wikilink(target: str, label: str, *, max_chars: int = 200) -> str:
    """``[[target|label]]``, with a label that cannot escape the brackets.

    The label is stripped of `[`, `]`, `|`, `#` and `^` rather than escaped,
    because inside a wikilink Obsidian treats a backslash as a literal backslash
    and closes the link at the first `]]` anyway -- so `md_escape_inline` alone
    is not a defence here, only outside the brackets.

    An empty target (a topic below the note threshold, a story with no note)
    returns the label as ordinary escaped text, so the note never gains a link
    that resolves to nothing.
    """
    safe_label = " ".join(strip_control_chars(label or "").split()).translate(_UNLINKABLE).strip()
    if len(safe_label) > max_chars:
        safe_label = safe_label[:max_chars].rstrip() + "…"
    if not target:
        return md_escape_inline(label, max_chars=max_chars)
    safe_target = str(target).translate(_UNLINKABLE).strip()
    if not safe_target:
        return md_escape_inline(label, max_chars=max_chars)
    return f"[[{safe_target}|{safe_label}]]" if safe_label else f"[[{safe_target}]]"
