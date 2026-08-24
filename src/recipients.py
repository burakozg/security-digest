"""Recipients (users) and the digests derived from them.

An instance can define who reads it -- a list of {name, email} -- and let each
topic name its recipient. The digest list is then *derived* rather than written
by hand: one digest per user, containing the topics addressed to them plus every
topic addressed to "all".

Why derive instead of hand-writing digests: the two have to agree, and when they
are maintained separately they silently drift. Adding a reader means adding a
digest, copying its sections and labels, and listing the same topics again; miss
a step and someone gets nothing, with no error. Deriving makes the topic's
`recipient` field the single place that decides who sees it.

An instance that writes `digests:` in config.yaml explicitly (the security one)
defines no users, and nothing here applies to it.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from src.weekly import DAILY, DAY_NAMES, DAYS, DEFAULT_SEND_WEEKDAY, FREQUENCIES, WEEKLY

log = logging.getLogger(__name__)

# Literal recipient meaning "every user". Not a valid email, so it can't collide
# with one.
ALL = "all"

# Deliberately loose: this catches typos and pasted display names, not RFC 5322
# violations. Rejecting an address a real mail server would accept is worse than
# letting an odd one through, since the send fails visibly either way.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.match((value or "").strip()))


DEFAULT_SEND_DAY = DAY_NAMES[DEFAULT_SEND_WEEKDAY]


def _frequency(entry: dict[str, Any], who: str) -> str:
    """How often this reader is emailed. An unrecognised value falls back to
    daily rather than dropping the reader: getting mail too often is a nuisance,
    getting none is an outage nobody notices."""
    raw = str(entry.get("frequency", "") or DAILY).strip().lower()
    if raw not in FREQUENCIES:
        log.warning("Unknown frequency %r for %s, treating as %s", raw, who, DAILY)
        return DAILY
    return raw


def _send_day(entry: dict[str, Any], frequency: str, who: str) -> str | None:
    """Which weekday a weekly reader's digest goes out. None for a daily one --
    the field would be meaningless there, so it is not written at all."""
    if frequency != WEEKLY:
        return None
    raw = str(entry.get("send_day", "") or "").strip().lower()[:3]
    if not raw:
        return DEFAULT_SEND_DAY
    if raw not in DAYS:
        log.warning("Unknown send_day %r for %s, using %s", entry.get("send_day"), who,
                    DEFAULT_SEND_DAY)
        return DEFAULT_SEND_DAY
    return raw


def normalise_users(raw: list[Any] | None) -> list[dict[str, str]]:
    """Clean a users list, dropping entries that can't receive mail.

    Deduplicates on email case-insensitively -- two entries with one address
    would produce two digests to the same inbox."""
    users: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        email = str(entry.get("email", "")).strip()
        name = str(entry.get("name", "")).strip() or email
        if not valid_email(email):
            log.warning("Skipping recipient with invalid email: %r", entry)
            continue
        if email.casefold() in seen:
            log.warning("Skipping duplicate recipient email: %s", email)
            continue
        seen.add(email.casefold())
        frequency = _frequency(entry, email)
        user = {"name": name, "email": email, "frequency": frequency}
        send_day = _send_day(entry, frequency, email)
        if send_day:
            user["send_day"] = send_day
        users.append(user)
    return users


def topics_for_user(topics: list[dict[str, Any]], email: str) -> list[str]:
    """Names of the topics this user should receive.

    A topic with no recipient counts as "all": an unassigned topic being fetched
    and summarised but delivered to nobody is the more expensive mistake, and it
    is invisible until someone notices the digest is thin."""
    names: list[str] = []
    for topic in topics or []:
        if not isinstance(topic, dict):
            continue
        name = str(topic.get("name", "")).strip()
        if not name:
            continue
        recipient = str(topic.get("recipient", "") or ALL).strip()
        if recipient.casefold() in (ALL, "") or recipient.casefold() == email.casefold():
            names.append(name)
    return names


def derive_digests(
    users: list[dict[str, str]],
    topics: list[dict[str, Any]],
    template: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build one digest per user from the topics addressed to them.

    A user with no topics gets no digest at all rather than an empty one -- an
    email with nothing in it is worse than no email."""
    template = template or {}
    title_format = template.get("title_format", "{name}'s Digest")
    sections = template.get("sections") or ["key", "notable", "mention"]
    labels = template.get("labels")
    # Sections a weekly edition actually prints. Narrower than the daily list by
    # default; see weekly._sections for why it must not narrow `sections` itself.
    weekly_sections = template.get("weekly_sections")

    digests: list[dict[str, Any]] = []
    for user in users:
        sources = topics_for_user(topics, user["email"])
        if not sources:
            log.warning(
                "Recipient %s (%s) has no topics assigned -- no digest will be built for them",
                user["name"], user["email"],
            )
            continue
        digest: dict[str, Any] = {
            "title": title_format.format(name=user["name"], email=user["email"]),
            "to": user["email"],
            "sections": list(sections),
            "sources": sources,
        }
        if labels:
            digest["labels"] = dict(labels)
        # Delivery cadence is the reader's, so it travels with them onto the
        # digest derived for them.
        digest["frequency"] = user.get("frequency", DAILY)
        if digest["frequency"] == WEEKLY:
            digest["send_day"] = user.get("send_day", DEFAULT_SEND_DAY)
            if weekly_sections:
                digest["weekly_sections"] = list(weekly_sections)
        digests.append(digest)
    return digests


def warn_on_unknown_recipients(
    users: list[dict[str, str]], topics: list[dict[str, Any]]
) -> list[str]:
    """Report topics addressed to an email that is not a known recipient.

    This is the failure mode of deriving digests from a free-text field: delete a
    user, or mistype an address, and the topic is still fetched and summarised
    but reaches nobody. Warns rather than raises so the rest still delivers."""
    known = {u["email"].casefold() for u in users}
    messages: list[str] = []
    for topic in topics or []:
        if not isinstance(topic, dict):
            continue
        recipient = str(topic.get("recipient", "") or ALL).strip()
        if recipient.casefold() in (ALL, ""):
            continue
        if recipient.casefold() not in known:
            message = (
                f"Topic {topic.get('name')!r} is addressed to {recipient!r}, "
                f"which is not a known recipient -- it will be fetched but never delivered"
            )
            messages.append(message)
            log.warning("%s", message)
    return messages
