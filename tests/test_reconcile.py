"""Tests for src.reconcile: merging admin-panel overrides back into the
git-tracked base files before a deploy pushes a new copy up."""

import yaml

from src.reconcile import merge_llm, merge_sources


def test_merge_sources_no_override_file_is_noop(tmp_path):
    base = tmp_path / "sources.yaml"
    base.write_text(yaml.dump({"rss": [{"name": "A", "url": "https://a.com"}]}))
    override = tmp_path / "sources_overrides.yaml"  # does not exist

    changes = merge_sources(base, override)

    assert changes == []
    assert yaml.safe_load(base.read_text())["rss"] == [{"name": "A", "url": "https://a.com"}]


def test_merge_sources_adds_new_feed_from_override(tmp_path):
    base = tmp_path / "sources.yaml"
    base.write_text(yaml.dump({"rss": [{"name": "A", "url": "https://a.com"}]}))
    override = tmp_path / "sources_overrides.yaml"
    override.write_text(yaml.dump({"rss": [
        {"name": "A", "url": "https://a.com"},
        {"name": "B", "url": "https://b.com"},
    ]}))

    changes = merge_sources(base, override)

    assert len(changes) == 1
    assert "added" in changes[0] and "B" in changes[0]
    merged = yaml.safe_load(base.read_text())["rss"]
    assert {"name": "A", "url": "https://a.com"} in merged
    assert {"name": "B", "url": "https://b.com"} in merged


def test_merge_sources_preserves_base_only_entries():
    """The scenario this whole feature exists for: a feed added locally to
    sources.yaml (e.g. via git) must survive the merge even though it's not
    in the override."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        tmp_path = Path(d)
        base = tmp_path / "sources.yaml"
        base.write_text(yaml.dump({"rss": [
            {"name": "A", "url": "https://a.com"},
            {"name": "Medium AI Advances", "url": "https://medium.com/feed/ai-advances"},
        ]}))
        override = tmp_path / "sources_overrides.yaml"
        override.write_text(yaml.dump({"rss": [{"name": "A", "url": "https://a.com"}]}))

        changes = merge_sources(base, override)

        assert changes == []  # override has nothing new or different
        merged = yaml.safe_load(base.read_text())["rss"]
        assert {"name": "Medium AI Advances", "url": "https://medium.com/feed/ai-advances"} in merged


def test_merge_sources_override_wins_on_url_change(tmp_path):
    base = tmp_path / "sources.yaml"
    base.write_text(yaml.dump({"rss": [{"name": "A", "url": "https://old.example.com"}]}))
    override = tmp_path / "sources_overrides.yaml"
    override.write_text(yaml.dump({"rss": [{"name": "A", "url": "https://new.example.com"}]}))

    changes = merge_sources(base, override)

    assert len(changes) == 1
    assert "updated" in changes[0]
    merged = yaml.safe_load(base.read_text())["rss"]
    assert merged == [{"name": "A", "url": "https://new.example.com"}]


def test_merge_sources_identical_content_is_noop(tmp_path):
    base = tmp_path / "sources.yaml"
    base.write_text(yaml.dump({"rss": [{"name": "A", "url": "https://a.com"}]}))
    override = tmp_path / "sources_overrides.yaml"
    override.write_text(yaml.dump({"rss": [{"name": "A", "url": "https://a.com"}]}))

    changes = merge_sources(base, override)

    assert changes == []


def test_merge_llm_no_override_file_is_noop(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"llm": {"provider": "anthropic", "model": "claude-haiku-4-5", "temperature": 0.3}}))
    override = tmp_path / "llm_overrides.yaml"  # does not exist

    changes = merge_llm(config, override)

    assert changes == []
    assert yaml.safe_load(config.read_text())["llm"]["provider"] == "anthropic"


def test_merge_llm_applies_override_and_keeps_untouched_keys(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({
        "llm": {"provider": "anthropic", "model": "claude-haiku-4-5", "temperature": 0.3, "batch_size": 8},
    }))
    override = tmp_path / "llm_overrides.yaml"
    override.write_text(yaml.dump({"llm": {"provider": "openai", "model": "gpt-5.6-luna"}}))

    changes = merge_llm(config, override)

    assert len(changes) == 2  # provider and model both changed
    merged = yaml.safe_load(config.read_text())["llm"]
    assert merged["provider"] == "openai"
    assert merged["model"] == "gpt-5.6-luna"
    # untouched by the override -- preserved from base
    assert merged["temperature"] == 0.3
    assert merged["batch_size"] == 8


def test_merge_llm_identical_content_is_noop(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"llm": {"provider": "openai", "model": "gpt-5.6-luna"}}))
    override = tmp_path / "llm_overrides.yaml"
    override.write_text(yaml.dump({"llm": {"provider": "openai", "model": "gpt-5.6-luna"}}))

    changes = merge_llm(config, override)

    assert changes == []


def test_merge_llm_preserves_other_top_level_config_keys(tmp_path):
    """Regression guard: merging llm: must not disturb sibling top-level keys
    like retry:/sources:/digests: in config.yaml."""
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({
        "retry": {"max_retries": 3},
        "llm": {"provider": "anthropic", "model": "claude-haiku-4-5"},
        "digests": [{"title": "Security Digest"}],
    }))
    override = tmp_path / "llm_overrides.yaml"
    override.write_text(yaml.dump({"llm": {"provider": "openai", "model": "gpt-5.6-luna"}}))

    merge_llm(config, override)

    doc = yaml.safe_load(config.read_text())
    assert doc["retry"] == {"max_retries": 3}
    assert doc["digests"] == [{"title": "Security Digest"}]
    assert doc["llm"]["provider"] == "openai"


def test_merge_sources_folds_back_a_routing_change(tmp_path):
    """Routing is editable in the admin panel now, so a change to `digests`
    alone must reach the git-tracked sources.yaml. Comparing only the URL meant
    it never did, and the next deploy shipped a seed that disagreed with what
    the panel was running."""
    base = tmp_path / "sources.yaml"
    base.write_text("rss:\n- name: Krebs\n  url: https://k\n  digests: [Security Digest]\n")
    override = tmp_path / "sources_overrides.yaml"
    override.write_text("rss:\n- name: Krebs\n  url: https://k\n  digests: [Security Digest, AI Security Digest]\n")

    changes = merge_sources(base, override)

    assert changes, "a routing-only change must be detected"
    written = yaml.safe_load(base.read_text())
    assert written["rss"][0]["digests"] == ["Security Digest", "AI Security Digest"]
