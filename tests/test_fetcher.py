"""Tests for src.fetcher: schedule.txt parsing and load_config's layered
override merging. No network calls -- fetch_feed/fetch_all are intentionally
out of scope here."""

from unittest.mock import patch

import pytest
import yaml

from src.fetcher import _parse_schedule_file, fetch_all, fetch_feed, load_config


def test_parse_schedule_file_basic(tmp_path):
    path = tmp_path / "schedule.txt"
    path.write_text("enabled=true\nhour=7\nminute=30\ntimezone=Europe/Stockholm\n")
    result = _parse_schedule_file(path)
    assert result == {"enabled": True, "hour": 7, "minute": 30, "timezone": "Europe/Stockholm"}


def test_parse_schedule_file_ignores_comments_and_blank_lines(tmp_path):
    path = tmp_path / "schedule.txt"
    path.write_text("# comment\n\nenabled=false\n")
    result = _parse_schedule_file(path)
    assert result == {"enabled": False}


def test_parse_schedule_file_missing_file_returns_empty(tmp_path):
    assert _parse_schedule_file(tmp_path / "does_not_exist.txt") == {}


def test_parse_schedule_file_invalid_hour_falls_back_to_default(tmp_path):
    path = tmp_path / "schedule.txt"
    path.write_text("hour=not_a_number\n")
    result = _parse_schedule_file(path)
    assert result["hour"] == 7


def test_load_config_merges_sources_file(tmp_path):
    (tmp_path / "config.yaml").write_text(yaml.dump({"sources_file": "sources.yaml"}))
    (tmp_path / "sources.yaml").write_text(
        yaml.dump({"rss": [{"name": "Test", "url": "https://x.com/feed"}]})
    )
    config = load_config(tmp_path / "config.yaml")
    assert config["sources"]["rss"] == [{"name": "Test", "url": "https://x.com/feed"}]


def test_load_config_sources_overrides_take_precedence(tmp_path):
    (tmp_path / "config.yaml").write_text(yaml.dump({"sources_file": "sources.yaml"}))
    (tmp_path / "sources.yaml").write_text(
        yaml.dump({"rss": [{"name": "Base", "url": "https://base.com"}]})
    )
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "sources_overrides.yaml").write_text(
        yaml.dump({"rss": [{"name": "Override", "url": "https://override.com"}]})
    )
    config = load_config(tmp_path / "config.yaml")
    assert config["sources"]["rss"] == [{"name": "Override", "url": "https://override.com"}]


def test_load_config_schedule_file_merges_into_schedule(tmp_path):
    """Regression check for task 2.7: schedule.txt is the sole source."""
    (tmp_path / "config.yaml").write_text(yaml.dump({"schedule_file": "schedule.txt"}))
    (tmp_path / "schedule.txt").write_text("enabled=true\nhour=9\nminute=0\ntimezone=UTC\n")
    config = load_config(tmp_path / "config.yaml")
    assert config["schedule"] == {"enabled": True, "hour": 9, "minute": 0, "timezone": "UTC"}


def test_load_config_llm_overrides_merge(tmp_path):
    (tmp_path / "config.yaml").write_text(
        yaml.dump({"llm": {"provider": "openrouter", "model": "qwen/qwen3.7-flash"}})
    )
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "llm_overrides.yaml").write_text(
        yaml.dump({"llm": {"provider": "mistral", "model": "mistral-large-latest"}})
    )
    config = load_config(tmp_path / "config.yaml")
    assert config["llm"]["provider"] == "mistral"
    assert config["llm"]["model"] == "mistral-large-latest"


def test_load_config_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "does_not_exist.yaml")


def test_fetch_all_undated_items_survive_trim_over_stale_dated_ones():
    """Regression check for task 3.8: items without a published date used to sort
    last (empty string key) and were always the first cut by max_total_items,
    regardless of how recent they actually were. fetch_feed is mocked -- no
    network involved."""
    config = {
        "sources": {
            "rss": [{"name": "Test Feed", "url": "https://example.com/feed"}],
            "max_items_per_source": 20,
            "max_total_items": 2,
        }
    }
    fake_items = [
        {"title": "Undated (just fetched)", "link": "https://x.com/1", "source": "Test Feed", "description": "", "published": ""},
        {"title": "Old dated 2020", "link": "https://x.com/2", "source": "Test Feed", "description": "", "published": "2020-01-01T00:00:00"},
        {"title": "Older dated 2019", "link": "https://x.com/3", "source": "Test Feed", "description": "", "published": "2019-01-01T00:00:00"},
    ]
    with patch("src.fetcher.fetch_feed", return_value=fake_items):
        result = fetch_all(config)

    titles = [i["title"] for i in result]
    assert len(result) == 2
    assert "Undated (just fetched)" in titles
    assert "Older dated 2019" not in titles


def test_load_config_raises_on_invalid_config(tmp_path):
    """Task 3.3: load_config() validates the fully-merged config before
    returning it -- a wrong-type value raises a clear pydantic ValidationError
    at load time instead of silently reaching the pipeline."""
    from pydantic import ValidationError

    (tmp_path / "config.yaml").write_text(
        yaml.dump({"sources": {"seen_retention_days": "not-a-number"}})
    )
    with pytest.raises(ValidationError):
        load_config(tmp_path / "config.yaml")


def test_fetch_feed_truncates_description_to_configured_length(monkeypatch):
    """Some publishers (Medium among them) embed the full article text in
    <description> for some entries -- max_description_chars controls how
    much of that is kept, instead of the old hardcoded 1000-char cutoff."""
    long_text = "x" * 20000
    rss = f"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test Feed</title>
<item>
  <title>Full-text entry</title>
  <link>https://example.com/1</link>
  <description>{long_text}</description>
</item>
</channel></rss>"""
    monkeypatch.setattr("src.fetcher._fetch_feed_bytes", lambda url: rss.encode())

    default_items = fetch_feed("https://example.com/feed", "Test Feed")
    assert len(default_items[0]["description"]) == 1000  # unchanged default

    wide_items = fetch_feed("https://example.com/feed", "Test Feed", max_description_chars=5000)
    assert len(wide_items[0]["description"]) == 5000


def test_fetch_all_passes_configured_max_description_chars(monkeypatch):
    """Regression check that fetch_all() actually reads sources.max_description_chars
    from config and threads it through to fetch_feed, rather than only the
    default parameter value working."""
    captured = {}

    def fake_fetch_feed(url, name, limit=20, config=None, max_description_chars=1000,
                        extra=None, search_feed=False, health=None):
        captured["max_description_chars"] = max_description_chars
        return []

    monkeypatch.setattr("src.fetcher.fetch_feed", fake_fetch_feed)

    config = {
        "sources": {
            "rss": [{"name": "Test", "url": "https://example.com/feed"}],
            "max_description_chars": 7500,
        }
    }
    fetch_all(config)
    assert captured["max_description_chars"] == 7500


def test_optional_config_files_that_are_directories_are_ignored(tmp_path):
    """Docker silently creates a *directory* when a bind mount's host path
    doesn't exist yet. exists() is then True and open() dies with
    IsADirectoryError on every run -- a crash loop rather than a clean skip.
    Recreating a container before the deploy script has pushed a newly-mounted
    file does exactly this."""
    (tmp_path / "config.yaml").write_text(
        yaml.dump({"sources_file": "sources.yaml", "topics_file": "topics.yaml",
                   "schedule_file": "schedule.txt"})
    )
    for name in ("sources.yaml", "topics.yaml", "schedule.txt"):
        (tmp_path / name).mkdir()

    # Loads cleanly, with each unusable path simply contributing nothing.
    config = load_config(tmp_path / "config.yaml")
    assert config.get("sources", {}).get("rss", []) == []
    assert config.get("sources", {}).get("topic_feeds", []) == []
    assert config.get("schedule", {}) == {}


def test_config_path_that_is_a_directory_raises_clearly(tmp_path):
    """The main config is not optional, so this must fail -- but with a message
    that names the actual cause rather than a bare IsADirectoryError."""
    (tmp_path / "config.yaml").mkdir()
    with pytest.raises(IsADirectoryError, match="bind mount"):
        load_config(tmp_path / "config.yaml")


# --- feed health ------------------------------------------------------------
# fetch_feed fills in a `health` dict when given one. The contract that matters:
# a caller who does NOT pass one gets exactly the old behaviour and touches no
# database, so the pipeline's failure handling is unchanged by the column.


class _Entry(dict):
    """feedparser entries support both attribute and item access."""

    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)


def _parsed(entries, bozo=False, bozo_exception=None):
    from types import SimpleNamespace

    return SimpleNamespace(entries=entries, bozo=bozo, bozo_exception=bozo_exception)


def _entry(title="T", published=None):
    e = _Entry(title=title, link="https://e.com/a", summary="s")
    if published:
        e["published_parsed"] = published.timetuple()
    return e


def test_fetch_feed_reports_a_healthy_feed(monkeypatch):
    import datetime

    newest = datetime.datetime(2026, 8, 24, 9, 0)
    monkeypatch.setattr(
        "src.fetcher._parse_feed",
        lambda url: _parsed([_entry("A", newest), _entry("B", datetime.datetime(2026, 8, 20))]))
    health = {}
    items = fetch_feed("https://e.com/f", "F", health=health)
    assert len(items) == 2
    assert health["status"] == "ok" and health["items"] == 2
    assert health["newest"] == newest.isoformat()


def test_fetch_feed_counts_every_entry_not_just_the_kept_slice(monkeypatch):
    """The column answers "is this feed alive", which a `limit`-truncated count
    would understate -- and it is what lets the on-demand probe pass limit=1 and
    stay fast without reporting "1 item"."""
    monkeypatch.setattr("src.fetcher._parse_feed", lambda url: _parsed([_entry() for _ in range(9)]))
    health = {}
    items = fetch_feed("https://e.com/f", "F", limit=1, health=health)
    assert len(items) == 1 and health["items"] == 9


def test_fetch_feed_reports_a_feed_that_fetches_but_is_empty(monkeypatch):
    """HTTP 200 with no entries is broken, and nothing about it raises."""
    monkeypatch.setattr("src.fetcher._parse_feed", lambda url: _parsed([]))
    health = {}
    assert fetch_feed("https://e.com/f", "F", health=health) == []
    assert health["status"] == "empty"


def test_fetch_feed_reports_why_a_fetch_failed(monkeypatch):
    import httpx

    def boom(url):
        raise httpx.HTTPStatusError(
            "Client error", request=httpx.Request("GET", url), response=httpx.Response(404))

    monkeypatch.setattr("src.fetcher._parse_feed", boom)
    health = {}
    assert fetch_feed("https://e.com/f", "F", config={"retry": {"max_retries": 0}},
                      health=health) == []
    assert health["status"] == "error" and health["detail"] == "HTTP 404"


def test_fetch_feed_without_a_health_dict_behaves_exactly_as_before(monkeypatch):
    """The pipeline must be unaffected by the column: no database, no new
    failure mode, same swallow-and-continue on a dead feed."""
    def boom(url):
        raise ValueError("down")

    monkeypatch.setattr("src.fetcher._parse_feed", boom)
    assert fetch_feed("https://e.com/f", "F", config={"retry": {"max_retries": 0}}) == []


def test_fetch_all_records_health_for_every_feed(monkeypatch, tmp_path):
    import src.db
    from src.feed_health import load

    db = tmp_path / "digest.db"
    monkeypatch.setattr(src.db, "DB_PATH", db)
    monkeypatch.setattr(
        "src.fetcher._parse_feed",
        lambda url: _parsed([]) if "dead" in url else _parsed([_entry()]))

    fetch_all({"sources": {"rss": [
        {"name": "Live", "url": "https://e.com/live"},
        {"name": "Dead", "url": "https://e.com/dead"},
    ]}})

    stored = load(db)
    assert stored["https://e.com/live"]["status"] == "ok"
    assert stored["https://e.com/dead"]["status"] == "empty"


def test_fetch_all_forgets_a_feed_that_was_removed(monkeypatch, tmp_path):
    import src.db
    from src.feed_health import load, record

    db = tmp_path / "digest.db"
    monkeypatch.setattr(src.db, "DB_PATH", db)
    record([{"url": "https://e.com/gone", "name": "Gone", "status": "error"}], db)
    monkeypatch.setattr("src.fetcher._parse_feed", lambda url: _parsed([_entry()]))

    fetch_all({"sources": {"rss": [{"name": "Live", "url": "https://e.com/live"}]}})
    assert list(load(db)) == ["https://e.com/live"]


def test_a_broken_health_database_never_fails_the_fetch(monkeypatch, caplog):
    """The digest is the job; the column is a convenience on top of it."""
    import src.feed_health

    monkeypatch.setattr("src.fetcher._parse_feed", lambda url: _parsed([_entry()]))
    monkeypatch.setattr(src.feed_health, "record",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk is full")))

    with caplog.at_level("WARNING"):
        items = fetch_all({"sources": {"rss": [{"name": "F", "url": "https://e.com/f"}]}})

    assert len(items) == 1
    assert "the fetch itself was fine" in caplog.text
