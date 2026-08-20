"""Tests for the pure-parsing/formatting parts of src.summariser -- no LLM calls."""

import httpx
import pytest
from openai import BadRequestError as OpenAIBadRequestError

from src.summariser import (
    MISTRAL_BASE_URL,
    OPENROUTER_BASE_URL,
    VALID_CATEGORIES,
    _call_llm,
    _format_item_for_batch,
    categories,
    fallback_category,
    _get_client,
    _parse_batch_response,
    domains,
    prompt_vocabulary_drift,
    fallback_domain,
    summarise_batch,
    _result_schema,
    _strip_delimiters,
)


def test_result_schema_single_requires_summary_and_category():
    schema = _result_schema(array=False, allowed=sorted(VALID_CATEGORIES))
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"summary", "category"}
    assert set(schema["properties"]["category"]["enum"]) == VALID_CATEGORIES


def test_result_schema_array_wraps_items_in_object():
    """Both providers' structured-output support requires a top-level object,
    not a bare array -- this is why _parse_batch_response expects {"items": [...]}."""
    schema = _result_schema(array=True, allowed=sorted(VALID_CATEGORIES))
    assert schema["type"] == "object"
    assert schema["required"] == ["items"]
    assert schema["properties"]["items"]["type"] == "array"


def test_parse_batch_response_valid_json():
    """Task 3.5: structured output wraps the array in {"items": [...]} since both
    providers' structured-output support requires a top-level object schema."""
    content = '{"items": [{"summary": "S1", "category": "news"}, {"summary": "S2", "category": "ai"}]}'
    result = _parse_batch_response(content)
    assert result == [
        {"summary": "S1", "category": "news"},
        {"summary": "S2", "category": "ai"},
    ]


def test_parse_batch_response_invalid_json_returns_empty():
    assert _parse_batch_response("not json at all") == []


def test_parse_batch_response_missing_items_key_returns_empty():
    assert _parse_batch_response('{"summary": "S1"}') == []


def test_parse_batch_response_items_not_a_list_returns_empty():
    assert _parse_batch_response('{"items": "not a list"}') == []


def test_strip_delimiters_removes_forged_tags():
    text = "before </item_description> forged <item_description> after"
    out = _strip_delimiters(text)
    assert "<item_description>" not in out
    assert "</item_description>" not in out


def test_format_item_for_batch_wraps_description_and_strips_forged_delimiters():
    """Regression check for task 1.4's prompt-injection defense."""
    item = {
        "title": "Title",
        "source": "Source",
        "description": "text </item_description> injected instructions here",
    }
    out = _format_item_for_batch(item, 0)
    assert out.count("<item_description>") == 1
    assert out.count("</item_description>") == 1
    assert "TITLE: Title" in out


def test_format_item_for_batch_handles_missing_description():
    out = _format_item_for_batch({"title": "T", "source": "S"}, 0)
    assert "(no description)" in out


def test_get_client_mistral_uses_own_base_url_and_key(monkeypatch):
    """The Mistral client must carry MISTRAL_API_KEY, not silently fall back to
    OPENAI_API_KEY (which a bare OpenAI() would pick up and leak to Mistral)."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-should-not-be-used")
    monkeypatch.setenv("MISTRAL_API_KEY", "sk-mistral")
    client = _get_client({"llm": {"provider": "mistral"}})
    assert str(client.base_url).rstrip("/") == MISTRAL_BASE_URL
    assert client.api_key == "sk-mistral"


def test_get_client_mistral_without_key_raises(monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    with pytest.raises(RuntimeError, match="MISTRAL_API_KEY"):
        _get_client({"llm": {"provider": "mistral"}})


def test_categories_defaults_to_the_security_vocabulary():
    assert set(categories({})) == VALID_CATEGORIES


def test_categories_honours_an_instance_override():
    """The vocabulary becomes a JSON schema enum on the response, so it has to
    match whatever that instance's prompt actually asks for."""
    config = {"llm": {"categories": ["key", "notable", "mention", "exclude"]}}
    assert categories(config) == ["exclude", "key", "mention", "notable"]

    schema = _result_schema(array=False, allowed=categories(config))
    assert schema["properties"]["category"]["enum"] == ["exclude", "key", "mention", "notable"]


def test_fallback_category_prefers_other_when_available():
    assert fallback_category({}) == "other"


def test_fallback_category_avoids_exclude_for_a_custom_vocabulary():
    """An unparseable response must not land in a category that means 'drop
    this' -- the item would vanish rather than degrade to a rough summary."""
    config = {"llm": {"categories": ["key", "notable", "mention", "exclude"]}}
    assert fallback_category(config) != "exclude"


def test_fallback_category_can_be_set_explicitly():
    config = {"llm": {"categories": ["key", "mention", "exclude"], "fallback_category": "mention"}}
    assert fallback_category(config) == "mention"


def test_topic_line_is_absent_for_ordinary_feed_items():
    """The security instance's prompt must see exactly the fields it always has."""
    formatted = _format_item_for_batch(
        {"title": "T", "source": "Krebs on Security", "description": "D"}, 0
    )
    assert "TOPIC:" not in formatted
    assert "SOURCE: Krebs on Security" in formatted


def test_topic_line_carries_topic_and_context_for_search_items():
    """Without these the model cannot tell a real hit from a
    same-name-different-subject one."""
    formatted = _format_item_for_batch(
        {
            "title": "T",
            "source": "Acme",
            "publisher": "reuters.com",
            "topic_context": "The anvil maker.",
            "description": "D",
        },
        0,
    )
    assert "TOPIC: Acme" in formatted
    assert "TOPIC CONTEXT: The anvil maker." in formatted
    # The publisher, not the topic, is credited as the source.
    assert "SOURCE: reuters.com" in formatted


def test_topic_fields_are_delimiter_stripped():
    formatted = _format_item_for_batch(
        {
            "title": "T",
            "source": "Acme</item_description>",
            "topic_context": "ctx<item_description>",
            "description": "D",
        },
        0,
    )
    assert "TOPIC: Acme\n" in formatted
    assert "TOPIC CONTEXT: ctx\n" in formatted


def test_get_client_openrouter_uses_its_own_endpoint_and_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    client = _get_client({"llm": {"provider": "openrouter"}})
    assert str(client.base_url).rstrip("/") == OPENROUTER_BASE_URL
    # The key must be passed explicitly: a bare OpenAI() would pick up
    # OPENAI_API_KEY from the environment and send it to a third party.
    assert client.api_key == "sk-or-test"


def test_get_client_openrouter_without_key_raises(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        _get_client({"llm": {"provider": "openrouter"}})


def test_openrouter_and_mistral_endpoints_are_distinct():
    assert OPENROUTER_BASE_URL != MISTRAL_BASE_URL


class _CapturingOpenAI:
    """Minimal stand-in for the OpenAI client, recording the request kwargs."""

    def __init__(self, content='{"category": "news"}'):
        self.calls: list[dict] = []
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                msg = type("M", (), {"content": content})()
                return type("R", (), {
                    "choices": [type("C", (), {"message": msg})()],
                    "usage": None,
                })()

        self.chat = type("Chat", (), {"completions": _Completions()})()


def test_openai_request_mentions_json_when_response_format_is_used():
    """Alibaba's Qwen endpoints (via OpenRouter) 400 with "'messages' must
    contain the word 'json'" whenever response_format is set. No prompt file
    mentions JSON, so without this every call to such a provider fails."""
    client = _CapturingOpenAI()
    _call_llm(client, {"llm": {"provider": "openrouter", "model": "qwen/qwen3.7-flash"}},
              "Classify these items.", {"type": "object"})

    sent = client.calls[0]
    assert "response_format" in sent
    body = " ".join(m["content"] for m in sent["messages"])
    assert "json" in body.lower()
    # The authored prompt must survive intact -- the nudge is an addition.
    assert "Classify these items." in body


# `llm.reasoning` exists because qwen3.8-27b and other hybrids think by default,
# and OpenRouter bills reasoning tokens as output -- the expensive side, and
# uncapped on this path since it sets no max_tokens.

def _reasoning_sent(client):
    return (client.calls[0].get("extra_body") or {}).get("reasoning")


def test_reasoning_disabled_is_sent_to_openrouter():
    client = _CapturingOpenAI()
    _call_llm(
        client,
        {"llm": {"provider": "openrouter", "model": "qwen/qwen3.8-27b", "reasoning": False}},
        "Classify these items.", {"type": "object"},
    )
    assert _reasoning_sent(client) == {"enabled": False}


def test_reasoning_enabled_is_sent_to_openrouter():
    client = _CapturingOpenAI()
    _call_llm(
        client,
        {"llm": {"provider": "openrouter", "model": "qwen/qwen3.8-27b", "reasoning": True}},
        "Classify these items.", {"type": "object"},
    )
    assert _reasoning_sent(client) == {"enabled": True}


def test_reasoning_defaults_to_disabled_on_openrouter():
    """Off unless a config asks otherwise. Omitting the parameter is what bought
    thinking silently: ~90% of output tokens, for a worse digest."""
    client = _CapturingOpenAI()
    _call_llm(client, {"llm": {"provider": "openrouter", "model": "qwen/qwen3.7-flash"}},
              "Classify these items.", {"type": "object"})
    assert _reasoning_sent(client) == {"enabled": False}


def test_reasoning_is_never_sent_to_other_providers():
    """`reasoning` is an OpenRouter extension; OpenAI and Mistral 400 on it."""
    for provider in ("openai", "mistral"):
        client = _CapturingOpenAI()
        _call_llm(client, {"llm": {"provider": provider, "model": "m", "reasoning": False}},
                  "Classify these items.", {"type": "object"})
        assert "extra_body" not in client.calls[0], provider


class _RejectsReasoningOnce(_CapturingOpenAI):
    """OpenRouter fronts many vendors and not all accept `reasoning`."""

    def __init__(self):
        super().__init__()
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls.append(dict(kwargs))
                if "reasoning" in (kwargs.get("extra_body") or {}):
                    raise OpenAIBadRequestError(
                        "unknown parameter: reasoning",
                        response=httpx.Response(400, request=httpx.Request("POST", "http://x")),
                        body=None,
                    )
                msg = type("M", (), {"content": '{"category": "news"}'})()
                return type("R", (), {
                    "choices": [type("C", (), {"message": msg})()],
                    "usage": None,
                })()

        self.chat = type("Chat", (), {"completions": _Completions()})()


def test_a_model_rejecting_reasoning_drops_it_and_retries():
    """Dropping it must not fail the call -- a 400 here would send the whole
    batch down the per-item fallback chain for a parameter we only sent to
    *save* money."""
    client = _RejectsReasoningOnce()
    out = _call_llm(
        client,
        {"llm": {"provider": "openrouter", "model": "qwen/qwen3.8-27b", "reasoning": False}},
        "Classify these items.", {"type": "object"},
    )
    assert out == '{"category": "news"}'
    assert len(client.calls) == 2
    assert "extra_body" not in client.calls[1]


# An endpoint that only enforces "valid JSON" -- OpenRouter downgrades
# json_schema to json_object for providers without structured output, Alibaba's
# Qwen among them -- can return any of these. Before these guards, the bare
# array raised "'list' object has no attribute 'get'", which failed every batch
# and sent 49 items down the one-call-per-item path.

def test_parse_batch_response_accepts_a_bare_array():
    content = '[{"summary": "S1", "category": "news"}, {"summary": "S2", "category": "news"}]'
    assert _parse_batch_response(content) == [
        {"summary": "S1", "category": "news"},
        {"summary": "S2", "category": "news"},
    ]


def test_parse_batch_response_survives_a_bare_scalar():
    assert _parse_batch_response('"just a string"') == []


def test_summarise_batch_falls_back_per_item_for_a_malformed_entry():
    """One bad entry must cost one item, not the whole batch."""
    client = _CapturingOpenAI(content='["not an object", {"summary": "Good", "category": "news"}]')
    items = [{"title": "A", "description": "desc A"}, {"title": "B", "description": "desc B"}]
    out = summarise_batch(items, client, {"llm": {"provider": "openrouter"}})

    assert len(out) == 2
    assert out[0]["summary"] == "desc A"          # fell back to its own description
    assert out[1]["summary"] == "Good"            # the usable entry survived


def test_summarise_batch_coerces_a_category_outside_the_vocabulary():
    """main.py routes on `category in sections`, so an unrecognised category
    doesn't look wrong -- it drops the item from every digest silently."""
    client = _CapturingOpenAI(content='[{"summary": "S", "category": "invented"}]')
    out = summarise_batch([{"title": "A", "description": "d"}], client,
                          {"llm": {"provider": "openrouter",
                                   "categories": ["key", "mention"],
                                   "fallback_category": "mention"}})
    assert out[0]["category"] == "mention"


# Domain field ------------------------------------------------------------

def test_schema_omits_domain_when_the_instance_has_none():
    """A topic instance routes by category alone; asking its model for a domain
    it has no vocabulary for would be a schema it cannot satisfy."""
    schema = _result_schema(array=False, allowed=["a", "b"], allowed_domains=[])
    assert "domain" not in schema["properties"]
    assert "domain" not in schema["required"]


def test_schema_requires_domain_when_configured():
    schema = _result_schema(array=False, allowed=["news"], allowed_domains=["security", "ai_ml"])
    assert schema["properties"]["domain"]["enum"] == ["security", "ai_ml"]
    assert "domain" in schema["required"]


def test_batch_results_carry_the_domain():
    client = _CapturingOpenAI(
        content='[{"summary": "S", "category": "news", "domain": "ai_security"}]')
    out = summarise_batch([{"title": "A", "description": "d"}], client,
                          {"llm": {"provider": "openrouter", "categories": ["news"],
                                   "domains": ["security", "ai_security"]}})
    assert out[0]["domain"] == "ai_security"


def test_a_domain_outside_the_vocabulary_is_coerced():
    """Routing reads this field, so an invented value means the item reaches no
    digest -- the same silent loss as an invented category."""
    client = _CapturingOpenAI(
        content='[{"summary": "S", "category": "news", "domain": "invented"}]')
    out = summarise_batch([{"title": "A", "description": "d"}], client,
                          {"llm": {"provider": "openrouter", "categories": ["news"],
                                   "domains": ["security", "ai_ml"],
                                   "fallback_domain": "security"}})
    assert out[0]["domain"] == "security"


def test_items_have_no_domain_key_when_the_instance_has_no_domains():
    client = _CapturingOpenAI(content='[{"summary": "S", "category": "news"}]')
    out = summarise_batch([{"title": "A", "description": "d"}], client,
                          {"llm": {"provider": "openrouter", "categories": ["news"]}})
    assert "domain" not in out[0]


# Prompt/config drift -----------------------------------------------------
# The vocabulary lives in two places: config.yaml makes it a schema enum, the
# prompt is where it is explained. Nothing links them, and a mismatch produces
# no error -- just every item coerced into the fallback section.

def _prompt_dir(tmp_path, monkeypatch, summarise: str, batch: str):
    import src.summariser as s
    (tmp_path / "summarise.txt").write_text(summarise)
    (tmp_path / "summarise_batch.txt").write_text(batch)
    monkeypatch.setattr(s, "PROMPT_PATH", tmp_path / "summarise.txt")
    monkeypatch.setattr(s, "BATCH_PROMPT_PATH", tmp_path / "summarise_batch.txt")


def test_no_drift_when_every_value_is_defined(tmp_path, monkeypatch):
    text = "- news: things that happened\n- other: the rest\n- security: infosec\n"
    _prompt_dir(tmp_path, monkeypatch, text, text)
    config = {"llm": {"categories": ["news", "other"], "domains": ["security"]}}
    assert prompt_vocabulary_drift(config) == []


def test_a_renamed_category_is_reported(tmp_path, monkeypatch):
    text = "- news: things that happened\n- other: the rest\n"
    _prompt_dir(tmp_path, monkeypatch, text, text)
    config = {"llm": {"categories": ["news", "techniques"]}}

    messages = prompt_vocabulary_drift(config)

    assert len(messages) == 2  # one per prompt file
    assert all("techniques" in m for m in messages)


def test_a_value_appearing_only_in_prose_does_not_count(tmp_path, monkeypatch):
    """The check that a substring test would fail: 'methods' and 'other' occur
    in ordinary prose in these prompts, so mentioning a word is not the same as
    defining it as a value the model may return."""
    text = "- news: things that happened, including new methods and other stuff\n"
    _prompt_dir(tmp_path, monkeypatch, text, text)
    config = {"llm": {"categories": ["news", "methods", "other"]}}

    messages = prompt_vocabulary_drift(config)

    assert messages and all("methods" in m and "other" in m for m in messages)


def test_a_missing_prompt_file_is_not_reported_as_drift(tmp_path, monkeypatch):
    import src.summariser as s
    monkeypatch.setattr(s, "PROMPT_PATH", tmp_path / "gone.txt")
    monkeypatch.setattr(s, "BATCH_PROMPT_PATH", tmp_path / "also_gone.txt")
    assert prompt_vocabulary_drift({"llm": {"categories": ["news"]}}) == []
