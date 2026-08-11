"""Tests for src.routing: feeds declare their digests, and the derived mapping.

The bug these exist to prevent: delivery is an AND of category and feed, and for
a long time the two were written in separate files with nothing checking they
agreed. Nine feeds were each losing a whole category in silence.
"""

import pytest

from src.routing import (
    accepting_digests,
    apply_feed_routing,
    routing_matrix,
)


def _config(feeds, digests):
    return {"sources": {"rss": feeds}, "digests": digests}


def test_digest_sources_are_derived_from_the_feeds_that_name_it():
    config = _config(
        [
            {"name": "Krebs", "url": "u", "digests": ["Security Digest", "AI Security Digest"]},
            {"name": "Ars", "url": "u", "digests": ["AI News"]},
        ],
        [{"title": "Security Digest", "sections": ["news"]},
         {"title": "AI Security Digest", "sections": ["ai"]},
         {"title": "AI News", "sections": ["ai_general"]}],
    )
    assert apply_feed_routing(config) == []

    by_title = {d["title"]: d.get("sources") for d in config["digests"]}
    assert by_title["Security Digest"] == ["Krebs"]
    assert by_title["AI Security Digest"] == ["Krebs"]
    assert by_title["AI News"] == ["Ars"]


def test_a_feed_can_feed_several_digests():
    """Multiple assignment is a feature: one category each, so an item lands in
    at most one of them and there is no duplication."""
    config = _config(
        [{"name": "VentureBeat AI", "url": "u", "digests": ["AI Security Digest", "AI News"]}],
        [{"title": "AI Security Digest", "sections": ["ai"]},
         {"title": "AI News", "sections": ["ai_general"]}],
    )
    apply_feed_routing(config)

    assert accepting_digests(config, "VentureBeat AI", "ai") == ["AI Security Digest"]
    assert accepting_digests(config, "VentureBeat AI", "ai_general") == ["AI News"]
    # ...and a category neither digest accepts reaches nobody.
    assert accepting_digests(config, "VentureBeat AI", "news") == []


def test_an_empty_digest_list_means_delivered_nowhere():
    config = _config(
        [{"name": "Orphan", "url": "u", "digests": []},
         {"name": "Wired Up", "url": "u", "digests": ["D"]}],
        [{"title": "D", "sections": ["news"]}],
    )
    apply_feed_routing(config)

    assert config["digests"][0]["sources"] == ["Wired Up"]
    assert accepting_digests(config, "Orphan", "news") == []


def test_an_explicit_sources_list_wins_and_warns():
    """Two places declaring one mapping is the exact failure being removed, so
    it must not silently pick a winner."""
    config = _config(
        [{"name": "Krebs", "url": "u", "digests": ["D"]}],
        [{"title": "D", "sections": ["news"], "sources": ["Somebody Else"]}],
    )
    messages = apply_feed_routing(config)

    assert config["digests"][0]["sources"] == ["Somebody Else"]
    assert any("lists its own sources" in m for m in messages)


def test_routing_to_a_digest_that_does_not_exist_warns():
    config = _config(
        [{"name": "Krebs", "url": "u", "digests": ["Typo Digest"]}],
        [{"title": "Real Digest", "sections": ["news"]}],
    )
    messages = apply_feed_routing(config)

    assert any("Typo Digest" in m and "does not exist" in m for m in messages)


def test_instances_without_feed_routing_are_untouched():
    """An instance that has not migrated must behave exactly as before."""
    config = _config(
        [{"name": "Krebs", "url": "u"}],
        [{"title": "D", "sections": ["news"], "sources": ["Krebs"]}],
    )
    assert apply_feed_routing(config) == []
    assert config["digests"][0]["sources"] == ["Krebs"]


def test_matrix_reports_the_pair_that_reaches_no_digest():
    """The per-feed warning could not see this: the feed IS routed, but one of
    its categories has nowhere to go."""
    config = _config(
        [{"name": "Krebs", "url": "u", "digests": ["Security Digest"]}],
        [{"title": "Security Digest", "sections": ["news"]}],
    )
    config["llm"] = {"categories": ["news", "ai", "exclude"]}
    apply_feed_routing(config)

    matrix = routing_matrix(config)
    # 'exclude' is a decision, not a gap, so it is not a column.
    assert matrix["categories"] == ["ai", "news"]
    assert matrix["unroutable"] == [{"feed": "Krebs", "category": "ai"}]
    # The feed is routed, so it is not an orphan -- that distinction is the
    # whole reason the matrix exists.
    assert matrix["orphan_feeds"] == []


def test_matrix_flags_a_feed_that_reaches_nothing_at_all():
    config = _config(
        [{"name": "Orphan", "url": "u", "digests": []}],
        [{"title": "D", "sections": ["news"]}],
    )
    config["llm"] = {"categories": ["news", "exclude"]}
    apply_feed_routing(config)

    assert routing_matrix(config)["orphan_feeds"] == ["Orphan"]


def test_a_digest_with_no_sources_still_accepts_every_feed():
    """Pre-existing behaviour that the derivation must not change: an empty
    sources list means 'any feed', which is why silently dropping the routing
    on save would turn curated digests into one firehose."""
    config = _config([{"name": "Anything", "url": "u"}],
                     [{"title": "D", "sections": ["news"]}])
    assert accepting_digests(config, "Anything", "news") == ["D"]


def test_a_digest_no_feed_routes_to_receives_nothing_not_everything():
    """The trap this guards: an empty `sources` list already meant "accept every
    feed", so a derived-and-empty list would say the exact opposite of what it
    means -- an unrouted digest would take the whole firehose."""
    config = _config(
        [{"name": "Krebs", "url": "u", "digests": ["Security Digest"]}],
        [{"title": "Security Digest", "sections": ["news"]},
         {"title": "Nobody Routes Here", "sections": ["news"]}],
    )
    apply_feed_routing(config)

    assert accepting_digests(config, "Krebs", "news") == ["Security Digest"]


def test_delivery_and_the_matrix_agree():
    """main.py and the admin table must not drift apart -- they share
    accepts_feed() precisely so the displayed grid is the one that runs."""
    from src.routing import accepts_feed

    config = _config(
        [{"name": "Krebs", "url": "u", "digests": ["D"]},
         {"name": "Orphan", "url": "u", "digests": []}],
        [{"title": "D", "sections": ["news"]}],
    )
    apply_feed_routing(config)
    digest = config["digests"][0]

    for feed in ("Krebs", "Orphan"):
        delivered = accepts_feed(digest, feed) and "news" in digest["sections"]
        assert delivered == ("D" in accepting_digests(config, feed, "news"))


def test_admin_save_round_trips_routing(tmp_path, monkeypatch):
    """Load the sources editor, save it back untouched, and the routing must be
    identical. Dropping `digests` here would leave every digest with no sources,
    which does not error -- it silently means "accept every feed"."""
    monkeypatch.setenv("DIGEST_ADMIN_TOKEN", "t")
    monkeypatch.setenv("DIGEST_ROOT", str(tmp_path))

    import importlib
    import src.utils
    importlib.reload(src.utils)
    import src.web.app as web
    importlib.reload(web)

    (tmp_path / "config.yaml").write_text(
        "sources_file: sources.yaml\n"
        "digests:\n"
        "- title: D One\n  sections: [news]\n"
        "- title: D Two\n  sections: [ai]\n"
    )
    (tmp_path / "sources.yaml").write_text(
        "rss:\n"
        "- name: Feed A\n  url: https://a\n  digests: [D One, D Two]\n"
        "- name: Feed B\n  url: https://b\n  digests: []\n"
    )

    from fastapi.testclient import TestClient
    client = TestClient(web.app)
    head = {"X-Admin-Token": "t"}

    before = client.get("/admin/sources", headers=head).json()
    assert {f["name"]: f["digests"] for f in before["rss"]} == {
        "Feed A": ["D One", "D Two"], "Feed B": [],
    }

    saved = client.post("/admin/sources", headers=head, json={"rss": before["rss"]}).json()
    assert saved["ok"], saved

    after = client.get("/admin/sources", headers=head).json()
    assert {f["name"]: f["digests"] for f in after["rss"]} == \
           {f["name"]: f["digests"] for f in before["rss"]}


# Domains -----------------------------------------------------------------
# Once several digests share one section list, the section can no longer say
# which digest an item belongs to. Without a second field, a feed routed to two
# digests delivers every item to both -- as two separate emails.

def _domain_config():
    sections = ["news", "thought_leadership", "methods", "other"]
    return {
        "sources": {"rss": [
            {"name": "Krebs", "url": "u",
             "digests": ["Security Digest", "AI Security Digest", "AI News"]},
        ]},
        "llm": {"categories": sections + ["exclude"],
                "domains": ["security", "ai_security", "ai_ml"]},
        "digests": [
            {"title": "Security Digest", "domain": "security", "sections": sections},
            {"title": "AI Security Digest", "domain": "ai_security", "sections": sections},
            {"title": "AI News", "domain": "ai_ml", "sections": sections},
        ],
    }


def test_the_domain_keeps_shared_sections_apart():
    config = _domain_config()
    apply_feed_routing(config)

    assert accepting_digests(config, "Krebs", "news", "security") == ["Security Digest"]
    assert accepting_digests(config, "Krebs", "news", "ai_security") == ["AI Security Digest"]
    assert accepting_digests(config, "Krebs", "news", "ai_ml") == ["AI News"]


def test_no_item_can_reach_two_digests():
    """The failure this whole field exists to prevent: the same story arriving
    twice, in two different emails."""
    config = _domain_config()
    apply_feed_routing(config)

    for domain in config["llm"]["domains"]:
        for section in ["news", "thought_leadership", "methods", "other"]:
            hit = accepting_digests(config, "Krebs", section, domain)
            assert len(hit) <= 1, f"{domain}/{section} reached {hit}"


def test_without_a_domain_field_a_digest_accepts_any():
    """Backward compatibility: every instance behaved this way before domains,
    and a single-digest instance still wants it."""
    config = {
        "sources": {"rss": [{"name": "F", "url": "u", "digests": ["D"]}]},
        "digests": [{"title": "D", "sections": ["news"]}],
    }
    apply_feed_routing(config)

    assert accepting_digests(config, "F", "news", "anything") == ["D"]
    assert accepting_digests(config, "F", "news", None) == ["D"]


def test_matrix_switches_its_columns_to_the_routing_field():
    """With domains the digests share a section list, so category columns would
    be a grid of identical cells -- the domain is what actually routes."""
    config = _domain_config()
    apply_feed_routing(config)

    matrix = routing_matrix(config)
    assert matrix["axis"] == "domain"
    assert matrix["categories"] == ["ai_ml", "ai_security", "security"]
    assert matrix["unroutable"] == []


def test_a_feed_missing_the_digests_key_alongside_others_warns():
    """It contributes to nobody's list and so reaches nothing, which reads as an
    oversight rather than a decision -- exactly what this module exists to end."""
    config = _config(
        [{"name": "Declared", "url": "u", "digests": ["D"]},
         {"name": "Forgotten", "url": "u"}],
        [{"title": "D", "sections": ["news"]}],
    )
    messages = apply_feed_routing(config)

    assert any("Forgotten" in m and "delivered nowhere" in m for m in messages)
