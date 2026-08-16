"""Tests for the parts of src.delivery that don't depend on PROJECT_ROOT or touch
SMTP/network -- _deliver_file takes an explicit file_path and has neither."""

import pytest

from src.delivery import _deliver_file, _resolve_email_config


def test_deliver_file_uses_slug_for_safe_filename(tmp_path):
    """Regression check for task 3.8: _deliver_file used to build its own filename
    with title.replace(" ", "-").lower(), so a title containing '/' would create
    an unexpected nested path instead of a single file."""
    delivery_cfg = {"file_path": str(tmp_path / "digest.md")}
    _deliver_file("content", delivery_cfg, title="AI/ML Weekly: Digest")

    files = list(tmp_path.iterdir())
    assert [f.name for f in files] == ["aiml-weekly-digest.md"]
    assert (tmp_path / "aiml-weekly-digest.md").read_text() == "content"


def test_deliver_file_without_title_uses_base_path(tmp_path):
    path = tmp_path / "digest.md"
    _deliver_file("content", {"file_path": str(path)}, title=None)
    assert path.read_text() == "content"


# --- per-digest recipients -------------------------------------------------
# One instance serves several readers by giving each digest its own `to:`
# (derived from users.yaml -- see src/recipients.py). Getting this wrong sends
# one person's topics to another, so the resolution is pinned here rather than
# left to the delivery path's parameter threading.

BASE_EMAIL_CONFIG = {
    "delivery": {
        "output": "email",
        "email": {"from": "sender@example.com", "to": "fallback@example.com"},
    }
}


@pytest.fixture
def smtp_password(monkeypatch):
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    monkeypatch.delenv("SMTP_USER", raising=False)
    # Cleared explicitly: load_dotenv() runs at import, so a developer with real
    # addresses in .env would otherwise have them override every config-based
    # expectation below -- tests that pass or fail depending on an untracked file.
    monkeypatch.delenv("DIGEST_EMAIL_FROM", raising=False)
    monkeypatch.delenv("DIGEST_EMAIL_TO", raising=False)


def _recipients(config, digest_cfg=None):
    return _resolve_email_config(config, digest_cfg)[3]


def test_digest_to_overrides_global_recipient(smtp_password):
    assert _recipients(BASE_EMAIL_CONFIG, {"to": "her@example.com"}) == ["her@example.com"]


def test_digest_without_to_falls_back_to_global(smtp_password):
    assert _recipients(BASE_EMAIL_CONFIG, {"title": "No recipient"}) == ["fallback@example.com"]
    assert _recipients(BASE_EMAIL_CONFIG, None) == ["fallback@example.com"]


def test_two_digests_resolve_to_different_recipients(smtp_password):
    """The whole point: one run, one SMTP account, two readers."""
    hers = _recipients(BASE_EMAIL_CONFIG, {"to": "her@example.com"})
    his = _recipients(BASE_EMAIL_CONFIG, {"to": "him@example.com"})
    assert hers == ["her@example.com"] and his == ["him@example.com"]
    assert not set(hers) & set(his)


def test_digest_to_accepts_a_list_and_a_comma_separated_string(smtp_password):
    assert _recipients(BASE_EMAIL_CONFIG, {"to": ["a@x.com", "b@x.com"]}) == ["a@x.com", "b@x.com"]
    assert _recipients(BASE_EMAIL_CONFIG, {"to": "a@x.com, b@x.com"}) == ["a@x.com", "b@x.com"]


def test_empty_digest_to_falls_back_rather_than_sending_nowhere(smtp_password):
    """An empty `to:` must not resolve to an empty recipient list -- that would
    either raise or silently send to nobody depending on the SMTP server."""
    assert _recipients(BASE_EMAIL_CONFIG, {"to": ""}) == ["fallback@example.com"]
    assert _recipients(BASE_EMAIL_CONFIG, {"to": []}) == ["fallback@example.com"]


def test_missing_recipient_everywhere_still_raises(smtp_password):
    with pytest.raises(ValueError, match="to"):
        _resolve_email_config({"delivery": {"email": {"from": "s@x.com"}}}, {"title": "None"})


def test_sender_is_not_per_digest(smtp_password):
    """Only the recipient varies: everything goes out from the one configured
    account, over the one SMTP connection."""
    _, _, from_addr, _, user, _ = _resolve_email_config(
        BASE_EMAIL_CONFIG, {"to": "her@example.com", "from": "spoofed@example.com"}
    )
    assert from_addr == "sender@example.com"
    assert user == "sender@example.com"


# Addresses from .env -----------------------------------------------------
# config.yaml is committed and is pushed over the target's copy on every deploy,
# so an address there is both published and fragile. Sanitising it for a public
# repo left `to: you@example.com`, which the next deploy would have sent every
# digest to.

def test_env_addresses_override_config(smtp_password, monkeypatch):
    monkeypatch.setenv("DIGEST_EMAIL_FROM", "real-sender@example.com")
    monkeypatch.setenv("DIGEST_EMAIL_TO", "real@example.com")

    _, _, from_addr, to_addrs, _, _ = _resolve_email_config(BASE_EMAIL_CONFIG)

    assert from_addr == "real-sender@example.com"
    assert to_addrs == ["real@example.com"]


def test_a_digests_own_recipient_still_beats_the_env(smtp_password, monkeypatch):
    """Per-digest `to:` is routing, not a default -- it must outrank the global
    address wherever that comes from, or every reader gets everyone's digest."""
    monkeypatch.setenv("DIGEST_EMAIL_TO", "global@example.com")

    assert _recipients(BASE_EMAIL_CONFIG, {"to": "her@example.com"}) == ["her@example.com"]


def test_env_recipients_accept_a_comma_separated_list(smtp_password, monkeypatch):
    monkeypatch.setenv("DIGEST_EMAIL_TO", "a@example.com, b@example.com")

    assert _recipients(BASE_EMAIL_CONFIG) == ["a@example.com", "b@example.com"]


def test_config_without_addresses_works_when_the_env_supplies_them(smtp_password, monkeypatch):
    """The shipped config.yaml carries no addresses at all now."""
    monkeypatch.setenv("DIGEST_EMAIL_FROM", "s@example.com")
    monkeypatch.setenv("DIGEST_EMAIL_TO", "r@example.com")
    config = {"delivery": {"output": "email", "email": {"smtp_host": "smtp.example.com"}}}

    _, _, from_addr, to_addrs, _, _ = _resolve_email_config(config)

    assert (from_addr, to_addrs) == ("s@example.com", ["r@example.com"])


def test_no_addresses_anywhere_names_the_env_vars(smtp_password):
    """The error has to say where to put them, or the next person edits
    config.yaml again and the deploy overwrites it again."""
    config = {"delivery": {"output": "email", "email": {"smtp_host": "smtp.example.com"}}}

    with pytest.raises(ValueError, match="DIGEST_EMAIL_FROM and DIGEST_EMAIL_TO"):
        _resolve_email_config(config)
