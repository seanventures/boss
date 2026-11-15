"""A grant for a project whose code is ON THIS MACHINE covers that code (BOSSAPP-1163).

Found by BOSS on the laptop, building a project with code "On this machine" (a local repository,
`local_<project>/<name>`): Request changes on the merge decision opened a follow-up build for the
Software Engineer on the local repository, and the operator's 30-day consent — minted with
`resources={}` because workflow_needs dropped local repositories — did not cover it
(`grants.covers` refuses a grant whose repositories miss the work's). 409 consent_required, no
sheet. Each test here fails without the fix it names:

  * a local build names its repository for consent (code_task_service._consent_repos);
  * the consent body carries it as `local`, never as GitHub access (consent.repo_blocks), and a
    workflow that builds in it names it (consent.workflow_needs);
  * the grant stores it as `resources.local` (grants._validate_local) and covers reads the union
    of its GitHub and local repositories, case-insensitively (grants.covers);
  * the GitHub guard never asks GitHub about a repository on this machine;
  * the `decide` tool lets ConsentRequired through to the registry, so the press answers the 409
    the console's consent sheet opens on (tools/builtin.py).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from boss_engine.identity import consent, grants
from boss_engine.identity.delegation import DelegationGrant, GrantStatus
from boss_engine.services.code_task_service import _consent_repos

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "prj_desk"
LOCAL = f"local_{PROJECT}/desk"
FORGE = "agt_forge000001"


def _grant(resources: dict) -> DelegationGrant:
    return DelegationGrant(
        id="dlg_t", org_id="org_boss", user_principal_id="usr_alice", agent_ids=(FORGE,),
        permissions=frozenset(consent.CODE_TASK_PERMISSIONS), project_ids=(PROJECT,), resources=resources,
        allow_subdelegation=False, allowed_children=(), max_depth=3, status=GrantStatus.ACTIVE,
        created_at=datetime.now(timezone.utc), expires_at=None, purpose="For 30 days")


def _covers(g: DelegationGrant, repos) -> bool:
    return grants.covers(g, agent_ids=[FORGE], permissions=frozenset(consent.CODE_TASK_PERMISSIONS),
                         project_id=PROJECT, repos=list(repos), run_id="run_ct:crun_followup")


# ---- the build names its local repository ----------------------------------------------------------

def test_a_build_on_this_machine_names_its_repository_for_consent():
    assert _consent_repos(f"local_{PROJECT}", "desk", None) == [LOCAL]
    assert _consent_repos(f"local_{PROJECT}", "desk", "local") == [LOCAL]      # the stamped spec
    assert _consent_repos(f"local_{PROJECT}", "desk", "github") == [LOCAL]
    # Unchanged: no repository, and another host, name none (round 3's "/" regression).
    assert _consent_repos("", "", None) == [] and _consent_repos("acme", "", "local") == []
    assert _consent_repos("acme", "app", "bitbucket") == []


def test_split_and_blocks_keep_github_and_this_machine_apart():
    assert grants.split_repos(["acme/web", LOCAL]) == (["acme/web"], [LOCAL])
    only_local = consent.repo_blocks([LOCAL])
    assert only_local["github"] is None
    assert only_local["local"] == {"repo": LOCAL, "repos": [LOCAL], "summary": "on this machine"}
    both = consent.repo_blocks(["acme/web", LOCAL])
    assert both["github"]["repos"] == ["acme/web"] and "pull requests write" in both["github"]["summary"]
    assert both["local"]["repos"] == [LOCAL]
    assert consent.repo_blocks([], summary=False) == {"github": None, "local": None}
    assert "summary" not in consent.repo_blocks(["acme/web"], summary=False)["github"]


def test_a_workflow_that_builds_in_a_local_repository_names_it(monkeypatch):
    monkeypatch.setattr(consent, "_repos_for", lambda db, project_id, input_data: [LOCAL, "acme/web"])
    monkeypatch.setattr(consent, "build_step_builders", lambda db, project_id, step, input_data=None: ["forge"])
    spec = {"steps": [{"id": "build", "kind": "code_task", "binding": {}}]}
    roles, perms, repos = consent.workflow_needs(None, PROJECT, spec, {})
    assert "forge" in roles and consent.CODE_TASK_PERMISSIONS <= perms
    assert repos == [LOCAL, "acme/web"], "the local repository is named, so the run's grant covers it"


# ---- the grant stores it, covers reads it ---------------------------------------------------------

def test_validate_local_takes_only_this_projects_registered_repositories(monkeypatch):
    registered = {(PROJECT, LOCAL)}
    monkeypatch.setattr(grants, "local_repo_registered", lambda db, p, f: (p, f) in registered)
    assert grants._validate_local({}, [PROJECT]) == {}
    assert grants._validate_local({"local": {"repos": [LOCAL, LOCAL]}}, [PROJECT]) == {"repos": [LOCAL]}
    with pytest.raises(grants.GrantRefused) as refused:
        grants._validate_local({"local": {"repos": ["acme/web"]}}, [PROJECT])
    assert refused.value.code == "bad_repos" and refused.value.status == 422
    with pytest.raises(grants.GrantRefused):
        grants._validate_local({"local": ["x"]}, [PROJECT])
    # A grant for project A never names project B's code (review of PR #115).
    with pytest.raises(grants.GrantRefused) as other:
        grants._validate_local({"local": {"repos": ["local_prj_other/x"]}}, [PROJECT])
    assert other.value.code == "repo_outside_projects" and other.value.detail["project"] == "prj_other"
    # ...nor a name in its own project that is not registered there.
    with pytest.raises(grants.GrantRefused) as ghost:
        grants._validate_local({"local": {"repos": [f"local_{PROJECT}/ghost"]}}, [PROJECT])
    assert ghost.value.code == "repo_not_registered"


def test_validate_github_refuses_a_repository_on_this_machine():
    with pytest.raises(grants.GrantRefused) as refused:
        grants._validate_github({"github": {"repos": [LOCAL], "contents": "write"}})
    assert refused.value.code == "bad_repos" and refused.value.detail["reason"] == "local_repo_in_github"
    assert grants._validate_github({"github": {"repos": ["acme/web"], "contents": "write"}})["repos"] == ["acme/web"]


def test_covers_reads_the_local_block_and_the_union():
    assert not _covers(_grant({}), [LOCAL]), "the defect: a grant with no repositories covers no build"
    assert _covers(_grant({"local": {"repos": [LOCAL]}}), [LOCAL])
    assert _covers(_grant({"local": {"repos": [LOCAL]}}), [LOCAL.upper()]), "one canonical form, any case"
    # The project's Captain authority lists bound local repositories under `github`: still covered.
    assert _covers(_grant({"github": {"repos": [LOCAL], "contents": "write"}}), [LOCAL])
    # A build that touches both kinds needs both; GitHub names compare case-insensitively.
    mixed = _grant({"github": {"repos": ["Acme/Web"], "contents": "write"}, "local": {"repos": [LOCAL]}})
    assert _covers(mixed, ["acme/web", LOCAL])
    assert not _covers(_grant({"github": {"repos": ["acme/web"], "contents": "write"}}), [LOCAL])
    assert grants.grant_repos(mixed) == frozenset({"acme/web", LOCAL.lower()})


def test_the_github_guard_never_asks_about_a_repository_on_this_machine():
    # `db=None`: reaching the policy or GitHub would fail, so passing proves neither was asked.
    assert grants.user_repo_access_refusal("org_boss", "usr_alice",
                                           {"repos": [LOCAL], "contents": "write"}, db=None) is None


# ---- the dock's press ----------------------------------------------------------------------------

def test_the_decide_tool_lets_consent_through_to_the_registry():
    src = (ROOT / "boss_engine" / "tools" / "builtin.py").read_text(encoding="utf-8")
    start = src.index("    def decide(ctx, *, project_id, actor, args):")
    body = src[start:src.index("    @reg.tool(", start)]
    body = re.sub(r"#[^\n]*", "", body)
    consent_at, generic_at = body.index("except ConsentRequired:"), body.index("except Exception as exc:")
    assert consent_at < generic_at, "ConsentRequired must be re-raised before the catch-all turns it into a string"
    assert re.search(r"except ConsentRequired:\s*raise", body)
