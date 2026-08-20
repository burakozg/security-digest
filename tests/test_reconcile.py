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


# A feed in the override but not in the base is ambiguous: the panel added it,
# or the base deleted it. Resolved by the stamp of what was last deployed --
# without which two dead feeds deleted by hand on 2026-08-20 came straight back.

def _setup_deletion(tmp_path, stamped):
    base = tmp_path / "sources.yaml"
    base.write_text(yaml.dump({"rss": [{"name": "Alive", "url": "https://a.com"}]}))
    override = tmp_path / "sources_overrides.yaml"
    override.write_text(yaml.dump({"rss": [
        {"name": "Alive", "url": "https://a.com"},
        {"name": "Dead", "url": "https://dead.com"},
    ]}))
    stamp = tmp_path / ".deployed-sources"
    stamp.write_text("\n".join(stamped))
    return base, override, stamp


def test_feed_deleted_from_base_stays_deleted_when_it_was_deployed(tmp_path):
    base, override, stamp = _setup_deletion(tmp_path, ["Alive", "Dead"])

    changes = merge_sources(base, override, stamp)

    names = [f["name"] for f in yaml.safe_load(base.read_text())["rss"]]
    assert names == ["Alive"], "the deleted feed was put back"
    assert any("removed" in c and "Dead" in c for c in changes), changes


def test_feed_only_in_override_is_still_added_when_never_deployed(tmp_path):
    """A genuine admin-panel addition must not be mistaken for a deletion."""
    base, override, stamp = _setup_deletion(tmp_path, ["Alive"])

    changes = merge_sources(base, override, stamp)

    names = [f["name"] for f in yaml.safe_load(base.read_text())["rss"]]
    assert names == ["Alive", "Dead"]
    assert any("added" in c and "Dead" in c for c in changes), changes


def test_without_a_stamp_nothing_is_assumed_deleted(tmp_path):
    """First deploy after this change: no stamp exists yet, so keep the old
    additive behaviour rather than dropping feeds on a guess."""
    base, override, _ = _setup_deletion(tmp_path, [])
    missing = tmp_path / "no-such-stamp"

    changes = merge_sources(base, override, missing)

    names = [f["name"] for f in yaml.safe_load(base.read_text())["rss"]]
    assert names == ["Alive", "Dead"]
    assert any("added" in c for c in changes)


def test_removal_only_reconcile_does_not_rewrite_the_file(tmp_path):
    """sources.yaml carries 19 lines of comments that yaml.dump would strip.
    A reconcile whose only finding is a removal changes nothing in the file,
    so it must not be rewritten at all."""
    base, override, stamp = _setup_deletion(tmp_path, ["Alive", "Dead"])
    base.write_text("# a comment worth keeping\nrss:\n  - name: Alive\n    url: https://a.com\n")
    before = base.read_text()

    changes = merge_sources(base, override, stamp)

    assert any("removed" in c for c in changes)
    assert base.read_text() == before, "file was rewritten and lost its comments"


# merge_llm had the same shape as merge_sources: the override won every key it
# set, so editing or deleting provider/model in config.yaml was reverted by the
# next deploy. Resolved against a stamp of the block as last deployed.

def _llm_case(tmp_path, base_llm, override_llm, stamped):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.dump({"llm": base_llm, "retry": {"max_retries": 3}}))
    override = tmp_path / "llm_overrides.yaml"
    override.write_text(yaml.dump({"llm": override_llm}))
    stamp = tmp_path / ".deployed-llm"
    if stamped is not None:
        stamp.write_text(yaml.dump(stamped))
    return cfg, override, stamp


def test_llm_key_edited_by_hand_survives_when_panel_did_not_change_it(tmp_path):
    cfg, override, stamp = _llm_case(
        tmp_path,
        base_llm={"provider": "openrouter", "model": "qwen/qwen3.8-27b"},
        override_llm={"provider": "openrouter", "model": "qwen/qwen3.7-flash"},
        stamped={"provider": "openrouter", "model": "qwen/qwen3.7-flash"},
    )

    changes = merge_llm(cfg, override, stamp)

    assert yaml.safe_load(cfg.read_text())["llm"]["model"] == "qwen/qwen3.8-27b"
    assert any("kept" in c and "model" in c for c in changes), changes


def test_llm_key_deleted_by_hand_stays_deleted(tmp_path):
    cfg, override, stamp = _llm_case(
        tmp_path,
        base_llm={"provider": "openrouter"},
        override_llm={"provider": "openrouter", "model": "qwen/qwen3.7-flash"},
        stamped={"provider": "openrouter", "model": "qwen/qwen3.7-flash"},
    )

    changes = merge_llm(cfg, override, stamp)

    assert "model" not in yaml.safe_load(cfg.read_text())["llm"]
    assert any("removed" in c and "model" in c for c in changes), changes


def test_llm_key_changed_in_the_panel_still_wins(tmp_path):
    """The feature's original purpose: a panel edit must be folded back to git."""
    cfg, override, stamp = _llm_case(
        tmp_path,
        base_llm={"provider": "openrouter", "model": "qwen/qwen3.7-flash"},
        override_llm={"provider": "openrouter", "model": "deepseek/deepseek-chat-v3.1"},
        stamped={"provider": "openrouter", "model": "qwen/qwen3.7-flash"},
    )

    changes = merge_llm(cfg, override, stamp)

    assert yaml.safe_load(cfg.read_text())["llm"]["model"] == "deepseek/deepseek-chat-v3.1"
    assert any("->" in c for c in changes), changes


def test_llm_without_a_stamp_keeps_the_old_override_wins_behaviour(tmp_path):
    cfg, override, _ = _llm_case(
        tmp_path,
        base_llm={"provider": "openrouter", "model": "qwen/qwen3.8-27b"},
        override_llm={"provider": "openrouter", "model": "qwen/qwen3.7-flash"},
        stamped=None,
    )

    merge_llm(cfg, override, tmp_path / "no-such-stamp")

    assert yaml.safe_load(cfg.read_text())["llm"]["model"] == "qwen/qwen3.7-flash"


def test_llm_report_only_reconcile_does_not_strip_config_comments(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("llm:\n  # four lines of explanation live in the real file\n  provider: openrouter\n")
    override = tmp_path / "llm_overrides.yaml"
    override.write_text(yaml.dump({"llm": {"provider": "openrouter", "model": "gone"}}))
    stamp = tmp_path / ".deployed-llm"
    stamp.write_text(yaml.dump({"provider": "openrouter", "model": "gone"}))
    before = cfg.read_text()

    changes = merge_llm(cfg, override, stamp)

    assert any("removed" in c for c in changes)
    assert cfg.read_text() == before, "config.yaml was rewritten and lost its comments"
