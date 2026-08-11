"""Tests for admin-managed topics: which file is live, and the endpoint
validation that stops a bad save."""

import pytest
import yaml
from fastapi.testclient import TestClient

from src.fetcher import load_config

TOKEN = "test-admin-token"


# --- live file vs seed -------------------------------------------------------


def test_the_live_topic_list_wins_over_the_seed(tmp_path):
    """Once an instance is running, data/topics.yaml is the only list that
    matters -- the repo's topics.yaml must not shadow what the panel wrote."""
    (tmp_path / "config.yaml").write_text(yaml.dump({}))
    (tmp_path / "topics.yaml").write_text(yaml.dump({"topics": [{"name": "Seed"}]}))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "topics.yaml").write_text(
        yaml.dump({"topics": [{"name": "Live", "lang": "sv", "country": "SE"}]})
    )

    config = load_config(tmp_path / "config.yaml")
    assert [t["name"] for t in config["topics"]] == ["Live"]
    assert any("setmkt=sv-SE" in f["url"] for f in config["sources"]["topic_feeds"])


def test_a_brand_new_instance_starts_from_the_seed(tmp_path):
    """The repo's topics.yaml populates an instance that has never run, so a
    fresh deployment isn't empty and the context prose survives in git."""
    (tmp_path / "config.yaml").write_text(yaml.dump({}))
    (tmp_path / "topics.yaml").write_text(yaml.dump({"topics": [{"name": "Seed"}]}))
    config = load_config(tmp_path / "config.yaml")
    assert [t["name"] for t in config["topics"]] == ["Seed"]


# --- endpoints ------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    """App wired to a throwaway instance root, with admin enabled."""
    import src.web.app as web

    (tmp_path / "config.yaml").write_text(yaml.dump({
        "digests": [{"title": "Watch", "sections": ["key"], "sources": ["Acme"]}],
    }))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "topics.yaml").write_text(yaml.dump({"topics": [
        {"name": "Acme", "queries": ['"Acme"'], "context": "The anvil maker.",
         "lang": "en", "country": "US"},
    ]}))
    monkeypatch.setattr(web, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("DIGEST_ADMIN_TOKEN", TOKEN)
    return TestClient(web.app)


AUTH = {"X-Admin-Token": TOKEN}


def test_get_topics_returns_the_configured_list_with_digest_routing(client):
    r = client.get("/admin/topics", headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert [t["name"] for t in data["topics"]] == ["Acme"]
    assert data["topics"][0]["context"] == "The anvil maker."
    # The read-only column that reveals a topic nothing routes to.
    assert data["topics"][0]["digests"] == ["Watch"]


def test_topics_endpoints_require_the_admin_token(client):
    assert client.get("/admin/topics").status_code == 401
    assert client.post("/admin/topics", json={"topics": []}).status_code == 401


def test_save_topics_writes_the_live_file_and_takes_effect(client, tmp_path):
    r = client.post("/admin/topics", headers=AUTH, json={"topics": [
        {"name": "Vattenfall", "queries": ["\"Vattenfall\""], "context": "Energy co.",
         "lang": "sv", "country": "SE"},
    ]})
    assert r.status_code == 200 and r.json()["ok"] is True

    written = yaml.safe_load((tmp_path / "data" / "topics.yaml").read_text())
    assert written["topics"] == [{
        "name": "Vattenfall", "queries": ['"Vattenfall"'],
        "context": "Energy co.", "lang": "sv", "country": "SE",
        # Unset recipient persists as "all" rather than being omitted, so the
        # stored record says plainly who it reaches.
        "recipient": "all",
    }]

    # And that file is what the app now serves.
    after = client.get("/admin/topics", headers=AUTH).json()
    assert [t["name"] for t in after["topics"]] == ["Vattenfall"]


def test_save_topics_rejects_a_missing_name(client):
    r = client.post("/admin/topics", headers=AUTH, json={"topics": [{"context": "no name"}]})
    assert r.status_code == 400
    assert "name is required" in r.json()["message"]


def test_save_topics_rejects_duplicate_names(client):
    """Two topics with one name produce two feed sets routing to the same
    digests: duplicated items and doubled LLM spend."""
    r = client.post("/admin/topics", headers=AUTH, json={"topics": [
        {"name": "Acme"}, {"name": "acme"},
    ]})
    assert r.status_code == 400
    assert "duplicate" in r.json()["message"].lower()


def test_save_topics_accepts_newline_separated_queries(client, tmp_path):
    """The textarea submits one query per line."""
    r = client.post("/admin/topics", headers=AUTH, json={"topics": [
        {"name": "Acme", "queries": '"Acme"\nAcme lawsuit\n\n'},
    ]})
    assert r.status_code == 200
    written = yaml.safe_load((tmp_path / "data" / "topics.yaml").read_text())
    assert written["topics"][0]["queries"] == ['"Acme"', "Acme lawsuit"]


def test_saving_an_empty_feed_list_is_refused(client):
    """The RSS editor renders one blank row when there are no feeds, so a stray
    Save would otherwise write an override emptying the whole feed list."""
    r = client.post("/admin/sources", headers=AUTH, json={"rss": []})
    assert r.status_code == 400
    assert "empty feed list" in r.json()["message"]


def test_an_empty_feed_list_can_still_be_saved_deliberately(client):
    r = client.post("/admin/sources", headers=AUTH, json={"rss": [], "allow_empty": True})
    assert r.status_code == 200 and r.json()["ok"] is True
