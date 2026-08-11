"""Tests for src.digest: section grouping and markdown rendering, including the
XSS-escaping behavior added in task 1.2."""

from src.digest import build_digest, group_by_section, render_markdown


def test_group_by_section_orders_by_config():
    items = [
        {"category": "other", "title": "C"},
        {"category": "news", "title": "A"},
        {"category": "thought_leadership", "title": "B"},
    ]
    grouped = group_by_section(items, ["news", "thought_leadership", "other"])
    assert list(grouped.keys()) == ["news", "thought_leadership", "other"]
    assert grouped["news"][0]["title"] == "A"


def test_group_by_section_unknown_category_falls_back_to_other():
    items = [{"category": "made_up_category", "title": "X"}]
    grouped = group_by_section(items, ["news", "thought_leadership"])
    assert "other" in grouped
    assert grouped["other"][0]["title"] == "X"


def test_group_by_section_omits_empty_sections():
    items = [{"category": "news", "title": "A"}]
    grouped = group_by_section(items, ["news", "thought_leadership", "other"])
    assert "thought_leadership" not in grouped


def test_render_markdown_escapes_html_and_neutralises_bad_link():
    items = [{
        "title": "<img src=x onerror=alert(1)> [x](y)",
        "link": "javascript:alert(1)",
        "source": "<script>alert(2)</script>Krebs",
        "summary": "Normal summary <script>alert(3)</script>",
        "category": "news",
    }]
    digest_cfg = {"title": "Test Digest", "sections": ["news"]}
    md = render_markdown(items, {}, digest_cfg)

    assert "<img" not in md
    assert "<script>alert" not in md
    assert "javascript:" not in md
    assert "&lt;img src=x onerror=alert(1)&gt;" in md
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in md
    assert "&lt;script&gt;alert(3)&lt;/script&gt;" in md


def test_render_markdown_preserves_legitimate_link():
    items = [{
        "title": "Real Story",
        "link": "https://example.com/story",
        "source": "Krebs",
        "summary": "A real summary.",
        "category": "news",
    }]
    md = render_markdown(items, {}, {"title": "Digest", "sections": ["news"]})
    assert "[Real Story](https://example.com/story)" in md


def test_render_markdown_drops_non_http_link_but_keeps_title():
    items = [{
        "title": "Suspicious Item",
        "link": "javascript:alert(1)",
        "source": "Feed",
        "summary": "Summary.",
        "category": "news",
    }]
    md = render_markdown(items, {}, {"title": "Digest", "sections": ["news"]})
    assert "javascript:" not in md
    assert "Suspicious Item" in md
    assert "[Suspicious Item]" not in md  # not rendered as a link at all


def test_build_digest_is_render_markdown_alias():
    items = [{"title": "T", "link": "https://x.com", "source": "S", "summary": "Sum", "category": "news"}]
    cfg = {"title": "D", "sections": ["news"]}
    assert build_digest(items, {}, cfg) == render_markdown(items, {}, cfg)


def test_section_labels_can_be_overridden_per_digest():
    """A digest with its own category vocabulary supplies its own headings; the
    built-in map only covers the security ones."""
    items = [{"title": "T", "link": "https://x.com/1", "source": "Acme",
              "summary": "S", "category": "key"}]
    digest_cfg = {"title": "Topic Watch", "sections": ["key"],
                  "labels": {"key": "Worth knowing"}}
    out = render_markdown(items, {}, digest_cfg)
    assert "## Worth knowing" in out


def test_unlabelled_sections_still_render_title_cased():
    items = [{"title": "T", "link": "https://x.com/1", "source": "Acme",
              "summary": "S", "category": "notable"}]
    out = render_markdown(items, {}, {"title": "Topic Watch", "sections": ["notable"]})
    assert "## Notable" in out


def test_publisher_is_credited_over_the_topic_name():
    """For a topic feed the source is the tracked entity, not who wrote the
    piece -- crediting the topic would attribute an Aftonbladet story to
    Vattenfall."""
    items = [{"title": "T", "link": "https://x.com/1", "source": "Vattenfall",
              "publisher": "aftonbladet.se", "summary": "S", "category": "key"}]
    out = render_markdown(items, {}, {"title": "W", "sections": ["key"]})
    assert "*aftonbladet.se*" in out
    assert "*Vattenfall*" not in out


def test_source_is_used_when_there_is_no_publisher():
    items = [{"title": "T", "link": "https://x.com/1", "source": "Krebs on Security",
              "summary": "S", "category": "news"}]
    out = render_markdown(items, {}, {"title": "W", "sections": ["news"]})
    assert "*Krebs on Security*" in out
