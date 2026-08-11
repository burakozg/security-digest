"""Tests for fair trimming and the topic-feed additions to fetch_all.

The failure this guards against is quiet: with a global sort-and-truncate, one
heavily-covered topic's items are all newer than everyone else's and fill the
whole budget, so another reader's digest arrives empty with nothing in the logs
to explain it.
"""

import datetime
from unittest.mock import patch

from src.fetcher import fetch_all
from src.settings import warn_on_unwired_sources


def _item(source, published, title=None):
    return {
        "title": title or f"{source} {published}",
        "link": f"https://example.com/{source}/{published}",
        "source": source,
        "description": "",
        "published": published,
    }


def _config(max_total, fair_trim, **sources):
    return {
        "sources": {
            "rss": [
                {"name": "Loud", "url": "https://example.com/loud"},
                {"name": "Quiet", "url": "https://example.com/quiet"},
            ],
            "max_items_per_source": 20,
            "max_total_items": max_total,
            "fair_trim": fair_trim,
            **sources,
        }
    }


# One feed publishes constantly and recently; the other publishes rarely and
# older. Every Loud item is newer than every Quiet item.
LOUD = [_item("Loud", f"2026-08-03T{h:02d}:00:00") for h in range(10)]
QUIET = [_item("Quiet", "2026-08-01T09:00:00"), _item("Quiet", "2026-08-01T08:00:00")]


def _fetch(config):
    def fake(url, name, **kwargs):
        return list(LOUD) if name == "Loud" else list(QUIET)

    with patch("src.fetcher.fetch_feed", side_effect=fake):
        return fetch_all(config)


def test_without_fair_trim_the_loud_feed_starves_the_quiet_one():
    """Documents the old behaviour, which is still correct for a single-reader
    instance and is why the flag defaults to off."""
    result = _fetch(_config(max_total=4, fair_trim=False))
    assert [i["source"] for i in result] == ["Loud"] * 4


def test_fair_trim_guarantees_every_feed_is_represented():
    result = _fetch(_config(max_total=4, fair_trim=True))
    assert {i["source"] for i in result} == {"Loud", "Quiet"}
    assert sum(1 for i in result if i["source"] == "Quiet") == 2


def test_fair_trim_still_honours_the_total_cap():
    result = _fetch(_config(max_total=3, fair_trim=True))
    assert len(result) == 3
    assert "Quiet" in {i["source"] for i in result}


def test_fair_trim_takes_newest_first_within_each_feed():
    result = _fetch(_config(max_total=2, fair_trim=True))
    loud = [i for i in result if i["source"] == "Loud"]
    assert loud[0]["published"] == "2026-08-03T09:00:00"


def test_fair_trim_does_not_pad_when_a_feed_runs_out():
    """A one-item feed must not truncate the round-robin for everyone else."""
    result = _fetch(_config(max_total=50, fair_trim=True))
    assert len(result) == len(LOUD) + len(QUIET)


def test_max_age_days_drops_stale_items():
    """News search returns results of any age, unlike publisher feeds.

    Dates are relative to now, not literals: an absolute "recent" timestamp is
    only recent on the day it was written, and this test duly started failing
    the moment the date rolled over."""
    recent = (datetime.datetime.now() - datetime.timedelta(hours=2)).isoformat()
    ancient = (datetime.datetime.now() - datetime.timedelta(days=400)).isoformat()
    config = _config(max_total=50, fair_trim=True, max_age_days=1)
    with patch("src.fetcher.fetch_feed", side_effect=lambda url, name, **kw: [
        _item(name, recent, "recent"),
        _item(name, ancient, "ancient"),
    ]):
        result = fetch_all(config)
    assert result  # not everything dropped
    assert all(i["title"] != "ancient" for i in result)
    assert any(i["title"] == "recent" for i in result)


def test_undated_items_survive_the_age_filter():
    """An undated item is far more likely a feed that omits the field than one
    that is genuinely ancient; the seen-store stops it being delivered twice."""
    config = _config(max_total=50, fair_trim=True, max_age_days=1)
    with patch("src.fetcher.fetch_feed", side_effect=lambda url, name, **kw: [
        _item(name, "", "undated")
    ]):
        result = fetch_all(config)
    assert [i["title"] for i in result] == ["undated", "undated"]


def test_topic_feeds_are_fetched_alongside_rss():
    """Topic feeds live in their own config key so the admin panel can't freeze
    generated URLs into sources_overrides.yaml, but fetch_all must still read
    both lists."""
    config = {
        "sources": {
            "rss": [{"name": "Feed", "url": "https://example.com/feed"}],
            "topic_feeds": [
                {"name": "Acme", "url": "https://news.google.com/x", "topic_context": "The anvil maker."}
            ],
            "max_total_items": 50,
        }
    }
    seen = {}

    def fake(url, name, **kwargs):
        seen[name] = kwargs.get("search_feed"), kwargs.get("extra")
        return []

    with patch("src.fetcher.fetch_feed", side_effect=fake):
        fetch_all(config)

    assert set(seen) == {"Feed", "Acme"}
    assert seen["Feed"] == (False, None)
    assert seen["Acme"] == (True, {"topic_context": "The anvil maker."})


def test_warns_when_a_digest_names_an_unknown_source():
    """Routing matches on name strings, so a typo silently yields an empty
    digest rather than an error."""
    config = {
        "sources": {"rss": [{"name": "Acme", "url": "https://example.com"}]},
        "digests": [{"title": "Watch", "sources": ["Acme", "Amce"]}],
    }
    messages = warn_on_unwired_sources(config)
    assert any("'Amce'" in m and "matches no feed or topic" in m for m in messages)
    assert not any("'Acme'" in m for m in messages)


def test_warns_when_a_topic_reaches_no_digest():
    """A topic nothing routes to is fetched and summarised for nobody: real LLM
    spend, no output."""
    config = {
        "sources": {"topic_feeds": [
            {"name": "Acme", "url": "https://x"},
            {"name": "Orphan", "url": "https://y"},
        ]},
        "digests": [{"title": "Watch", "sources": ["Acme"]}],
    }
    messages = warn_on_unwired_sources(config)
    assert any("'Orphan'" in m and "never delivered" in m for m in messages)


def test_no_unrouted_warning_when_a_digest_takes_everything():
    """A digest with no `sources` is unrestricted, so nothing is unrouted."""
    config = {
        "sources": {"rss": [{"name": "Acme", "url": "https://x"}]},
        "digests": [{"title": "Everything", "sections": ["news"]}],
    }
    assert warn_on_unwired_sources(config) == []
