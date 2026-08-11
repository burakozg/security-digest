"""Tests for the token consumption log."""

import datetime
from types import SimpleNamespace

from src.usage import daily, estimate_cost, extract_usage, record


class _AnthropicResp:
    """Shape confirmed against the installed SDK: input_tokens/output_tokens."""
    usage = SimpleNamespace(input_tokens=1200, output_tokens=340,
                            cache_read_input_tokens=0, service_tier="standard")


class _OpenAIResp:
    """OpenAI and every OpenAI-compatible endpoint: prompt/completion_tokens."""
    usage = SimpleNamespace(prompt_tokens=900, completion_tokens=210, total_tokens=1110)


def test_extract_usage_reads_the_anthropic_shape():
    assert extract_usage(_AnthropicResp()) == (1200, 340)


def test_extract_usage_reads_the_openai_shape():
    assert extract_usage(_OpenAIResp()) == (900, 210)


def test_extract_usage_returns_none_when_absent():
    """An endpoint that omits usage must degrade to 'not logged' rather than
    logging zeros, which would look like a real measurement of nothing."""
    assert extract_usage(SimpleNamespace()) is None
    assert extract_usage(SimpleNamespace(usage=None)) is None
    assert extract_usage(SimpleNamespace(usage=SimpleNamespace(other=1))) is None


def test_cost_uses_the_catalog_price():
    # claude-haiku-4-5 is $1.00 in / $5.00 out per 1M.
    assert estimate_cost("anthropic", "claude-haiku-4-5", 1_000_000, 1_000_000) == 6.0
    assert estimate_cost("anthropic", "claude-haiku-4-5", 500_000, 0) == 0.5


def test_cost_is_none_for_a_model_not_in_the_catalog():
    """None, not 0.0: a zero understates the day's spend while looking like a
    measurement, whereas a gap is visibly a gap."""
    assert estimate_cost("openrouter", "someone/unlisted-model", 1_000_000, 1_000_000) is None


def test_cost_lookup_is_provider_scoped():
    assert estimate_cost("openai", "claude-haiku-4-5", 1_000_000, 0) is None


def test_record_and_daily_totals(tmp_path):
    db = tmp_path / "digest.db"
    record("anthropic", "claude-haiku-4-5", 1000, 200, kind="cluster", db_path=db)
    record("anthropic", "claude-haiku-4-5", 500, 100, kind="batch", db_path=db)

    rows = daily(days=7, db_path=db)
    assert len(rows) == 1
    today = rows[0]
    assert today["day"] == datetime.date.today().isoformat()
    assert today["calls"] == 2
    assert today["input_tokens"] == 1500
    assert today["output_tokens"] == 300
    assert today["total_tokens"] == 1800
    assert today["cost_is_partial"] is False
    assert today["cost_usd"] == round(1500 / 1e6 * 1.0 + 300 / 1e6 * 5.0, 4)
    assert today["models"] == ["anthropic/claude-haiku-4-5"]


def test_an_uncosted_call_marks_the_day_partial(tmp_path):
    """Otherwise a day's total reads as complete when part of it is missing."""
    db = tmp_path / "digest.db"
    record("anthropic", "claude-haiku-4-5", 1000, 0, db_path=db)
    record("openrouter", "someone/unlisted-model", 5_000_000, 0, db_path=db)

    today = daily(days=1, db_path=db)[0]
    assert today["calls"] == 2
    assert today["input_tokens"] == 5_001_000     # tokens are still counted
    assert today["cost_is_partial"] is True       # but the money is a lower bound


def test_recording_never_raises_even_on_a_broken_database(tmp_path):
    """Accounting must not be able to fail a digest that was produced fine."""
    unwritable = tmp_path / "nope"
    unwritable.write_text("not a database")
    record("anthropic", "claude-haiku-4-5", 1, 1, db_path=unwritable)   # must not raise


def test_daily_window_excludes_older_days(tmp_path):
    db = tmp_path / "digest.db"
    record("anthropic", "claude-haiku-4-5", 10, 1, db_path=db)
    from src.db import get_connection
    old = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
    conn = get_connection(db)
    conn.execute(
        "INSERT INTO token_usage (at, day, provider, model, kind, input_tokens, output_tokens, cost_usd) "
        "VALUES (?, ?, 'anthropic', 'claude-haiku-4-5', 'batch', 999, 0, 0.001)",
        (old + "T09:00:00", old),
    )
    conn.commit(); conn.close()

    assert [r["day"] for r in daily(days=3, db_path=db)] == [datetime.date.today().isoformat()]
    assert len(daily(days=30, db_path=db)) == 2


def test_a_dated_model_id_is_priced_as_its_alias():
    """config.yaml pins dated ids (claude-haiku-4-5-20251001) while the catalog
    lists aliases, so exact-match-only left the project's own default
    configuration with no price at all."""
    dated = estimate_cost("anthropic", "claude-haiku-4-5-20251001", 1_000_000, 1_000_000)
    alias = estimate_cost("anthropic", "claude-haiku-4-5", 1_000_000, 1_000_000)
    assert dated == alias == 6.0


def test_the_longest_matching_alias_wins():
    """claude-sonnet-4-6-2026xxxx must price as claude-sonnet-4-6, never as a
    shorter id that happens to prefix it."""
    from src.usage import _price_for
    assert _price_for("anthropic", "claude-sonnet-4-6-20260101") == (3.00, 15.00)


def test_a_different_release_does_not_borrow_a_price():
    """mistral-small-2603 is a distinct release from mistral-small-latest; the
    suffix rule requires a version-looking tail, so it stays unpriced rather
    than silently inheriting a rate that may not apply."""
    assert estimate_cost("mistral", "mistral-small", 1_000_000, 0) is None


def test_openrouter_vendor_ids_still_match_exactly():
    assert estimate_cost("openrouter", "qwen/qwen3.7-flash", 1_000_000, 0) == 0.03
