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
    return messages


def _result_schema(*, array: bool, allowed: list[str], allowed_domains: list[str] | None = None) -> dict[str, Any]:
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
    """Create API client for OpenAI, Anthropic, Mistral, OpenRouter or Ollama.
    All but Anthropic are OpenAI-compatible, so they reuse the OpenAI SDK with a
    different base_url."""
    llm = config.get("llm", {})
    provider = llm.get("provider", "openai")

    if provider == "anthropic":
        try:
            from anthropic import Anthropic
        except ImportError as e:
            raise RuntimeError("Install anthropic: pip install anthropic") from e
        return Anthropic()  # ANTHROPIC_API_KEY from env
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
    return OpenAI()  # uses OPENAI_API_KEY from env


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


# (param name, does this 400 blame that param?, how to drop it) -- see _do_openai.
# `strict` is an OpenAI extension to json_schema; dropping it still leaves the
# schema itself in force on endpoints that only implement the standard field.
_OPENAI_PARAM_FALLBACKS: list[tuple[str, Any, Any]] = [
    ("temperature", lambda m: "temperature" in m and "does not support" in m, _drop_temperature),
    ("strict", lambda m: "strict" in m, _drop_strict),
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
        llm.get("provider", "openai"), llm.get("model", ""), counts[0], counts[1], kind=kind
    )


def _call_llm(
    client: Any, config: dict[str, Any], prompt: str, schema: dict[str, Any], kind: str = "summarise"
) -> str:
    """Call the LLM with a JSON schema constraining the response shape, and return
    the response content (guaranteed valid JSON matching schema). Retries on
    transient failures."""
    llm = config.get("llm", {})
    provider = llm.get("provider", "openai")
    model = llm.get("model", "gpt-5.6-luna")
    temperature = float(llm.get("temperature", 0.3))

    retry_cfg = config.get("retry", {})
    max_retries = retry_cfg.get("max_retries", 3)
    initial_delay = retry_cfg.get("initial_delay", 1.0)
    max_delay = retry_cfg.get("max_delay", 60.0)

    if provider == "anthropic":
        # Lazy import: anthropic is an optional dependency (only needed for this
        # provider), matching the lazy import in _get_client.
        from anthropic import AuthenticationError as AnthropicAuthError

        def _do_anthropic() -> str:
            resp = client.messages.create(
                model=model,
                max_tokens=16384,
                temperature=temperature,
                messages=[{"role": "user", "content": prompt}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
            _log_usage(config, resp, kind)
            parts: list[str] = []
            for block in resp.content:
                t = getattr(block, "text", None)
                if t:
                    parts.append(t)
            return "".join(parts)

        return retry(
            _do_anthropic,
            max_retries=max_retries, initial_delay=initial_delay, max_delay=max_delay,
            non_retryable=(AnthropicAuthError,),
        )

    # OpenAI, plus the OpenAI-compatible endpoints of Mistral and Ollama. Where a
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


def _cluster_schema(allowed: list[str], allowed_domains: list[str] | None = None) -> dict[str, Any]:
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


def _merge_cluster(
    items: list[dict[str, Any]], members: list[int], result: dict[str, Any],
    config: dict[str, Any],
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
    return {
        **primary,
        "title": result.get("title") or primary.get("title", ""),
        "summary": result.get("summary") or (primary.get("description", "") or "")[:300],
        "category": _coerce_category(result.get("category"), config),
        **_coerce_domain(result.get("domain"), config),
        # link stays the primary one so history and any single-link consumer
        # keeps working unchanged.
        "link": links[0]["link"] if links else primary.get("link", ""),
        "links": links,
        "published": max((i.get("published") or "") for i in members_items),
    }


def _assign_clusters(
    items: list[dict[str, Any]], clusters: list[dict[str, Any]], config: dict[str, Any]
) -> list[dict[str, Any]]:
    """Turn the model's clusters into digest items, defensively.

    The schema constrains the shape but not the arithmetic: indices can repeat,
    fall out of range, or omit an item entirely. An item silently dropped here is
    news the reader never sees, so anything unclaimed becomes its own single-item
    cluster rather than disappearing."""
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
        output.append(_merge_cluster(items, members, cluster, config))

    unclaimed = [i for i in range(len(items)) if i not in claimed]
    if unclaimed:
        log.warning("Clustering left %d item(s) unassigned; keeping them separate", len(unclaimed))
        for i in unclaimed:
            output.append(_merge_cluster(items, [i], {
                "title": items[i].get("title", ""),
                "summary": (items[i].get("description", "") or "")[:300],
                "category": fallback_category(config),
            }, config))
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
        _cluster_schema(categories(config), domains(config)), kind="cluster"
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
                       allowed_domains=domains(config)), kind="batch"
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
            })
        else:
            log.warning("Missing result for item %d: %s", i + 1, item.get("title", "")[:50])
            output.append({
                **item,
                "summary": (item.get("description", "") or "")[:300],
                "category": fallback_category(config),
                **_coerce_domain(None, config),
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
                           allowed_domains=domains(config)), kind="item"
        )
        data = json.loads(content)
        summary = data["summary"]
        category = _coerce_category(data.get("category"), config)
        domain = _coerce_domain(data.get("domain"), config)
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        log.warning("Failed to parse LLM output for '%s': %s", item.get("title"), e)
        summary = item.get("description", "")[:300]
        category = fallback_category(config)
        domain = _coerce_domain(None, config)

    return {
        **item,
        "summary": summary,
        "category": category,
        **domain,
    }


def _cluster_all(
    items: list[dict[str, Any]], client: Any, config: dict[str, Any]
) -> list[dict[str, Any]]:
    """Cluster and summarise, one LLM call per topic.

    Grouped by source rather than clustering everything together, and that is a
    correctness requirement rather than a convenience: `source` is what digests
    route on, so merging two topics' items into one story would deliver it to
    whichever recipient the surviving item happened to belong to, and silently
    deny it to the other."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(str(item.get("source", "")), []).append(item)

    result: list[dict[str, Any]] = []
    for source, group in groups.items():
        log.info("Clustering %d item(s) for '%s'", len(group), source)
        try:
            result.extend(cluster_topic(group, client, config))
        except Exception as e:
            log.warning("Clustering failed for '%s', falling back to per-item: %s", source, e)
            result.extend(summarise_batch(group, client, config))
    return result


def summarise_all(
    items: list[dict[str, Any]], config: dict[str, Any]
) -> list[dict[str, Any]]:
    """Summarise all items in batches, with fallback to per-item on batch parse failure."""
    if not items:
        return []

    client = _get_client(config)
    llm_cfg = config.get("llm", {})
    batch_size = int(llm_cfg.get("batch_size", 8))

    if llm_cfg.get("cluster", False):
        return _cluster_all(items, client, config)

    result = []
    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        log.info("Summarising batch %d-%d/%d (%d items)", start + 1, start + len(batch), len(items), len(batch))

        try:
            batch_result = summarise_batch(batch, client, config)
            if len(batch_result) == len(batch):
                result.extend(batch_result)
            else:
                # Fallback to per-item
                for item in batch:
                    result.append(summarise_item(item, client, config))
        except Exception as e:
            log.warning("Batch summarisation failed, falling back to per-item: %s", e)
            for item in batch:
                result.append(summarise_item(item, client, config))

    return result


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
