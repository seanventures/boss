"""The community docs site is served at boss.sean.network; the public repo only checks the build."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMMUNITY = ROOT / "docs" / "community"
CONFIG = ROOT / "mkdocs.community.yml"


def test_community_mkdocs_uses_the_public_repo_and_the_site_host() -> None:
    text = CONFIG.read_text(encoding="utf-8")
    manifest = (ROOT / "scripts" / "community_export_manifest.json").read_text(encoding="utf-8")
    assert "mkdocs.community.yml" in manifest
    assert "https://github.com/seanventures/boss" in text
    assert "site_url: https://boss.sean.network/docs/" in text
    assert "homepage: https://boss.sean.network/" in text
    assert "github.io/boss" not in text
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


def test_docs_workflow_only_builds_in_the_public_repo() -> None:
    text = (COMMUNITY / "publishing.md").read_text(encoding="utf-8")
    assert "https://boss.sean.network/" in text
    assert "https://boss.sean.network/docs/" in text
    assert "github.io/boss" not in text
    workflow = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    assert "mkdocs build -f mkdocs.community.yml --strict -d _site/docs" in workflow
    assert "cp -R docs/landing/. _site/" in workflow
    assert workflow.count("github.repository == 'seanventures/boss'") == 1
    for step in ("deploy-pages", "configure-pages", "upload-pages-artifact", "id-token", "pages: write"):
        assert step not in workflow, step
    assert "pull_request:" in workflow
    assert "branches: [main]" in workflow


def test_landing_page_links_resolve_and_count_is_redacted_on_export() -> None:
    import re
    import sys

    site = ROOT / "docs" / "landing"
    page = (site / "index.html").read_text(encoding="utf-8")
    for local in re.findall(r'(?:href|src)="((?:assets/|styles|app)[^"#]*)"', page):
        assert (site / local).exists(), local
    manual = {path.stem for path in COMMUNITY.glob("*.md")}
    for slug in re.findall(r'href="docs/([^"/]*)/?"', page):
        assert not slug or slug in manual, slug
    assert "goldeneye" not in page.lower()
    assert '<meta property="og:url" content="https://boss.sean.network/">' in page
    assert '<link rel="canonical" href="https://boss.sean.network/">' in page
    sys.path.insert(0, str(ROOT / "scripts"))
    import community_private_host as private_host

    public = private_host.redact_private_host("docs/landing/index.html", page.encode()).decode()
    assert '<b data-count="api">983</b>' in public
    assert "994" not in public
