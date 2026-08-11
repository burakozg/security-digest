"""Tests for clustering several reports of one event into a single digest item.

The response can't be positional here -- N items in, fewer out -- so the model
says which indices it merged, and everything below guards the arithmetic of that.
"""

from unittest.mock import patch

from src.digest import render_markdown
from src.summariser import (
    _assign_clusters,
    _cluster_all,
    _format_item_for_cluster,
    _merge_cluster,
)

ITEMS = [
    {"title": "Naver Unveils AI Factory Deal Structure with Nvidia, Brookfield",
     "link": "https://sedaily.com/1", "publisher": "Seoul Economic Daily",
     "source": "Nvidia", "published": "2026-08-03T10:00:00", "description": ""},
    {"title": "Naver Establishes Subsidiary for AI Factory Operation",
     "link": "https://chosun.com/2", "publisher": "조선일보",
     "source": "Nvidia", "published": "2026-08-03T09:00:00", "description": ""},
    {"title": "Naver secures Nvidia, Brookfield for AI factory funding",
     "link": "https://techinasia.com/3", "publisher": "Tech in Asia",
     "source": "Nvidia", "published": "2026-08-03T11:00:00", "description": ""},
]

DEAL = {"title": "Naver structures AI factory deal with Nvidia and Brookfield",
        "summary": "Naver will receive Nvidia GPUs.", "category": "key", "members": [0, 2]}
NEWS_CONFIG = {"llm": {"categories": ["key", "notable", "mention", "exclude"],
                       "fallback_category": "mention"}}

SUBSIDIARY = {"title": "Naver sets up AI factory subsidiary",
              "summary": "A new subsidiary will run the facility.", "category": "notable",
              "members": [1]}


def test_a_cluster_keeps_every_members_link():
    """The link list is the point: it's what credits each outlet and lets the
    reader check a second account."""
    merged = _merge_cluster(ITEMS, [0, 2], DEAL, NEWS_CONFIG)
    assert [s["publisher"] for s in merged["links"]] == ["Seoul Economic Daily", "Tech in Asia"]
    assert [s["link"] for s in merged["links"]] == ["https://sedaily.com/1", "https://techinasia.com/3"]


def test_a_cluster_takes_the_newest_publication_date():
    """The story is as recent as its freshest report."""
    assert _merge_cluster(ITEMS, [0, 2], DEAL, NEWS_CONFIG)["published"] == "2026-08-03T11:00:00"


def test_a_cluster_keeps_a_single_primary_link_for_history():
    merged = _merge_cluster(ITEMS, [0, 2], DEAL, NEWS_CONFIG)
    assert merged["link"] == "https://sedaily.com/1"


def test_duplicate_links_within_a_cluster_are_collapsed():
    items = [dict(ITEMS[0]), dict(ITEMS[0])]
    assert len(_merge_cluster(items, [0, 1], DEAL, NEWS_CONFIG)["links"]) == 1


def test_assign_clusters_merges_and_separates_as_instructed():
    out = _assign_clusters(ITEMS, [DEAL, SUBSIDIARY], {})
    assert len(out) == 2
    assert len(out[0]["links"]) == 2
    assert len(out[1]["links"]) == 1


def test_an_item_the_model_forgot_is_kept_not_dropped():
    """An item silently dropped here is news the reader never sees, so anything
    unclaimed becomes its own single-item cluster."""
    out = _assign_clusters(ITEMS, [DEAL], {})  # index 1 omitted
    assert len(out) == 2
    assert any(o["title"] == ITEMS[1]["title"] for o in out)


def test_an_item_claimed_twice_lands_in_one_cluster_only():
    greedy = {"title": "Everything", "summary": "s", "category": "key", "members": [0, 1, 2]}
    out = _assign_clusters(ITEMS, [DEAL, greedy], {})
    assert sum(len(o["links"]) for o in out) == 3
    assert len(out) == 2


def test_out_of_range_indices_are_ignored():
    bad = {"title": "T", "summary": "s", "category": "key", "members": [0, 99, -1]}
    out = _assign_clusters(ITEMS, [bad], {})
    assert len(out[0]["links"]) == 1


def test_clustering_never_merges_across_topics():
    """`source` is what digests route on: merging two topics' items would deliver
    the story to whichever recipient the survivor belonged to and deny it to the
    other."""
    items = [dict(ITEMS[0]), {**ITEMS[2], "source": "Vattenfall"}]
    calls = []

    def fake(group, client, config):
        calls.append([i["source"] for i in group])
        return group

    with patch("src.summariser.cluster_topic", side_effect=fake):
        _cluster_all(items, None, {})

    assert sorted(calls) == [["Nvidia"], ["Vattenfall"]]


def test_render_lists_every_source_when_an_item_was_clustered():
    item = {**_merge_cluster(ITEMS, [0, 2], DEAL, NEWS_CONFIG), "category": "key"}
    out = render_markdown([item], {}, {"title": "W", "sections": ["key"]})
    assert "[Seoul Economic Daily](https://sedaily.com/1)" in out
    assert "[Tech in Asia](https://techinasia.com/3)" in out
    assert " · " in out


def test_render_falls_back_to_one_publisher_when_not_clustered():
    item = {"title": "T", "link": "https://x.com/1", "source": "Nvidia",
            "publisher": "Reuters", "summary": "S", "category": "key"}
    out = render_markdown([item], {}, {"title": "W", "sections": ["key"]})
    assert "*Reuters*" in out


def test_the_index_the_model_sees_is_the_list_offset():
    """Regression guard for a bug that produced entirely plausible output: the
    batch formatter labels the first item "Item 1", but `members` comes back as
    list offsets. Reusing that 1-based label put every summary on the wrong
    article's link, and nothing looked broken until the links were opened."""
    for i, item in enumerate(ITEMS):
        assert _format_item_for_cluster(item, i).startswith(f"INDEX: {i}\n")
    assert _format_item_for_cluster(ITEMS[0], 0).startswith("INDEX: 0")


def test_an_unassigned_item_gets_a_category_this_instance_actually_delivers():
    """The fallback must come from the instance's own vocabulary. Defaulting to
    "other" on a topic instance ([key, notable, mention, exclude]) puts the item
    in a section no digest lists, so it is silently dropped -- the exact failure
    the unassigned-item safety net exists to prevent."""
    config = {"llm": {"categories": ["key", "notable", "mention", "exclude"],
                      "fallback_category": "mention"}}
    out = _assign_clusters(ITEMS, [DEAL], config)
    orphan = next(o for o in out if o["title"] == ITEMS[1]["title"])
    assert orphan["category"] == "mention"
