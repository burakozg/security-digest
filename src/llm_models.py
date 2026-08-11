"""Curated OpenAI / Anthropic / Mistral / OpenRouter models for admin UI and validation.

Prices are approximate list USD per 1M tokens (input / output); they change on
provider sites -- use for comparison only, not billing. OpenAI and Anthropic
entries verified 2026-07-13, Mistral entries 2026-07-27, OpenRouter entries
2026-08-04, by fetching current
model lists/pricing directly from the providers (not from training-data memory,
which goes stale). Review roughly every 3 months -- prompt for a refresh:
"Refresh the model catalog in src/llm_models.py per task 3.7 in IMPROVEMENTS.md."

Note: Claude Sonnet 5's list price is $3.00/$15.00 per 1M tokens; an
introductory $2.00/$10.00 rate applies through 2026-08-31.

Mistral entries use the "-latest" aliases rather than dated ids
(mistral-small-2603, mistral-medium-2604, mistral-large-2512 as of 2026-07-27)
so the dropdown doesn't pin a version that gets retired; the tradeoff is that
prices here drift when an alias moves to a new release. A dated id can still be
typed into the admin panel's custom-model field -- it is validated live.

OpenRouter is a router, not a model vendor: its ids are ``vendor/model`` and it
bills at the underlying vendor's rate (plus its own margin). Only models whose
OpenRouter listing reports ``response_format`` support are included -- this
pipeline constrains every response with a JSON schema, and a model routed
without that support returns free text the parser then rejects. Hundreds more
ids are available and can be typed into the custom-model field; they are
validated live, but structured-output support is not checked there.

The Qwen entries are the instruct models, deliberately not the "-thinking",
"-coder" or "-vl" variants: this pipeline asks for a one-line summary and a
category per item, so reasoning tokens are billed as output for no benefit
(qwen3-30b-a3b-thinking-2507 costs $2.40/1M out against $0.13 for the flash
instruct), and the coder/vision tunings are irrelevant to prose.

``price_tier`` is 1-4 ($ to $$$$) relative to models in this catalog.
"""

import logging
from typing import Any

log = logging.getLogger(__name__)


def catalog() -> list[dict[str, Any]]:
    """Provider + model list for admin dropdown (recommended = good default for batch digest work)."""
    # Fields: input_usd_per_mtok, output_usd_per_mtok (USD per 1M tokens), price_tier 1-4
    return [
        {"provider": "anthropic", "id": "claude-haiku-4-5", "label": "Claude Haiku 4.5", "recommended": True, "input_usd_per_mtok": 1.00, "output_usd_per_mtok": 5.00, "price_tier": 1},
        {"provider": "anthropic", "id": "claude-sonnet-5", "label": "Claude Sonnet 5", "recommended": True, "input_usd_per_mtok": 3.00, "output_usd_per_mtok": 15.00, "price_tier": 2},
        {"provider": "anthropic", "id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6", "recommended": False, "input_usd_per_mtok": 3.00, "output_usd_per_mtok": 15.00, "price_tier": 2},
        {"provider": "anthropic", "id": "claude-opus-4-8", "label": "Claude Opus 4.8", "recommended": False, "input_usd_per_mtok": 5.00, "output_usd_per_mtok": 25.00, "price_tier": 3},
        {"provider": "openai", "id": "gpt-5.6-luna", "label": "GPT-5.6 Luna", "recommended": True, "input_usd_per_mtok": 1.00, "output_usd_per_mtok": 6.00, "price_tier": 1},
        {"provider": "openai", "id": "gpt-5.6-terra", "label": "GPT-5.6 Terra", "recommended": True, "input_usd_per_mtok": 2.50, "output_usd_per_mtok": 15.00, "price_tier": 2},
        {"provider": "openai", "id": "gpt-5.6-sol", "label": "GPT-5.6 Sol", "recommended": False, "input_usd_per_mtok": 5.00, "output_usd_per_mtok": 30.00, "price_tier": 3},
        {"provider": "mistral", "id": "mistral-small-latest", "label": "Mistral Small 4", "recommended": True, "input_usd_per_mtok": 0.15, "output_usd_per_mtok": 0.60, "price_tier": 1},
        {"provider": "mistral", "id": "mistral-large-latest", "label": "Mistral Large 3", "recommended": False, "input_usd_per_mtok": 0.50, "output_usd_per_mtok": 1.50, "price_tier": 1},
        {"provider": "mistral", "id": "mistral-medium-latest", "label": "Mistral Medium 3.5", "recommended": False, "input_usd_per_mtok": 1.50, "output_usd_per_mtok": 7.50, "price_tier": 2},
        {"provider": "openrouter", "id": "qwen/qwen3.7-flash", "label": "Qwen3.7 Flash", "recommended": True, "input_usd_per_mtok": 0.03, "output_usd_per_mtok": 0.13, "price_tier": 1},
        {"provider": "openrouter", "id": "google/gemini-2.5-flash-lite", "label": "Gemini 2.5 Flash Lite", "recommended": True, "input_usd_per_mtok": 0.10, "output_usd_per_mtok": 0.40, "price_tier": 1},
        {"provider": "openrouter", "id": "qwen/qwen3-235b-a22b-2507", "label": "Qwen3 235B A22B Instruct", "recommended": False, "input_usd_per_mtok": 0.15, "output_usd_per_mtok": 0.60, "price_tier": 1},
        {"provider": "openrouter", "id": "deepseek/deepseek-chat-v3.1", "label": "DeepSeek V3.1", "recommended": True, "input_usd_per_mtok": 0.25, "output_usd_per_mtok": 0.95, "price_tier": 1},
        {"provider": "openrouter", "id": "google/gemini-2.5-flash", "label": "Gemini 2.5 Flash", "recommended": False, "input_usd_per_mtok": 0.30, "output_usd_per_mtok": 2.50, "price_tier": 1},
        {"provider": "openrouter", "id": "anthropic/claude-haiku-4.5", "label": "Claude Haiku 4.5 (via OpenRouter)", "recommended": False, "input_usd_per_mtok": 1.00, "output_usd_per_mtok": 5.00, "price_tier": 1},
        {"provider": "openrouter", "id": "anthropic/claude-sonnet-5", "label": "Claude Sonnet 5 (via OpenRouter)", "recommended": False, "input_usd_per_mtok": 2.00, "output_usd_per_mtok": 10.00, "price_tier": 2},
    ]


def is_catalog_model(provider: str, model_id: str) -> bool:
    """Fast, no-network check against the curated catalog only."""
    return any(m["provider"] == provider and m["id"] == model_id for m in catalog())


def is_valid_model(provider: str, model_id: str) -> bool:
    """True if model_id is in the curated catalog, or -- for anything else -- if the
    provider's live Models API confirms it exists. This lets a brand-new model be
    used the day it ships without a code change or redeploy, instead of hard-
    rejecting anything not in the (inevitably stale) catalog.

    Falls back to rejecting if the live check itself fails (network error, missing/
    bad API key) rather than raising -- an unreachable provider shouldn't be
    confused with a genuinely invalid model id; the failure is logged either way.
    """
    if is_catalog_model(provider, model_id):
        return True

    try:
        if provider == "anthropic":
            from anthropic import Anthropic
            Anthropic().models.retrieve(model_id)
            return True
        if provider == "openai":
            from openai import OpenAI
            OpenAI().models.retrieve(model_id)
            return True
        if provider in ("mistral", "openrouter"):
            import os

            from openai import OpenAI

            from src.summariser import MISTRAL_BASE_URL, OPENROUTER_BASE_URL
            env_var = "MISTRAL_API_KEY" if provider == "mistral" else "OPENROUTER_API_KEY"
            base_url = MISTRAL_BASE_URL if provider == "mistral" else OPENROUTER_BASE_URL
            api_key = os.environ.get(env_var)
            if not api_key:
                log.warning("%s not set; cannot validate %s live", env_var, model_id)
                return False
            OpenAI(base_url=base_url, api_key=api_key).models.retrieve(model_id)
            return True
    except Exception as e:
        log.warning("Live model validation failed for %s/%s: %s", provider, model_id, e)
        return False

    return False
