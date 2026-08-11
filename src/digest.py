"""Build the digest: group items by category and render markdown."""

import datetime
from collections import defaultdict
from html import escape
from typing import Any

from src.utils import PROJECT_ROOT, render_template

DIGEST_PROMPT_PATH = PROJECT_ROOT / "prompts" / "digest.txt"

SAFE_LINK_SCHEMES = ("http://", "https://")


def _safe_text(s: str) -> str:
    """Escape HTML and neutralise markdown link syntax in untrusted feed/LLM text."""
    return escape(s or "").replace("[", "&#91;").replace("]", "&#93;")


def _safe_link(link: str) -> str:
    """Return link only if it's http(s); otherwise empty (rendered as plain text)."""
    link = (link or "").strip()
    return link if link.lower().startswith(SAFE_LINK_SCHEMES) else ""


def _render_sources(item: dict[str, Any], fallback: str) -> str:
    """Byline for an item: every outlet that reported the story, each linked.

    A clustered item merges several reports of one event, so crediting only the
    first would hide that the others exist -- and the reader loses the ability to
    check a second account. Falls back to the plain publisher name for items that
    never went through clustering."""
    links = item.get("links") or []
    if len(links) < 2:
        return f"*{fallback}*"

    parts = []
    for entry in links:
        publisher = _safe_text(entry.get("publisher", "") or "source")
        href = _safe_link(entry.get("link", ""))
        parts.append(f"[{publisher}]({href})" if href else publisher)
    return "*" + " · ".join(parts) + "*"


def group_by_section(
    items: list[dict[str, Any]], section_order: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Group items by category, using the configured section order."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        cat = item.get("category", "other")
        if cat not in grouped and cat not in section_order:
            grouped["other"].append(item)
        else:
            grouped[cat].append(item)

    # Return only sections that have items, in config order
    result: dict[str, list[dict[str, Any]]] = {}
    for section in section_order:
        if section in grouped and grouped[section]:
            result[section] = grouped[section]
    # Add any leftover categories
    for section, section_items in grouped.items():
        if section not in result:
            result[section] = section_items

    return result


def render_markdown(
    items: list[dict[str, Any]],
    config: dict[str, Any],
    digest_cfg: dict[str, Any] | None = None,
) -> str:
    """Render summarised items as a markdown digest."""
    digest_cfg = digest_cfg or config.get("digest", {})
    title = digest_cfg.get("title", "Security Digest")
    sections = digest_cfg.get("sections", ["news", "thought_leadership", "other"])

    today = datetime.date.today().isoformat()

    header = render_template(DIGEST_PROMPT_PATH, title=title, date=today)
    parts = [header, ""]

    grouped = group_by_section(items, sections)

    # Built-in headings for the security vocabulary; a digest with its own
    # categories supplies its own via `labels:` in config.yaml. Anything named in
    # neither still renders, title-cased, further down.
    section_labels = {
        "news": "News & Incidents",
        "thought_leadership": "Thought Leadership",
        "ai": "AI & ML Security",
        "ai_general": "Major Updates",
        "methods": "New Methods & Approaches",
        "other": "Other",
        **(digest_cfg.get("labels") or {}),
    }

    for section, section_items in grouped.items():
        label = section_labels.get(section, section.replace("_", " ").title())
        parts.append(f"## {label}\n")

        for item in section_items:
            title_str = _safe_text(item.get("title", "Untitled") or "Untitled")
            link = _safe_link(item.get("link", ""))
            summary = _safe_text(item.get("summary", ""))
            # For a topic feed the source is the tracked entity, not who wrote
            # the piece, so credit the publisher where the feed named one.
            source = _safe_text(item.get("publisher") or item.get("source", ""))

            if link:
                parts.append(f"### [{title_str}]({link})")
            else:
                parts.append(f"### {title_str}")
            parts.append(_render_sources(item, source))
            parts.append("")
            parts.append(summary)
            parts.append("")

        parts.append("")

    return "\n".join(parts).strip()


def build_digest(
    items: list[dict[str, Any]],
    config: dict[str, Any],
    digest_cfg: dict[str, Any] | None = None,
) -> str:
    """Group items by section and render the full digest as markdown."""
    return render_markdown(items, config, digest_cfg)


if __name__ == "__main__":
    import yaml

    with open(PROJECT_ROOT / "config.yaml") as f:
        config = yaml.safe_load(f)

    # Sample data for testing
    sample = [
        {"title": "Major Ransomware Campaign Hits Healthcare", "link": "https://example.com/1", "source": "Bleeping Computer", "summary": "Widespread attack affecting hospitals.", "category": "news"},
        {"title": "Tech Giant Discloses Breach", "link": "https://example.com/2", "source": "SecurityWeek", "summary": "Customer data exposed in breach.", "category": "news"},
        {"title": "The Future of Zero Trust in 2025", "link": "https://example.com/3", "source": "Dark Reading", "summary": "Analysis of emerging trends.", "category": "thought_leadership"},
    ]

    print(build_digest(sample, config))
