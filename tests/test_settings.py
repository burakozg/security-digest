"""Tests for src.settings: pydantic validation of the merged config dict."""

import logging

import pytest
from pydantic import ValidationError

from src.settings import Settings, validate_config, warn_on_unwired_sources


def test_validate_config_accepts_empty_dict():
    """Matches the previous .get(..., default) behavior -- a config missing
    everything must not fail validation, since every field has a default."""
    settings = validate_config({})
    assert settings.llm.provider == "openai"
    assert settings.retry.max_retries == 3


def test_validate_config_accepts_realistic_config():
    config = {
        "retry": {"max_retries": 5, "initial_delay": 2.0, "max_delay": 30.0},
        "sources": {
            "max_total_items": 50,
            "seen_retention_days": 14,
            "rss": [{"name": "Krebs on Security", "url": "https://krebsonsecurity.com/feed/"}],
        },
        "llm": {"provider": "anthropic", "model": "claude-haiku-4-5", "temperature": 0.3},
        "digests": [{"title": "Security Digest", "sections": ["news", "thought_leadership"]}],
        "schedule": {"enabled": True, "hour": 7, "minute": 0, "timezone": "Europe/Stockholm"},
        "delivery": {
            "output": "email",
            "email": {"smtp_host": "smtp.gmail.com", "smtp_port": 587, "from": "a@b.com", "to": "a@b.com"},
        },
    }
    settings = validate_config(config)
    assert settings.llm.provider == "anthropic"
    assert settings.sources.rss[0].name == "Krebs on Security"
    assert settings.digests[0].title == "Security Digest"
    assert settings.delivery.email.from_ == "a@b.com"


def test_validate_config_rejects_wrong_type():
    """The bug class task 3.3 targets: a typo'd or wrong-type value used to
    silently fall through .get(..., default) chains deep in the pipeline."""
    with pytest.raises(ValidationError, match="seen_retention_days"):
        validate_config({"sources": {"seen_retention_days": "not-a-number"}})


def test_validate_config_ignores_unknown_fields():
    """extra="allow": config.yaml's on-disk shape must not need to change --
    an unrecognised field (e.g. one this model hasn't been taught about yet)
    must not break validation."""
    settings = validate_config({"some_future_field": "value", "llm": {"provider": "openai"}})
    assert settings.llm.provider == "openai"


def test_settings_model_defaults_are_self_consistent():
    """A completely bare Settings() must construct without error -- every field
    needs a usable default, or an empty config.yaml would fail validation."""
    settings = Settings()
    assert settings.web.min_run_interval_minutes == 30
    assert settings.history.max_entries == 10000


def test_wiring_warnings_are_logged_once_per_process(caplog):
    """Config is re-read on every HTTP request and the dashboard polls every few
    seconds; logging each time buried a real traceback under thousands of
    identical lines."""
    import src.settings as settings_module
    settings_module._warned.clear()

    config = {
        "sources": {"rss": [{"name": "Orphan Feed", "url": "https://example.com/f"}]},
        "digests": [{"title": "D", "sources": ["Something Else"]}],
    }

    with caplog.at_level(logging.WARNING, logger="src.settings"):
        first = warn_on_unwired_sources(config)
        second = warn_on_unwired_sources(config)

    # The caller still gets the full picture both times...
    assert first == second and first
    # ...but the log records it once.
    assert len(caplog.records) == len(first)
