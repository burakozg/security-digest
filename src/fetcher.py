"""Fetch security news from RSS feeds."""

import datetime
import logging
from pathlib import Path
from time import struct_time
from typing import Any

import feedparser
import httpx
import yaml

from src.recipients import derive_digests, normalise_users, warn_on_unknown_recipients
from src.retry import retry
from src.routing import apply_feed_routing
from src.settings import validate_config
from src.topics import (
    clean_link,
    expand_topics,
    publisher_from_link,
    strip_html,
    strip_title_suffix,
)
from src.utils import PROJECT_ROOT

log = logging.getLogger(__name__)

FETCH_TIMEOUT_SECONDS = 15.0
MAX_FEED_BYTES = 5 * 1024 * 1024  # 5 MB


def _parse_schedule_file(path: Path) -> dict[str, Any]:
    """Parse schedule.txt: key=value lines."""
    result: dict[str, Any] = {}
    if not _optional_file(path):
        return result
    for line in path.read_text().strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k == "enabled":
                result[k] = v.lower() in ("true", "1", "yes")
            elif k == "hour":
                result[k] = int(v) if v.isdigit() else 7
            elif k == "minute":
                result[k] = int(v) if v.isdigit() else 0
            elif k == "timezone":
                result[k] = v
    return result


def _optional_file(path: Path) -> bool:
    """Whether an optional config file is present and actually a file.

    Not just exists(): every one of these paths is a bind mount target in
    production, and Docker silently creates a *directory* when the host path
    doesn't exist yet. That happens for real -- recreate a container after adding
    a mount but before the deploy script has pushed the file, and this directory
    appears. exists() is then True and the open() below dies with
    IsADirectoryError on every single run. Treating a directory as "absent" turns
    a crash loop into the same clean skip as a genuinely missing optional file."""
    if path.is_dir():
        log.warning(
            "%s is a directory, not a file -- ignoring it. This usually means a "
            "bind mount pointed at a path that didn't exist on the host; deploy "
            "the file and recreate the container.", path,
        )
        return False
    return path.is_file()


def load_config(config_path: str | Path = "config.yaml") -> dict[str, Any]:
    """Load configuration from YAML file and merge external overrides."""
    path = Path(config_path)
    if path.is_dir():
        raise IsADirectoryError(
            f"Config path is a directory, not a file: {path}. A bind mount "
            f"probably pointed at a host path that doesn't exist yet."
        )
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with open(path) as f:
        config = yaml.safe_load(f) or {}

    base = path.parent

    # Load sources from external file if specified
    sources_file = config.get("sources_file")
    if sources_file:
        src_path = base / sources_file
        if _optional_file(src_path):
            with open(src_path) as f:
                sources_data = yaml.safe_load(f) or {}
            if "rss" in sources_data:
                if "sources" not in config:
                    config["sources"] = {}
                config["sources"]["rss"] = sources_data["rss"]

    # Writable RSS list (admin panel when sources.yaml is read-only)
    sources_overrides_file = config.get("sources_overrides_file", "data/sources_overrides.yaml")
    if sources_overrides_file:
        ov_path = base / sources_overrides_file
        if _optional_file(ov_path):
            with open(ov_path) as f:
                ov = yaml.safe_load(f) or {}
            if isinstance(ov, dict) and isinstance(ov.get("rss"), list):
                if "sources" not in config:
                    config["sources"] = {}
                config["sources"]["rss"] = ov["rss"]

    # Topics: the live list is data/topics.yaml, which the admin panel writes in
    # place (data/ is mounted read-write). The instance's own topics.yaml is only
    # a SEED -- it populates a brand-new instance and is then inert. That keeps
    # the carefully-written `context` prose in git for a fresh deployment without
    # ever shadowing what the panel wrote: one live file, one writer, and no
    # merge-on-deploy deciding which copy wins.
    topics: list[Any] | None = None
    for key, default in (("topics_file", "data/topics.yaml"), ("topics_seed_file", "topics.yaml")):
        candidate = config.get(key, default)
        if not candidate:
            continue
        topics_path = base / candidate
        if _optional_file(topics_path):
            with open(topics_path) as f:
                topics_data = yaml.safe_load(f) or {}
            if isinstance(topics_data, dict) and isinstance(topics_data.get("topics"), list):
                topics = topics_data["topics"]
                break

    if topics:
        config.setdefault("sources", {})["topic_feeds"] = expand_topics(topics)
        config["topics"] = topics

    # Recipients, and the digests derived from them. An instance that lists
    # users lets each topic name who receives it, and the digest list is built
    # from that rather than written out by hand -- see src/recipients.py for why.
    # An instance with no users (the security one) keeps its explicit digests:.
    # Recipients live in the writable data/ directory, not alongside the
    # git-tracked config: they are subscriber state, not configuration. That
    # means the admin panel writes the file directly (data/ is mounted
    # read-write everywhere, like prompts/), so there is no read-only base file,
    # no override file shadowing it, and nothing for a deploy to reconcile or
    # clobber. One file, one writer, one place to look.
    users_file = config.get("users_file", "data/users.yaml")
    users_raw: list[Any] | None = None
    if users_file:
        users_path = base / users_file
        if _optional_file(users_path):
            with open(users_path) as f:
                users_data = yaml.safe_load(f) or {}
            if isinstance(users_data, dict) and isinstance(users_data.get("users"), list):
                users_raw = users_data["users"]

    users = normalise_users(users_raw) if users_raw else []
    config["users"] = users

    # Whether this instance's digests are DERIVED from recipients or DECLARED in
    # its config file. Declared wins, always: deriving over a declared list means
    # one stray "Add recipient" click replaces every digest an instance sends
    # with whatever the topics happen to produce -- which, on a feed-driven
    # instance with no topics, is nothing at all. That is a silent, total outage
    # behind an inviting button, so the two modes are kept strictly separate.
    declares_digests = bool(config.get("digests"))
    config["digests_are_derived"] = not declares_digests

    if declares_digests:
        if users:
            log.warning(
                "%d recipient(s) are configured but this instance declares its own "
                "digests, so they are ignored. Recipients only drive delivery on an "
                "instance that omits digests: and uses digest_template instead.",
                len(users),
            )
    elif users:
        warn_on_unknown_recipients(users, topics or [])
        config["digests"] = derive_digests(users, topics or [], config.get("digest_template"))

    # Feeds name the digests they feed; each digest's `sources` is derived from
    # that rather than written out a second time in config.yaml. Runs after the
    # declared-vs-derived decision above so it can see the final digest list.
    for message in apply_feed_routing(config):
        log.warning("%s", message)

    # Load schedule from external file if specified
    schedule_file = config.get("schedule_file")
    if schedule_file:
        sched_path = base / schedule_file
        schedule_data = _parse_schedule_file(sched_path)
        if schedule_data:
            config["schedule"] = {**config.get("schedule", {}), **schedule_data}

    # Load LLM overrides from writable file (for admin updates on read-only config.yaml)
    llm_overrides_file = config.get("llm_overrides_file", "data/llm_overrides.yaml")
    if llm_overrides_file:
        llm_path = base / llm_overrides_file
        if _optional_file(llm_path):
            with open(llm_path) as f:
                llm_data = yaml.safe_load(f) or {}
            if isinstance(llm_data, dict):
                llm_cfg = llm_data.get("llm", llm_data)
                if isinstance(llm_cfg, dict) and llm_cfg:
                    config["llm"] = {**config.get("llm", {}), **llm_cfg}

    # Load delivery overrides from writable file (real from/to addresses live
    # here instead of the git-tracked config, same reasoning as llm_overrides
    # above: config.yaml ships with placeholder addresses for the public repo).
    delivery_overrides_file = config.get("delivery_overrides_file", "data/delivery_overrides.yaml")
    if delivery_overrides_file:
        delivery_path = base / delivery_overrides_file
        if _optional_file(delivery_path):
            with open(delivery_path) as f:
                delivery_data = yaml.safe_load(f) or {}
            email_cfg = delivery_data.get("email") if isinstance(delivery_data, dict) else None
            if isinstance(email_cfg, dict) and email_cfg:
                config.setdefault("delivery", {}).setdefault("email", {})
                config["delivery"]["email"] = {**config["delivery"]["email"], **email_cfg}

    # Validate the fully-merged config once, here, at load time -- a typo'd
    # field name or wrong type now raises a clear error immediately instead of
    # silently falling through a `.get(..., default)` chain deep in the
    # pipeline. See src/settings.py for what is and isn't covered.
    validate_config(config)

    return config


def _fetch_feed_bytes(url: str) -> bytes:
    """Fetch feed content with a timeout and size cap. Raises on network/HTTP failure."""
    with httpx.stream(
        "GET",
        url,
        headers={"User-Agent": "SecurityDigest/1.0"},
        timeout=FETCH_TIMEOUT_SECONDS,
        follow_redirects=True,
    ) as resp:
        resp.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_bytes():
            total += len(chunk)
            if total > MAX_FEED_BYTES:
                raise ValueError(f"Feed exceeds {MAX_FEED_BYTES} byte limit: {url}")
            chunks.append(chunk)
        return b"".join(chunks)


def _parse_feed(url: str) -> Any:
    """Fetch and parse RSS feed (called with retry). Raises on network/HTTP failure
    so transient errors are actually retried."""
    content = _fetch_feed_bytes(url)
    return feedparser.parse(content)


def _entry_publisher(entry: Any, link: str, fallback: str, search_feed: bool) -> str:
    """Who actually published an entry.

    For an ordinary publisher feed the feed name is the answer. For a news-search
    feed it is not -- the feed name there is the topic being tracked, so an
    Aftonbladet story about Vattenfall would otherwise be credited to Vattenfall.
    Google names the outlet in <source>; Bing doesn't, so fall back to the host of
    the (already unwrapped) article URL before giving up on the feed name."""
    source = getattr(entry, "source", None)
    title = ""
    if isinstance(source, dict):
        title = str(source.get("title", "")).strip()
    elif source is not None:
        title = str(getattr(source, "title", "") or "").strip()
    if title:
        return title
    if search_feed:
        return publisher_from_link(link) or fallback
    return fallback


def fetch_feed(
    url: str,
    name: str,
    limit: int = 20,
    config: dict[str, Any] | None = None,
    max_description_chars: int = 1000,
    extra: dict[str, Any] | None = None,
    search_feed: bool = False,
) -> list[dict[str, Any]]:
    """Fetch and parse a single RSS feed. Retries on transient failures.

    max_description_chars bounds how much of the feed's own <description>/
    <summary> field is kept. Some publishers (Medium among them) embed the
    full article text in the feed itself for some posts and only a short
    teaser for others -- the field length varies per entry, not just per
    feed, so this is a cap, not a guarantee of getting the full text.

    extra is merged into every item produced -- used to carry a topic feed's
    context through to the summariser.

    search_feed marks a news-search feed (src/topics.py) rather than a publisher
    feed, which changes how the publisher is resolved and strips the markup those
    feeds put in <description>."""
    retry_cfg = (config or {}).get("retry", {})
    max_retries = retry_cfg.get("max_retries", 3)
    initial_delay = retry_cfg.get("initial_delay", 1.0)
    max_delay = retry_cfg.get("max_delay", 60.0)

    try:
        parsed = retry(
            _parse_feed,
            url,
            max_retries=max_retries,
            initial_delay=initial_delay,
            max_delay=max_delay,
        )
    except Exception as e:
        log.warning("Failed to fetch %s (%s) after retries: %s", name, url, e)
        return []

    if parsed.bozo and not parsed.entries:
        log.warning(
            "Feed %s (%s) parsed with errors and returned no entries: %s",
            name, url, getattr(parsed, "bozo_exception", "unknown parse error"),
        )

    items = []
    for entry in parsed.entries[:limit]:
        # Handle different date formats across feeds
        published = getattr(entry, "published_parsed", None) or getattr(
            entry, "updated_parsed", None
        )
        published_iso = ""
        if published and isinstance(published, struct_time):
            dt = datetime.datetime(*published[:6])
            published_iso = dt.isoformat()

        link = clean_link(entry.get("link", ""))
        description = entry.get("summary", entry.get("description", ""))
        if search_feed:
            description = strip_html(description)

        publisher = _entry_publisher(entry, link, name, search_feed)
        title = entry.get("title", "").strip()
        if search_feed:
            title = strip_title_suffix(title, publisher)

        items.append({
            **(extra or {}),
            "title": title,
            "link": link,
            "description": description[:max_description_chars],
            "published": published_iso,
            "source": name,
            "publisher": publisher,
        })

    return items


def _drop_stale(
    items: list[dict[str, Any]], max_age_days: int, name: str
) -> list[dict[str, Any]]:
    """Drop items published more than max_age_days ago.

    Publisher feeds only carry recent posts, so this never mattered before. News
    search does not work that way: Bing in particular happily returns years-old
    articles for a topic query, and Google's `when:` filter only constrains one of
    the two engines. Without this the first run on a new topic delivers a digest
    of old news.

    Items with no published date are kept -- an undated item is more likely a feed
    that omits the field than one that is genuinely ancient, and the seen-store
    stops it being delivered twice."""
    cutoff = datetime.datetime.now() - datetime.timedelta(days=max_age_days)
    kept = []
    for item in items:
        published = item.get("published")
        if not published:
            kept.append(item)
            continue
        try:
            if datetime.datetime.fromisoformat(published) >= cutoff:
                kept.append(item)
        except ValueError:
            kept.append(item)

    dropped = len(items) - len(kept)
    if dropped:
        log.info("%s: dropped %d item(s) older than %d days", name, dropped, max_age_days)
    return kept


def _fair_trim(
    per_feed: list[list[dict[str, Any]]], max_total: int, fetch_time: str
) -> list[dict[str, Any]]:
    """Take items round-robin across feeds -- one from each in turn, newest
    first within each -- until max_total is reached.

    A plain global sort-and-truncate is fine when every item ends up in the same
    digest, but not when feeds belong to different readers: on a day when one
    topic is heavily covered, its items are all newer than everyone else's and
    fill the entire budget, so another reader's digest arrives empty with
    nothing in the logs to explain why. Round-robin guarantees every feed is
    represented before any feed gets a second item."""
    per_feed = [
        sorted(items, key=lambda x: x["published"] or fetch_time, reverse=True)
        for items in per_feed
    ]
    result: list[dict[str, Any]] = []
    for row in zip(*_pad(per_feed)):
        for item in row:
            if item is None:
                continue
            if len(result) >= max_total:
                return result
            result.append(item)
    return result


def _pad(per_feed: list[list[dict[str, Any]]]) -> list[list[Any]]:
    """Pad each feed's list to equal length with None so zip() doesn't stop at
    the shortest -- a feed with one item must not truncate the round-robin."""
    if not per_feed:
        return []
    longest = max(len(items) for items in per_feed)
    return [items + [None] * (longest - len(items)) for items in per_feed]


def fetch_all(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Fetch from all configured RSS sources and topic feeds, and return
    combined items."""
    if config is None:
        config = load_config()

    sources = config.get("sources", {})
    # Topic feeds are generated from topics.yaml (src/topics.py) and kept apart
    # from the hand-maintained rss list, but from here on they're just feeds.
    topic_feeds = list(sources.get("topic_feeds", []))
    topic_urls = {f.get("url") for f in topic_feeds}
    rss_feeds = list(sources.get("rss", [])) + topic_feeds
    max_per_source = sources.get("max_items_per_source", 20)
    max_total = sources.get("max_total_items", 50)
    max_description_chars = sources.get("max_description_chars", 1000)
    fair_trim = bool(sources.get("fair_trim", False))
    max_age_days = sources.get("max_age_days")

    fetch_time = datetime.datetime.now().isoformat()

    per_feed: list[list[dict[str, Any]]] = []
    for feed in rss_feeds:
        name = feed.get("name", feed.get("url", "unknown"))
        url = feed.get("url")
        if not url:
            log.warning("Skipping feed without URL: %s", name)
            continue

        context = feed.get("topic_context")
        items = fetch_feed(
            url, name, limit=max_per_source, config=config,
            max_description_chars=max_description_chars,
            extra={"topic_context": context} if context else None,
            search_feed=url in topic_urls,
        )
        if max_age_days:
            items = _drop_stale(items, int(max_age_days), name)
        per_feed.append(items)

    total = sum(len(items) for items in per_feed)

    if fair_trim:
        result = _fair_trim(per_feed, max_total, fetch_time)
    else:
        # Sort by published date (newest first). Items without a published date
        # fall back to fetch_time rather than sorting last on an empty string --
        # otherwise undated items are always the first ones cut by
        # max_total_items below, every single run, regardless of how genuinely
        # recent they are.
        all_items = [item for items in per_feed for item in items]
        all_items.sort(key=lambda x: x["published"] or fetch_time, reverse=True)
        result = all_items[:max_total]

    if total > max_total:
        log.info(
            "Trimmed %d fetched items to %d (max_total_items, fair_trim=%s)",
            total, len(result), fair_trim,
        )
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Which instance this runs against comes from DIGEST_ROOT (see src/utils.py),
    # e.g. DIGEST_ROOT=instances/news python -m src.fetcher
    items = fetch_all(load_config(PROJECT_ROOT / "config.yaml"))
    print(f"Fetched {len(items)} items\n")
    for i, item in enumerate(items[:5], 1):
        print(f"{i}. [{item['source']}] {item['title']}")
        print(f"   {item['link']}\n")
