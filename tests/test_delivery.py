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
