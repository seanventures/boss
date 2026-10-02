from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from apps.api.app.config import Settings
from apps.api.app.services.github_adapter import PullRequestResult
from apps.api.app.services.skill_executors import (
    SkillExecutorConfigurationError,
    SkillExecutorContext,
    registered_executor_ids,
    resolve_executor,
)


class FakeGitHub:
    repository_full_name = "acme/widget"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def create_pull_request(
        self, branch: str, title: str, body: str
    ) -> PullRequestResult:
        self.calls.append((branch, title, body))
        return PullRequestResult(url="https://github.test/acme/widget/pull/7", number=7)


class FakeReceipts:
    def __init__(self, *, existing: dict | None = None) -> None:
        self.existing = existing
        self.succeeded: list[tuple[str, str, dict]] = []
        self.failed: list[tuple[str, dict]] = []

    def create_or_reuse(self, *args, **kwargs):
        if self.existing is not None:
            return (self.existing, False)
        return ({"id": "rcpt_test", "status": "in_progress"}, True)

    def mark_succeeded(self, connection, receipt_id, provider_ref, result):
        self.succeeded.append((receipt_id, provider_ref, result))

    def mark_failed(self, connection, receipt_id, error):
        self.failed.append((receipt_id, error))


class FakeDeployments:
    def __init__(self) -> None:
        self.target_inputs: dict = {}

    def create_deployment_target(self, connection, **kwargs):
        self.target_inputs = kwargs
        return {"id": "deptgt_test"}


@dataclass
class FakeConnection:
    pass


def _context(**overrides) -> SkillExecutorContext:
    values = {
        "settings": Settings(),
        "connection": FakeConnection(),
        "project_id": "project_test",
        "actor": "release_manager",
        "org_id": "org_test",
        "workspace_id": "workspace_test",
    }
    values.update(overrides)
    return SkillExecutorContext(**values)


#: Executors registered under a BEHAVIOUR rather than under a skill id.
#:
#: These are not skills and never will be. An operator-authored skill has a key the operator
#: invented, so nothing can be registered under it in advance; what runs it is the executor for
#: its `behaviour_kind`, reached through `resolve_executor` looking the definition up and
#: dispatching to `skill.<behaviour>`. Listing them here is the rule the assertion below is
#: actually trying to express.
#:
#: `skill.sandboxed_command` has been registered since authored skills shipped and this test has
#: been failing on it ever since — it read as "somebody registered an executor for a skill that
#: does not exist", which is exactly the defect worth catching and exactly not what this is.
# EVERY AUTHORED BEHAVIOUR BUT `builtin` IS RUN BY A DISPATCHER NAMED FOR IT (resolve_executor
# maps an authored skill to `skill.<behaviour>`). Derived from skill_definitions.BEHAVIOURS rather
# than listed by hand: the hand list named two of the three, so registering `skill.mcp_tool` (the
# executor whose absence made every authored MCP skill fail on its first run) turned this test
# red for doing the right thing. REWRITTEN to read the behaviours, so a new behaviour without a
# dispatcher fails below and a dispatcher for no behaviour fails above.
from boss_engine.services.skill_definitions import BEHAVIOURS as _BEHAVIOURS  # noqa: E402

BEHAVIOUR_DISPATCHERS = {f"skill.{b}" for b in _BEHAVIOURS if b != "builtin"}


def test_registry_only_exposes_skill_specs_with_real_bindings():
    skill_ids: set[str] = set()
    root = Path(__file__).resolve().parents[3] / "packages" / "specpack" / "skills"
    for path in root.glob("*.yaml"):
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        skill_ids.update(str(item["id"]) for item in payload.get("skills", []))

    # EVERY REGISTERED EXECUTOR IS EITHER A DECLARED SKILL OR A BEHAVIOUR DISPATCHER.
    #
    # The point of the check is that nothing is bound to a skill the specpack does not declare —
    # a typo in a decorator, or a skill deleted from the pack and left bound. Both still fail.
    unexplained = set(registered_executor_ids()) - skill_ids - BEHAVIOUR_DISPATCHERS
    assert not unexplained, (
        f"these executors are bound to nothing the specpack declares: {sorted(unexplained)}")

    # And a dispatcher really is a dispatcher: registered, and not a declared skill.
    for dispatcher in BEHAVIOUR_DISPATCHERS:
        assert dispatcher in registered_executor_ids()
        assert dispatcher not in skill_ids

    assert resolve_executor("github.create_pr_sandbox") is not None
    assert resolve_executor("deployment.target.register") is not None
    assert resolve_executor("unknown.skill") is None


def test_resolved_executor_requires_server_context():
    executor = resolve_executor("github.create_pr_sandbox")
    assert executor is not None

    with pytest.raises(SkillExecutorConfigurationError, match="server-side"):
        executor({})


def test_github_executor_reuses_adapter_and_records_receipt():
    github = FakeGitHub()
    receipts = FakeReceipts()
    executor = resolve_executor(
        "github.create_pr_sandbox",
        context=_context(github_adapter=github, receipt_service=receipts),
    )
    assert executor is not None

    result = executor(
        {
            "repo": "acme/widget",
            "branch": "boss/change",
            "title": "Add governed change",
            "body": "Receipt-backed proposal",
        }
    )

    assert result == {
        "status": "created",
        "pr_ref": "https://github.test/acme/widget/pull/7",
        "pr_url": "https://github.test/acme/widget/pull/7",
        "pull_request_number": 7,
        "receipt_id": "rcpt_test",
        "actual_cost_usd_micros": 0,
    }
    assert github.calls == [
        ("boss/change", "Add governed change", "Receipt-backed proposal")
    ]
    assert receipts.succeeded[0][0] == "rcpt_test"


def test_deployment_executor_reuses_resource_plane_service():
    deployments = FakeDeployments()
    executor = resolve_executor(
        "deployment.target.register",
        context=_context(deployment_service=deployments),
    )
    assert executor is not None

    result = executor(
        {
            "app_id": "boss",
            "repo": "acme/widget",
            "environment": "staging",
            "provider": "cloudflare",
            "domains": ["staging.example.test"],
        }
    )

    assert result == {"deployment_target_id": "deptgt_test"}
    assert deployments.target_inputs["project_id"] == "project_test"
    assert deployments.target_inputs["target_kind"] == "cloudflare"
    assert deployments.target_inputs["domains"] == ["staging.example.test"]


def test_existing_in_progress_receipt_prevents_duplicate_github_effect():
    github = FakeGitHub()
    receipts = FakeReceipts(
        existing={"id": "rcpt_existing", "status": "in_progress"}
    )
    executor = resolve_executor(
        "github.create_pr_sandbox",
        context=_context(github_adapter=github, receipt_service=receipts),
    )
    assert executor is not None

    result = executor(
        {
            "repo": "acme/widget",
            "branch": "boss/change",
            "title": "Add governed change",
            "body": "Receipt-backed proposal",
        }
    )

    assert result["status"] == "in_progress"
    assert result["receipt_id"] == "rcpt_existing"
    assert github.calls == []
