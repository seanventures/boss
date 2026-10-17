"""A new release retires the app's own containers from the resources' project an earlier release
ran in (services/stack_deploy.py: earlier_shared_projects, retire_app_in_shared).

THE CASE (HelpDesk AI, sdep_f08aa1eb78af45d984d6, 2026-10-17). The previous build asked BOSS for
its database, so its app ran inside the resources' project bossapp-helpdesk-ai-deploy with port
8000. The next build brought its own Postgres, ran as project helpdesk-ai, and `compose up` failed
on "port is already allocated": the old app container was nobody's to retire (that project also
holds res-db and its data, so `retire_own_projects` rightly refuses it as a whole).

The retire removes exactly the containers started from under the target's directory, never the
resources' services, never another app's, never a volume — checked here by running the very
shell BOSS sends to the host against a fake `docker`."""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from boss_engine.services import stack_deploy

TARGET_DIR = "/opt/boss-apps/helpdesk-ai"
RENDERED = {"dir": TARGET_DIR, "install": [{"name": "up", "run": "x", "timeout_seconds": 60}],
            "update": [{"name": "up", "run": "x", "timeout_seconds": 60}]}


def test_the_retire_is_a_step_before_the_release_comes_up():
    out = stack_deploy.retire_app_in_shared(
        RENDERED, ["bossapp-helpdesk-ai-deploy", "Bad Name; rm -rf /"])
    names = [s["name"] for s in out["install"]]
    assert names == [f"{stack_deploy.RETIRE_SHARED_STEP} (bossapp-helpdesk-ai-deploy)", "up"]
    assert [s["name"] for s in out["update"]] == names
    run = out["install"][0]["run"]
    assert "com.docker.compose.project.working_dir" in run and f"{TARGET_DIR}/" in run
    assert "docker rm -f" in run and " -v" not in run and "volume" not in run
    assert "compose" not in run.split("docker ps", 1)[0], "never a project-wide down"
    assert stack_deploy.retire_app_in_shared(RENDERED, []) is RENDERED


def _fake_docker(tmp_path: Path, containers: dict[str, tuple[str, str]]) -> Path:
    """A `docker` that knows `ps -aq --filter label=…project=P`, `inspect -f …` and `rm -f`,
    logging every removal. containers: id -> (project, working_dir)."""
    db = tmp_path / "containers.txt"
    db.write_text("".join(f"{cid} {proj} {wd}\n" for cid, (proj, wd) in containers.items()))
    removed = tmp_path / "removed.txt"
    script = tmp_path / "bin" / "docker"
    script.parent.mkdir()
    script.write_text(f"""#!/bin/sh
DB={db}; RM={removed}
case "$1" in
  ps) proj=$(echo "$@" | sed -n 's/.*com.docker.compose.project=\\([^ ]*\\).*/\\1/p')
      awk -v p="$proj" '$2==p {{print $1}}' "$DB" ;;
  inspect) id=$(eval echo \\${{$#}})
      case "$3" in *Name*) echo "/$id" ;; *) awk -v i="$id" '$1==i {{print $3}}' "$DB" ;; esac ;;
  rm) id=$(eval echo \\${{$#}}); echo "$*" >> "$RM"; echo "$id" ;;
esac
""")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return removed


def test_only_the_apps_own_containers_go(tmp_path):
    removed = _fake_docker(tmp_path, {
        "app1": ("bossapp-helpdesk-ai-deploy", f"{TARGET_DIR}/releases/0f3c/helpdesk-ai"),
        "resdb": ("bossapp-helpdesk-ai-deploy", "/opt/boss-infra/helpdesk-ai/deploy"),
        "lookalike": ("bossapp-helpdesk-ai-deploy", "/opt/boss-apps/helpdesk-ai-two/releases/1"),
        "otherapp": ("bossapp-other-deploy", "/opt/boss-apps/other/releases/9"),
    })
    run = stack_deploy.retire_app_in_shared(RENDERED, ["bossapp-helpdesk-ai-deploy"])[
        "install"][0]["run"]
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"}
    done = subprocess.run(["bash", "-c", run], capture_output=True, text=True, env=env,
                          timeout=30)
    assert done.returncode == 0, done.stderr
    assert "BOSS_RETIRED app1" in done.stdout
    assert removed.read_text().split() == ["rm", "-f", "app1"], \
        "res-db, a look-alike directory and another app's project are never touched"


def test_nothing_of_the_app_there_is_nothing_done(tmp_path):
    removed = _fake_docker(tmp_path, {
        "resdb": ("bossapp-helpdesk-ai-deploy", "/opt/boss-infra/helpdesk-ai/deploy")})
    run = stack_deploy.retire_app_in_shared(RENDERED, ["bossapp-helpdesk-ai-deploy"])[
        "install"][0]["run"]
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"}
    done = subprocess.run(["bash", "-c", run], capture_output=True, text=True, env=env,
                          timeout=30)
    assert done.returncode == 0 and "BOSS_SHARED_CHECKED" in done.stdout
    assert not removed.exists()


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *_a, **_k):
        rows = self.rows

        class R:
            def all(self):
                return [(r,) for r in rows]
        return R()


def test_the_projects_come_from_this_targets_earlier_releases():
    conn = _Rows(["bossapp-helpdesk-ai-deploy", "bossapp-helpdesk-ai-staging", "bad name"])
    assert stack_deploy.earlier_shared_projects(conn, "deptgt_x", keep=None) == [
        "bossapp-helpdesk-ai-deploy", "bossapp-helpdesk-ai-staging"]
    assert stack_deploy.earlier_shared_projects(
        conn, "deptgt_x", keep="bossapp-helpdesk-ai-deploy") == ["bossapp-helpdesk-ai-staging"], \
        "the project this release runs in is never retired from under it"


def test_the_apply_reads_what_the_plan_recorded():
    source = Path(stack_deploy.__file__).read_text()
    assert '"retire_app_in": moved_from' in source
    assert 'retire_app_in_shared(rendered, [' in source


# ── the old resources' values stay with the release that asked for them ──────────────────────

class _Nested:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _BindingRows:
    """infra_platform_resources rows: (bindings,)"""
    def __init__(self, rows):
        self.rows = rows

    def begin_nested(self):
        return _Nested()

    def execute(self, *_a, **_k):
        rows = self.rows

        class R:
            def all(self):
                return [(r,) for r in rows]
        return R()


def _manifest(resources: bool):
    from boss_engine.services.app_deploy_manifest import load_manifest

    doc = ("version: 1\nname: helpdesk-ai\nkind: compose\ncompose:\n  file: docker-compose.yml\n"
           "health: [{url: \"{target.public_url}/auth/providers\", expect: 200}]\n")
    if resources:
        doc += "resources:\n  db: {capability: database.relational, engine: postgres}\n"
    return load_manifest(doc)


def test_a_release_that_brings_its_own_database_is_not_handed_the_old_one(monkeypatch):
    """HelpDesk AI sdep_121762db692549afa39b: the release's compose file runs its own Postgres
    (DATABASE_URL defaults to its `db` service), BOSS still wrote the earlier release's resource
    binding DATABASE_URL=…res-db… into its env file, and the app exited on EAI_AGAIN res-db."""
    from boss_engine.app_links import service as links
    from boss_engine.services import governed_host_deploy
    from boss_engine.templates import base_url

    monkeypatch.setattr(governed_host_deploy, "_app_key_for", lambda *a, **k: "helpdesk ai")
    monkeypatch.setattr(links, "project_org", lambda *a, **k: "org_x")
    monkeypatch.setattr(base_url, "deploy_environment", lambda *a, **k: (
        {"DATABASE_URL": "postgresql://app@res-db/db", "SESSION_SECRET": "s" * 40}, [], ""))
    conn = _BindingRows([{"DATABASE_URL": {"secretRef": "app_secret://deploy/helpdesk:20ai/"
                                                        "DATABASE_URL"}}])
    own = stack_deploy.app_deploy_env(conn, "p", {"id": "t"}, None, manifest=_manifest(False))
    assert "DATABASE_URL" not in own and own["SESSION_SECRET"], \
        "a release without resources keeps its own database and every other app value"
    asks = stack_deploy.app_deploy_env(conn, "p", {"id": "t"}, None, manifest=_manifest(True))
    assert asks["DATABASE_URL"].endswith("res-db/db"), "a release that asks for them keeps them"
    legacy = stack_deploy.app_deploy_env(conn, "p", {"id": "t"}, None)
    assert "DATABASE_URL" in legacy, "a caller with no manifest sees what it always saw"


def test_the_binding_names_are_read_from_the_apps_live_resources():
    rows = [{"DATABASE_URL": {}}, '{"REDIS_URL": {}}', None]
    assert stack_deploy.resource_binding_names(_BindingRows(rows), "p", "helpdesk ai") == \
        {"DATABASE_URL", "REDIS_URL"}
