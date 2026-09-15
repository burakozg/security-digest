"""Typed validation for the merged config produced by fetcher.load_config().

This does not replace the dict-based config threaded through the rest of the
codebase (main.py, delivery.py, summariser.py, digest.py, web/app.py all still
take a plain `config: dict` and read it with `.get()`, unchanged) -- doing a
full attribute-access migration across every consumer and the test suite was
judged too invasive for a live, deployed app relative to the benefit, and the
IMPROVEMENTS.md plan this implements explicitly allows deferring that part.

What this DOES deliver: load_config() validates the fully-merged config
through these models before returning it, so a typo'd field name, wrong type
(e.g. a string where seen_retention_days expects an int), or malformed digest
entry raises a clear pydantic ValidationError at startup -- instead of the
previous behavior, where a bad value would silently fall through `.get(...,
default)` chains deep inside the pipeline and only surface as a confusing
downstream symptom (or not at all).

`extra="allow"` everywhere: config.yaml's on-disk shape is unchanged by this
task, so an unrecognised top-level or nested field must not break validation
-- only fields modeled below are type-checked.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict

log = logging.getLogger(__name__)


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="allow")


class RetryConfig(_Lenient):
    max_retries: int = 3
    initial_delay: float = 1.0
    max_delay: float = 60.0


class RssFeed(_Lenient):
    name: str
    url: str
    topic_context: str | None = None  # only set on generated topic feeds


class Topic(_Lenient):
    name: str
    queries: list[str] | str | None = None
    context: str | None = None
    lang: str | None = None
    country: str | None = None
    window: str | None = None
    engines: list[str] | None = None
    # Recipient email, or "all". Absent means "all" -- see recipients.topics_for_user.
    recipient: str | None = None


class User(_Lenient):
    """A reader of this instance. Digests are derived one per user."""

    name: str
    email: str
    # How often this reader is emailed: "daily" or "weekly". A weekly reader's
    # items are still gathered every run and held in a queue (see src/weekly.py),
    # then consolidated into one edition on send_day.
    frequency: str = "daily"
    # Weekday a weekly reader's digest goes out ("mon".."sun"). None on a daily
    # reader, where it would mean nothing.
    send_day: str | None = None


class DigestTemplate(_Lenient):
    """Shape shared by every derived digest (see src/recipients.py). Only used by
    instances that define users; ignored where digests: is written by hand."""

    title_format: str = "{name}'s Digest"
    sections: list[str] = []
    labels: dict[str, str] | None = None
    # Sections a WEEKLY edition prints, if narrower than the daily list. Applied
    # when rendering, never to `sections` itself -- that field is what main.py
    # routes on, so trimming it would leave the dropped categories unrouted,
    # unseen and re-summarised every day. See weekly._sections.
    weekly_sections: list[str] | None = None


class SourcesConfig(_Lenient):
    max_items_per_source: int = 20
    max_total_items: int = 50
    max_description_chars: int = 1000  # feed <description>/<summary> is truncated to this
    seen_store: str = "data/seen.json"  # legacy; seen storage moved to data/digest.db (task 3.4)
    seen_retention_days: int = 14
    content_dedupe: bool = False
    content_similarity_threshold: float = 0.85
    # Merge same-topic items whose distinctive words overlap, on top of the
    # character-similarity check above. Off by default because it is too blunt
    # for publisher feeds; news search needs it (see dedupe._same_story).
    story_dedupe: bool = False
    story_containment_threshold: float = 0.6
    # Round-robin the max_total_items budget across feeds instead of taking the
    # globally newest. Off by default because it changes which items survive the
    # cut; instances serving more than one reader should turn it on (see
    # fetcher._fair_trim).
    fair_trim: bool = False
    # Drop items older than this many days. Off by default (publisher feeds only
    # carry recent posts); topic instances need it because news search returns
    # results of any age. See fetcher._drop_stale.
    max_age_days: int | None = None
    # How stale a feed's newest entry may be before the admin panel's Status
    # column calls it quiet. Read at display time, so changing it re-reads what
    # is already recorded rather than needing a fresh run. See src/feed_health.py.
    quiet_after_days: int = 21
    rss: list[RssFeed] = []
    # Generated from topics.yaml at load time, not written by hand.
    topic_feeds: list[RssFeed] = []


class LLMConfig(_Lenient):
    provider: str = "openrouter"
    model: str = "qwen/qwen3.7-flash"
    temperature: float = 0.3
    batch_size: int = 8
    # OpenRouter's `reasoning` control, for hybrid models that think by default
    # (qwen3.7-flash and qwen3.8-27b among them). Off by default because it is
    # measurably wrong for this workload, not merely expensive: on 2026-08-20,
    # A/B runs over one identical feed pull showed thinking accounted for ~90%
    # of output tokens on both instances while making the digest *worse* --
    # security lost 3 vulnerability advisories and got vaguer summaries, and
    # news excluded 27 of 33 items against 2 with thinking off. Clustering was
    # unaffected. Set true per instance if a future model reverses that.
    # OpenRouter-only: no other provider on this path accepts the parameter.
    reasoning: bool = False
    # The category vocabulary this instance's prompt assigns, enforced as a JSON
    # schema enum on the response. None means the security-oriented default in
    # src/summariser.py; a topic instance overrides it with its own set.
    categories: list[str] | None = None
    # The subject areas this instance sorts items into, enforced as a second
    # schema enum. Separate from `categories` because they answer different
    # questions: the domain says which digest an item belongs to, the category
    # says which section it sits in. One field cannot do both once several
    # digests share a section list -- an item matching two digests is delivered
    # to both, as two emails.
    domains: list[str] | None = None
    fallback_domain: str | None = None
    # Ask the model to name the things each story is actually about -- CVEs,
    # vendors, threat actors, named victims. Neither a category nor a domain:
    # those are beats, and a beat is not what a topic note is about. Off by
    # default, and left off the response schema entirely when off, so an
    # instance with no vault pays nothing for it. Read by src/vault/.
    extract_entities: bool = False
    # Merge items reporting the same event into one digest entry with several
    # source links, instead of summarising each item separately. Off by default:
    # it changes the shape of the pipeline's output (N items in, fewer out).
    cluster: bool = False
    # What one clustering call is allowed to merge within: "source" (each feed
    # on its own) or "all" (every feed together). A topic instance must stay on
    # "source" -- the topic IS the routing key. A publisher instance needs "all",
    # because the duplicates worth merging are the ones that span outlets. See
    # summariser._cluster_groups and routing.warn_on_cross_feed_clustering.
    cluster_scope: str = "source"
    # Article-text budget for the grouping call. Setting it splits clustering in
    # two: group on the trimmed copy, then summarise the merged stories from the
    # full text. Needed where max_description_chars is large -- otherwise the
    # summary would be written from the trimmed copy, a real quality loss.
    cluster_chars: int | None = None
    fallback_category: str | None = None


class DigestDef(_Lenient):
    title: str = "Security Digest"
    sections: list[str] = []
    sources: list[str] | None = None
    # Subject area this digest carries. None means "any", which is what every
    # instance did before domains existed and what a single-digest instance
    # still wants.
    domain: str | None = None
    # Recipients for this digest specifically; falls back to delivery.email.to.
    # This is what lets one instance serve several readers different topic sets.
    to: str | list[str] | None = None
    # Section heading overrides, e.g. {"key": "Worth knowing"}. Anything absent
    # falls back to the built-in map and then to title-casing the section name.
    labels: dict[str, str] | None = None
    # Delivery cadence, carried over from the recipient this digest was derived
    # for. "daily" sends on every run; "weekly" queues and sends on send_day.
    frequency: str = "daily"
    send_day: str | None = None
    weekly_sections: list[str] | None = None


class ScheduleConfig(_Lenient):
    enabled: bool = False
    hour: int = 7
    minute: int = 0
    timezone: str = "UTC"


class SiteConfig(_Lenient):
    """Dashboard branding. One codebase serves every instance, so the name in
    the header has to come from config -- otherwise a topic tracker greets its
    readers as "Security Digest". Defaults preserve the original wording."""

    title: str = "Security Digest"
    tagline: str = "Curated news for security consultants"
    icon: str = "🛡"


class WebConfig(_Lenient):
    min_run_interval_minutes: int = 30


class HistoryConfig(_Lenient):
    path: str = "data/digest_history.json"  # legacy; history moved to data/digest.db (task 3.4)
    max_entries: int = 10000


class EmailConfig(_Lenient):
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    from_: str | None = None
    to: str | list[str] | None = None
    smtp_user: str | None = None

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    def __init__(self, **data: Any) -> None:
        # "from" is a Python keyword -- config.yaml uses it as a plain (legal)
        # YAML key, so accept it positionally under an aliased field name.
        if "from" in data and "from_" not in data:
            data["from_"] = data.pop("from")
        super().__init__(**data)


class DeliveryConfig(_Lenient):
    output: str = "console"
    file_path: str = "./output/digest.md"
    email: EmailConfig | None = None


class VaultConfig(_Lenient):
    """Projecting delivered digests into an Obsidian vault over LiveSync.

    Deployment topology, and therefore deliberately NOT admin-panel-overridable
    -- there is no data/vault_overrides.yaml and src/reconcile.py knows nothing
    about this block. Which database receives your notes is not a setting to
    fat-finger in a browser.

    Two LiveSync client settings this depends on, both of which silently break
    the projection if turned on: end-to-end encryption, because we write
    plaintext chunks, and path obfuscation, because entries are keyed by path.
    """

    enabled: bool = False
    # The CouchDB the *vault* replicates against, which is not this app's own
    # database. None by default: the real address is deployment detail and
    # belongs in the environment (VAULT_COUCHDB_URL), never in this git-tracked
    # file, which a deploy pushes over the target's copy.
    couchdb_url: str | None = None
    # None, not a default name: the vault database is whichever one your LiveSync
    # clients replicate against, and guessing means a 404 at best and writing
    # notes into another application's database at worst. src/vault/ refuses to
    # build a client without one. Normally supplied as VAULT_DB in .env.
    db: str | None = None
    user: str = "security_digest"
    # Where projected notes land in the vault. A root folder of its own, so the
    # folder it would otherwise sit inside keeps a single meaning.
    folder: str = "12 daily-digest"
    # Topic notes are filed separately, and this is deliberately the same folder
    # podcast-digest writes its entity notes to: one thing, one note, whichever
    # corpus noticed it. See src/vault/notes.py for the sharing contract.
    topics_folder: str = "99 topics"
    # Mentions before a thing is worth a note of its own. One mention is a detail
    # in a story, not a thread through the corpus, and a vault with four thousand
    # single-use CVE notes is a worse graph than none.
    min_mentions: int = 2
    max_mentions: int = 20000
    timeout_s: float = 30.0
    # Which digests project. Empty means every digest this instance sends.
    digests: list[str] = []


class Settings(_Lenient):
    """Top-level validated config, mirroring config.yaml's merged shape
    (base file + sources/schedule/llm overrides, as already merged by
    fetcher.load_config() before validation)."""

    retry: RetryConfig = RetryConfig()
    sources: SourcesConfig = SourcesConfig()
    llm: LLMConfig = LLMConfig()
    digest: DigestDef | None = None
    digests: list[DigestDef] = []
    topics: list[Topic] = []
    users: list[User] = []
    digest_template: DigestTemplate | None = None
    schedule: ScheduleConfig = ScheduleConfig()
    site: SiteConfig = SiteConfig()
    web: WebConfig = WebConfig()
    history: HistoryConfig = HistoryConfig()
    delivery: DeliveryConfig = DeliveryConfig()
    vault: VaultConfig = VaultConfig()


# Wiring warnings already emitted this process -- see warn_on_unwired_sources.
_warned: set[str] = set()


def warn_on_unwired_sources(config: dict[str, Any]) -> list[str]:
    """Report feeds/topics and digest source lists that don't line up.

    Routing is matched on name strings (main.py filters `source in
    digest_sources`), which fails silently in both directions: a typo in a
    digest's `sources` yields an empty digest with no error, and a topic nothing
    routes to is fetched and summarised for nobody -- real LLM spend, no output.
    Neither is malformed config, so this warns rather than raises; a run with one
    bad name should still deliver the digests that are wired correctly.

    Returns the warning messages (also logged), so tests and callers can inspect
    them."""
    sources = config.get("sources") or {}
    known = {
        str(f.get("name", "")).strip()
        for f in list(sources.get("rss") or []) + list(sources.get("topic_feeds") or [])
        if isinstance(f, dict) and f.get("name")
    }
    digests = config.get("digests") or []

    routed: set[str] = set()
    messages: list[str] = []
    for digest in digests:
        title = digest.get("title", "Digest")
        for name in digest.get("sources") or []:
            name = str(name).strip()
            routed.add(name)
            if name not in known:
                messages.append(
                    f"Digest {title!r} lists source {name!r}, which matches no feed or topic"
                )

    # Only meaningful once at least one digest restricts by source; a digest with
    # no `sources` takes everything, so nothing is unrouted.
    if any(d.get("sources") for d in digests) and all(d.get("sources") for d in digests):
        for name in sorted(known - routed):
            messages.append(f"Source {name!r} is in no digest -- it will be fetched but never delivered")

    for message in messages:
        # Config is re-read on every HTTP request, and the dashboard polls
        # /status and /api/digests every few seconds -- logging each time buried
        # a real pipeline traceback under thousands of identical lines. The
        # warning is about the config, so once per distinct message is all it
        # can usefully say; a config edit produces a new message and logs again.
        if message in _warned:
            continue
        _warned.add(message)
        log.warning("%s", message)
    return messages


def validate_config(config: dict[str, Any]) -> Settings:
    """Validate a fully-merged config dict. Raises pydantic.ValidationError
    with a clear, field-by-field message on the first genuinely malformed
    config -- see the module docstring for what this does and doesn't cover."""
    settings = Settings.model_validate(config)
    warn_on_unwired_sources(config)
    return settings
