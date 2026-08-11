"""Tests for what src.main.run() does with items no digest accepts.

Before this, an unroutable item was summarised at full token cost, delivered
nowhere, logged nowhere, and then marked seen -- so it was gone for a fortnight
and nothing said it had ever existed. On 2026-08-11 that was 20 of 40 items.
"""

import logging

import pytest

import src.main as main


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """run() with the network, the LLM and delivery replaced, and its own db."""
    db = tmp_path / "digest.db"
    monkeypatch.setattr(main, "get_connection_path", None, raising=False)

    state = {"delivered": [], "seen": []}

    monkeypatch.setattr(main, "build_digest", lambda items, config, d: "body")
    monkeypatch.setattr(main, "deliver",
                        lambda content, config, title=None, digest_cfg=None:
                            state["delivered"].append(title))
    monkeypatch.setattr(main, "record_sent", lambda items, title, config: None)
    monkeypatch.setattr(main, "update_status", lambda *a, **k: None)
    monkeypatch.setattr(main, "dedupe_content", lambda items, **k: items)
    monkeypatch.setattr(main, "filter_new", lambda items, **k: items)
    monkeypatch.setattr(main, "mark_seen",
                        lambda items, **k: state["seen"].extend(i["link"] for i in items))
    return state


def _config():
    return {
        "sources": {"rss": [
            {"name": "Krebs", "url": "u", "digests": ["Security Digest"]},
        ]},
        "digests": [{"title": "Security Digest", "sections": ["news"], "sources": ["Krebs"]}],
        "llm": {"categories": ["news", "ai", "exclude"]},
    }


def _run(monkeypatch, items, summarised):
    monkeypatch.setattr(main, "load_config", lambda *a, **k: _config())
    monkeypatch.setattr(main, "fetch_all", lambda config: items)
    monkeypatch.setattr(main, "summarise_all", lambda items, config: summarised)
    return main.run()


def test_unroutable_items_are_logged_with_their_feed_and_category(wired, monkeypatch, caplog):
    items = [{"link": "a", "source": "Krebs", "title": "A"},
             {"link": "b", "source": "Krebs", "title": "B"}]
    summarised = [
        {**items[0], "category": "news", "summary": "s"},   # delivered
        {**items[1], "category": "ai", "summary": "s"},     # no digest takes ai
    ]

    with caplog.at_level(logging.WARNING, logger="src.main"):
        _run(monkeypatch, items, summarised)

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "1 item(s) matched no digest" in text
    assert "Krebs" in text and "ai" in text


def test_only_delivered_items_are_marked_seen(wired, monkeypatch):
    """An item dropped by a routing gap must come back once the gap is closed."""
    items = [{"link": "a", "source": "Krebs", "title": "A"},
             {"link": "b", "source": "Krebs", "title": "B"}]
    summarised = [
        {**items[0], "category": "news", "summary": "s"},
        {**items[1], "category": "ai", "summary": "s"},
    ]

    _run(monkeypatch, items, summarised)

    assert wired["seen"] == ["a"]


def test_excluded_items_are_still_marked_seen(wired, monkeypatch):
    """'exclude' is a decision the model was asked to make, not a routing gap --
    re-summarising it every run would pay for the same answer forever."""
    items = [{"link": "a", "source": "Krebs", "title": "A"}]
    summarised = [{**items[0], "category": "exclude", "summary": "s"}]

    _run(monkeypatch, items, summarised)

    assert wired["seen"] == ["a"]


def test_every_member_of_a_delivered_cluster_is_marked_seen(wired, monkeypatch):
    """Clustering merges several fetched items into one delivered item, so
    matching on identity (or on the primary link alone) would leave the other
    members unseen and re-deliver them tomorrow."""
    items = [{"link": "a", "source": "Krebs", "title": "A"},
             {"link": "b", "source": "Krebs", "title": "B"}]
    merged = {"link": "a", "source": "Krebs", "title": "A", "category": "news",
              "summary": "s", "links": [{"publisher": "p", "link": "a"},
                                        {"publisher": "q", "link": "b"}]}

    _run(monkeypatch, items, [merged])

    assert sorted(wired["seen"]) == ["a", "b"]


def _domain_config():
    sections = ["news", "methods"]
    return {
        "sources": {"rss": [{"name": "Krebs", "url": "u",
                             "digests": ["Security Digest", "AI Security Digest"]}]},
        "llm": {"categories": sections + ["exclude"],
                "domains": ["security", "ai_security"]},
        "digests": [
            {"title": "Security Digest", "domain": "security", "sections": sections,
             "sources": ["Krebs"]},
            {"title": "AI Security Digest", "domain": "ai_security", "sections": sections,
             "sources": ["Krebs"]},
        ],
    }


def test_one_story_is_delivered_to_exactly_one_digest(wired, monkeypatch):
    """Both digests list Krebs and share a section list, so without the domain
    this item would go out twice, in two separate emails."""
    monkeypatch.setattr(main, "load_config", lambda *a, **k: _domain_config())
    items = [{"link": "a", "source": "Krebs", "title": "Prompt injection in the wild"}]
    monkeypatch.setattr(main, "fetch_all", lambda config: items)
    monkeypatch.setattr(main, "summarise_all", lambda items, config: [
        {**items[0], "category": "news", "domain": "ai_security", "summary": "s"},
    ])

    main.run()

    assert wired["delivered"] == ["AI Security Digest"]
