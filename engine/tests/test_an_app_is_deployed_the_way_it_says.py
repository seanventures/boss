"""An app that says how it is deployed is deployed the way it says — never guessed at.

HelpDesk's build wrote helpdesk-ai/boss.deploy.yaml (compose: a Node server and its Postgres,
health /readyz). Its host target took the one-container deploy anyway, which guessed "static":
nginx served helpdesk-ai's UNBUILT index.html (`<script src="/src/main.tsx">`), the page was
blank, and the health check on `/` passed because nginx answered 200.

  A. One rule (app_deploy_manifest.declared_manifest): the manifest the build's app declares —
     a directory's own over the repository root's, which belongs to another app. The deploy
     routes the app's host target through the stack path with that manifest; the one-container
     path refuses an app that declares itself, whichever route reaches it.
  B. Health is the manifest's: the stack runs its checks, then writes them onto the target, so
     monitor and later gates probe /readyz, not `/`. A static deploy refuses a page that loads
     source a browser cannot run.
"""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services.app_deploy_manifest import (
    declared_manifest,
    load_manifest,
    manifest_workdir,
    render,
)
from boss_engine.services.stack_deploy import (
    RETIRE_STEP,
    HostAccess,
    StackApplier,
    address_env,
    stack_target_for_deploy,
    target_address,
    target_health_checks,
)

# What HelpDesk's lineage actually wrote (abridged): its app, a workaround copy, and the
# repository root's manifest, which is stack-demo's.
HELPDESK_LINEAGE = [
    "helpdesk-ai/package.json", "helpdesk-ai/server.mjs", "helpdesk-ai/index.html",
    "helpdesk-ai/docker-compose.yml", "helpdesk-ai/boss.deploy.yaml",
    "helpdesk-ai/boss.preview.command", "helpdesk-ai-locked-orig/package.json",
    "helpdesk-ai-locked-orig/boss.preview.command", "boss.deploy.yaml", ".gitignore",
]


# ── A. the one rule ───────────────────────────────────────────────────────────────────────────

def test_the_apps_own_manifest_wins_over_the_repository_roots():
    assert declared_manifest(HELPDESK_LINEAGE) == "helpdesk-ai/boss.deploy.yaml"
    assert manifest_workdir("helpdesk-ai/boss.deploy.yaml") == "helpdesk-ai"


def test_a_root_manifest_counts_only_when_it_is_the_only_one():
    assert declared_manifest(["boss.deploy.yaml", "app.py"]) == "boss.deploy.yaml"
    assert manifest_workdir("boss.deploy.yaml") == ""


def test_no_manifest_means_the_app_declared_nothing():
    assert declared_manifest(["web/index.html", "web/app.js"]) is None
    assert declared_manifest([]) is None


def test_of_two_app_manifests_the_one_holding_the_build_wins():
    files = ["a/boss.deploy.yaml", "b/boss.deploy.yaml", "b/x.js", "b/y.js", "a/z.js"]
    assert declared_manifest(files) == "b/boss.deploy.yaml"


# ── A. the one-container path refuses an app that declares itself ─────────────────────────────

def _write(root, files):
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return root


def _build_dist(monkeypatch, repo, changed):
    from boss_engine.services import deploy_service as ds
    from boss_engine.services import repo_host

    branch = "boss/code/crun_x"
    for args in (["init", "-q", "-b", branch], ["add", "-A"],
                 ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "app"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    monkeypatch.setattr(repo_host, "clone_url_for", lambda *_a, **_k: "file://" + str(repo))
    return ds._build_dist(SimpleNamespace(target_repo_host="github"), "o", "r", branch,
                          changed=changed)


UNBUILT = ('<!doctype html><html><body><div id="root"></div>'
           '<script type="module" src="/src/main.tsx"></script></body></html>\n')
HELPDESK_MANIFEST = """version: 1
name: helpdesk-ai
kind: compose
compose: {file: docker-compose.yml}
env:
  POSTGRES_PASSWORD: {generate: password}
health: [{url: "{target.public_url}/readyz", expect: 200}]
open: {url: "{target.public_url}"}
"""


def test_the_helpdesk_shape_is_refused_by_the_one_container_deploy(tmp_path, monkeypatch):
    repo = _write(tmp_path / "repo", {
        "helpdesk-ai/index.html": UNBUILT,
        "helpdesk-ai/src/main.tsx": "console.log('app')\n",
        "helpdesk-ai/boss.deploy.yaml": HELPDESK_MANIFEST,
        "helpdesk-ai/docker-compose.yml": "services: {}\n",
        "boss.deploy.yaml": "version: 1\nname: stack-demo\n",
    })
    with pytest.raises(RuntimeError) as refused:
        _build_dist(monkeypatch, repo, ["helpdesk-ai/index.html", "helpdesk-ai/boss.deploy.yaml",
                                        "boss.deploy.yaml"])
    assert "helpdesk-ai/boss.deploy.yaml" in str(refused.value)
    assert "declares how it is deployed" in str(refused.value)


def test_a_declared_app_is_refused_even_when_its_build_never_touched_the_manifest(
        tmp_path, monkeypatch):
    """A target deployed with only its app named (Infra's button, a redeploy) still finds it."""
    repo = _write(tmp_path / "repo", {
        "helpdesk-ai/index.html": "<html><body>built</body></html>\n",
        "helpdesk-ai/boss.deploy.yaml": HELPDESK_MANIFEST,
    })
    with pytest.raises(RuntimeError) as refused:
        _build_dist(monkeypatch, repo, ["helpdesk-ai/index.html"])
    assert "helpdesk-ai/boss.deploy.yaml" in str(refused.value)


def test_a_page_that_loads_source_is_never_served(tmp_path, monkeypatch):
    repo = _write(tmp_path / "repo", {"web/index.html": UNBUILT,
                                      "web/src/main.tsx": "console.log('app')\n"})
    with pytest.raises(RuntimeError) as refused:
        _build_dist(monkeypatch, repo, ["web/index.html", "web/src/main.tsx"])
    said = str(refused.value)
    assert "/src/main.tsx" in said and "never built" in said


def test_a_built_static_page_still_deploys(tmp_path, monkeypatch):
    repo = _write(tmp_path / "repo", {
        "web/index.html": '<html><body><script type="module" src="/assets/index-3f2a.js">'
                          "</script></body></html>\n",
        "web/assets/index-3f2a.js": "console.log(1)\n"})
    root, mode, _entry, _sha = _build_dist(monkeypatch, repo, ["web/index.html"])
    assert mode == "static" and (root / "dist" / "index.html").is_file()


# ── A/B. the stack runs from the manifest's directory, at the target's address ────────────────

COMMIT = "c" * 40


class FakeAdapter:
    def __init__(self):
        self.calls: list[str] = []

    def stack_exec(self, **kwargs):
        command = kwargs["command"]
        self.calls.append(command)
        out = ""
        if "BOSS_PREPARED" in command:
            out = "BOSS_MODE=install\nBOSS_PREPARED\n"
        for marker in ("BOSS_UPLOADED", "BOSS_ENV_WRITTEN", "BOSS_SWITCHED", "BOSS_FINALIZED"):
            if f"echo {marker}" in command:
                out += marker + "\n"
        if "BOSS_HEALTHY" in command:
            out += "BOSS_HEALTHY 200\n"
        if "BOSS_RETIRED" in command:
            out += "BOSS_RETIRED helpdesk\n"
        return subprocess.CompletedProcess([], 0, out, "")


HOST = {"address": "192.0.2.202", "ssh_user": "root"}
TARGET = {"id": "deptgt_h", "target_kind": "ssh", "app_name": "HelpDesk", "repo": "boss-sandbox",
          "domains": ["192.0.2.202:8000"],
          "health_checks": [{"kind": "http", "path": "/", "port": 8000, "scheme": "http",
                             "expect_status": 200}]}


def test_a_host_targets_address_is_its_domain_not_the_bare_host():
    assert target_address(TARGET, HOST) == "http://192.0.2.202:8000"
    assert address_env(TARGET, HOST) == {"APP_BASE_URL": "http://192.0.2.202:8000",
                                         "PORT": "8000"}
    assert target_address({"public_url": "https://app.test/"}, HOST) == "https://app.test"
    assert target_address({}, HOST) == "http://192.0.2.202"


def test_the_stack_runs_in_the_manifests_directory_after_retiring_the_old_container():
    manifest = load_manifest(HELPDESK_MANIFEST)
    from boss_engine.services.stack_deploy import _deployment_ctx

    rendered = render(manifest, _deployment_ctx(TARGET, HOST, COMMIT, "pull/223"))
    rendered.update(workdir="helpdesk-ai", retire_container="helpdesk",
                    retire_mount="/opt/boss-apps/helpdesk/")
    adapter = FakeAdapter()
    outcome = StackApplier(
        access=HostAccess(host="192.0.2.202", user="root", port=22, key_material="k",
                          adapter=adapter),
        rendered=rendered, commit=COMMIT, ref="pull/223", env_values={},
        redact_values=[], changeset_id="chg_ok", fetch_tree=lambda dest: None,
        record=lambda steps, log: None, now=lambda: "t").run()

    compose = next(c for c in adapter.calls if "docker compose" in c)
    assert f"/releases/{COMMIT}/helpdesk-ai &&" in compose
    retire = next(i for i, c in enumerate(adapter.calls) if "BOSS_RETIRED" in c)
    assert retire < adapter.calls.index(compose)
    assert "grep -q -F /opt/boss-apps/helpdesk/" in adapter.calls[retire]
    names = [s["name"] for s in outcome["steps"]]
    assert names.index(RETIRE_STEP) < names.index("health")
    # B: the health that ran is the manifest's, at the target's own address.
    assert any("http://192.0.2.202:8000/readyz" in c for c in adapter.calls)


def test_the_manifests_health_becomes_the_targets():
    checks = target_health_checks([
        {"url": "http://192.0.2.202:8000/readyz", "expect": 200, "timeout_seconds": 60},
        {"url": "http://127.0.0.1:9000/internal", "expect": 200, "timeout_seconds": 60}])
    assert checks == [{"kind": "http", "scheme": "http", "port": 8000, "path": "/readyz",
                       "expect_status": 200}]


# ── A. the deploy step and deploy_app route a declared app's host target to its manifest ──────

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test")


@pytest.fixture()
def db():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            wid = c.execute(text("SELECT id FROM workspaces LIMIT 1")).scalar()
    except Exception as error:  # pragma: no cover
        pytest.skip(f"boss_test unavailable: {error}")
    if wid is None:
        pytest.skip("boss_test has no workspace")
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with engine.begin() as c:
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'declared deploy test', :i)"), {"i": pid, "w": wid})
        c.execute(text("INSERT INTO deployment_hosts (id, project_id, name, address, status) "
                       "VALUES (:h, :p, 'vm', '192.0.2.202', 'verified')"),
                  {"h": f"dh_{pid}", "p": pid})
        c.execute(text(
            "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
            " target_kind, target_ref, host_id, domains, created_by, status) "
            "VALUES (:t, :p, 'HelpDesk', 'boss-sandbox', 'staging', 'ssh', "
            " 'root@192.0.2.202:22', :h, '[\"192.0.2.202:8000\"]'::jsonb, 'ceo', 'active')"),
            {"t": f"deptgt_{pid}", "p": pid, "h": f"dh_{pid}"})
        for run_id, files in ((f"crun_root_{pid}", HELPDESK_LINEAGE),
                              (f"crun_plain_{pid}", ["web/index.html"])):
            c.execute(text(
                "INSERT INTO code_task_runs (id, project_id, title, status, spec, state, "
                " finalize_ctx) VALUES (:i, :p, 'HelpDesk', 'completed', "
                " CAST(:s AS JSONB), CAST(:st AS JSONB), CAST(:f AS JSONB))"),
                {"i": run_id, "p": pid,
                 "s": json.dumps({"owner": "paramg", "repo": "boss-sandbox"}),
                 "st": json.dumps({"impl_files": files}),
                 "f": json.dumps({"app_name": "HelpDesk"})})
        # A repair attempt: it touched one file, yet its app still declares itself.
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, title, status, spec, state, "
            " finalize_ctx) VALUES (:i, :p, 'HelpDesk', 'completed', CAST(:s AS JSONB), "
            " CAST(:st AS JSONB), CAST(:f AS JSONB))"),
            {"i": f"crun_repair_{pid}", "p": pid,
             "s": json.dumps({"owner": "paramg", "repo": "boss-sandbox"}),
             "st": json.dumps({"impl_files": ["helpdesk-ai/server.mjs"]}),
             "f": json.dumps({"app_name": "HelpDesk", "root_run_id": f"crun_root_{pid}"})})
    yield engine, pid
    with engine.begin() as c:
        c.execute(text("DELETE FROM code_task_runs WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM deployment_targets WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM deployment_hosts WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})


def test_a_declared_apps_host_target_ships_from_its_manifest(db):
    engine, pid = db
    with engine.begin() as c:
        # The workflow's deploy step: a build and a host, no target named.
        found = stack_target_for_deploy(
            c, pid, {"run_id": f"crun_repair_{pid}", "host_id": f"dh_{pid}"})
        named = stack_target_for_deploy(
            c, pid, {"run_id": f"crun_root_{pid}", "target_id": f"deptgt_{pid}"})
        plain = stack_target_for_deploy(c, pid, {"run_id": f"crun_plain_{pid}"})
    assert found and found["id"] == f"deptgt_{pid}"
    assert found["manifest_path"] == "helpdesk-ai/boss.deploy.yaml"
    assert named and named["manifest_path"] == "helpdesk-ai/boss.deploy.yaml"
    assert plain is None, "an app that declares nothing keeps the one-container deploy"


def test_the_deploy_step_waits_on_the_stack_deployment_it_planned():
    from boss_engine.services.workflow_capability_runner import _awaited_handle

    assert _awaited_handle("deploy", {"status": "planned", "stack_deployment_id": "sdep_1"}) \
        == {"kind": "stack_deployment", "id": "sdep_1"}
    assert _awaited_handle("deploy", {"deployment_run_id": "deprun_1"}) \
        == {"kind": "deployment", "id": "deprun_1"}
