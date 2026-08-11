"""Tests for src.utils: slug() and render_template()."""

from src.utils import render_template, slug


def test_slug_basic():
    assert slug("AI Security") == "ai-security"


def test_slug_lowercases_and_replaces_spaces():
    assert slug("Security Digest") == "security-digest"


def test_slug_strips_unsafe_path_characters():
    """Regression check for task 3.8: '/' or ':' in a title used to produce a
    broken or unexpected file path wherever the slug was used to name a file."""
    result = slug("AI/ML Weekly: Digest")
    assert "/" not in result
    assert ":" not in result
    assert result == "aiml-weekly-digest"


def test_render_template_substitutes_named_placeholders(tmp_path):
    path = tmp_path / "t.txt"
    path.write_text("Hello {name}, today is {date}.")
    out = render_template(path, name="World", date="2026-01-01")
    assert out == "Hello World, today is 2026-01-01."


def test_render_template_leaves_literal_braces_untouched(tmp_path):
    """The bug task 2.2 fixed: str.format() would raise KeyError here."""
    path = tmp_path / "t.txt"
    path.write_text('Example: {"key": "value"} and {items}')
    out = render_template(path, items="X")
    assert out == 'Example: {"key": "value"} and X'


def test_render_template_reloads_file_on_each_call(tmp_path):
    """The bug task 2.1 fixed: prompts were cached at import time."""
    path = tmp_path / "t.txt"
    path.write_text("v1 {x}")
    assert render_template(path, x="!") == "v1 !"
    path.write_text("v2 {x}")
    assert render_template(path, x="!") == "v2 !"
