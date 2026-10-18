"""GitHub Actions CI, governed — through the real API, on Postgres, with a fake GitHub.

  * "Builds may change CI workflows" is off by default, per repository; turning it on needs an
    Owner/Admin with a fresh step-up, is recorded, and makes the App ask for workflows: write
    (with the exact GitHub pages to accept it for an App that already exists);
  * a build whose PR touches .github/workflows/ is flagged "CI changed" and only a PERSON can
    approve its merge or its deploy — an agent member, a policy and the effect path are refused;
  * wait-for-CI records the PR head's checks in words and a failure or a timeout blocks the deploy
    approval (and the deployment effect), linking the failing check; the governed pipeline's own
    wait does the same.
"""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.app.main import app
from boss_engine.identity import github_setup
from boss_engine.services import ci_gate, decisions

PROJECT = "boss-dogfood"
SHA = "c" * 40


def _binding(engine, project: str = PROJECT, repo: str = "acme/web") -> str:
    tag = uuid4().hex[:8]
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
            "credential_ref, status, created_by) VALUES (:i, :p, 'ci test', "
            "'personal_access_token', 'env:NOPE', 'configured', 't')"), {"i": f"ghc_ci_{tag}", "p": project})
        c.execute(text(
            "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, "
            "full_name, role, status, created_by) VALUES (:b, :p, :c, 'acme', 'web', :r, 'source', "
            "'active', 't')"), {"b": f"ghb_ci_{tag}", "p": project, "c": f"ghc_ci_{tag}", "r": repo})
    return f"ghb_ci_{tag}"


def _drop(engine, binding_id: str) -> None:
    with engine.begin() as c:
        c.execute(text("DELETE FROM github_connections WHERE id = :c"),
                  {"c": binding_id.replace("ghb_", "ghc_")})


def _decision(engine, recommendation: dict, *, status: str = "pending") -> str:
    decision_id = f"dec_ci_{uuid4().hex[:10]}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, risk_tier, "
            "default_action, status, approved_by) VALUES (:i, :p, 'CI test', CAST(:r AS JSONB), 1, "
            "'hold', :s, :by)"),
            {"i": decision_id, "p": PROJECT, "r": json.dumps(recommendation), "s": status,
             "by": "auto_approval" if status == "approved" else None})
    return decision_id


def _status(engine, decision_id: str) -> str:
    with engine.begin() as c:
        return c.execute(text("SELECT status FROM decisions WHERE id = :i"),
                         {"i": decision_id}).scalar_one()


def _agent_member(engine, member_id: str = "deploy-bot") -> str:
    """An AGENT seated as a member with every right to resolve decisions — the strongest agent
    there can be. It still cannot approve a CI change."""
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
            "role_keys, permissions, status, created_by) VALUES (:i, :p, :m, 'Deploy bot', 'agent', "
            "'[\"ceo\", \"cto\", \"release_manager\"]'::jsonb, '[\"decision_resolve\"]'::jsonb, "
            "'active', 't') ON CONFLICT DO NOTHING"), {"i": f"pm_{member_id}", "p": PROJECT, "m": member_id})
    return member_id


class FakeCi:
    def __init__(self, runs, has_workflows=True):
        self.runs, self.workflows, self.repository = runs, has_workflows, "acme/web"

    def check_runs(self, sha):
        return self.runs

    def statuses(self, sha):
        return []

    def has_workflows(self, sha):
        return self.workflows

    def pull_request(self, number):
        return {"head": {"sha": SHA}}


def _fake_github(monkeypatch, runs, **kw):
    reader = FakeCi(runs, **kw)
    monkeypatch.setattr(ci_gate, "reader_for_project", lambda *a, **k: reader)
    return reader


FAILING = [{"id": 1, "name": "lint", "status": "completed", "conclusion": "success",
            "html_url": "https://github.test/acme/web/runs/1"},
           {"id": 7, "name": "unit-tests", "status": "completed", "conclusion": "failure",
            "html_url": "https://github.test/acme/web/runs/7"}]
PASSING = [{"id": 1, "name": "lint", "status": "completed", "conclusion": "success"},
           {"id": 8, "name": "unit-tests", "status": "completed", "conclusion": "success"},
           {"id": 9, "name": "build", "status": "completed", "conclusion": "success"}]
PENDING = [{"id": 3, "name": "e2e", "status": "in_progress", "conclusion": None,
            "html_url": "https://github.test/acme/web/runs/3"}]


# ---- 1. the opt-in ---------------------------------------------------------------------------------

def test_the_opt_in_is_off_by_default_recorded_and_widens_the_apps_request(client, engine, monkeypatch):
    binding = _binding(engine)
    monkeypatch.setattr(github_setup, "app_config", lambda *a, **k: {
        "slug": "boss-acme", "account_login": "acme", "installation_id": "4242",
        # The account types GitHub reported (identity/github_urls.py builds every link from them).
        "owner_login": "acme", "owner_type": "Organization", "account_type": "Organization"})
    try:
        got = client.get(f"/v1/setup/repository/{binding}/ci-workflows", params={"project_id": PROJECT})
        assert got.status_code == 200, got.text
        assert got.json()["enabled"] is False and got.json()["label"] == "Builds may change CI workflows"
        assert "permissions_url" not in got.json()
        with engine.begin() as c:
            assert ci_gate.build_github_needs(c, PROJECT, "acme/web", {"contents": "write"}) == {
                "contents": "write"}

        on = client.put(f"/v1/setup/repository/{binding}/ci-workflows",
                        json={"project_id": PROJECT, "enabled": True, "actor": "ceo"})
        assert on.status_code == 200, on.text
        body = on.json()
        assert body["enabled"] is True and body["app_permission"] == "workflows: write"
        # The opt-in IS the grant for this repository (identity policy allows it by default).
        assert body["grant"]["applies"] is True and "acme/web" in body["grant"]["words"]
        assert body["permissions_url"] == (
            "https://github.com/organizations/acme/settings/apps/boss-acme/permissions")
        assert body["installation_url"].endswith("/organizations/acme/settings/installations/4242")
        with engine.begin() as c:
            event = c.execute(text(
                "SELECT actor, data FROM events WHERE entity_id = :b AND "
                "event_type = 'repository.ci_workflows_write_changed'"), {"b": binding}).one()
            assert event.actor == "ceo" and event.data["after"] is True and event.data["before"] is False
            assert ci_gate.any_repo_allows_workflows(c)
            assert ci_gate.build_github_needs(c, PROJECT, "acme/web", {"contents": "write"}) == {
                "contents": "write", "workflows": "write"}
        off = client.put(f"/v1/setup/repository/{binding}/ci-workflows",
                         json={"project_id": PROJECT, "enabled": False, "actor": "ceo"})
        assert off.json()["enabled"] is False
    finally:
        _drop(engine, binding)


def test_changing_the_opt_in_needs_an_owner_or_admin_with_a_fresh_step_up(engine):
    from apps.api.tests.identity_fixtures import IdentityWorld
    from apps.api.tests.identity_fxd_support import STEP_UP_MINUTES, session_ctx

    world = IdentityWorld.create(engine)
    owner, operator = world.person("ci-owner"), world.person("ci-operator")
    world.bind(owner, "owner")
    world.bind(operator, "operator", project=world.project_a)
    binding = _binding(engine, project=world.project_a)
    real = TestClient(app, raise_server_exceptions=False)
    put = dict(url=f"/v1/setup/repository/{binding}/ci-workflows",
               json={"project_id": world.project_a, "enabled": True})
    stale = session_ctx(owner, mfa_age=timedelta(minutes=STEP_UP_MINUTES + 1))
    try:
        with world.acting_as(stale):
            refused = real.put(**put)
        assert refused.status_code == 401 and "step_up" in refused.text, refused.text
        with world.acting_as(session_ctx(operator, mfa_age=timedelta(minutes=1))):
            assert real.put(**put).status_code == 403
        with world.acting_as(session_ctx(owner, mfa_age=None)):        # password only
            assert real.put(**put).status_code == 401
        with engine.begin() as c:
            meta = c.execute(text("SELECT metadata FROM github_repository_bindings WHERE id = :b"),
                             {"b": binding}).scalar_one()
        assert not meta.get(ci_gate.SETTING_KEY)                         # nothing changed
    finally:
        _drop(engine, binding)


# ---- 2. CI changed: a person approves, nobody else ------------------------------------------------

def test_a_pr_touching_workflows_is_flagged_and_only_a_person_can_approve_merge_or_deploy(client, engine):
    checks = client.post("/v1/ci/workflow-checks", json={"project_id": PROJECT, "files": [
        {"path": ".github/workflows/ci.yml",
         "content": "on: pull_request_target\njobs:\n  a:\n    steps:\n      - uses: actions/checkout@v4\n"
                    "        with:\n          ref: ${{ github.event.pull_request.head.sha }}\n"},
        {"path": "app.py", "content": "x = 1"}]})
    assert checks.status_code == 200, checks.text
    flagged = checks.json()
    assert flagged["ci_changed"] is True and flagged["requires_person_approval"] is True
    assert {f["check"] for f in flagged["findings"]} == {"unpinned_action", "pull_request_target"}
    assert flagged["pull_request_comment"].startswith("**CI changed**")

    url = f"https://github.com/acme/web/pull/{uuid4().int % 10000}"
    merge = _decision(engine, {"action": "review_pull_request", "pull_request_url": url,
                               "head_sha": SHA, "ci_changed": True,
                               "ci_changed_files": flagged["files"], "ci_findings": flagged["findings"],
                               "requires_person_approval": True})
    gate = client.get("/v1/ci/gate", params={"project_id": PROJECT, "decision_id": merge}).json()
    assert gate["ci_changed"] is True and gate["person_approval_required"] is True

    bot = _agent_member(engine)
    refused = client.post(f"/v1/decisions/{merge}/approve", json={"actor": bot, "note": "lgtm"})
    assert refused.status_code == 409, refused.text
    assert "Only a person" in refused.text and _status(engine, merge) == "pending"
    with engine.begin() as c:                       # the autonomy policy cannot approve it either
        assert decisions.approve_by_policy(c, merge, project_id=PROJECT,
                                           actor=decisions.SYSTEM_ACTOR, note="policy") is False
    assert _status(engine, merge) == "pending"

    approved = client.post(f"/v1/decisions/{merge}/approve", json={"actor": "ceo", "note": "read it"})
    assert approved.status_code == 200, approved.text

    # The deploy of the same pull request inherits the flag.
    deploy = _decision(engine, {"action": "review_deployment", "pull_request_url": url,
                                "merged_sha": "d" * 40,
                                "environment": api_main.settings.sandbox_deploy_environment})
    gate = client.get("/v1/ci/gate", params={"project_id": PROJECT, "decision_id": deploy}).json()
    assert gate["ci_changed"] is True and gate["ci_changed_files"] == [".github/workflows/ci.yml"]
    assert client.post(f"/v1/decisions/{deploy}/approve",
                       json={"actor": bot, "note": "ship"}).status_code == 409
    # An approval that reached `approved` by any other path is refused at the effect itself.
    sneaked = _decision(engine, {"action": "review_deployment", "pull_request_url": url,
                                 "merged_sha": "d" * 40,
                                 "environment": api_main.settings.sandbox_deploy_environment},
                        status="approved")
    effect = client.post("/v1/software-factory/deployments", json={
        "project_id": PROJECT, "decision_id": sneaked, "dedupe_key": f"ci_{uuid4().hex[:8]}",
        "merged_sha": "d" * 40, "pull_request_url": url, "title": "x",
        "environment": api_main.settings.sandbox_deploy_environment})
    assert effect.status_code == 422 and "not a person" in effect.text, effect.text


# ---- 3. wait for CI ----------------------------------------------------------------------------------

def test_a_failing_check_blocks_the_deploy_approval_and_links_the_check(client, engine, monkeypatch):
    _fake_github(monkeypatch, FAILING)
    url = f"https://github.com/acme/web/pull/{uuid4().int % 10000}"
    wait = client.post("/v1/ci/waits", json={"project_id": PROJECT, "pull_request_url": url,
                                             "actor": "ceo"})
    assert wait.status_code == 200, wait.text
    body = wait.json()
    assert body["status"] == "failed" and body["head_sha"] == SHA and body["blocks_deploy"] is True
    assert body["summary"] == "CI failed: 'unit-tests' failed — https://github.test/acme/web/runs/7"
    assert body["failing_url"] == "https://github.test/acme/web/runs/7"

    deploy = _decision(engine, {"action": "review_deployment", "pull_request_url": url,
                                "merged_sha": "e" * 40,
                                "environment": api_main.settings.sandbox_deploy_environment})
    gate = client.get("/v1/ci/gate", params={"project_id": PROJECT, "decision_id": deploy}).json()
    assert gate["ci_blocks"] is True and gate["ci_wait"]["id"] == body["id"]
    refused = client.post(f"/v1/decisions/{deploy}/approve", json={"actor": "ceo", "note": "ship"})
    assert refused.status_code == 409
    assert "https://github.test/acme/web/runs/7" in refused.text and "until CI passes" in refused.text
    assert _status(engine, deploy) == "pending"
    sneaked = _decision(engine, {"action": "review_deployment", "pull_request_url": url,
                                 "merged_sha": "e" * 40,
                                 "environment": api_main.settings.sandbox_deploy_environment},
                        status="approved")
    effect = client.post("/v1/software-factory/deployments", json={
        "project_id": PROJECT, "decision_id": sneaked, "dedupe_key": f"ci_{uuid4().hex[:8]}",
        "merged_sha": "e" * 40, "pull_request_url": url, "title": "x",
        "environment": api_main.settings.sandbox_deploy_environment})
    assert effect.status_code == 422 and "Deploy refused until CI passes" in effect.text

    # CI fixed and re-run: the latest wait passes and the same approval goes through.
    _fake_github(monkeypatch, PASSING)
    again = client.post("/v1/ci/waits", json={"project_id": PROJECT, "pull_request_url": url})
    assert again.json()["status"] == "passed" and again.json()["summary"] == "CI passed: 3 checks"
    assert client.post(f"/v1/decisions/{deploy}/approve",
                       json={"actor": "ceo", "note": "ship"}).status_code == 200


def test_ci_that_never_finishes_times_out_in_words_and_blocks(client, engine, monkeypatch):
    _fake_github(monkeypatch, PENDING)
    url = f"https://github.com/acme/web/pull/{uuid4().int % 10000}"
    wait = client.post("/v1/ci/waits", json={"project_id": PROJECT, "pull_request_url": url,
                                             "timeout_minutes": 1}).json()
    assert wait["status"] == "waiting" and "waiting on 'e2e'" in wait["summary"]
    with engine.begin() as c:                                    # a minute and a bit later
        c.execute(text("UPDATE ci_waits SET created_at = created_at - interval '70 seconds', "
                       "deadline_at = deadline_at - interval '70 seconds' "
                       "WHERE id = :i"), {"i": wait["id"]})
    polled = client.post(f"/v1/ci/waits/{wait['id']}/poll", json={"project_id": PROJECT}).json()
    assert polled["status"] == "timed_out"
    assert polled["summary"] == ("CI did not finish in 1 min: 'e2e' still running — "
                                 "https://github.test/acme/web/runs/3")
    deploy = _decision(engine, {"action": "review_deployment", "pull_request_url": url,
                                "environment": api_main.settings.sandbox_deploy_environment})
    refused = client.post(f"/v1/decisions/{deploy}/approve", json={"actor": "ceo", "note": "x"})
    assert refused.status_code == 409 and "did not finish" in refused.text
    got = client.get(f"/v1/ci/waits/{wait['id']}", params={"project_id": PROJECT}).json()
    assert got["status"] == "timed_out" and got["attempts"] == 2


def test_the_governed_pipeline_waits_for_ci_before_the_deploy_approval(engine, monkeypatch):
    """The build's own wait (governed_code_task_service._wait_for_ci): blocking, bounded, each
    look in its own transaction, and recorded against the PR so the deploy approval is gated."""
    from boss_engine.services import governed_code_task_service as gcts

    url = f"https://github.com/acme/web/pull/{uuid4().int % 10000}"
    looks = iter([PENDING, PENDING, FAILING])

    class Github:
        repository_full_name = "acme/web"

        def pull_request_snapshot(self, number):
            return {"head_sha": SHA}

        def _request(self, method, path, *a, **k):
            if "/contents/.github/workflows" in path:
                return [{"name": "ci.yml"}]
            if path.endswith("/status"):
                return {"statuses": []}
            return {"check_runs": next(looks)}

    slept: list[float] = []
    settings = SimpleNamespace(ci_wait_timeout_seconds=1800)
    run_id = f"crun_ci_{uuid4().hex[:8]}"
    out = gcts._wait_for_ci(engine, settings, SimpleNamespace(github=Github()),  # noqa: SLF001
                            {"pull_request_number": 5, "pull_request_url": url}, PROJECT, run_id,
                            "acme/web", sleep=slept.append)
    assert out["status"] == "failed" and "'unit-tests' failed" in out["summary"]
    assert len(slept) == 2
    with engine.begin() as c:
        row = c.execute(text("SELECT status, code_task_run_id, failing_url FROM ci_waits "
                             "WHERE pull_request_url = :u"), {"u": url}).one()
        assert tuple(row) == ("failed", run_id, "https://github.test/acme/web/runs/7")
        assert c.execute(text("SELECT data->>'status' FROM events WHERE entity_id = :r AND "
                              "event_type = 'code_task.ci_waited'"), {"r": run_id}).scalar() == "failed"
        wait = ci_gate.latest_wait(c, PROJECT, pull_request_url=url)
    assert wait["blocks_deploy"] is True


def test_a_repo_without_ci_is_said_so_and_not_waited_on(engine):
    from boss_engine.services import governed_code_task_service as gcts

    class Github:
        repository_full_name = "acme/web"

        def pull_request_snapshot(self, number):
            return {"head_sha": SHA}

        def _request(self, method, path, *a, **k):
            import httpx

            request = httpx.Request("GET", "https://api.github.test" + path)
            raise httpx.HTTPStatusError("404", request=request,
                                        response=httpx.Response(404, request=request))

    out = gcts._wait_for_ci(engine, SimpleNamespace(ci_wait_timeout_seconds=1800),  # noqa: SLF001
                            SimpleNamespace(github=Github()),
                            {"pull_request_number": 5, "pull_request_url": "https://github.com/a/b/pull/1"},
                            PROJECT, f"crun_ci_{uuid4().hex[:8]}", "acme/web", sleep=lambda s: None)
    assert out["status"] == "no_ci" and out["summary"].startswith("No CI is configured for acme/web")


def test_ci_that_cannot_be_read_blocks_merge_and_deploy_unless_a_person_forces_the_merge(
        client, engine, monkeypatch):
    """FAIL CLOSED: a wait whose reads all fail ends `unreadable`, never passed."""
    class Down(FakeCi):
        def check_runs(self, sha):
            raise RuntimeError("GitHub answered 502")
    reader = Down([])
    monkeypatch.setattr(ci_gate, "reader_for_project", lambda *a, **k: reader)
    url = f"https://github.com/acme/web/pull/{uuid4().int % 10000}"
    wait = client.post("/v1/ci/waits", json={"project_id": PROJECT, "pull_request_url": url,
                                             "timeout_minutes": 1}).json()
    assert wait["status"] == "waiting"
    with engine.begin() as c:
        c.execute(text("UPDATE ci_waits SET created_at = created_at - interval '70 seconds', "
                       "deadline_at = deadline_at - interval '70 seconds' "
                       "WHERE id = :i"), {"i": wait["id"]})
    polled = client.post(f"/v1/ci/waits/{wait['id']}/poll", json={"project_id": PROJECT}).json()
    assert polled["status"] == "unreadable" and polled["blocks_deploy"] is True
    assert polled["summary"] == "CI could not be read: RuntimeError: GitHub answered 502"

    deploy = _decision(engine, {"action": "review_deployment", "pull_request_url": url,
                                "environment": api_main.settings.sandbox_deploy_environment})
    refused = client.post(f"/v1/decisions/{deploy}/approve", json={"actor": "ceo", "note": "x"})
    assert refused.status_code == 409 and "CI could not be read" in refused.text
    forced_deploy = client.post(f"/v1/decisions/{deploy}/approve",
                                json={"actor": "ceo", "note": "ship it", "force": True})
    assert forced_deploy.status_code == 409                       # never a forced deploy
    merge = _decision(engine, {"action": "review_pull_request", "pull_request_url": url})
    assert client.post(f"/v1/decisions/{merge}/approve",
                       json={"actor": "ceo", "note": "x"}).status_code == 409
    forced = client.post(f"/v1/decisions/{merge}/approve",
                         json={"actor": "ceo", "note": "CI is down; reviewed by hand", "force": True})
    assert forced.status_code == 200, forced.text


def test_a_red_ci_read_again_after_a_rerun_clears_or_rearms_and_never_opens_on_an_error(
        client, engine, monkeypatch):
    """A person re-runs the failed job on GitHub (same commit). Pressing Approve reads CI again:
    green lets the approval through. An explicit look re-arms a wait whose re-run is still in
    progress with a fresh deadline. A read that fails keeps the refusal — the gate never opens on
    an error."""
    _fake_github(monkeypatch, FAILING)
    url = f"https://github.com/acme/web/pull/{uuid4().int % 10000}"
    wait = client.post("/v1/ci/waits", json={"project_id": PROJECT, "pull_request_url": url}).json()
    assert wait["status"] == "failed"
    deploy = _decision(engine, {"action": "review_deployment", "pull_request_url": url,
                                "environment": api_main.settings.sandbox_deploy_environment})

    # GitHub unreachable at approval time: still refused, still the failing check.
    def broken(*a, **k):
        raise RuntimeError("GitHub is down")
    monkeypatch.setattr(ci_gate, "reader_for_project", broken)
    refused = client.post(f"/v1/decisions/{deploy}/approve", json={"actor": "ceo", "note": "x"})
    assert refused.status_code == 409 and "runs/7" in refused.text

    # The re-run is in progress: an explicit look re-arms the wait, the approval still waits.
    _fake_github(monkeypatch, PENDING)
    polled = client.post(f"/v1/ci/waits/{wait['id']}/poll", json={"project_id": PROJECT}).json()
    assert polled["status"] == "waiting" and polled["summary"].startswith("CI is running again")
    assert polled["blocks_deploy"] is True
    refused = client.post(f"/v1/decisions/{deploy}/approve", json={"actor": "ceo", "note": "x"})
    assert refused.status_code == 409 and "still running" in refused.text

    # It goes green: one more look passes, and the approval goes through.
    _fake_github(monkeypatch, PASSING)
    polled = client.post(f"/v1/ci/waits/{wait['id']}/poll", json={"project_id": PROJECT}).json()
    assert polled["status"] == "passed" and polled["summary"] == "CI passed: 3 checks"
    assert client.post(f"/v1/decisions/{deploy}/approve",
                       json={"actor": "ceo", "note": "ship"}).status_code == 200


def test_pressing_approve_after_a_rerun_went_green_reads_ci_again(client, engine, monkeypatch):
    _fake_github(monkeypatch, FAILING)
    url = f"https://github.com/acme/web/pull/{uuid4().int % 10000}"
    wait = client.post("/v1/ci/waits", json={"project_id": PROJECT, "pull_request_url": url}).json()
    assert wait["status"] == "failed"
    deploy = _decision(engine, {"action": "review_deployment", "pull_request_url": url,
                                "environment": api_main.settings.sandbox_deploy_environment})
    _fake_github(monkeypatch, PASSING)
    assert client.post(f"/v1/decisions/{deploy}/approve",
                       json={"actor": "ceo", "note": "ship"}).status_code == 200
    got = client.get(f"/v1/ci/waits/{wait['id']}", params={"project_id": PROJECT}).json()
    assert got["status"] == "passed"


def test_ci_is_read_with_the_token_clone_and_pr_use(monkeypatch):
    """D2: the project connection's fine-grained token answered 403 on check runs, statuses and
    Actions runs while the App's installation token read them. CI is now read with the shared
    repo_token decision; the connection's credential is only the fallback."""
    from boss_engine.services import ci_gate, github_app_adapter, kit_connections

    monkeypatch.setattr(kit_connections, "github_state",
                        lambda *a, **k: {"connected": True, "repo": "acme/shop"})
    monkeypatch.setattr(kit_connections, "github_token", lambda *a, **k: "connection-token")
    asked = []
    monkeypatch.setattr(github_app_adapter, "repo_token",
                        lambda s, o, r, **k: asked.append((o, r)) or "app-token")
    reader = ci_gate.reader_for_project(None, object(), "p1", "acme/shop")
    assert reader.token == "app-token" and asked == [("acme", "shop")]

    def no_app(*a, **k):
        raise github_app_adapter.GitHubAppConfigurationError("not installed here")

    monkeypatch.setattr(github_app_adapter, "repo_token", no_app)
    assert ci_gate.reader_for_project(None, object(), "p1", "acme/shop").token == \
        "connection-token"
