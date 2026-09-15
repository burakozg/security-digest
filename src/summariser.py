"""Summarise security news items using an LLM."""

import json
import re
import os

# Load .env for OPENAI_API_KEY (no-op if python-dotenv not installed)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
import logging
from typing import Any

from openai import AuthenticationError as OpenAIAuthError
from openai import BadRequestError as OpenAIBadRequestError
from openai import OpenAI

from src.retry import retry
from src.usage import extract_usage
from src.usage import record as record_usage
from src.utils import PROJECT_ROOT, render_template
from src.vault.text import MAX_ENTITY_CHARS, canonical

log = logging.getLogger(__name__)

PROMPT_PATH = PROJECT_ROOT / "prompts" / "summarise.txt"
BATCH_PROMPT_PATH = PROJECT_ROOT / "prompts" / "summarise_batch.txt"
CLUSTER_PROMPT_PATH = PROJECT_ROOT / "prompts" / "cluster.txt"

# Category vocabulary for the security instance. An instance whose prompt assigns
# a different set (a topic tracker grading relevance, say) overrides it with
# llm.categories in config.yaml -- the enum is enforced as a JSON schema on the
# response, so it has to match whatever prompts/summarise*.txt actually asks for.
VALID_CATEGORIES = {"news", "thought_leadership", "ai", "ai_general", "other", "exclude"}

# OpenAI-compatible endpoints, shared with src/llm_models.py, which points the
# same SDK at them for live model-id validation.
MISTRAL_BASE_URL = "https://api.mistral.ai/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_DELIM_OPEN = "<item_description>"
_DELIM_CLOSE = "</item_description>"


def _strip_delimiters(text: str) -> str:
    """Remove literal delimiter strings from untrusted feed text so an item can't
    forge a fake boundary and break out of its wrapped section in the prompt."""
    return (text or "").replace(_DELIM_OPEN, "").replace(_DELIM_CLOSE, "")


def categories(config: dict[str, Any]) -> list[str]:
    """The category vocabulary for this instance, sorted. llm.categories in
    config.yaml if set, else the security-oriented default."""
    configured = (config.get("llm") or {}).get("categories")
    if configured:
        return sorted({str(c).strip() for c in configured if str(c).strip()})
    return sorted(VALID_CATEGORIES)


def fallback_category(config: dict[str, Any]) -> str:
    """Category assigned when a response can't be parsed at all, so the item is
    still delivered rather than silently lost.

    "other" where the vocabulary has it. An instance with a different vocabulary
    should set llm.fallback_category explicitly: the default below picks the
    last category alphabetically excluding "exclude", which is deterministic but
    arbitrary, and picking wrong here means unparseable items land in a section
    no digest lists -- and vanish."""
    llm = config.get("llm") or {}
    configured = llm.get("fallback_category")
    if configured:
        return str(configured)
    allowed = categories(config)
    if "other" in allowed:
        return "other"
    usable = [c for c in allowed if c != "exclude"]
    return usable[-1] if usable else allowed[-1]


def domains(config: dict[str, Any]) -> list[str]:
    """The subject areas this instance sorts items into, sorted. Empty when the
    instance doesn't use domains, in which case the field is left off the schema
    entirely and routing works exactly as it did before."""
    configured = (config.get("llm") or {}).get("domains")
    if not configured:
        return []
    return sorted({str(d).strip() for d in configured if str(d).strip()})


def fallback_domain(config: dict[str, Any]) -> str | None:
    """Domain assigned when a response can't be parsed. Explicit config wins;
    otherwise the first alphabetically, which is arbitrary but deterministic --
    an instance that cares should say which one it wants, because an item given
    a domain no digest carries is an item nobody reads."""
    allowed = domains(config)
    if not allowed:
        return None
    configured = (config.get("llm") or {}).get("fallback_domain")
    if configured and str(configured) in allowed:
        return str(configured)
    return allowed[0]


#: Cap on entities kept per story. A model asked for "the things this is about"
#: will occasionally return twenty, most of them incidental; a story that is
#: genuinely about eight named things is already unusual.
MAX_ENTITIES_PER_ITEM = 8


def extract_entities(config: dict[str, Any]) -> bool:
    """Whether to ask the model for the named things each story is about.

    Off by default, and the field is left off the schema entirely when it is --
    an instance with no vault to project into should not pay output tokens for a
    list nothing reads."""
    return bool((config.get("llm") or {}).get("extract_entities"))


def _coerce_entities(value: Any, config: dict[str, Any]) -> dict[str, list[str]]:
    """{"entities": [...]} for merging into a result, or {} where this instance
    doesn't extract them.

    Same defensive reasoning as _coerce_category: the schema is not always
    enforced (see _unwrap_list), so this may be handed a string, a list of dicts,
    or a sentence. Anything unusable becomes an empty list rather than an
    exception, because entities are a bonus on top of the digest and must never
    cost an item its delivery."""
    if not extract_entities(config):
        return {}
    if not isinstance(value, list):
        return {"entities": []}
    kept: list[str] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, str):
            continue
        surface = " ".join(entry.split()).strip(" .,;:")
        if not surface or len(surface) > MAX_ENTITY_CHARS:
            continue
        key = canonical(surface)
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append(surface)
        if len(kept) >= MAX_ENTITIES_PER_ITEM:
            break
    return {"entities": kept}


def prompt_vocabulary_drift(config: dict[str, Any]) -> list[str]:
    """Configured category/domain values that no summarise prompt mentions.

    The vocabulary lives in two places that must agree: config.yaml sets the
    JSON-schema enums the API enforces, and the prompt files are where the
    values are actually explained to the model. Nothing links them. Rename a
    category in config and the prompt still teaches the old name; add one to the
    prompt and the schema rejects it.

    Neither failure looks like a failure. The model returns something outside
    the enum, _coerce_category quietly substitutes the fallback, and every item
    lands in one section with no error anywhere. This is cheap to check and the
    only thing standing between an edit in the admin panel and a digest that has
    silently collapsed into "Other"."""
    messages: list[str] = []
    for path, label in ((PROMPT_PATH, "summarise.txt"), (BATCH_PROMPT_PATH, "summarise_batch.txt")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue  # a missing prompt is a louder failure elsewhere
        for field, values in (("category", categories(config)), ("domain", domains(config))):
            # A definition line ("- news: something that happened"), not a bare
            # substring: values like "methods" and "other" occur in ordinary
            # prose throughout these prompts, so a substring test passes for a
            # value the prompt never actually defines -- exactly the case worth
            # catching.
            missing = [
                v for v in values
                if not re.search(rf"^\s*-\s*{re.escape(v)}\s*:", text, re.MULTILINE)
            ]
            if missing:
                messages.append(
                    f"{label} never mentions {field} value(s) {', '.join(sorted(missing))} -- "
                    f"the API will reject anything else, so those values can never be assigned"
                )
        # Same class of silent misconfiguration, one level up: the schema will
        # happily accept an empty list, so a prompt that never asks for entities
        # produces a vault whose topic notes are simply never created, with no
        # error anywhere to say why.
        if extract_entities(config) and not re.search(r"\bentit(y|ies)\b", text, re.IGNORECASE):
            messages.append(
                f"{label} never mentions entities, but llm.extract_entities is on -- "
                f"the schema accepts an empty list, so no topic notes would ever be written"
            )
    return messages


# How items are grouped before the clustering call. "source" clusters each feed
# on its own; "all" clusters every feed together. See _cluster_groups.
CLUSTER_SCOPE_SOURCE = "source"
CLUSTER_SCOPE_ALL = "all"


def cluster_scope(config: dict[str, Any]) -> str:
    scope = str(config.get("llm", {}).get("cluster_scope", CLUSTER_SCOPE_SOURCE)).strip().lower()
    if scope not in (CLUSTER_SCOPE_SOURCE, CLUSTER_SCOPE_ALL):
        log.warning("Unknown llm.cluster_scope %r, clustering per source", scope)
        return CLUSTER_SCOPE_SOURCE
    return scope


def cluster_chars(config: dict[str, Any]) -> int | None:
    """Description budget for the grouping call, or None for a single pass.

    Setting it splits clustering in two: the grouping call sees a trimmed copy
    of each item and decides only which of them are the same story, then the
    merged stories are summarised from the FULL text. That is not an
    optimisation -- summarising from the trimmed copy would be a quality
    regression on an instance whose max_description_chars is deliberately
    large, so trimming the input and re-summarising go together."""
    raw = config.get("llm", {}).get("cluster_chars")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        log.warning("llm.cluster_chars %r is not a number, clustering in one pass", raw)
        return None
    return value if value > 0 else None


def _result_schema(
    *, array: bool, allowed: list[str], allowed_domains: list[str] | None = None,
    with_entities: bool = False,
) -> dict[str, Any]:
    """JSON schema for a single {summary, category[, domain]} result, or
    (array=True) an object wrapping an array of them for the batch call.
    Top-level type must be "object" for both providers' structured-output
    support, hence the wrapper rather than a bare top-level array.

    `domain` is only present when the instance defines domains -- the category
    says which section an item sits in, the domain says which digest it belongs
    to, and an instance with one digest per category needs only the first."""
    properties: dict[str, Any] = {
        "summary": {"type": "string"},
        "category": {"type": "string", "enum": allowed},
    }
    required = ["summary", "category"]
    if allowed_domains:
        properties["domain"] = {"type": "string", "enum": allowed_domains}
        required.append("domain")
    if with_entities:
        # No enum: this vocabulary is open by nature -- the whole value of it is
        # naming a CVE or a vendor nobody listed in advance.
        properties["entities"] = {"type": "array", "items": {"type": "string"}}
        required.append("entities")

    item_schema = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    if not array:
        return item_schema
    return {
        "type": "object",
        "properties": {"items": {"type": "array", "items": item_schema}},
        "required": ["items"],
        "additionalProperties": False,
    }


def _get_client(config: dict[str, Any]) -> Any:
    """Create API client for Mistral, OpenRouter or Ollama -- all OpenAI-compatible,
    reached through the OpenAI SDK with a different base_url. OpenAI's own hosted
    API and Anthropic are not usable providers here (open-weight models only), so
    an unrecognised provider raises rather than silently falling back to either."""
    llm = config.get("llm", {})
    provider = llm.get("provider", "openrouter")

    if provider == "mistral":
        # api_key must be passed explicitly -- a bare OpenAI() would silently pick
        # up OPENAI_API_KEY and send it to Mistral.
        api_key = os.environ.get("MISTRAL_API_KEY")
        if not api_key:
            raise RuntimeError("Set MISTRAL_API_KEY in .env to use the mistral provider")
        return OpenAI(base_url=MISTRAL_BASE_URL, api_key=api_key)
    if provider == "openrouter":
        # Same care as Mistral: pass the key explicitly. A bare OpenAI() would
        # silently pick up OPENAI_API_KEY and send it to a third party.
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("Set OPENROUTER_API_KEY in .env to use the openrouter provider")
        return OpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key)
    if provider == "ollama":
        return OpenAI(
            base_url="http://localhost:11434/v1",
            api_key="ollama",
        )
    raise RuntimeError(
        f"Unsupported llm.provider {provider!r}; use mistral, openrouter or ollama"
    )


# Some OpenAI-compatible endpoints refuse `response_format` unless the word
# "json" also appears in the messages -- Alibaba's Qwen endpoints (reached here
# via OpenRouter) return a 400 saying exactly that. The schema alone used to
# carry this requirement, so no prompt file mentions JSON anywhere, and every
# call to such a provider failed: on 2026-08-05 both instances fetched, then
# summarised nothing, and delivered no digest.
#
# Appending the sentence unconditionally rather than as a 400-triggered retry
# keeps it one request per call, and it is true of every provider on this path
# -- the response is JSON regardless of who is asked.
_JSON_NUDGE = "\n\nRespond with JSON matching the required schema."


def _drop_temperature(kwargs: dict[str, Any]) -> None:
    kwargs.pop("temperature", None)


def _drop_strict(kwargs: dict[str, Any]) -> None:
    kwargs["response_format"]["json_schema"].pop("strict", None)


def _drop_reasoning(kwargs: dict[str, Any]) -> None:
    extra = kwargs.get("extra_body")
    if extra:
        extra.pop("reasoning", None)
    if not kwargs.get("extra_body"):
        kwargs.pop("extra_body", None)


# (param name, does this 400 blame that param?, how to drop it) -- see _do_openai.
# `strict` is an OpenAI extension to json_schema; dropping it still leaves the
# schema itself in force on endpoints that only implement the standard field.
_OPENAI_PARAM_FALLBACKS: list[tuple[str, Any, Any]] = [
    ("temperature", lambda m: "temperature" in m and "does not support" in m, _drop_temperature),
    ("strict", lambda m: "strict" in m, _drop_strict),
    # `reasoning` is an OpenRouter extension. It is only ever sent to OpenRouter,
    # but OpenRouter fronts many vendors and not all of them accept it -- dropping
    # it costs nothing here, where thinking is unwanted anyway.
    ("reasoning", lambda m: "reasoning" in m, _drop_reasoning),
]


def _log_usage(config: dict[str, Any], response: Any, kind: str) -> None:
    """Record what a call consumed. Only successful responses reach here, so a
    retried-then-failed attempt costs tokens that are not logged -- the provider
    bills for it, we cannot see it, and pretending otherwise would be worse than
    the small undercount."""
    counts = extract_usage(response)
    if counts is None:
        return
    llm = config.get("llm", {})
    record_usage(
        llm.get("provider", "openrouter"), llm.get("model", ""), counts[0], counts[1], kind=kind
    )


def _call_llm(
    client: Any, config: dict[str, Any], prompt: str, schema: dict[str, Any], kind: str = "summarise"
) -> str:
    """Call the LLM with a JSON schema constraining the response shape, and return
    the response content (guaranteed valid JSON matching schema). Retries on
    transient failures."""
    llm = config.get("llm", {})
    provider = llm.get("provider", "openrouter")
    model = llm.get("model", "qwen/qwen3.7-flash")
    temperature = float(llm.get("temperature", 0.3))
    reasoning = bool(llm.get("reasoning", False))

    retry_cfg = config.get("retry", {})
    max_retries = retry_cfg.get("max_retries", 3)
    initial_delay = retry_cfg.get("initial_delay", 1.0)
    max_delay = retry_cfg.get("max_delay", 60.0)

    # The OpenAI-compatible endpoints of Mistral, OpenRouter and Ollama. Where a
    # compatible endpoint doesn't accept an OpenAI-specific request param it 400s
    # deterministically, so _OPENAI_PARAM_FALLBACKS drops that param and retries
    # immediately -- failing outright would send the whole batch down the
    # per-item/truncated-description fallback chain in summarise_batch/
    # summarise_all, and waiting on retry()'s backoff would never help.
    def _do_openai() -> str:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt + _JSON_NUDGE}],
            "temperature": temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "digest_result", "schema": schema, "strict": True},
            },
        }
        # Hybrid models think by default, and OpenRouter bills the reasoning
        # tokens as output -- the expensive side, and uncapped here since this
        # path sets no max_tokens. Sent on every OpenRouter call rather than
        # only when configured: the default is off, and leaving the parameter
        # out is what silently bought thinking in the first place. Only
        # OpenRouter understands it; OpenAI and Mistral would 400.
        if provider == "openrouter":
            kwargs["extra_body"] = {"reasoning": {"enabled": reasoning}}
        remaining = list(_OPENAI_PARAM_FALLBACKS)
        while True:
            try:
                response = client.chat.completions.create(**kwargs)
                break
            except OpenAIBadRequestError as e:
                message = str(e)
                for i, (name, matches, drop) in enumerate(remaining):
                    if matches(message):
                        log.info("Model %s rejected %s; retrying without it", model, name)
                        drop(kwargs)
                        remaining.pop(i)  # only try each fallback once
                        break
                else:
                    raise
        _log_usage(config, response, kind)
        return response.choices[0].message.content or ""

    return retry(
        _do_openai,
        max_retries=max_retries, initial_delay=initial_delay, max_delay=max_delay,
        non_retryable=(OpenAIAuthError,),
    )


def _topic_line(item: dict[str, Any]) -> str:
    """TOPIC/TOPIC CONTEXT lines for an item that came from a topic search feed.

    Without these the model has no way to judge relevance: a search for a company
    name returns same-name-different-subject hits, and only the topic's own
    description distinguishes them. Empty string for ordinary publisher feeds, so
    the security instance's prompt sees exactly the fields it always has.

    Both values are config-authored rather than feed-supplied, but they are passed
    through _strip_delimiters anyway -- the cost is nothing and it keeps the
    invariant that everything interpolated into the prompt is delimiter-safe."""
    topic = _strip_delimiters(item.get("source", "")) if item.get("topic_context") else ""
    if not topic:
        return ""
    context = _strip_delimiters(item.get("topic_context", ""))
    line = f"TOPIC: {topic}\n"
    if context:
        line += f"TOPIC CONTEXT: {context}\n"
    return line


def _cluster_schema(
    allowed: list[str], allowed_domains: list[str] | None = None,
    with_entities: bool = False,
) -> dict[str, Any]:
    """Schema for the clustering call: groups of item indices, each with one
    headline, summary and category.

    `members` is what makes this different from the batch call -- several inputs
    collapse to one output, so the response can't be positional and has to say
    explicitly which items it merged."""
    properties: dict[str, Any] = {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "category": {"type": "string", "enum": allowed},
        "members": {"type": "array", "items": {"type": "integer"}},
    }
    required = ["title", "summary", "category", "members"]
    if allowed_domains:
        properties["domain"] = {"type": "string", "enum": allowed_domains}
        required.append("domain")
    if with_entities:
        properties["entities"] = {"type": "array", "items": {"type": "string"}}
        required.append("entities")

    return {
        "type": "object",
        "properties": {
            "clusters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            }
        },
        "required": ["clusters"],
        "additionalProperties": False,
    }


def _group_schema() -> dict[str, Any]:
    """Schema for the grouping half of a two-pass cluster: which items are the
    same story, and nothing else.

    Deliberately not _cluster_schema: a headline and a summary per cluster would
    be written and then thrown away by the summarising pass, and output tokens
    are the expensive half of the call."""
    return {
        "type": "object",
        "properties": {
            "clusters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"members": {"type": "array", "items": {"type": "integer"}}},
                    "required": ["members"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["clusters"],
        "additionalProperties": False,
    }


def _combined_description(
    members_items: list[dict[str, Any]], max_chars: int
) -> str:
    """The members' article text, pooled, for summarising a merged story.

    Each outlet carries detail the others left out -- the figure in one, the
    attribution in another. Summarising only the first report would discard the
    rest, which is most of the reason for merging them in the first place."""
    parts: list[str] = []
    budget = max_chars
    for item in members_items:
        text = (item.get("description") or "").strip()
        if not text or budget <= 0:
            continue
        publisher = item.get("publisher") or item.get("source", "")
        chunk = f"[{publisher}] {text}" if publisher else text
        parts.append(chunk[:budget])
        budget -= len(parts[-1])
    return "\n\n".join(parts)


def _merge_cluster(
    items: list[dict[str, Any]], members: list[int], result: dict[str, Any],
    config: dict[str, Any], pool_descriptions: int | None = None,
) -> dict[str, Any]:
    """Build one digest item from the source items a cluster merged.

    Keeps every member's link: that list is the whole point of clustering, and
    it's what lets the digest credit each outlet that covered the story. The
    newest publication date wins, since the item is as recent as its freshest
    report."""
    members_items = [items[i] for i in members]
    links: list[dict[str, str]] = []
    seen_links: set[str] = set()
    for item in members_items:
        link = item.get("link", "")
        if not link or link in seen_links:
            continue
        seen_links.add(link)
        links.append({
            "publisher": item.get("publisher") or item.get("source", ""),
            "link": link,
        })

    primary = members_items[0]
    merged = {
        **primary,
        "title": result.get("title") or primary.get("title", ""),
        "summary": result.get("summary") or (primary.get("description", "") or "")[:300],
        "category": _coerce_category(result.get("category"), config),
        **_coerce_domain(result.get("domain"), config),
        # Empty on the two-pass path, where this call only groups -- the
        # summarising pass that follows fills them in from the full text.
        **_coerce_entities(result.get("entities"), config),
        # link stays the primary one so history and any single-link consumer
        # keeps working unchanged.
        "link": links[0]["link"] if links else primary.get("link", ""),
        "links": links,
        "published": max((i.get("published") or "") for i in members_items),
    }
    if pool_descriptions and len(members_items) > 1:
        merged["description"] = _combined_description(members_items, pool_descriptions)
    return merged


def _assign_clusters(
    items: list[dict[str, Any]], clusters: list[dict[str, Any]], config: dict[str, Any],
    pool_descriptions: int | None = None, singletons_implicit: bool = False,
) -> list[dict[str, Any]]:
    """Turn the model's clusters into digest items, defensively.

    The schema constrains the shape but not the arithmetic: indices can repeat,
    fall out of range, or omit an item entirely. An item silently dropped here is
    news the reader never sees, so anything unclaimed becomes its own single-item
    cluster rather than disappearing.

    singletons_implicit says an unclaimed item is expected rather than a fault.
    The grouping pass asks only for groups of two or more -- listing forty
    single-member groups is output tokens spent to say nothing, and it makes a
    genuinely dropped item indistinguishable from a story only one outlet
    covered. With them implicit the rescue below IS the contract, so it stops
    being worth a warning."""
    claimed: set[int] = set()
    output: list[dict[str, Any]] = []

    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        members = [
            i for i in cluster.get("members", [])
            if isinstance(i, int) and 0 <= i < len(items) and i not in claimed
        ]
        if not members:
            continue
        claimed.update(members)
        output.append(_merge_cluster(items, members, cluster, config, pool_descriptions))

    unclaimed = [i for i in range(len(items)) if i not in claimed]
    if unclaimed:
        if singletons_implicit:
            log.info("%d item(s) stand alone", len(unclaimed))
        else:
            log.warning("Clustering left %d item(s) unassigned; keeping them separate",
                        len(unclaimed))
        for i in unclaimed:
            output.append(_merge_cluster(items, [i], {
                "title": items[i].get("title", ""),
                "summary": (items[i].get("description", "") or "")[:300],
                "category": fallback_category(config),
            }, config, pool_descriptions))
    return output


def _format_item_for_cluster(item: dict[str, Any], index: int) -> str:
    """Format an item for the clustering prompt.

    Uses its own labelling rather than reusing the batch formatter: that one
    prints "Item 1" for the first item because the batch response is positional
    and the number is only a human-readable marker. Clustering is different --
    the model reports which indices it merged, and those come straight back as
    list offsets, so the label it sees must be the offset itself. Reusing the
    1-based label silently shifted every summary onto the wrong article's link."""
    return f"INDEX: {index}\n" + _format_item_for_batch(item, index).split("\n", 1)[1]


def cluster_topic(
    items: list[dict[str, Any]], client: Any, config: dict[str, Any]
) -> list[dict[str, Any]]:
    """Summarise one topic's items, merging those that report the same story."""
    items_text = "\n".join(_format_item_for_cluster(item, i) for i, item in enumerate(items))
    prompt = render_template(CLUSTER_PROMPT_PATH, items=items_text)

    content = _call_llm(
        client, config, prompt,
        _cluster_schema(categories(config), domains(config),
                        with_entities=extract_entities(config)), kind="cluster"
    )
    clusters = _unwrap_list(content, "clusters")
    if clusters is None:
        log.warning("Clustering returned no usable clusters; falling back to per-item batch")
        return summarise_batch(items, client, config)

    merged = _assign_clusters(items, clusters, config)
    if len(merged) < len(items):
        log.info("Merged %d items into %d stories", len(items), len(merged))
    return merged


def _format_item_for_batch(item: dict[str, Any], index: int) -> str:
    """Format a single item for the batch prompt. Untrusted feed text is stripped of
    any literal delimiter strings and wrapped in <item_description> tags so it can't
    be mistaken for prompt instructions (see prompts/summarise_batch.txt)."""
    title = _strip_delimiters(item.get("title", ""))
    source = _strip_delimiters(item.get("publisher") or item.get("source", ""))
    description = _strip_delimiters(item.get("description", "")) or "(no description)"
    return f"""Item {index + 1}:
{_topic_line(item)}TITLE: {title}
SOURCE: {source}
DESCRIPTION:
{_DELIM_OPEN}
{description}
{_DELIM_CLOSE}
"""


def _unwrap_list(content: str, key: str) -> list[Any] | None:
    """Pull the result list out of a response, tolerating a bare top-level array.

    The schema asks for {"<key>": [...]} because a top-level array isn't accepted
    by either provider's structured-output support. But not every endpoint
    *enforces* the schema: OpenRouter downgrades json_schema to json_object for
    providers that lack structured output (Alibaba's Qwen among them), which
    constrains the reply to "some JSON" and nothing more. Qwen answers with the
    bare array, which is a reasonable reading of the prompt.

    Returns None when there is no usable list, so callers can tell "the model
    said something else" from "the model returned an empty list"."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        data = data.get(key)
    return data if isinstance(data, list) else None


def _coerce_category(value: Any, config: dict[str, Any]) -> str:
    """An unenforced schema can return a category outside the vocabulary, and
    main.py routes on `category in sections` -- so an unrecognised one is not a
    cosmetic flaw, it drops the item from every digest silently."""
    return value if value in categories(config) else fallback_category(config)


def _coerce_domain(value: Any, config: dict[str, Any]) -> dict[str, str]:
    """{"domain": ...} for merging into a result, or {} where this instance has
    no domains. Same reasoning as _coerce_category: routing reads this field, so
    a value outside the vocabulary means the item reaches no digest at all."""
    allowed = domains(config)
    if not allowed:
        return {}
    return {"domain": value if value in allowed else (fallback_domain(config) or allowed[0])}


def _parse_batch_response(content: str) -> list[dict[str, str]]:
    """Parse a batch response ({"items": [{summary, category}, ...]}, or a bare
    array) into a plain list. Returns empty for a genuinely malformed response
    (e.g. a non-JSON error body), which triggers the per-item fallback in
    summarise_batch."""
    return _unwrap_list(content, "items") or []


def summarise_batch(
    items: list[dict[str, Any]], client: Any, config: dict[str, Any]
) -> list[dict[str, Any]]:
    """Summarise a batch of items in one LLM call."""
    items_text = "\n".join(_format_item_for_batch(item, i) for i, item in enumerate(items))
    prompt = render_template(BATCH_PROMPT_PATH, items=items_text)

    content = _call_llm(
        client, config, prompt,
        _result_schema(array=True, allowed=categories(config),
                       allowed_domains=domains(config),
                       with_entities=extract_entities(config)), kind="batch"
    )
    results = _parse_batch_response(content)

    output = []
    for i, item in enumerate(items):
        r = results[i] if i < len(results) else None
        # isinstance rather than trusting the schema: an endpoint that only
        # enforces "valid JSON" can return a list of strings here, and a
        # KeyError would fail the whole batch over one bad entry.
        if isinstance(r, dict) and r.get("summary"):
            output.append({
                **item,
                "summary": r["summary"],
                "category": _coerce_category(r.get("category"), config),
                **_coerce_domain(r.get("domain"), config),
                **_coerce_entities(r.get("entities"), config),
            })
        else:
            log.warning("Missing result for item %d: %s", i + 1, item.get("title", "")[:50])
            output.append({
                **item,
                "summary": (item.get("description", "") or "")[:300],
                "category": fallback_category(config),
                **_coerce_domain(None, config),
                **_coerce_entities(None, config),
            })
    return output


def summarise_item(
    item: dict[str, Any], client: Any, config: dict[str, Any]
) -> dict[str, Any]:
    """Summarise a single item and add summary + category. Fallback when batch fails."""
    # {topic} is only present in a topic instance's prompt; render_template
    # substitutes named placeholders and ignores kwargs the template doesn't use,
    # so passing it costs nothing for the security instance.
    prompt = render_template(
        PROMPT_PATH,
        title=_strip_delimiters(item.get("title", "")),
        source=_strip_delimiters(item.get("publisher") or item.get("source", "")),
        description=_strip_delimiters(item.get("description", "")) or "(no description)",
        topic=_topic_line(item),
    )

    try:
        content = _call_llm(
            client, config, prompt,
            _result_schema(array=False, allowed=categories(config),
                           allowed_domains=domains(config),
                           with_entities=extract_entities(config)), kind="item"
        )
        data = json.loads(content)
        summary = data["summary"]
        category = _coerce_category(data.get("category"), config)
        domain = _coerce_domain(data.get("domain"), config)
        entities = _coerce_entities(data.get("entities"), config)
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        log.warning("Failed to parse LLM output for '%s': %s", item.get("title"), e)
        summary = item.get("description", "")[:300]
        category = fallback_category(config)
        domain = _coerce_domain(None, config)
        entities = _coerce_entities(None, config)

    return {
        **item,
        "summary": summary,
        "category": category,
        **domain,
        **entities,
    }


def _cluster_groups(
    items: list[dict[str, Any]], config: dict[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    """The sets of items each clustering call is allowed to merge within.

    Per SOURCE by default, and on a topic instance that is a correctness
    requirement rather than a convenience: `source` is the tracked topic, which
    is what digests route on, so merging two topics' items into one story would
    deliver it to whichever recipient the surviving item happened to belong to
    and silently deny it to the other.

    On a publisher instance the same rule makes clustering useless. There
    `source` is the outlet, and the duplicates worth merging are precisely the
    ones that span outlets -- one advisory written up by Krebs, Bleeping
    Computer and The Hacker News lands in three different groups and never
    meets itself. Such an instance sets cluster_scope: all, which is safe only
    while every feed reaches the same digests; routing.warn_on_cross_feed_
    clustering checks that and says so when it stops being true."""
    if cluster_scope(config) == CLUSTER_SCOPE_ALL:
        return {"all feeds": list(items)}
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(str(item.get("source", "")), []).append(item)
    return groups


def _trimmed_for_grouping(
    items: list[dict[str, Any]], max_chars: int
) -> list[dict[str, Any]]:
    """Copies with the article text cut down for the grouping call.

    Deciding whether two reports are the same story needs the headline and the
    opening -- who, what, which product. The rest is detail that only matters
    once something is being summarised, and carrying it here would put an
    instance's whole day of full-text articles into one prompt."""
    return [{**item, "description": (item.get("description") or "")[:max_chars]} for item in items]


def group_stories(
    items: list[dict[str, Any]], client: Any, config: dict[str, Any], max_chars: int
) -> list[dict[str, Any]]:
    """First half of a two-pass cluster: merge the same story, summarise nothing.

    Returns merged items still carrying their source text, for summarise_batch
    to write up from the full article rather than from the trimmed copy this
    call was shown."""
    trimmed = _trimmed_for_grouping(items, max_chars)
    items_text = "\n".join(_format_item_for_cluster(item, i) for i, item in enumerate(trimmed))
    prompt = render_template(CLUSTER_PROMPT_PATH, items=items_text)

    content = _call_llm(client, config, prompt, _group_schema(), kind="cluster")
    clusters = _unwrap_list(content, "clusters")
    if clusters is None:
        raise ValueError("clustering returned no usable groups")

    merged = _assign_clusters(
        items, clusters, config,
        pool_descriptions=int(config.get("sources", {}).get("max_description_chars", 1000)),
        singletons_implicit=True,
    )
    if len(merged) < len(items):
        log.info("Merged %d items into %d stories", len(items), len(merged))
    return merged


def _summarise_in_batches(
    items: list[dict[str, Any]], client: Any, config: dict[str, Any]
) -> list[dict[str, Any]]:
    """Summarise in batches, falling back to per-item when a batch won't parse."""
    batch_size = int(config.get("llm", {}).get("batch_size", 8))
    result: list[dict[str, Any]] = []
    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        log.info("Summarising batch %d-%d/%d (%d items)",
                 start + 1, start + len(batch), len(items), len(batch))
        try:
            batch_result = summarise_batch(batch, client, config)
            if len(batch_result) == len(batch):
                result.extend(batch_result)
            else:
                for item in batch:
                    result.append(summarise_item(item, client, config))
        except Exception as e:
            log.warning("Batch summarisation failed, falling back to per-item: %s", e)
            for item in batch:
                result.append(summarise_item(item, client, config))
    return result


def _cluster_all(
    items: list[dict[str, Any]], client: Any, config: dict[str, Any]
) -> list[dict[str, Any]]:
    """Cluster and summarise. One LLM call per group, plus a summarising pass
    when llm.cluster_chars splits the work in two."""
    max_chars = cluster_chars(config)
    groups = _cluster_groups(items, config)

    result: list[dict[str, Any]] = []
    for key, group in groups.items():
        log.info("Clustering %d item(s) for '%s'", len(group), key)
        try:
            if max_chars:
                result.extend(group_stories(group, client, config, max_chars))
            else:
                result.extend(cluster_topic(group, client, config))
        except Exception as e:
            if max_chars:
                # The summarising pass still runs, so the day's items are all
                # delivered -- unmerged, which reads as duplicates rather than
                # as the silence a re-raise would produce.
                log.warning("Grouping failed for '%s', leaving its items unmerged: %s", key, e)
                result.extend(group)
            else:
                log.warning("Clustering failed for '%s', falling back to per-item: %s", key, e)
                result.extend(summarise_batch(group, client, config))

    if max_chars:
        log.info("Summarising %d story/stories from the full article text", len(result))
        result = _summarise_in_batches(result, client, config)
    return result


def summarise_all(
    items: list[dict[str, Any]], config: dict[str, Any]
) -> list[dict[str, Any]]:
    """Summarise all items in batches, with fallback to per-item on batch parse failure."""
    if not items:
        return []

    client = _get_client(config)
    llm_cfg = config.get("llm", {})

    if llm_cfg.get("cluster", False):
        return _cluster_all(items, client, config)

    return _summarise_in_batches(items, client, config)


if __name__ == "__main__":
    import sys
    from src.fetcher import fetch_all, load_config

    logging.basicConfig(level=logging.INFO)

    config = load_config(PROJECT_ROOT / "config.yaml")

    items = fetch_all(config)[:5]
    if not items:
        print("No items to summarise")
        sys.exit(1)

    print("Summarising 5 items (batch mode)...\n")
    for item in summarise_all(items, config):
        print(f"[{item['category'].upper()}] {item['title']}")
        print(f"  {item['summary']}\n")
