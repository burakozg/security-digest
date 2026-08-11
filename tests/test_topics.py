"""Tests for src.topics -- URL construction and the feed-quirk cleanups. No network."""

from urllib.parse import parse_qs, urlparse

from src.topics import (
    clean_link,
    expand_topics,
    publisher_from_link,
    strip_html,
    strip_title_suffix,
)


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


def test_expand_topics_builds_one_feed_per_engine_per_query():
    feeds = expand_topics([
        {"name": "Acme", "queries": ['"Acme"', "Acme lawsuit"], "lang": "en", "country": "GB"}
    ])
    assert len(feeds) == 4
    assert {f["name"] for f in feeds} == {"Acme"}


def test_expand_topics_targets_the_configured_market():
    """hl/gl/ceid and setmkt must agree with the topic's language and country --
    without them the engines localise from the outbound IP, which for a NAS on a
    home connection has nothing to do with what language the topic is in."""
    feeds = expand_topics([{"name": "Vattenfall", "lang": "sv", "country": "SE"}])
    google = next(f for f in feeds if "news.google.com" in f["url"])
    bing = next(f for f in feeds if "bing.com" in f["url"])

    assert _query(google["url"])["hl"] == ["sv"]
    assert _query(google["url"])["gl"] == ["SE"]
    assert _query(google["url"])["ceid"] == ["SE:sv"]
    assert _query(bing["url"])["setmkt"] == ["sv-SE"]


def test_expand_topics_defaults_query_to_name_and_applies_window():
    feeds = expand_topics([{"name": "Acme"}])
    google = next(f for f in feeds if "news.google.com" in f["url"])
    assert _query(google["url"])["q"] == ["Acme when:2d"]


def test_expand_topics_carries_context_onto_every_feed():
    """The context is what lets the summariser reject same-name-different-subject
    hits, so it has to reach the items, not just live in topics.yaml."""
    feeds = expand_topics([{"name": "Acme", "context": "The anvil maker."}])
    assert feeds
    assert all(f["topic_context"] == "The anvil maker." for f in feeds)


def test_expand_topics_honours_engine_selection():
    feeds = expand_topics([{"name": "Acme", "engines": ["google"]}])
    assert len(feeds) == 1
    assert "news.google.com" in feeds[0]["url"]


def test_expand_topics_skips_unnamed_topics():
    """A topic with no name can't be routed to any digest, so it's dropped rather
    than fetched into a digest nothing lists."""
    assert expand_topics([{"queries": ["orphan"]}]) == []
    assert expand_topics(None) == []


def test_clean_link_unwraps_bing_redirect():
    link = (
        "http://www.bing.com/news/apiclick.aspx?ref=FexRss&aid=&tid=abc"
        "&url=https%3A%2F%2Fwww.aftonbladet.se%2Fa%2Fxyz&c=1"
    )
    assert clean_link(link) == "https://www.aftonbladet.se/a/xyz"


def test_clean_link_leaves_google_redirect_alone():
    """Google's token is opaque and only resolves in a browser. Guessing at it
    would turn a working link into a dead one."""
    link = "https://news.google.com/rss/articles/CBMiYEFVX3lxTFAw?oc=5"
    assert clean_link(link) == link


def test_clean_link_passes_through_ordinary_urls():
    assert clean_link("https://krebsonsecurity.com/post") == "https://krebsonsecurity.com/post"
    assert clean_link("") == ""


def test_publisher_from_link_uses_host_without_www():
    assert publisher_from_link("https://www.nyteknik.se/a/b") == "nyteknik.se"


def test_publisher_from_link_returns_empty_for_aggregators():
    """An unresolved aggregator link says nothing about who published the piece,
    so the caller must fall back rather than credit 'news.google.com'."""
    assert publisher_from_link("https://news.google.com/rss/articles/CBMi") == ""
    assert publisher_from_link("http://www.bing.com/news/apiclick.aspx?x=1") == ""
    assert publisher_from_link("") == ""


def test_strip_html_flattens_markup_and_entities():
    raw = '<a href="x">Rolls-Royce SMR &amp; Vattenfall</a>&nbsp;&nbsp;<font>Ny Teknik</font>'
    assert strip_html(raw) == "Rolls-Royce SMR & Vattenfall Ny Teknik"


def test_strip_html_leaves_plain_text_untouched():
    assert strip_html("Just a sentence.") == "Just a sentence."
    assert strip_html("") == ""


def test_strip_title_suffix_removes_a_matching_publisher():
    assert strip_title_suffix(
        "AI is rewriting the software - Business Insider", "Business Insider"
    ) == "AI is rewriting the software"


def test_strip_title_suffix_is_case_insensitive():
    assert strip_title_suffix("Story - Ny Teknik", "ny teknik") == "Story"


def test_strip_title_suffix_leaves_non_matching_dash_clauses_alone():
    """Plenty of real headlines end in a dash clause; cutting those would corrupt
    the headline."""
    title = "Quantum computers - and why they matter"
    assert strip_title_suffix(title, "Reuters") == title


def test_strip_title_suffix_needs_a_known_publisher():
    title = "Story - Business Insider"
    assert strip_title_suffix(title, "") == title


def test_publisher_from_link_keeps_syndicators():
    """msn.com is where the link actually goes, so naming it is honest -- the
    alternative is falling back to the topic name and claiming Nvidia published a
    story about Nvidia."""
    assert publisher_from_link("https://www.msn.com/en-us/money/x") == "msn.com"
