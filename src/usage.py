"""Token consumption log.

Every LLM call records what it cost in tokens, so daily spend is a fact rather
than a guess. This matters here because the pipeline's cost is not proportional
to anything visible in the digest: clustering sends a whole topic's items in one
prompt, a retry re-sends the same prompt, and a model switch can change the bill
by a factor of thirty without changing a single line of output.

Stored in the same SQLite database as the seen-store, status and history
(data/digest.db), so it is per instance automatically and needs no new mount.

Cost is estimated, not billed: prices come from the curated catalog in
src/llm_models.py and are only as fresh as that file. A model absent from the
catalog -- anything typed into the admin panel's custom-model field -- logs its
tokens with a NULL cost rather than a wrong one, so the token counts stay
trustworthy even when the money column can't be.
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import Any

from src.db import get_connection

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    day TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    kind TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL
)
"""
_SCHEMA_INDEX = "CREATE INDEX IF NOT EXISTS idx_token_usage_day ON token_usage(day)"

# Keep roughly a year: enough to see seasonality and the effect of a model
# change, small enough that the table never needs thinking about.
DEFAULT_RETENTION_DAYS = 400


def _ensure_schema(conn) -> None:
    conn.execute(_SCHEMA)
    conn.execute(_SCHEMA_INDEX)


def _price_for(provider: str, model: str) -> tuple[float, float] | None:
    """(input, output) USD per 1M tokens for a model, or None if unlisted.

    Falls back to the longest catalog id that this model is a dated release of:
    config.yaml pins `claude-haiku-4-5-20251001` while the catalog lists the
    `claude-haiku-4-5` alias, so an exact-match-only lookup left the project's
    own default configuration with no price at all. The suffix must look like a
    version (`-` followed by a digit) so that, say, `mistral-small` never picks
    up `mistral-small-latest`'s price by accident -- a different release with
    genuinely different rates."""
    from src.llm_models import catalog

    entries = [m for m in catalog() if m["provider"] == provider]
    for m in entries:
        if m["id"] == model:
            return m["input_usd_per_mtok"], m["output_usd_per_mtok"]

    best: dict[str, Any] | None = None
    for m in entries:
        suffix = model[len(m["id"]):]
        if model.startswith(m["id"]) and len(suffix) > 1 and suffix[0] == "-" and suffix[1].isdigit():
            if best is None or len(m["id"]) > len(best["id"]):
                best = m
    return (best["input_usd_per_mtok"], best["output_usd_per_mtok"]) if best else None


def estimate_cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> float | None:
    """USD estimate from the curated catalog, or None if the model isn't in it.

    None rather than 0.0: a zero would silently understate a day's spend and
    look like a real measurement, whereas a gap is visibly a gap."""
    price = _price_for(provider, model)
    if price is None:
        return None
    return round(input_tokens / 1_000_000 * price[0] + output_tokens / 1_000_000 * price[1], 6)


def extract_usage(response: Any) -> tuple[int, int] | None:
    """Pull (input, output) token counts off a provider response.

    Two shapes, both confirmed against the installed SDKs rather than assumed:
    Anthropic reports usage.input_tokens/output_tokens, while OpenAI and every
    OpenAI-compatible endpoint report usage.prompt_tokens/completion_tokens.
    Returns None when a response carries no usage at all, so an endpoint that
    omits it degrades to "not logged" instead of logging zeros."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return None

    inp = getattr(usage, "input_tokens", None)
    out = getattr(usage, "output_tokens", None)
    if inp is None and out is None:
        inp = getattr(usage, "prompt_tokens", None)
        out = getattr(usage, "completion_tokens", None)
    if inp is None and out is None:
        return None
    return int(inp or 0), int(out or 0)


def record(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    kind: str = "summarise",
    db_path: Path | str | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> None:
    """Log one call's token use. Never raises: a failure to record accounting
    must not fail the digest that was otherwise produced successfully."""
    now = datetime.datetime.now()
    try:
        conn = get_connection(db_path)
        try:
            _ensure_schema(conn)
            conn.execute(
                "INSERT INTO token_usage (at, day, provider, model, kind, input_tokens, output_tokens, cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    now.isoformat(timespec="seconds"),
                    now.date().isoformat(),
                    provider,
                    model,
                    kind,
                    int(input_tokens),
                    int(output_tokens),
                    estimate_cost(provider, model, input_tokens, output_tokens),
                ),
            )
            cutoff = (now - datetime.timedelta(days=retention_days)).date().isoformat()
            conn.execute("DELETE FROM token_usage WHERE day < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("Could not record token usage: %s", e)


def daily(days: int = 30, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    """Per-day totals, newest first.

    `cost_usd` sums only the calls whose model was in the catalog;
    `cost_is_partial` says whether any call that day was left out of it, so a
    total is never quietly read as complete when it isn't."""
    since = (datetime.date.today() - datetime.timedelta(days=days - 1)).isoformat()
    conn = get_connection(db_path)
    try:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT day, "
            "       SUM(input_tokens)  AS input_tokens, "
            "       SUM(output_tokens) AS output_tokens, "
            "       COUNT(*)           AS calls, "
            "       SUM(COALESCE(cost_usd, 0)) AS cost_usd, "
            "       SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS uncosted, "
            "       GROUP_CONCAT(DISTINCT provider || '/' || model) AS models "
            "FROM token_usage WHERE day >= ? GROUP BY day ORDER BY day DESC",
            (since,),
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            "day": r["day"],
            "input_tokens": r["input_tokens"] or 0,
            "output_tokens": r["output_tokens"] or 0,
            "total_tokens": (r["input_tokens"] or 0) + (r["output_tokens"] or 0),
            "calls": r["calls"],
            "cost_usd": round(r["cost_usd"] or 0.0, 4),
            "cost_is_partial": bool(r["uncosted"]),
            "models": sorted((r["models"] or "").split(",")) if r["models"] else [],
        }
        for r in rows
    ]
