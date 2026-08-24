"""Tests for the Status column's endpoints: what /admin/sources reports for
each feed, and what the on-demand check does."""

import pytest
import yaml
from fastapi.testclient import TestClient

from src.feed_health import record

TOKEN = "test-admin-token"
AUTH = {"X-Admin-Token": TOKEN}

KREBS = "https://krebsonsecurity.com/feed/"
DEAD = "https://threatpost.com/feed/"


@pytest.fixture
def client(tmp_path, monkeypatch):
    import src.db
    import src.web.app as web

    (tmp_path / "config.yaml").write_text(yaml.dump({
        "digests": [{"title": "Security Digest", "sections": ["news"]}],
        "sources": {"rss": [
            {"name": "Krebs", "url": KREBS},
            {"name": "Threatpost", "url": DEAD},
        ]},
    }))
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(web, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(src.db, "DB_PATH", tmp_path / "digest.db")
    monkeypatch.setenv("DIGEST_ADMIN_TOKEN", TOKEN)
    return TestClient(web.app)


def _feeds(client):
    return {f["url"]: f for f in client.get("/admin/sources", headers=AUTH).json()["rss"]}


def test_a_feed_with_no_record_reports_unknown_not_healthy(client):
    """"We have never checked" and "it works" are different answers, and only
    one of them should be green."""
    feeds = _feeds(client)
    assert feeds[KREBS]["health"]["status"] == "unknown"
    assert feeds[KREBS]["health"]["summary"] == "never checked"


def test_the_recorded_outcome_reaches_the_column(client, tmp_path):
    record([
        {"url": KREBS, "name": "Krebs", "status": "ok", "items": 8},
        {"url": DEAD, "name": "Threatpost", "status": "error", "detail": "HTTP 404"},
    ], tmp_path / "digest.db")

    feeds = _feeds(client)
    assert feeds[KREBS]["health"]["status"] == "ok"
    assert "8 items" in feeds[KREBS]["health"]["summary"]
    assert feeds[DEAD]["health"]["status"] == "error"
    assert feeds[DEAD]["health"]["summary"].startswith("HTTP 404 · failing")


def test_health_endpoints_require_the_admin_token(client):
    assert client.get("/admin/sources").status_code == 401
    assert client.post("/admin/sources/check", json={}).status_code == 401


# --- check now --------------------------------------------------------------


@pytest.fixture
def probe(monkeypatch):
    """The live probe with the network replaced. Records which URLs it hit."""
    from types import SimpleNamespace

    hit = []

    def fake_parse(url):
        hit.append(url)
        if url == DEAD:
            import httpx
            raise httpx.HTTPStatusError(
                "gone", request=httpx.Request("GET", url), response=httpx.Response(410))
        return SimpleNamespace(entries=[{"title": "A", "link": "l", "summary": "s"}],
                               bozo=False, bozo_exception=None)

    monkeypatch.setattr("src.fetcher._parse_feed", fake_parse)
    return hit


def test_checking_everything_probes_every_feed_and_reports_the_failures(client, probe):
    r = client.post("/admin/sources/check", headers=AUTH, json={})
    data = r.json()
    assert r.status_code == 200 and data["ok"] is True
    assert sorted(probe) == sorted([KREBS, DEAD])
    assert data["health"][KREBS]["status"] == "ok"
    assert data["health"][DEAD]["status"] == "error"
    assert data["message"] == "Checked 2 feed(s) — 1 failed: Threatpost"


def test_checking_one_feed_probes_only_that_one(client, probe):
    r = client.post("/admin/sources/check", headers=AUTH, json={"url": KREBS})
    assert probe == [KREBS]
    assert r.json()["message"] == "Checked 1 feed(s) — all fetched."


def test_a_check_persists_so_the_column_survives_a_reload(client, probe):
    client.post("/admin/sources/check", headers=AUTH, json={})
    assert _feeds(client)[DEAD]["health"]["status"] == "error"


def test_the_check_will_not_fetch_a_url_that_is_not_configured(client, probe):
    """Otherwise an authenticated admin endpoint becomes a request forwarder
    that fetches whatever the caller names."""
    r = client.post("/admin/sources/check", headers=AUTH,
                    json={"url": "http://169.254.169.254/latest/meta-data/"})
    assert r.status_code == 404
    assert probe == []


def test_checking_an_instance_with_no_feeds_says_so(tmp_path, monkeypatch, probe):
    import src.db
    import src.web.app as web

    (tmp_path / "config.yaml").write_text(yaml.dump({"sources": {"rss": []}}))
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(web, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(src.db, "DB_PATH", tmp_path / "digest.db")
    monkeypatch.setenv("DIGEST_ADMIN_TOKEN", TOKEN)

    r = TestClient(web.app).post("/admin/sources/check", headers=AUTH, json={})
    assert r.json() == {"ok": True, "message": "No feeds to check.", "health": {}}


def test_the_quiet_threshold_is_configurable_per_instance(tmp_path, monkeypatch):
    """AISI Blog publishes roughly monthly and legitimately sits near the
    default, so an instance has to be able to move the line without a code
    change -- and moving it must re-read what is stored, not need a fresh run."""
    import datetime

    import src.db
    import src.web.app as web

    old = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(days=20)).isoformat(timespec="seconds")

    def build(quiet_after_days):
        root = tmp_path / f"inst{quiet_after_days}"
        (root / "data").mkdir(parents=True)
        (root / "config.yaml").write_text(yaml.dump({
            "sources": {"quiet_after_days": quiet_after_days,
                        "rss": [{"name": "Slow blog", "url": KREBS}]},
        }))
        monkeypatch.setattr(web, "PROJECT_ROOT", root)
        monkeypatch.setattr(src.db, "DB_PATH", root / "digest.db")
        monkeypatch.setenv("DIGEST_ADMIN_TOKEN", TOKEN)
        record([{"url": KREBS, "name": "Slow blog", "status": "ok", "items": 95,
                 "newest": old}], root / "digest.db")
        return TestClient(web.app).get("/admin/sources", headers=AUTH).json()["rss"][0]

    assert build(21)["health"]["status"] == "ok"
    assert build(14)["health"]["status"] == "quiet"
