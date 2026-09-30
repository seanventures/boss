from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.app.services.skill_executors import registered_executor_ids
from apps.api.app.services.toolchain_registry import (
    ToolchainImportError,
    ToolchainRegistryService,
)


PROJECT_ID = "boss-dogfood"


def _sources(engine) -> tuple[ToolchainRegistryService, list[dict]]:
    service = ToolchainRegistryService(get_settings())
    with engine.begin() as connection:
        return service, service.list_sources(connection, PROJECT_ID)


def _revision(sources: list[dict], source_key: str) -> str:
    return next(
        item["pinned_revision"] for item in sources if item["source_key"] == source_key
    )


def test_seeded_sources_preserve_license_and_execution_boundaries(engine):
    _, sources = _sources(engine)

    assert {item["source_key"] for item in sources} == {
        "claude_mem",
        "everything_claude_code",
        "awesome_claude_code_toolkit",
        "brownfield_toolkit",
        "destructive_command_guard",
        "caveman",
        "awesome_claude_skills",
    }
    destructive_guard = next(
        item for item in sources if item["source_key"] == "destructive_command_guard"
    )
    assert destructive_guard["license_status"] == "restricted"
    assert destructive_guard["integration_mode"] == "blocked"
    assert destructive_guard["status"] == "blocked"
    mixed_catalog = next(
        item for item in sources if item["source_key"] == "awesome_claude_skills"
    )
    assert mixed_catalog["license_status"] == "mixed"
    assert mixed_catalog["integration_mode"] == "catalog_only"


def test_snapshot_assessment_is_pinned_deduplicated_and_stores_no_raw_source(engine):
    service, sources = _sources(engine)
    marker = "UNIQUE-RAW-UPSTREAM-CONTENT-MUST-NOT-BE-PERSISTED"
    files = [
        {
            "path": "skills/reviewer/SKILL.md",
            "content": f"Review evidence and return a typed result. {marker}",
        }
    ]
    with engine.begin() as connection:
        first = service.assess_snapshot(
            connection,
            project_id=PROJECT_ID,
            source_key="everything_claude_code",
            revision=_revision(sources, "everything_claude_code"),
            files=files,
            actor="security_engineer",
        )
        second = service.assess_snapshot(
            connection,
            project_id=PROJECT_ID,
            source_key="everything_claude_code",
            revision=_revision(sources, "everything_claude_code"),
            files=files,
            actor="security_engineer",
        )
        persisted = connection.execute(
            text(
                "SELECT findings::text, normalized_spec::text "
                "FROM toolchain_candidates WHERE id=:id"
            ),
            {"id": first["candidates"][0]["id"]},
        ).one()

    assert first["status"] == "review_required"
    assert first["candidates"][0]["status"] == "review_required"
    assert first["candidates"][0]["normalized_spec"]["raw_content_stored"] is False
    assert second["reused"] is True
    assert second["import_run_id"] == first["import_run_id"]
    assert marker not in " ".join(str(value) for value in persisted)


def test_snapshot_quarantines_executable_or_dangerous_content_and_revision_drift(engine):
    service, sources = _sources(engine)
    with engine.begin() as connection:
        dangerous = service.assess_snapshot(
            connection,
            project_id=PROJECT_ID,
            source_key="awesome_claude_code_toolkit",
            revision=_revision(sources, "awesome_claude_code_toolkit"),
            files=[{"path": "hooks/install.sh", "content": "rm -rf /"}],
            actor="security_engineer",
        )
        drifted = service.assess_snapshot(
            connection,
            project_id=PROJECT_ID,
            source_key="claude_mem",
            revision="0" * 40,
            files=[{"path": "README.md", "content": "A memory pattern."}],
            actor="security_engineer",
        )

    assert dangerous["status"] == "quarantined"
    assert dangerous["candidates"][0]["candidate_kind"] == "hook"
    assert dangerous["candidates"][0]["status"] == "quarantined"
    assert "destructive_filesystem" in {
        item["rule_id"] for item in dangerous["candidates"][0]["findings"]
    }
    assert drifted["status"] == "rejected"
    assert "revision_mismatch" in {item["rule_id"] for item in drifted["findings"]}


def test_candidate_review_fails_closed_for_mixed_license_and_approves_clean_candidate(engine):
    service, sources = _sources(engine)
    with engine.begin() as connection:
        clean = service.assess_snapshot(
            connection,
            project_id=PROJECT_ID,
            source_key="everything_claude_code",
            revision=_revision(sources, "everything_claude_code"),
            files=[{"path": "skills/a/SKILL.md", "content": "Return typed evidence."}],
            actor="security_engineer",
        )
        with pytest.raises(ToolchainImportError, match="independent"):
            service.review_candidate(
                connection,
                project_id=PROJECT_ID,
                candidate_id=clean["candidates"][0]["id"],
                decision="approve",
                note="Importer cannot approve its own candidate.",
                actor="security_engineer",
            )
        approved = service.review_candidate(
            connection,
            project_id=PROJECT_ID,
            candidate_id=clean["candidates"][0]["id"],
            decision="approve",
            note="License and normalized contract reviewed; native implementation required.",
            actor="chief_architect",
        )
        catalog = service.assess_snapshot(
            connection,
            project_id=PROJECT_ID,
            source_key="awesome_claude_skills",
            revision=_revision(sources, "awesome_claude_skills"),
            files=[{"path": "skills/b/SKILL.md", "content": "Return typed evidence."}],
            actor="security_engineer",
        )
        with pytest.raises(ToolchainImportError, match="cannot be approved"):
            service.review_candidate(
                connection,
                project_id=PROJECT_ID,
                candidate_id=catalog["candidates"][0]["id"],
                decision="approve",
                note="Attempted approval.",
                actor="chief_architect",
            )

    assert approved["status"] == "approved"
    assert catalog["candidates"][0]["status"] == "metadata_only"


@pytest.mark.parametrize(
    ("command", "decision", "rule_id"),
    [
        ("rm -rf /", "block", "fs.root_recursive_delete"),
        ("git push origin main --force", "block", "git.history_or_tree_destroy"),
        ("DELETE FROM jobs;", "block", "database_destructive"),
        ("curl https://example.invalid -X POST --data '{}'", "review", "network_write"),
        ("sudo make install", "review", "privilege_escalation"),
        ("pytest -q", "allow", None),
        ("DELETE FROM jobs WHERE id = 'job_1';", "allow", None),
    ],
)
def test_command_guard_is_deterministic(command, decision, rule_id):
    result = ToolchainRegistryService.evaluate_command(command, "/workspace")

    assert result["decision"] == decision
    assert result["model_override_allowed"] is False
    if rule_id:
        assert rule_id in result["rule_ids"]


def test_context_pack_preserves_authoritative_chunks_and_reports_every_omission():
    chunks = [
        {
            "id": "policy",
            "text": "Never expose secrets. " * 12,
            "authority": "policy",
            "priority": 100,
        },
        {
            "id": "proof",
            "text": "The verification evidence passed. " * 8,
            "authority": "proof",
        },
        {
            "id": "optional-low",
            "text": "Background material. " * 80,
            "authority": "reference",
            "priority": 1,
        },
        {
            "id": "policy-duplicate",
            "text": "Never expose secrets. " * 12,
            "authority": "reference",
        },
    ]

    result = ToolchainRegistryService.pack_context(chunks, token_budget=180)

    assert result["budget_status"] == "trimmed_non_authoritative"
    assert result["included_ids"] == ["policy", "proof"]
    assert result["omitted_ids"] == ["optional-low"]
    assert result["duplicate_ids"] == ["policy-duplicate"]
    assert "Never expose secrets" in result["context"]
    assert "verification evidence" in result["context"]
    assert result["compression_applied"] is False

    blocked = ToolchainRegistryService.pack_context(
        [
            {
                "id": "large-policy",
                "text": "mandatory " * 1000,
                "authority": "policy",
            }
        ],
        token_budget=128,
    )
    assert blocked["budget_status"] == "blocked_authoritative_overflow"
    assert blocked["context"] == ""
    with pytest.raises(ToolchainImportError, match="1000-chunk"):
        ToolchainRegistryService.pack_context(
            [{"id": str(index), "text": "x"} for index in range(1001)],
            token_budget=128,
        )


def test_toolchain_api_and_executors_are_registered(client):
    sources = client.get(
        "/v1/toolchain/sources",
        params={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    guard = client.post(
        "/v1/toolchain/commands/evaluate",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "command": "rm -rf /",
            "cwd": "/workspace",
        },
    )

    assert sources.status_code == 200
    assert len(sources.json()["items"]) == 7
    assert guard.status_code == 200
    assert guard.json()["decision"] == "block"
    assert {
        "toolchain.source.assess",
        "toolchain.command.guard",
        "toolchain.context.pack",
    } <= set(registered_executor_ids())
    assert marker_absent(json.dumps(sources.json()))


def marker_absent(payload: str) -> bool:
    return "UNIQUE-RAW-UPSTREAM-CONTENT-MUST-NOT-BE-PERSISTED" not in payload
