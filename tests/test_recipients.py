"""Tests for recipients and the digests derived from them."""

import pytest
import yaml
from fastapi.testclient import TestClient

from src.fetcher import load_config
from src.recipients import (
    derive_digests,
    normalise_users,
    topics_for_user,
    warn_on_unknown_recipients,
)

TOKEN = "test-admin-token"
AUTH = {"X-Admin-Token": TOKEN}

ALICE = {"name": "Alice", "email": "alice@example.com"}
WIFE = {"name": "Wife", "email": "wife@example.com"}


# --- derivation -----------------------------------------------------------


def test_topic_without_a_recipient_goes_to_everyone():
    """An unassigned topic being fetched and delivered to nobody is the more
    expensive mistake, and it stays invisible until someone notices."""
    topics = [{"name": "Shared"}]
    assert topics_for_user(topics, ALICE["email"]) == ["Shared"]
    assert topics_for_user(topics, WIFE["email"]) == ["Shared"]


def test_topic_addressed_to_one_user_reaches_only_them():
    topics = [{"name": "Hers", "recipient": WIFE["email"]}]
    assert topics_for_user(topics, WIFE["email"]) == ["Hers"]
    assert topics_for_user(topics, ALICE["email"]) == []


def test_recipient_matching_ignores_case():
    topics = [{"name": "T", "recipient": "Wife@Example.com"}]
    assert topics_for_user(topics, "wife@example.com") == ["T"]


def test_derive_digests_builds_one_per_user():
    topics = [
        {"name": "Shared", "recipient": "all"},
        {"name": "His", "recipient": ALICE["email"]},
        {"name": "Hers", "recipient": WIFE["email"]},
    ]
    digests = derive_digests([ALICE, WIFE], topics, {"title_format": "{name}'s News"})

    assert [d["title"] for d in digests] == ["Alice's News", "Wife's News"]
    assert [d["to"] for d in digests] == [ALICE["email"], WIFE["email"]]
    assert digests[0]["sources"] == ["Shared", "His"]
    assert digests[1]["sources"] == ["Shared", "Hers"]


def test_user_with_no_topics_gets_no_digest():
    """An email with nothing in it is worse than no email."""
    topics = [{"name": "His", "recipient": ALICE["email"]}]
    digests = derive_digests([ALICE, WIFE], topics, None)
    assert [d["to"] for d in digests] == [ALICE["email"]]


def test_derived_digests_carry_the_template_sections_and_labels():
    template = {"sections": ["key", "mention"], "labels": {"key": "Worth knowing"}}
    d = derive_digests([ALICE], [{"name": "T"}], template)[0]
    assert d["sections"] == ["key", "mention"]
    assert d["labels"] == {"key": "Worth knowing"}


def test_normalise_users_drops_invalid_and_duplicate_entries():
    users = normalise_users([
        ALICE,
        {"name": "No email"},
        {"name": "Bad", "email": "not-an-email"},
        {"name": "Dupe", "email": "ALICE@EXAMPLE.com"},
    ])
    assert users == [ALICE]


def test_normalise_users_falls_back_to_email_as_name():
    assert normalise_users([{"email": "x@y.com"}]) == [{"name": "x@y.com", "email": "x@y.com"}]


def test_warns_on_a_topic_addressed_to_an_unknown_recipient():
    """Delete a user, or mistype an address, and the topic is still fetched and
    summarised but reaches nobody."""
    messages = warn_on_unknown_recipients([ALICE], [{"name": "Orphan", "recipient": "gone@example.com"}])
    assert any("Orphan" in m and "not a known recipient" in m for m in messages)


# --- config integration ---------------------------------------------------


def test_users_drive_the_digest_list(tmp_path):
    (tmp_path / "config.yaml").write_text(yaml.dump({
                "digest_template": {"title_format": "{name}'s News", "sections": ["key"]},
    }))
    (tmp_path / "topics.yaml").write_text(yaml.dump({"topics": [
        {"name": "Shared"}, {"name": "Hers", "recipient": WIFE["email"]},
    ]}))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "users.yaml").write_text(yaml.dump({"users": [ALICE, WIFE]}))

    config = load_config(tmp_path / "config.yaml")
    digests = {d["to"]: d for d in config["digests"]}
    assert set(digests) == {ALICE["email"], WIFE["email"]}
    assert digests[ALICE["email"]]["sources"] == ["Shared"]
    assert digests[WIFE["email"]]["sources"] == ["Shared", "Hers"]


def test_an_empty_users_list_never_blanks_explicit_digests(tmp_path):
    """Regression guard: an instance that writes digests: by hand and ships an
    empty users.yaml stub must keep its digests. Deriving over them would
    silently stop every digest it sends."""
    (tmp_path / "config.yaml").write_text(yaml.dump({
        "digests": [{"title": "Security Digest", "sections": ["news"]}],
    }))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "users.yaml").write_text(yaml.dump({"users": []}))

    config = load_config(tmp_path / "config.yaml")
    assert [d["title"] for d in config["digests"]] == ["Security Digest"]


def test_users_come_from_the_single_writable_file(tmp_path):
    (tmp_path / "config.yaml").write_text(yaml.dump({
            }))
    (tmp_path / "topics.yaml").write_text(yaml.dump({"topics": [{"name": "T"}]}))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "users.yaml").write_text(yaml.dump({"users": [ALICE]}))

    config = load_config(tmp_path / "config.yaml")
    assert [u["email"] for u in config["users"]] == [ALICE["email"]]


# --- endpoints ------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    import src.web.app as web

    (tmp_path / "config.yaml").write_text(yaml.dump({
                "digest_template": {"title_format": "{name}'s News", "sections": ["key"]},
    }))
    (tmp_path / "topics.yaml").write_text(yaml.dump({"topics": [
        {"name": "Shared"}, {"name": "His", "recipient": ALICE["email"]},
    ]}))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "users.yaml").write_text(yaml.dump({"users": [ALICE]}))
    monkeypatch.setattr(web, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("DIGEST_ADMIN_TOKEN", TOKEN)
    return TestClient(web.app)


def test_get_users_lists_recipients_and_what_they_receive(client):
    data = client.get("/admin/users", headers=AUTH).json()
    assert data["users"] == [{**ALICE, "topics": ["Shared", "His"]}]


def test_user_endpoints_require_the_admin_token(client):
    assert client.get("/admin/users").status_code == 401
    assert client.post("/admin/users", json={"users": []}).status_code == 401


def test_add_a_recipient_and_the_digest_appears(client):
    r = client.post("/admin/users", headers=AUTH, json={"users": [ALICE, WIFE]})
    assert r.status_code == 200 and r.json()["ok"] is True
    after = client.get("/admin/users", headers=AUTH).json()
    assert [u["email"] for u in after["users"]] == [ALICE["email"], WIFE["email"]]
    # The new recipient receives every "all" topic straight away.
    assert after["users"][1]["topics"] == ["Shared"]


def test_save_users_rejects_a_bad_email(client):
    r = client.post("/admin/users", headers=AUTH, json={"users": [{"name": "X", "email": "nope"}]})
    assert r.status_code == 400
    assert "not a valid email" in r.json()["message"]


def test_save_users_rejects_a_missing_name(client):
    r = client.post("/admin/users", headers=AUTH, json={"users": [{"email": "a@b.com"}]})
    assert r.status_code == 400
    assert "name is required" in r.json()["message"]


def test_save_users_rejects_duplicate_emails(client):
    r = client.post("/admin/users", headers=AUTH, json={"users": [
        ALICE, {"name": "Other", "email": ALICE["email"].upper()},
    ]})
    assert r.status_code == 400
    assert "duplicate" in r.json()["message"].lower()


def test_removing_a_recipient_reports_the_topics_it_orphans(client):
    """Removing someone leaves their topics fetched and summarised but delivered
    to nobody -- say so rather than dropping them silently."""
    r = client.post("/admin/users", headers=AUTH, json={"users": [WIFE]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["orphaned_topics"] == ["His"]
    assert "addressed to nobody" in body["message"]


def test_topic_cannot_be_addressed_to_an_unknown_recipient(client):
    r = client.post("/admin/topics", headers=AUTH, json={"topics": [
        {"name": "T", "recipient": "stranger@example.com"},
    ]})
    assert r.status_code == 400
    assert "not a known recipient" in r.json()["message"]


def test_topics_endpoint_exposes_users_for_the_dropdown(client):
    data = client.get("/admin/topics", headers=AUTH).json()
    assert [u["email"] for u in data["users"]] == [ALICE["email"]]
    assert {t["name"]: t["recipient"] for t in data["topics"]} == {
        "Shared": "all", "His": ALICE["email"],
    }


def test_saving_recipients_writes_the_one_file_the_app_reads(client, tmp_path):
    """No base/override pair: what the panel writes is what load_config reads,
    which is the whole point of keeping recipients in data/."""
    client.post("/admin/users", headers=AUTH, json={"users": [WIFE]})
    written = yaml.safe_load((tmp_path / "data" / "users.yaml").read_text())
    assert [u["email"] for u in written["users"]] == [WIFE["email"]]
    assert [u["email"] for u in load_config(tmp_path / "config.yaml")["users"]] == [WIFE["email"]]


# --- branding -------------------------------------------------------------


def test_site_branding_defaults_to_the_security_wording(tmp_path, monkeypatch):
    """An instance that sets nothing keeps the original header, so the live
    security dashboard is unchanged by this becoming configurable."""
    import src.web.app as web

    (tmp_path / "config.yaml").write_text(yaml.dump({}))
    monkeypatch.setattr(web, "PROJECT_ROOT", tmp_path)
    data = TestClient(web.app).get("/api/site").json()
    assert data["title"] == "Security Digest"
    assert data["icon"] == "🛡"


def test_site_branding_comes_from_config(tmp_path, monkeypatch):
    import src.web.app as web

    (tmp_path / "config.yaml").write_text(yaml.dump({
        "site": {"title": "News Digest", "tagline": "Topics worth following", "icon": "📰"},
    }))
    monkeypatch.setattr(web, "PROJECT_ROOT", tmp_path)
    data = TestClient(web.app).get("/api/site").json()
    assert data == {"title": "News Digest", "tagline": "Topics worth following", "icon": "📰"}


def test_site_branding_needs_no_admin_token(tmp_path, monkeypatch):
    """It is the name on a page anyone reaching the site already sees."""
    import src.web.app as web

    (tmp_path / "config.yaml").write_text(yaml.dump({"site": {"title": "X"}}))
    monkeypatch.setattr(web, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("DIGEST_ADMIN_TOKEN", raising=False)
    assert TestClient(web.app).get("/api/site").status_code == 200


# --- declared vs derived digests -------------------------------------------


def test_declared_digests_are_never_replaced_by_derivation(tmp_path):
    """The security instance declares three digests and has no topics. Deriving
    over them would replace all three with nothing -- a silent, total outage
    from one click on "Add recipient"."""
    (tmp_path / "config.yaml").write_text(yaml.dump({
        "digests": [{"title": "Security Digest", "sections": ["news"]},
                    {"title": "AI News", "sections": ["ai_general"]}],
    }))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "users.yaml").write_text(yaml.dump({"users": [ALICE]}))

    config = load_config(tmp_path / "config.yaml")
    assert [d["title"] for d in config["digests"]] == ["Security Digest", "AI News"]
    assert config["digests_are_derived"] is False


def test_an_instance_without_declared_digests_still_derives(tmp_path):
    (tmp_path / "config.yaml").write_text(yaml.dump({
        "digest_template": {"title_format": "{name}'s News", "sections": ["key"]},
    }))
    (tmp_path / "topics.yaml").write_text(yaml.dump({"topics": [{"name": "T"}]}))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "users.yaml").write_text(yaml.dump({"users": [ALICE]}))

    config = load_config(tmp_path / "config.yaml")
    assert [d["to"] for d in config["digests"]] == [ALICE["email"]]
    assert config["digests_are_derived"] is True


def test_saving_recipients_is_refused_where_they_would_do_nothing(tmp_path, monkeypatch):
    import src.web.app as web

    (tmp_path / "config.yaml").write_text(yaml.dump({
        "digests": [{"title": "Security Digest", "sections": ["news"]}],
    }))
    monkeypatch.setattr(web, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("DIGEST_ADMIN_TOKEN", TOKEN)
    client = TestClient(web.app)

    r = client.post("/admin/users", headers=AUTH, json={"users": [ALICE]})
    assert r.status_code == 400
    assert "declares its own digests" in r.json()["message"]
    # And the declared digests are untouched.
    assert [d["title"] for d in web._load_config()["digests"]] == ["Security Digest"]


def test_get_users_reports_which_mode_an_instance_is_in(tmp_path, monkeypatch):
    import src.web.app as web

    (tmp_path / "config.yaml").write_text(yaml.dump({
        "digests": [{"title": "Security Digest", "sections": ["news"]}],
    }))
    monkeypatch.setattr(web, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("DIGEST_ADMIN_TOKEN", TOKEN)
    data = TestClient(web.app).get("/admin/users", headers=AUTH).json()
    assert data["derived"] is False
    assert data["digests"] == ["Security Digest"]


# --- which admin cards apply to an instance --------------------------------


def _panel(tmp_path, monkeypatch, config, **files):
    import src.web.app as web
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    (tmp_path / "data").mkdir(exist_ok=True)
    for name, body in files.items():
        (tmp_path / name.replace("__", "/")).write_text(yaml.dump(body))
    monkeypatch.setattr(web, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("DIGEST_ADMIN_TOKEN", TOKEN)
    return TestClient(web.app).get("/admin/panel", headers=AUTH).json()


def test_a_feed_instance_shows_only_the_feeds_card(tmp_path, monkeypatch):
    """Recipients and topics do nothing on a digest that follows publications."""
    panel = _panel(tmp_path, monkeypatch,
                   {"digests": [{"title": "Security Digest", "sections": ["news"]}],
                    "sources_file": "sources.yaml"},
                   **{"sources.yaml": {"rss": [{"name": "Krebs", "url": "https://x/f"}]}})
    assert panel == {"recipients": False, "topics": False, "feeds": True}


def test_a_topic_instance_shows_recipients_and_topics_but_not_feeds(tmp_path, monkeypatch):
    panel = _panel(tmp_path, monkeypatch,
                   {"digest_template": {"sections": ["key"]}},
                   **{"data__topics.yaml": {"topics": [{"name": "Nvidia"}]}})
    assert panel == {"recipients": True, "topics": True, "feeds": False}


def test_a_brand_new_topic_instance_can_still_add_its_first_topic(tmp_path, monkeypatch):
    """No topics yet must not hide the only place to create one."""
    panel = _panel(tmp_path, monkeypatch, {"digest_template": {"sections": ["key"]}})
    assert panel["topics"] is True


def test_a_feed_instance_with_no_feeds_can_still_add_its_first(tmp_path, monkeypatch):
    panel = _panel(tmp_path, monkeypatch,
                   {"digests": [{"title": "D", "sections": ["news"]}]})
    assert panel["feeds"] is True


def test_a_feed_added_to_a_topic_instance_brings_its_card_back(tmp_path, monkeypatch):
    """The rules are self-correcting rather than hard-coded per instance."""
    panel = _panel(tmp_path, monkeypatch,
                   {"digest_template": {"sections": ["key"]}, "sources_file": "sources.yaml"},
                   **{"sources.yaml": {"rss": [{"name": "Krebs", "url": "https://x/f"}]}})
    assert panel["feeds"] is True


def test_panel_requires_the_admin_token(tmp_path, monkeypatch):
    import src.web.app as web
    (tmp_path / "config.yaml").write_text(yaml.dump({}))
    monkeypatch.setattr(web, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("DIGEST_ADMIN_TOKEN", TOKEN)
    assert TestClient(web.app).get("/admin/panel").status_code == 401
