"""The community docs site publishes from the public repo to its GitHub Pages host."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMMUNITY = ROOT / "docs" / "community"
CONFIG = ROOT / "mkdocs.community.yml"


def test_community_mkdocs_uses_the_public_repo_and_its_pages_host() -> None:
    text = CONFIG.read_text(encoding="utf-8")
    manifest = (ROOT / "scripts" / "community_export_manifest.json").read_text(encoding="utf-8")
    assert "mkdocs.community.yml" in manifest
    assert "https://github.com/seanventures/boss" in text
    assert "site_url: https://seanventures.github.io/boss/" in text
    private_owner = "seanheiney"
    assert private_owner not in text


def test_home_page_uses_the_receipt_and_local_stylesheet() -> None:
    config = CONFIG.read_text(encoding="utf-8")
    home = (COMMUNITY / "README.md").read_text(encoding="utf-8")
    css = (COMMUNITY / "stylesheets" / "extra.css").read_text(encoding="utf-8")
    assert "stylesheets/extra.css" in config
    assert "font: false" in config
    assert "assets/mark.svg" in config
    mark = (COMMUNITY / "assets" / "mark.svg").read_text(encoding="utf-8")
    assert "#1aa89a" in mark
    assert "boss-receipt" in home
    assert "boss-receipt__kicker" in home
    assert ".boss-receipt" in css
    assert "127.0.0.1:3000" in home


def test_pages_workflow_deploys_only_from_the_public_repo() -> None:
    text = (COMMUNITY / "publishing.md").read_text(encoding="utf-8")
    assert "https://seanventures.github.io/boss/" in text
    workflow = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    assert "mkdocs build -f mkdocs.community.yml --strict" in workflow
    assert workflow.count("github.repository == 'seanventures/boss'") == 2
