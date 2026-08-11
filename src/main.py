"""Run the full security digest pipeline: fetch → summarise → digest → deliver."""

import logging
import sys
from collections import Counter
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from src.fetcher import fetch_all, load_config
from src.dedupe import dedupe_content, filter_new, mark_seen
from src.summariser import prompt_vocabulary_drift, summarise_all
from src.digest import build_digest
from src.delivery import deliver, deliver_previous
from src.history import record_sent
from src.routing import EXCLUDE, accepts_domain, accepts_feed
from src.status import update as update_status
from src.utils import PROJECT_ROOT, slug

log = logging.getLogger(__name__)


def _item_links(item: dict) -> set[str]:
    """Every source link an item stands for -- its own, plus each member's when
    clustering merged several reports of one story into it."""
    links = {item.get("link", "")}
    links.update(l.get("link", "") for l in item.get("links") or [])
    return {l for l in links if l}


def _deliver_previous(config: dict, digest_filter: list[str] | None) -> int:
    """Deliver previously saved digests when there are no new items. Returns count delivered."""
    digests = config.get("digests") or []
    if digest_filter:
        digests = [d for d in digests if slug(d.get("title", "")) in digest_filter]
    delivered = 0
    for d in digests:
        title = d.get("title", "Digest")
        path = PROJECT_ROOT / "output" / "web" / f"{slug(title)}.html"
        if path.exists():
            log.info("Delivering previous digest: %s", title)
            deliver_previous(path, config, title, digest_cfg=d)
            delivered += 1
    return delivered


def run(config_path: Path | None = None, digest_filter: list[str] | None = None) -> dict:
    """Run the full pipeline."""
    config_path = config_path or PROJECT_ROOT / "config.yaml"

    config = load_config(config_path)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # Prompts are editable in the admin panel while the vocabulary they describe
    # is enforced from config.yaml. Checked here rather than at load time so it
    # costs two file reads per run, not two per HTTP request.
    for message in prompt_vocabulary_drift(config):
        log.warning("%s", message)

    try:
        update_status("running")
        log.info("Fetching security news...")
        items = fetch_all(config)
        if not items:
            log.warning("No items fetched")
            update_status("success", items=0)
            return {"success": True, "items": 0}

        sources = config.get("sources", {})
        if sources.get("story_dedupe") and config.get("llm", {}).get("cluster"):
            log.warning(
                "Both sources.story_dedupe and llm.cluster are enabled. The lexical "
                "pass runs first and discards duplicates outright, so clustering "
                "never sees them and their links are lost from the digest. Turn "
                "story_dedupe off when clustering."
            )
        if sources.get("content_dedupe", False):
            threshold = float(sources.get("content_similarity_threshold", 0.85))
            items = dedupe_content(
                items,
                similarity_threshold=threshold,
                story_dedupe=bool(sources.get("story_dedupe", False)),
                story_containment_threshold=float(
                    sources.get("story_containment_threshold", 0.6)
                ),
            )

        retention = sources.get("seen_retention_days", 14)
        items = filter_new(items, retention_days=retention)
        log.info("%d new items (after deduplication)", len(items))

        if not items:
            log.info("No new items — delivering previous digest(s)")
            delivered = _deliver_previous(config, digest_filter)
            update_status("success", items=0, previous_delivered=(delivered > 0))
            return {"success": True, "items": 0, "previous_delivered": delivered}

        log.info("Summarising %d items...", len(items))
        summarised = summarise_all(items, config)
        excluded = sum(1 for i in summarised if i.get("category") == "exclude")
        if excluded:
            log.info("Excluded %d items (categorised 'exclude' by the summariser)", excluded)

        digests = config.get("digests")
        if not digests:
            digests = [config.get("digest", {"title": "Security Digest", "sections": ["news", "thought_leadership", "other"]})]
        if digest_filter:
            digests = [d for d in digests if slug(d.get("title", "")) in digest_filter]
            if not digests:
                update_status("success", items=0)
                return {"success": True, "items": 0, "message": "No matching digests"}

        total_delivered = 0
        routed: set[int] = set()
        for d in digests:
            sections = set(d.get("sections", []))
            filtered = []
            for i in summarised:
                if (
                    i.get("category") in sections
                    and accepts_feed(d, i.get("source", ""))
                    and accepts_domain(d, i.get("domain"))
                ):
                    routed.add(id(i))
                    filtered.append(i)
            if not filtered:
                log.info("No items for '%s', skipping", d.get("title", "digest"))
                continue
            log.info("Building digest: %s (%d items)", d.get("title"), len(filtered))
            content = build_digest(filtered, config, d)
            log.info("Delivering...")
            deliver(content, config, title=d.get("title"), digest_cfg=d)
            record_sent(filtered, d.get("title", "Digest"), config)
            total_delivered += len(filtered)

        # Anything summarised but accepted by no digest. 'exclude' is a decision
        # the model was asked to make, so it is not a drop; everything else here
        # is an item that cost tokens and reached nobody, which used to leave no
        # trace at all. Grouped by (feed, category) because that pair is the
        # routing key -- it names the config gap directly.
        dropped = [i for i in summarised
                   if id(i) not in routed and i.get("category") != EXCLUDE]
        if dropped:
            by_pair = Counter(
                (i.get("source", "?"), i.get("domain") or "-", i.get("category", "?"))
                for i in dropped
            )
            log.warning(
                "%d item(s) matched no digest and were not delivered:", len(dropped)
            )
            for (source, domain, category), n in by_pair.most_common():
                log.warning(
                    "    %-28s %-14s %-18s %d item(s) -- no digest takes this",
                    source, domain, category, n,
                )

        # Only what was delivered (or deliberately excluded) is marked seen. An
        # item dropped by a routing gap stays unseen, so once the gap is closed
        # the backlog arrives instead of having been silently consumed. The cost
        # is that an unrouted item is re-summarised each run until it is routed
        # -- which the warning above exists to make you notice.
        #
        # Matched by link, not identity: summarise_all returns new dicts, and
        # clustering merges several fetched items into one, so the delivered
        # object is not the fetched object. A merged item carries every member's
        # link in `links`, and all of them count as delivered.
        settled: set[str] = set()
        for i in summarised:
            if id(i) in routed or i.get("category") == EXCLUDE:
                settled.update(_item_links(i))
        keep = [i for i in items if i.get("link") in settled]
        if len(keep) < len(items):
            log.info(
                "Marking %d/%d fetched items as seen; the rest stay unseen so they "
                "return once routing accepts them", len(keep), len(items),
            )
        mark_seen(keep, retention_days=retention)
        log.info("Done.")
        update_status("success", items=total_delivered)
        return {"success": True, "items": total_delivered}
    except Exception as e:
        log.exception("Pipeline failed")
        update_status("failure", error=str(e))
        return {"success": False, "error": str(e)}


if __name__ == "__main__":
    result = run()
    sys.exit(0 if result.get("success", False) else 1)
