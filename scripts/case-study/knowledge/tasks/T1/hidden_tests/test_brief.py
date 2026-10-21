"""Hidden acceptance for T1. It checks the brief, not a live Saleor fork."""
from pathlib import Path


def test_brief_uses_the_documentation_host():
    text = Path(__file__).resolve().parents[1].joinpath("brief.md").read_text(encoding="utf-8")
    assert "203.0.113.10" in text
    assert "hooks.example.com" in text
