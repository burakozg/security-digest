"""OpenRouter entries in the curated model catalog."""
from src.llm_models import catalog, is_catalog_model, is_valid_model


def test_openrouter_models_are_in_the_catalog():
    ids = [m["id"] for m in catalog() if m["provider"] == "openrouter"]
    assert ids, "no openrouter models listed"
    # OpenRouter ids are vendor-qualified; a bare id would route nowhere.
    assert all("/" in i for i in ids), ids


def test_openrouter_catalog_entries_are_complete():
    for m in [m for m in catalog() if m["provider"] == "openrouter"]:
        assert m["input_usd_per_mtok"] > 0 and m["output_usd_per_mtok"] > 0, m
        assert 1 <= m["price_tier"] <= 4, m
        assert m["label"], m


def test_catalog_lookup_is_provider_scoped():
    """A model id must not validate under the wrong provider -- the same name
    can exist on more than one, and routing it to the wrong endpoint fails at
    request time rather than at save time."""
    assert is_catalog_model("openrouter", "google/gemini-2.5-flash-lite")
    assert not is_catalog_model("openai", "google/gemini-2.5-flash-lite")
    assert not is_catalog_model("openrouter", "claude-haiku-4-5")


def test_unknown_openrouter_model_without_a_key_is_rejected_not_crashed(monkeypatch):
    """Live validation needs a key; without one it must decline rather than
    raise, so the admin panel shows a message instead of a 500."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert is_valid_model("openrouter", "some/unreleased-model") is False
