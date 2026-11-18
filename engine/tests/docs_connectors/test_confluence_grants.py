"""Who holds the Confluence tools (BOSSAPP-1384): the read tools go to the roles that read tickets
(and the documentation writer); publishing is a proposal granted only to the documentation writer;
page text is low-trust outside text; approving the proposal is a carried-out decision; and the
only hosts a Confluence credential may reach are Atlassian's."""
from __future__ import annotations

from pathlib import Path

import yaml

from boss_engine.services import agent_baseline, decision_actions, decision_words, env_refs, low_trust
from boss_engine.tools import build_default_registry

ROOT = Path(__file__).resolve().parents[3]


def _roles() -> dict[str, list[str]]:
    out = {}
    for path in (ROOT / "packages/specpack/roles").glob("*.yaml"):
        data = yaml.safe_load(path.read_text())
        out[data["role_id"]] = list((data.get("permissions") or {}).get("allowed_skills") or [])
    return out


def test_read_goes_to_the_ticket_readers_and_publish_only_to_the_documentation_writer():
    roles = _roles()
    readers = {r for r, skills in roles.items() if "confluence.read" in skills}
    ticket_readers = {r for r, skills in roles.items() if "tickets.read" in skills}
    assert ticket_readers <= readers, "every role that reads tickets also reads Confluence"
    assert readers - ticket_readers == {"docs_writer"}
    assert {r for r, skills in roles.items() if "confluence.publish" in skills} == {"docs_writer"}


def test_the_skills_offer_exactly_the_tools_and_only_the_write_is_a_write_bundle():
    bundles = agent_baseline.bundles()
    assert bundles["confluence.read"] == ("confluence_search", "confluence_read_page")
    assert bundles["confluence.publish"] == ("confluence_publish_page",)
    assert "confluence.publish" in agent_baseline.WRITE_BUNDLES
    assert "confluence.read" not in agent_baseline.WRITE_BUNDLES
    assert agent_baseline.may_write("confluence.publish", "confluence_publish_page")
    kit = yaml.safe_load((ROOT / "packages/specpack/skills/kit.yaml").read_text())
    skills = {s["id"]: s for s in kit["skills"]}
    assert skills["confluence.read"]["policy"]["tools"] == ["confluence_search", "confluence_read_page"]
    assert skills["confluence.publish"]["policy"]["tools"] == ["confluence_publish_page"]
    assert skills["confluence.read"]["risk_tier"] == 0 and skills["confluence.publish"]["risk_tier"] == 1
    for tool in agent_baseline.CONFLUENCE_READ_TOOLS + agent_baseline.CONFLUENCE_WRITE_TOOLS:
        assert agent_baseline.bundle_tool_spec(
            "confluence.publish" if tool == "confluence_publish_page" else "confluence.read", tool) is not None


def test_publish_is_a_queued_proposal_and_the_reads_are_reads():
    reg = build_default_registry()
    for name in ("confluence_search", "confluence_read_page"):
        assert reg.get(name).kind == "read" and reg.get(name).permission == "docs:read"
    publish = reg.get("confluence_publish_page")
    assert publish.kind == "write" and publish.queues_for_review and publish.reversibility == "safe"
    assert publish.permission == "docs:publish"
    assert publish.exposure and "agent_step" in publish.exposure.surfaces


def test_page_text_is_outside_text_and_publishing_is_external():
    assert "confluence_read_page" in low_trust._FETCH_TOKENS and "confluence.read" in low_trust._FETCH_TOKENS
    assert "confluence.publish" in low_trust._EXTERNAL_SKILL


def test_approving_a_page_is_carried_out_and_has_words():
    assert "apply_confluence_publish" in decision_actions.CARRIED_OUT_ON_APPROVAL
    assert decision_words._BY_ACTION["apply_confluence_publish"] == ("Approve a Confluence page", "Confluence")


def test_a_confluence_credential_only_reaches_atlassian():
    binding = env_refs.provider_binding("confluence")
    assert set(binding.hosts) == {"*.atlassian.net", "api.atlassian.com"}
    assert env_refs.OPERATOR_BASE_URLS["confluence"] == ("CONFLUENCE_BASE_URL",)


def test_only_the_documentation_preset_may_publish_and_the_writers_hold_no_integration_read():
    cat = yaml.safe_load((ROOT / "packages/identity/catalogue.yaml").read_text())
    presets = {p["key"]: p for p in cat["passport_presets"]}
    holders = {k for k, p in presets.items() if "docs:publish" in p["permissions"]}
    assert holders == {"documentation"} and presets["documentation"]["agents"] == ["quill"]
    assert "docs:read" in presets["documentation"]["permissions"]
    assert "quill" not in presets["writer"]["agents"]
    for key in ("writer", "documentation"):
        assert "integration:read" not in presets[key]["permissions"]
    for key in ("builder", "planner", "operator_agent"):
        assert "docs:read" in presets[key]["permissions"]
        assert "docs:publish" not in presets[key]["permissions"]
