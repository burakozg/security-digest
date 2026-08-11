"""Turn topic/entity definitions into ordinary RSS feeds.

The digest engine follows fixed publisher feeds. Tracking a *topic* -- a company,
a person, a place -- needs the opposite: ask a news search engine what it has on
that name today. Both major engines expose search as plain RSS, so a topic can be
reduced to a set of feed URLs and the rest of the pipeline (fetch, dedupe,
seen-store, routing, history) needs to know nothing about topics at all.

Two engines, because neither is sufficient alone:

- Google News has the better coverage and the only usable recency filter
  (`when:1d`), and names the real publisher in each entry's <source> element,
  but its <description> is only an <a> tag wrapping the headline -- there is no
  article text for the summariser to work from, and its article links are opaque
  redirects that don't resolve server-side.
- Bing News is staler and doesn't name the publisher, but its <description> is a
  real one-or-two-sentence snippet, and the true article URL is sitting in a
  query parameter on its redirect.

Run both and let the existing content_dedupe collapse the overlap: in practice
Google supplies the coverage and Bing supplies the text.

Both feeds are undocumented and can change without notice. fetch_feed() already
logs and returns [] for a feed that fails, so a break here degrades the digest
rather than failing the run.
"""

from __future__ import annotations

import logging
import re
from html import unescape
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Hosts that are pure redirectors -- a link still pointing at one of these has
# not been resolved to an original article, so no publisher name can be read off
# it. Syndicators like msn.com are deliberately absent: they're where the link
# actually goes, so naming them is honest, and the alternative is falling back to
# the topic name and claiming Nvidia published a story about Nvidia.
_AGGREGATOR_HOSTS = ("news.google.com", "bing.com")

# Google News appends " - Publisher" to every headline, which then renders next
# to the byline the digest already shows. Non-greedy on the separator so only the
# final segment is considered.
_TITLE_SUFFIX_RE = re.compile(r"\s+[-–—]\s+([^-–—]{2,40})$")

GOOGLE_NEWS_SEARCH = "https://news.google.com/rss/search"
BING_NEWS_SEARCH = "https://www.bing.com/news/search"

DEFAULT_LANG = "en"
DEFAULT_COUNTRY = "US"

# Google News recency filter. Wider than the daily cadence on purpose: a missed
# or delayed run would otherwise leave a permanent hole in coverage, and the
# seen-store already suppresses anything delivered before.
DEFAULT_WINDOW = "when:2d"


def _google_url(query: str, lang: str, country: str, window: str) -> str:
    """Google News search feed. `ceid` is the market pair and must agree with
    hl/gl or results silently fall back to a different edition."""
    q = f"{query} {window}".strip()
    return (
        f"{GOOGLE_NEWS_SEARCH}?q={quote_plus(q)}"
        f"&hl={lang}&gl={country}&ceid={country}:{lang}"
    )


def _bing_url(query: str, lang: str, country: str) -> str:
    """Bing News search feed. `setmkt` is not optional in practice -- without it
    Bing localises to the market it infers from the outbound IP, which for a NAS
    on a home connection is not necessarily the language the topic is written
    in. `sortbydate=1` is what little recency control this feed offers."""
    return (
        f"{BING_NEWS_SEARCH}?q={quote_plus(query)}"
        f"&format=RSS&setmkt={lang}-{country}&sortbydate=1"
    )


def expand_topics(topics: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Expand topic definitions into feed dicts shaped like sources.yaml entries.

    Every feed generated for a topic carries that topic's `name`, so items fetched
    through it land with source == topic name -- which is what digests[].sources
    routes on. One topic yielding several feeds (two engines x N queries) is
    intentional and harmless: they all funnel into the same topic.

    `context` rides along so the summariser can tell the model what this topic
    actually is, which is what lets it reject same-name-different-subject hits.
    """
    feeds: list[dict[str, Any]] = []
    for topic in topics or []:
        if not isinstance(topic, dict):
            continue
        name = str(topic.get("name", "")).strip()
        if not name:
            log.warning("Skipping topic without a name: %r", topic)
            continue

        queries = topic.get("queries") or [name]
        if isinstance(queries, str):
            queries = [queries]

        lang = str(topic.get("lang", DEFAULT_LANG)).strip() or DEFAULT_LANG
        country = str(topic.get("country", DEFAULT_COUNTRY)).strip() or DEFAULT_COUNTRY
        window = str(topic.get("window", DEFAULT_WINDOW)).strip()
        context = str(topic.get("context", "")).strip()
        engines = topic.get("engines") or ["google", "bing"]

        for query in queries:
            query = str(query).strip()
            if not query:
                continue
            if "google" in engines:
                feeds.append({
                    "name": name,
                    "url": _google_url(query, lang, country, window),
                    "topic_context": context,
                })
            if "bing" in engines:
                feeds.append({
                    "name": name,
                    "url": _bing_url(query, lang, country),
                    "topic_context": context,
                })

    return feeds


def clean_link(link: str) -> str:
    """Unwrap a news-aggregator redirect to the publisher's own URL where the
    real one is recoverable.

    Bing hands out `bing.com/news/apiclick.aspx?...&url=<real url>`, so the
    target is simply a query parameter. Google's `news.google.com/rss/articles/
    CBMi...` is an opaque token that only resolves in a browser -- it is returned
    unchanged rather than guessed at, since a wrong guess turns a working link
    into a dead one.
    """
    if not link:
        return link
    try:
        parsed = urlparse(link)
    except ValueError:
        return link

    if parsed.netloc.endswith("bing.com") and "apiclick" in parsed.path:
        target = parse_qs(parsed.query).get("url", [""])[0]
        if target.startswith(("http://", "https://")):
            return target

    return link


def publisher_from_link(link: str) -> str:
    """Best-effort publisher name from an article URL's host, e.g.
    'https://www.aftonbladet.se/...' -> 'aftonbladet.se'.

    Bing's feed, unlike Google's, has no <source> element naming the outlet. Left
    alone the item would be credited to the feed's name -- which for a topic feed
    is the tracked entity, so an Aftonbladet story about Vattenfall would appear
    to have been published *by* Vattenfall. The host is not a pretty name, but it
    is an honest one. Returns '' when the link is missing or still points at an
    aggregator, so the caller can fall back."""
    if not link:
        return ""
    try:
        host = urlparse(link).netloc.lower()
    except ValueError:
        return ""
    host = host.split(":")[0].removeprefix("www.")
    if not host or any(host == h or host.endswith("." + h) for h in _AGGREGATOR_HOSTS):
        return ""
    return host


def strip_title_suffix(title: str, publisher: str) -> str:
    """Drop Google News's trailing " - Publisher" from a headline.

    The digest renders the publisher on its own line, so left in place the outlet
    is named twice in a row. Only removed when it actually matches the publisher
    we resolved -- plenty of real headlines end in a dash clause ("... - and why
    it matters"), and cutting those would corrupt the headline."""
    if not title or not publisher:
        return title
    match = _TITLE_SUFFIX_RE.search(title)
    if not match:
        return title
    if match.group(1).strip().casefold() != publisher.strip().casefold():
        return title
    return title[: match.start()].rstrip()


def strip_html(text: str) -> str:
    """Flatten a feed description to plain text.

    Google's search feed puts an entire <a> element in <description> rather than
    any article text, so what reaches the summariser is markup wrapping a copy of
    the headline. Stripping tags does not recover the missing article text, but it
    stops the model spending its attention (and the request its tokens) on HTML.

    Entities are unescaped after tags are removed, so the &nbsp; runs these feeds
    use as separators become ordinary spaces rather than literal text."""
    if not text:
        return text
    return _WS_RE.sub(" ", unescape(_TAG_RE.sub(" ", text))).strip()
