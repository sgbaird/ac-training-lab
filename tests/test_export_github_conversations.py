"""Unit tests for the pure helpers in scripts/export_github_conversations.py.

The exporter's network-dependent code paths are not exercised here; only the
side-effect-free helpers (mention neutralization, asset extraction/localization,
and Link-header parsing) are validated.
"""

import importlib.util
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "export_github_conversations.py"
)
_spec = importlib.util.spec_from_file_location("export_github_conversations", _SCRIPT)
export = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export)


def test_neutralize_mentions_inserts_zero_width_space():
    result = export.neutralize_mentions("thanks @octocat and @sgbaird!")
    assert "@octocat" not in result
    assert f"@{export.ZERO_WIDTH_SPACE}octocat" in result
    assert f"@{export.ZERO_WIDTH_SPACE}sgbaird" in result


def test_neutralize_mentions_ignores_emails():
    text = "email me at user@example.com"
    assert export.neutralize_mentions(text) == text


def test_neutralize_mentions_handles_none():
    assert export.neutralize_mentions(None) == ""


def test_extract_asset_urls_markdown_and_html():
    text = (
        "![shot](https://example.com/a.png)\n"
        '<img src="https://example.com/b.jpg" />\n'
        "![again](<https://example.com/a.png>)\n"
        "![rel](not-a-url.png)"
    )
    urls = export.extract_asset_urls(text)
    assert urls == [
        "https://example.com/a.png",
        "https://example.com/b.jpg",
    ]


def test_asset_filename_is_stable_and_keeps_suffix():
    url = "https://example.com/path/image.png"
    name = export.asset_filename(url)
    assert name.endswith(".png")
    assert name == export.asset_filename(url)


def test_localize_assets_rewrites_urls():
    text = "See ![x](https://example.com/a.png) and <https://example.com/a.png>"
    mapping = {"https://example.com/a.png": "assets/abc.png"}
    result = export.localize_assets(text, mapping)
    assert "https://example.com/a.png" not in result
    assert "assets/abc.png" in result


def test_next_link_parses_rel_next():
    header = (
        '<https://api.github.com/x?page=2>; rel="next", '
        '<https://api.github.com/x?page=5>; rel="last"'
    )
    assert export._next_link(header) == "https://api.github.com/x?page=2"
    assert export._next_link("") is None
