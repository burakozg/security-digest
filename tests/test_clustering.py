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


# --- clustering across feeds, in two passes ---------------------------------
# A publisher instance has the opposite problem to a topic instance. There
# `source` is the outlet, so grouping per source puts the duplicates worth
# merging into different groups by construction and clustering achieves nothing.

import pytest

from src.summariser import (
    _cluster_groups,
    _combined_description,
    _summarise_in_batches,
    _trimmed_for_grouping,
    cluster_chars,
    cluster_scope,
    group_stories,
)

SEC_CONFIG = {
    "llm": {"categories": ["news", "other", "exclude"], "fallback_category": "other",
            "domains": ["security", "ai_ml"], "fallback_domain": "security",
            "cluster": True, "cluster_scope": "all", "cluster_chars": 800,
            "batch_size": 8},
    "sources": {"max_description_chars": 5000},
}


def _sec_items():
    return [
        {"title": "Critical RCE in Acme VPN", "link": "https://krebs/1", "source": "Krebs",
         "publisher": "Krebs", "published": "2026-08-25T10:00:00",
         "description": "Krebs on the Acme VPN flaw. " + "k" * 2000},
        {"title": "Acme patches actively exploited flaw", "link": "https://bc/2",
         "source": "Bleeping Computer", "publisher": "Bleeping Computer",
         "published": "2026-08-25T11:00:00",
         "description": "Bleeping adds the patch detail. " + "b" * 2000},
        {"title": "CISA adds Ivanti flaw to KEV", "link": "https://thn/3",
         "source": "The Hacker News", "publisher": "The Hacker News",
         "published": "2026-08-25T09:00:00", "description": "Unrelated. " + "t" * 2000},
    ]


def test_grouping_per_source_cannot_merge_across_outlets():
    """The reason `llm.cluster: true` alone does nothing on a publisher
    instance: each outlet is clustered alone, so the cross-outlet duplicates
    never meet."""
    groups = _cluster_groups(_sec_items(), {"llm": {}})
    assert sorted(groups) == ["Bleeping Computer", "Krebs", "The Hacker News"]
    assert all(len(g) == 1 for g in groups.values())


def test_cluster_scope_all_puts_every_feed_in_one_group():
    groups = _cluster_groups(_sec_items(), SEC_CONFIG)
    assert len(groups) == 1 and len(next(iter(groups.values()))) == 3


def test_an_unknown_scope_falls_back_to_per_source(caplog):
    """Never silently widen what may be merged on a bad value."""
    with caplog.at_level("WARNING"):
        assert cluster_scope({"llm": {"cluster_scope": "everything"}}) == "source"
    assert "Unknown llm.cluster_scope" in caplog.text


@pytest.mark.parametrize("raw,expected", [
    (None, None), (0, None), (-5, None), ("nonsense", None), (800, 800), ("800", 800),
])
def test_cluster_chars_is_read_defensively(raw, expected):
    assert cluster_chars({"llm": {"cluster_chars": raw}}) == expected


def test_the_grouping_call_sees_trimmed_text_but_the_items_keep_theirs():
    items = _sec_items()
    trimmed = _trimmed_for_grouping(items, 50)
    assert all(len(t["description"]) == 50 for t in trimmed)
    assert all(len(i["description"]) > 1000 for i in items), "originals must be untouched"


def test_a_merged_story_pools_every_outlets_text_for_the_summary():
    """Each outlet carries detail the others left out, which is most of the
    reason for merging them. Summarising only the first report discards it."""
    items = _sec_items()
    merged = _merge_cluster(items, [0, 1], {"title": "T", "summary": "s", "category": "news"},
                            SEC_CONFIG, pool_descriptions=5000)
    assert "Krebs on the Acme VPN flaw" in merged["description"]
    assert "Bleeping adds the patch detail" in merged["description"]
    assert "[Krebs]" in merged["description"] and "[Bleeping Computer]" in merged["description"]


def test_pooling_respects_the_budget():
    items = _sec_items()
    merged = _merge_cluster(items, [0, 1], {"title": "T", "summary": "s", "category": "news"},
                            SEC_CONFIG, pool_descriptions=120)
    assert len(merged["description"]) <= 120 + len("\n\n")


def test_a_single_member_story_keeps_its_own_text_unpooled():
    items = _sec_items()
    merged = _merge_cluster(items, [2], {"title": "T", "summary": "s", "category": "news"},
                            SEC_CONFIG, pool_descriptions=5000)
    assert merged["description"] == items[2]["description"]


def test_combined_description_skips_empty_members():
    out = _combined_description(
        [{"description": "", "publisher": "A"}, {"description": "real", "publisher": "B"}], 500)
    assert out == "[B] real"


def test_the_grouping_call_asks_for_groups_only_not_prose():
    """Headlines and summaries per cluster would be written and then thrown
    away by the summarising pass, and output tokens are the expensive half."""
    seen = {}

    def fake(client, config, prompt, schema, kind="summarise"):
        seen["schema"] = schema
        seen["prompt"] = prompt
        return '{"clusters": [{"members": [0, 1]}, {"members": [2]}]}'

    with patch("src.summariser._call_llm", side_effect=fake), \
         patch("src.summariser.render_template", side_effect=lambda p, **k: k["items"]):
        merged = group_stories(_sec_items(), object(), SEC_CONFIG, 800)

    props = seen["schema"]["properties"]["clusters"]["items"]["properties"]
    assert list(props) == ["members"]
    assert "summary" not in props and "title" not in props
    # And it was shown the trimmed text, not the full 2000-character articles.
    assert len(seen["prompt"]) < 4000
    assert [len(m.get("links", [])) for m in merged] == [2, 1]


def test_two_pass_summarises_merged_stories_from_the_full_text():
    calls = []

    def fake(client, config, prompt, schema, kind="summarise"):
        calls.append(kind)
        if kind == "cluster":
            return '{"clusters": [{"members": [0, 1]}, {"members": [2]}]}'
        return ('[{"summary": "Merged writeup.", "category": "news", "domain": "security"},'
                ' {"summary": "Other writeup.", "category": "news", "domain": "security"}]')

    with patch("src.summariser._call_llm", side_effect=fake), \
         patch("src.summariser._get_client", return_value=object()), \
         patch("src.summariser.render_template", side_effect=lambda p, **k: k.get("items", "")):
        out = _cluster_all(_sec_items(), object(), SEC_CONFIG)

    assert calls == ["cluster", "batch"], "one grouping call, then one summarising batch"
    assert len(out) == 2
    merged = next(o for o in out if len(o["links"]) == 2)
    assert merged["summary"] == "Merged writeup."
    # The merge survives the summarising pass -- both outlets still credited.
    assert [l["publisher"] for l in merged["links"]] == ["Krebs", "Bleeping Computer"]


def test_a_failed_grouping_call_still_delivers_the_day_unmerged():
    """Unmerged reads as duplicates, which the reader can skim. Re-raising would
    read as silence."""
    def fake(client, config, prompt, schema, kind="summarise"):
        if kind == "cluster":
            raise RuntimeError("model unavailable")
        return ('[{"summary": "s", "category": "news", "domain": "security"},'
                ' {"summary": "s", "category": "news", "domain": "security"},'
                ' {"summary": "s", "category": "news", "domain": "security"}]')

    with patch("src.summariser._call_llm", side_effect=fake), \
         patch("src.summariser._get_client", return_value=object()), \
         patch("src.summariser.render_template", side_effect=lambda p, **k: k.get("items", "")):
        out = _cluster_all(_sec_items(), object(), SEC_CONFIG)

    assert len(out) == 3 and all(o["summary"] == "s" for o in out)


def test_one_pass_clustering_is_unchanged_when_cluster_chars_is_unset():
    """The topic instance must keep behaving exactly as before."""
    calls = []

    def fake(client, config, prompt, schema, kind="summarise"):
        calls.append(kind)
        return ('{"clusters": [{"title": "T", "summary": "s", "category": "key",'
                ' "members": [0, 1, 2]}]}')

    with patch("src.summariser._call_llm", side_effect=fake), \
         patch("src.summariser._get_client", return_value=object()), \
         patch("src.summariser.render_template", side_effect=lambda p, **k: k.get("items", "")):
        out = _cluster_all(ITEMS, object(), NEWS_CONFIG)

    assert calls == ["cluster"], "no second pass"
    assert len(out) == 1 and out[0]["summary"] == "s"


def test_the_grouping_pass_treats_unlisted_items_as_standing_alone(caplog):
    """The grouping prompt asks for groups of two or more, so most items are
    absent from the response by design. Warning about that every run would be
    noise -- and would bury the case where a one-pass cluster really did drop
    something."""
    items = _sec_items()
    with caplog.at_level("WARNING"):
        out = _assign_clusters(items, [{"members": [0, 1]}], SEC_CONFIG,
                               singletons_implicit=True)
    assert len(out) == 2
    assert "unassigned" not in caplog.text


def test_one_pass_clustering_still_warns_about_a_dropped_item():
    """There an omission is a real fault: the item loses the summary and
    category the same call was supposed to give it."""
    caplog_text = []
    import logging

    class _Grab(logging.Handler):
        def emit(self, record):
            caplog_text.append(record.getMessage())

    logger = logging.getLogger("src.summariser")
    handler = _Grab(level=logging.WARNING)
    logger.addHandler(handler)
    try:
        _assign_clusters(_sec_items(), [{"members": [0]}], SEC_CONFIG)
    finally:
        logger.removeHandler(handler)
    assert any("unassigned" in m for m in caplog_text)


def test_a_day_with_no_duplicates_returns_every_item_untouched():
    out = _assign_clusters(_sec_items(), [], SEC_CONFIG, singletons_implicit=True)
    assert len(out) == 3 and all(len(o["links"]) == 1 for o in out)
