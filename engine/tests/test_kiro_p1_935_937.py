"""Tests for Kiro Crew P1 gaps:
- BOSSAPP-935 (Kiro gap 1): agents act by default inside the compliance boundary; new installs
  start in propose grace during onboarding, then route automatically; squads execute every
  reversible action while Where people decide stays the sole human gate.
- BOSSAPP-936 (Kiro gap 3): goal-owning area agents (Shepherd, SRE, Security, FinOps, Release,
  Infra) with canonical goals, measured milestones, self-recorded readings, heartbeats, and
  weekly reports (absorbing BOSSAPP-907 digest).
- BOSSAPP-937 (Kiro gap 4): GitLab and Azure DevOps behind RepoHost (clone, branch, commit,
  MR/PR, review comments, merge truth, webhooks) + cross-host conformance suite.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid

import httpx
import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.identity import github_urls
from boss_engine.services import azure_devops_adapter as azdo_mod
from boss_engine.services import gitlab_adapter as gl_mod
from boss_engine.services.ambient import area_agents, board, policy, squad_plugins, squads, task_classes as tc
from boss_engine.services.azure_devops_adapter import AzureDevOpsAdapter
from boss_engine.services.github_adapter import (
    IntegrationConfigurationError,
    PullRequestResult,
)
from boss_engine.services.gitlab_adapter import GitLabAdapter
from boss_engine.services.repo_host import (
    KNOWN_REPO_HOSTS,
    RepoHost,
    clone_url_for,
    git_access,
    make_repo_host,
    resolve_repo_host_kind,
)
from boss_engine.services.webhooks import InboundWebhookService


@pytest.fixture
def pg_conn():
    url = os.environ.get("BOSS_DATABASE_URL")
    if not url:
        pytest.skip("BOSS_DATABASE_URL not set")
    engine = create_engine(url, future=True)
    org_id = f"org_{uuid.uuid4().hex[:8]}"
    ws_id = f"ws_{uuid.uuid4().hex[:8]}"
    proj_id = f"proj_{uuid.uuid4().hex[:8]}"
    with engine.connect() as c:
        trans = c.begin()
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:id, 'Kiro Test Org')"), {"id": org_id})
        c.execute(
            text("INSERT INTO workspaces (id, org_id, name) VALUES (:id, :o, 'Kiro WS')"),
            {"id": ws_id, "o": org_id},
        )
        c.execute(
            text(
                "INSERT INTO projects (id, workspace_id, name, slug) "
                "VALUES (:id, :w, 'Kiro App', :s)"
            ),
            {"id": proj_id, "w": ws_id, "s": f"kiro-{proj_id[-6:]}"},
        )
        for role_key, name in (
            ("software_engineer", "Software Engineer"),
            ("qa", "QA Engineer"),
            ("sre", "SRE"),
            ("adversarial_reviewer", "Adversarial Reviewer"),
        ):
            c.execute(
                text(
                    "INSERT INTO roles (id, project_id, role_key, display_name) "
                    "VALUES (:id, :p, :rk, :dn) ON CONFLICT DO NOTHING"
                ),
                {"id": f"role_{uuid.uuid4().hex[:8]}", "p": proj_id, "rk": role_key, "dn": name},
            )
        try:
            yield c, proj_id, org_id, ws_id
        finally:
            trans.rollback()
    engine.dispose()


# ── BOSSAPP-935: Agents act by default inside the compliance boundary ──────────────────────────

def test_kiro_gap1_first_run_walk_propose_grace_then_route_and_reversible_squad_execution(pg_conn):
    c, project_id, _, _ = pg_conn
    settings = Settings()

    # 1. Fresh install defaults to 'route' (always-on by default inside the compliance boundary).
    assert policy.DEFAULT_COS_MODE == "route"
    assert policy.cos_mode(settings) == "route"
    assert policy.onboarding_mode_for(c, project_id, settings)["project_mode"] == "route"

    # 2. Newly onboarded project starts in propose grace until the person reviews the backlog.
    c.execute(
        text(
            "INSERT INTO project_onboarding_runs (id, project_id, state, started_at) "
            "VALUES (:id, :p, 'ready', now())"
        ),
        {"id": f"onb_{uuid.uuid4().hex[:8]}", "p": project_id},
    )
    grace_mode = policy.onboarding_mode_for(c, project_id, settings)
    assert grace_mode["project_mode"] == "propose"
    assert grace_mode["in_onboarding_grace"] is True
    # A2 actions wait for a person during grace
    held_verdict = policy.gate(c, project_id, "ci_fix_boss_pr", "open_draft_pr")
    assert held_verdict["allowed"] is False and held_verdict["needs_person"] is True

    # Once the person decides on an onboarding backlog item, grace lifts and route takes over.
    onb_item = board.open_item(
        c,
        project_id=project_id,
        task_class="ci_setup",
        title="Add CI",
        fingerprint=f"onb_ci_{uuid.uuid4().hex[:8]}",
        actor="agent:chief_of_staff",
    )["item"]
    c.execute(
        text(
            "INSERT INTO ambient_recommendations "
            "(work_item_id, project_id, detector, kind, fingerprint, title, why, proposed_shape, status, decided_at) "
            "VALUES (:wid, :p, 'onboarding', 'hardening', :fp, 'Add CI', 'Repo needs CI', 'ticket', 'accepted', now())"
        ),
        {"wid": onb_item["id"], "p": project_id, "fp": onb_item["fingerprint"]},
    )
    post_onb = policy.onboarding_mode_for(c, project_id, settings)
    assert post_onb["project_mode"] == "route"
    assert post_onb["in_onboarding_grace"] is False
    open_verdict = policy.gate(c, project_id, "ci_fix_boss_pr", "open_draft_pr")
    assert open_verdict["allowed"] is True

    # 3. Squads may execute EVERY reversible action; Where people decide stays the sole human gate.
    for action_key in (
        "label",
        "comment",
        "nudge",
        "staging_rollback",
        "staging_restart",
        "silence_rule",
        "quarantine_test",
        "start_workflow",
        "open_draft_pr",
        "convene_meeting",
    ):
        assert squad_plugins.executes(action_key) is True, f"expected squad to execute {action_key}"

    for human_only in tc.ALWAYS_A_PERSON:
        assert policy.is_compliance_boundary_action(human_only) is True
        assert squad_plugins.executes(human_only) is False
        verdict = policy.gate(c, project_id, "ci_fix_boss_pr", human_only)
        assert verdict["allowed"] is False and verdict["needs_person"] is True

    # 4. Execute reversible squad actions end-to-end with Receipts and undo metadata.
    item = board.open_item(
        c,
        project_id=project_id,
        task_class="ci_fix_boss_pr",
        title="Flaky CI test on PR #42",
        fingerprint=f"fp_{uuid.uuid4().hex[:8]}",
        actor="agent:chief_of_staff",
    )["item"]
    assembled = squads.assemble(c, item["id"])
    squad = assembled["squad"]
    res = squad_plugins.EXECUTORS["quarantine_test"](
        c,
        board.get(c, item["id"]),
        squad=squad,
        proposal={"id": "msg_p1", "body": {"action": "quarantine_test", "summary": "Quarantine flaky checkout test"}},
        decision={"act_id": "act_q1", "chosen": "quarantine_test", "decided_by": squad["lead"]},
        launchers=None,
    )
    assert res["kind"] == "reversible_action"
    assert res["action"] == "quarantine_test"
    assert res["quarantined"] is True
    assert res["receipt_id"] is not None

    # Direct invocation of a Where-people-decide action raises PermissionError.
    with pytest.raises(PermissionError, match="Where people decide"):
        squad_plugins.execute_reversible_action(
            c,
            board.get(c, item["id"]),
            squad=squad,
            proposal={"id": "msg_p2", "body": {"action": "merge", "summary": "Merge PR"}},
            decision={"act_id": "act_m1", "chosen": "merge", "decided_by": squad["lead"]},
        )


# ── BOSSAPP-936: Goal-owning area agents ───────────────────────────────────────────────────────

def test_kiro_gap3_goal_owning_area_agents_milestones_readings_heartbeat_weekly_report(pg_conn):
    c, project_id, _, _ = pg_conn

    # Seed all 6 area agents (Shepherd, SRE, Security, FinOps, Release, Infra)
    created = area_agents.seed_default_area_goals(c, project_id, actor="person:sean")
    assert len(created) == 6
    assert {g["area_key"] for g in created} == {"shepherd", "sre", "security", "finops", "release", "infra"}
    # Idempotent re-seed creates nothing new
    assert area_agents.seed_default_area_goals(c, project_id, actor="person:sean") == []

    # Verify canonical goals table link (BOSSAPP-830 integration)
    sre_goal = next(g for g in created if g["area_key"] == "sre")
    assert sre_goal["goal_id"] is not None
    assert len(sre_goal["milestones"]) == 2
    mttm_ms = next(m for m in sre_goal["milestones"] if m["metric_key"] == "mttm_minutes")

    # Record a worse-than-target reading (at_risk), then an achieved reading via heartbeat
    first = area_agents.record_reading(
        c, project_id, mttm_ms["id"], value=25.0, note="Staging outage took 25m"
    )
    assert first["milestone"]["status"] == "at_risk"
    assert first["milestone"]["current_value"] == 25.0

    hb = area_agents.run_heartbeat(
        c,
        project_id,
        "sre",
        readings=[
            {"metric_key": "mttm_minutes", "value": 8.5, "note": "Auto-rollback in 8.5m"},
            {"metric_key": "unacked_high_alerts", "value": 0.0, "note": "Zero unacked alerts"},
        ],
    )
    assert hb["heartbeat"]["status"] == "ok"
    assert hb["heartbeat"]["readings_count"] == 2
    assert all(m["status"] == "achieved" for m in hb["milestones"])

    # Create an A0 ambient work item to verify BOSSAPP-907 digest absorption in weekly report
    board.open_item(
        c,
        project_id=project_id,
        task_class="todo_triage",
        title="14 TODO comments in billing/",
        fingerprint=f"todo_{uuid.uuid4().hex[:8]}",
        actor="agent:chief_of_staff",
    )

    report = area_agents.generate_weekly_report(
        c, project_id, period="weekly", days=7, actor="agent:chief_of_staff", post_to_chat=True
    )
    assert report["posted_to_chat"] is True
    assert report["chat_message_ref"] is not None
    assert "Area Agents Weekly Report & Digest" in report["markdown"]
    assert report["digest_items"]["counts"]["observed_a0"] >= 1
    # Because SRE achieved all its milestones, weekly report proposes a next-cycle goal
    assert any(
        pg["area_key"] == "sre" and pg["reason"] == "all_milestones_achieved"
        for pg in report["proposed_goals"]
    )

    overview = area_agents.project_overview(c, project_id)
    assert len(overview["area_agents"]) == 6
    sre_view = next(a for a in overview["area_agents"] if a["area_key"] == "sre")
    assert sre_view["configured"] is True
    assert sre_view["latest_heartbeat"]["status"] == "ok"
    assert sre_view["latest_report"]["id"] == report["id"]


# ── BOSSAPP-937: GitLab + Azure DevOps RepoHost + Conformance + Webhooks ───────────────────────

def _gl_settings(**kw) -> Settings:
    base = {
        "BOSS_GITLAB_TOKEN": "glpat-secret",
        "BOSS_GITLAB_BASE_URL": "https://gitlab.example.com",
        "BOSS_TARGET_GITLAB_NAMESPACE": "acme",
        "BOSS_TARGET_GITLAB_REPO": "shop",
        "BOSS_GITLAB_WEBHOOK_SECRET": "gl-hook-secret",
    }
    base.update(kw)
    return Settings(**base)


def _azdo_settings(**kw) -> Settings:
    base = {
        "BOSS_AZURE_DEVOPS_ORG": "acmeorg",
        "BOSS_AZURE_DEVOPS_PROJECT": "shopproj",
        "BOSS_AZURE_DEVOPS_REPO": "shop",
        "BOSS_AZURE_DEVOPS_PAT": "azdo-pat-secret",
        "BOSS_AZURE_DEVOPS_WEBHOOK_SECRET": "azdo-hook-secret",
    }
    base.update(kw)
    return Settings(**base)


@pytest.mark.parametrize("kind", ["github", "bitbucket", "gitlab", "azure_devops"])
def test_kiro_gap4_conformance_protocol_factory_and_git_access(kind):
    assert kind in KNOWN_REPO_HOSTS
    settings = Settings(
        BOSS_BITBUCKET_TOKEN="bb",
        BOSS_TARGET_BITBUCKET_WORKSPACE="acme",
        BOSS_TARGET_BITBUCKET_REPO="shop",
        BOSS_GITLAB_TOKEN="gl",
        BOSS_TARGET_GITLAB_NAMESPACE="acme",
        BOSS_TARGET_GITLAB_REPO="shop",
        BOSS_AZURE_DEVOPS_ORG="acme",
        BOSS_AZURE_DEVOPS_PROJECT="proj",
        BOSS_AZURE_DEVOPS_REPO="shop",
        BOSS_AZURE_DEVOPS_PAT="pat",
    )
    adapter = make_repo_host(settings, host=kind)
    assert adapter.kind == kind
    assert isinstance(adapter, RepoHost)
    assert resolve_repo_host_kind(settings, {"repo_host": kind}) == kind

    web_base = {
        "github": "https://github.com",
        "bitbucket": "",
        "gitlab": "https://gitlab.com",
        "azure_devops": "https://dev.azure.com",
    }[kind]
    owner = "acme/proj" if kind == "azure_devops" else "acme"
    tok = github_urls.tag("TOK", "https://api.github.com") if kind == "github" else "TOK"
    url = clone_url_for(kind, owner, "shop", tok, web_base=web_base)
    assert "TOK@" in url
    access = git_access(kind, owner, "shop", tok, web_base=web_base)
    assert "TOK" not in access.url
    assert access.via == "token"
    assert "GIT_CONFIG_COUNT" in access.env


def test_kiro_gap4_gitlab_adapter_full_lifecycle(monkeypatch):
    calls: list[dict] = []
    file_exists = {"seen": False}

    def fake(method, url, *, headers=None, json=None, params=None, timeout=None):
        calls.append({"method": method, "url": url, "headers": headers or {}, "json": json, "params": params})
        req = httpx.Request(method, url)
        path = url.split("/api/v4", 1)[1]
        if path == "/projects/acme%2Fshop" and method == "GET":
            return httpx.Response(200, request=req, json={"default_branch": "main"})
        if path.endswith("/repository/branches/boss%2Fcode%2Fx1") and method == "GET":
            return httpx.Response(404, request=req, json={"message": "404 Branch Not Found"})
        if path.endswith("/repository/branches") and method == "POST":
            return httpx.Response(201, request=req, json={"commit": {"id": "glsha123"}})
        if "/repository/files/" in path and method == "GET":
            if not file_exists["seen"]:
                return httpx.Response(404, request=req, json={"message": "404 File Not Found"})
            return httpx.Response(200, request=req, json={"file_path": "src/app.py"})
        if "/repository/files/" in path and method in ("POST", "PUT"):
            file_exists["seen"] = True
            return httpx.Response(201, request=req, json={"file_path": "src/app.py"})
        if path.endswith("/merge_requests") and method == "GET":
            return httpx.Response(200, request=req, json=[])
        if path.endswith("/merge_requests") and method == "POST":
            return httpx.Response(
                201,
                request=req,
                json={"iid": 11, "web_url": "https://gitlab.example.com/acme/shop/-/merge_requests/11"},
            )
        if path.endswith("/merge_requests/11") and method == "GET":
            return httpx.Response(
                200,
                request=req,
                json={
                    "iid": 11,
                    "state": "opened",
                    "detailed_merge_status": "mergeable",
                    "source_branch": "boss/code/x1",
                    "target_branch": "main",
                    "sha": "glsha123",
                    "diff_refs": {"base_sha": "base000", "head_sha": "glsha123"},
                    "web_url": "https://gitlab.example.com/acme/shop/-/merge_requests/11",
                },
            )
        if path.endswith("/merge_requests/11/notes") and method == "POST":
            return httpx.Response(201, request=req, json={"id": 99})
        if path.endswith("/merge_requests/11/discussions") and method == "POST":
            return httpx.Response(201, request=req, json={"id": "disc1"})
        if path.endswith("/merge_requests/11/approve") and method == "POST":
            return httpx.Response(201, request=req, json={"approved": True})
        if path.endswith("/merge_requests/11/changes") and method == "GET":
            return httpx.Response(
                200,
                request=req,
                json={"changes": [{"old_path": "src/app.py", "new_path": "src/app.py", "diff": "+print(1)"}]},
            )
        if path.endswith("/merge_requests/11/merge") and method == "PUT":
            return httpx.Response(
                200, request=req, json={"state": "merged", "squash_commit_sha": "glmerged999"}
            )
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(gl_mod.httpx, "request", fake)
    adapter = GitLabAdapter(_gl_settings(BOSS_SANDBOX_MERGE_METHOD="squash"))
    assert isinstance(adapter, RepoHost)
    assert adapter.repository_full_name == "acme/shop"
    assert adapter.clone_url() == "https://oauth2:glpat-secret@gitlab.example.com/acme/shop.git"
    assert adapter.ensure_branch("boss/code/x1") == "glsha123"
    assert adapter.upsert_file("boss/code/x1", "src/app.py", "print(1)\n", "feat: add app") == "src/app.py"
    pr = adapter.create_pull_request("boss/code/x1", "Fix checkout", "Body")
    assert pr == PullRequestResult(
        url="https://gitlab.example.com/acme/shop/-/merge_requests/11", number=11
    )
    snap = adapter.pull_request_snapshot(11)
    assert snap["state"] == "open" and snap["merged"] is False and snap["mergeable"] is True
    comment = adapter.create_issue_comment(11, "Looks good")
    assert comment.id == 99
    rev = adapter.create_review(
        11, "Approved with note", event="APPROVE", comments=[{"path": "src/app.py", "line": 1, "body": "nice"}]
    )
    assert rev.state == "APPROVED"
    assert "diff --git a/src/app.py b/src/app.py" in adapter.get_pull_request_diff(11)
    merged = adapter.merge_pull_request(11, "merge checkout fix")
    assert merged.merged is True and merged.sha == "glmerged999"

    with pytest.raises(IntegrationConfigurationError):
        GitLabAdapter(_gl_settings(BOSS_GITLAB_TOKEN="")).default_branch()


def test_kiro_gap4_azure_devops_adapter_full_lifecycle(monkeypatch):
    calls: list[dict] = []
    branch_created = {"done": False}

    def fake(method, url, *, headers=None, auth=None, json=None, params=None, timeout=None):
        calls.append({"method": method, "url": url, "auth": auth, "json": json, "params": params})
        req = httpx.Request(method, url)
        path = url.split("/_apis/git/repositories/shop", 1)[1]
        if path == "" and method == "GET":
            return httpx.Response(200, request=req, json={"defaultBranch": "refs/heads/main"})
        if path == "/refs" and method == "GET":
            filt = (params or {}).get("filter", "")
            if filt == "heads/main":
                return httpx.Response(200, request=req, json={"value": [{"name": "refs/heads/main", "objectId": "azbase111"}]})
            if filt == "heads/boss/code/x1" and branch_created["done"]:
                return httpx.Response(200, request=req, json={"value": [{"name": "refs/heads/boss/code/x1", "objectId": "azhead222"}]})
            return httpx.Response(200, request=req, json={"value": []})
        if path == "/refs" and method == "POST":
            branch_created["done"] = True
            return httpx.Response(200, request=req, json={"value": [{"newObjectId": "azhead222"}]})
        if path == "/items" and method == "GET":
            return httpx.Response(404, request=req, json={"message": "Not found"})
        if path == "/pushes" and method == "POST":
            return httpx.Response(201, request=req, json={"pushId": 5})
        if path == "/pullrequests" and method == "GET":
            return httpx.Response(200, request=req, json={"value": []})
        if path == "/pullrequests" and method == "POST":
            return httpx.Response(201, request=req, json={"pullRequestId": 19})
        if path == "/pullrequests/19" and method == "GET":
            return httpx.Response(
                200,
                request=req,
                json={
                    "pullRequestId": 19,
                    "status": "active",
                    "mergeStatus": "succeeded",
                    "sourceRefName": "refs/heads/boss/code/x1",
                    "targetRefName": "refs/heads/main",
                    "lastMergeSourceCommit": {"commitId": "azhead222"},
                    "lastMergeTargetCommit": {"commitId": "azbase111"},
                },
            )
        if path == "/pullrequests/19/threads" and method == "POST":
            return httpx.Response(200, request=req, json={"id": 77})
        if path == "/pullrequests/19/iterations" and method == "GET":
            return httpx.Response(200, request=req, json={"value": [{"id": 1}]})
        if path == "/pullrequests/19/iterations/1/changes" and method == "GET":
            return httpx.Response(
                200,
                request=req,
                json={"changeEntries": [{"changeType": "add", "item": {"path": "/src/app.py"}}]},
            )
        if path == "/pullrequests/19" and method == "PATCH":
            return httpx.Response(
                200,
                request=req,
                json={"status": "completed", "lastMergeCommit": {"commitId": "azmerged888"}},
            )
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(azdo_mod.httpx, "request", fake)
    adapter = AzureDevOpsAdapter(_azdo_settings(BOSS_SANDBOX_MERGE_METHOD="squash"))
    assert isinstance(adapter, RepoHost)
    assert adapter.repository_full_name == "acmeorg/shopproj/shop"
    assert adapter.clone_url() == "https://pat:azdo-pat-secret@dev.azure.com/acmeorg/shopproj/_git/shop"
    assert adapter.ensure_branch("boss/code/x1") == "azhead222"
    assert adapter.upsert_file("boss/code/x1", "src/app.py", "print(1)\n", "feat: add app") == "src/app.py"
    pr = adapter.create_pull_request("boss/code/x1", "Fix checkout", "Body")
    assert pr == PullRequestResult(
        url="https://dev.azure.com/acmeorg/shopproj/_git/shop/pullrequest/19", number=19
    )
    snap = adapter.pull_request_snapshot(19)
    assert snap["state"] == "open" and snap["merged"] is False and snap["mergeable"] is True
    comment = adapter.create_issue_comment(19, "LGTM")
    assert comment.id == 77
    rev = adapter.create_review(
        19, "Approved", event="APPROVE", comments=[{"path": "src/app.py", "line": 1, "body": "clean"}]
    )
    assert rev.state == "APPROVED"
    assert "diff --git a/src/app.py b/src/app.py" in adapter.get_pull_request_diff(19)
    merged = adapter.merge_pull_request(19, "merge checkout fix")
    assert merged.merged is True and merged.sha == "azmerged888"

    with pytest.raises(IntegrationConfigurationError):
        AzureDevOpsAdapter(_azdo_settings(BOSS_AZURE_DEVOPS_PAT="")).default_branch()


def test_kiro_gap4_gitlab_and_azure_devops_webhooks(pg_conn):
    c, project_id, _, _ = pg_conn
    settings = Settings(
        BOSS_DEFAULT_PROJECT_ID=project_id,
        BOSS_GITLAB_WEBHOOK_SECRET="gl-secret-123",
        BOSS_AZURE_DEVOPS_WEBHOOK_SECRET="azdo-secret-456",
        BOSS_WEBHOOK_PROJECT_MAP_JSON=json.dumps({"acme/shop": project_id, "shopproj/shop": project_id}),
    )
    svc = InboundWebhookService(settings)

    # GitLab MR merged webhook (verified via X-Gitlab-Token) + dedupe + bad token rejection
    gl_payload = json.dumps(
        {
            "object_kind": "merge_request",
            "project": {"path_with_namespace": "acme/shop"},
            "user": {"username": "alice"},
            "object_attributes": {
                "iid": 11,
                "action": "merge",
                "state": "merged",
                "title": "Fix checkout",
                "source_branch": "boss/code/x1",
                "target_branch": "main",
                "merge_commit_sha": "glmerged999",
            },
        }
    ).encode()
    gl_delivery = f"gl-del-{uuid.uuid4().hex[:8]}"
    rejected = svc.ingest(
        c, "gitlab", gl_payload, {"x-gitlab-token": "wrong", "x-gitlab-event-uuid": gl_delivery}
    )
    assert rejected["status"] == "rejected"

    ok = svc.ingest(
        c, "gitlab", gl_payload, {"x-gitlab-token": "gl-secret-123", "x-gitlab-event-uuid": gl_delivery}
    )
    assert ok["status"] == "accepted" and ok["duplicate"] is False and ok["project_id"] == project_id
    dup = svc.ingest(
        c, "gitlab", gl_payload, {"x-gitlab-token": "gl-secret-123", "x-gitlab-event-uuid": gl_delivery}
    )
    assert dup["duplicate"] is True

    # Azure DevOps PR merged webhook (verified via HMAC) + dedupe + bad signature rejection
    az_payload = json.dumps(
        {
            "eventType": "git.pullrequest.merged",
            "resource": {
                "pullRequestId": 19,
                "status": "completed",
                "title": "Fix checkout",
                "sourceRefName": "refs/heads/boss/code/x1",
                "targetRefName": "refs/heads/main",
                "repository": {"name": "shop", "project": {"name": "shopproj"}},
                "lastMergeCommit": {"commitId": "azmerged888"},
            },
        }
    ).encode()
    az_sig = "sha256=" + hmac.new(b"azdo-secret-456", az_payload, hashlib.sha256).hexdigest()
    az_delivery = f"az-del-{uuid.uuid4().hex[:8]}"
    az_bad = svc.ingest(
        c, "azure_devops", az_payload, {"x-hub-signature-256": "sha256=bad", "x-vss-activityid": az_delivery}
    )
    assert az_bad["status"] == "rejected"

    az_ok = svc.ingest(
        c, "azure_devops", az_payload, {"x-hub-signature-256": az_sig, "x-vss-activityid": az_delivery}
    )
    assert az_ok["status"] == "accepted" and az_ok["duplicate"] is False and az_ok["project_id"] == project_id
    az_dup = svc.ingest(
        c, "azure_devops", az_payload, {"x-hub-signature-256": az_sig, "x-vss-activityid": az_delivery}
    )
    assert az_dup["duplicate"] is True
