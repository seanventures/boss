"""Deleting a project, end to end on a real database (services/projects_delete.py).

The operator's words: "provide option to delete the project … it should clean up all workflows,
builds and other resources in database". So a project is seeded the way BOSS writes one (the
HelpDesk story: workflow runs, builds, decisions, work items, ambient recommendations, alerts,
incidents, a knowledge graph, a server and its staging app, plus a queued job and a running
preview), deleted through the API, and then EVERY table of the schema is read for any text
naming it: none is left but the security audit log. Another project's rows are untouched. Every
refusal (a wrong name, no confirmation, a lapsed second factor, the home project, a live
production app without the tick, a server that will not let go of the app) writes nothing, and a
failure half-way rolls everything back.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.project_experience_seed import seed, seed_round_two
from boss_engine.config import get_settings
from boss_engine.services import projects_delete as pd

#: Rows that may still name a deleted project: the security audit log (hash-chained, kept by
#: design) and identity records revoked by the delete (kept as security records).
KEPT = {"identity_security_events"}


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture(scope="module")
def owner(world):
    person = world.person("delete-owner")
    world.bind(person, "owner")
    return person


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def hosts(monkeypatch):
    """The servers: every removal is recorded, and answers as the test says."""
    calls: list[dict] = []
    answer = {"status": "removed"}

    def remover(_engine, _settings, app_row):
        calls.append(app_row)
        return dict(answer)

    stopped: list[str] = []
    monkeypatch.setattr(pd, "HOST_REMOVER", remover)
    monkeypatch.setattr(pd, "PREVIEW_STOPPER", lambda _e, pid, _s: stopped.append(pid))
    return {"calls": calls, "answer": answer, "previews": stopped}


def _project(world, owner, client, engine, *, name: str | None = None) -> dict:
    slug = f"del-{uuid.uuid4().hex[:8]}"
    name = name or f"Delete Me {slug[-4:]}"
    with world.acting_as(world.ctx(owner)):
        made = client.post("/v1/projects", json={"name": name, "slug": slug})
    assert made.status_code == 201, made.text
    with engine.begin() as db:
        story = seed(db, slug)
        seed_round_two(db, slug)
        db.execute(text(
            "INSERT INTO jobs (id, project_id, kind, status, payload) VALUES "
            "(:i, :p, 'code_task.run', 'queued', CAST('{}' AS JSONB))"),
            {"i": f"job_{uuid.uuid4().hex[:12]}", "p": slug})
        db.execute(text(
            "INSERT INTO app_previews (id, project_id, repo, branch, status, mode) VALUES "
            "(:i, :p, 'acme/helpdesk', 'main', 'running', 'process')"),
            {"i": f"prv_{uuid.uuid4().hex[:12]}", "p": slug})
        try:
            with db.begin_nested():
                from boss_engine.services.knowledge import age

                if age.available(db):
                    age.rebuild(db, slug)
        except Exception:  # noqa: BLE001 - no AGE here: the graph rows are still seeded
            pass
    return {"id": slug, "name": name, **story}


def _delete(world, owner, client, project_id: str, mfa: bool = True, **body):
    payload = {"step_up": "confirmed", **body}
    with world.acting_as(world.ctx(owner, mfa=mfa)):
        return client.post(f"/v1/projects/{project_id}/delete", json=payload)


def _naming(engine, project_id: str) -> dict[str, int]:
    """Every text / text[] column of every table, rows naming the project: {table.column: n}."""
    out: dict[str, int] = {}
    with engine.connect() as db:
        cols = db.execute(text(
            "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_attribute a ON a.attrelid = c.oid "
            "AND a.attnum > 0 AND NOT a.attisdropped WHERE n.nspname = current_schema() "
            "AND c.relkind IN ('r', 'p') AND NOT c.relispartition "
            "AND format_type(a.atttypid, a.atttypmod) IN ('text', 'text[]')")).all()
        for table, col, typ in cols:
            cond = f'"{col}" = :p' if typ == "text" else f':p = ANY("{col}")'
            n = db.execute(text(f'SELECT count(*) FROM "{table}" WHERE {cond}'),
                           {"p": project_id}).scalar()
            if n:
                out[f"{table}.{col}"] = int(n)
    return out


def _rows(engine, project_id: str) -> dict[str, int]:
    with engine.connect() as db:
        cat = pd.catalog(db)
        return pd._direct_counts(db, cat, project_id)


def _audit(engine, org: str, project_id: str) -> list[dict]:
    with engine.connect() as db:
        return [dict(r) for r in db.execute(text(
            "SELECT event_type, actor_principal_id, details FROM identity_security_events "
            "WHERE org_id = :o AND target_id = :p AND event_type = 'project.deleted'"),
            {"o": org, "p": project_id}).mappings().all()]


def test_every_table_is_deleted_or_kept_by_name(engine):
    """A new table is classified when it is added: deleted through project_id, a foreign key or
    a logical key, or kept by name with why. None falls through."""
    with engine.connect() as db:
        cat = pd.catalog(db)
    got = pd.classify_tables(cat)
    assert got["unclassified"] == [], (
        "tables neither deleted with a project nor kept by name (projects_delete.LOGICAL_KEYS / "
        f"KEPT_TABLES): {got['unclassified']}")
    assert len(got["direct"]) > 200 and "workflow_runs" in got["direct"]
    assert "identity_security_events" in got["kept"]
    assert {"budget_policies", "experiment_assignments"} <= set(got["logical"])


def test_the_dialog_counts_what_goes_by_kind(world, owner, client, engine, hosts):
    a = _project(world, owner, client, engine)
    with world.acting_as(world.ctx(owner)):
        got = client.get(f"/v1/projects/{a['id']}/delete")
    assert got.status_code == 200, got.text
    seen = got.json()
    kinds = {c["key"]: c["count"] for c in seen["counts"]}
    for kind in ("workflow_runs", "builds", "jobs", "decisions", "work_items", "previews",
                 "alerts", "incidents", "kg_facts", "apps", "events"):
        assert kinds.get(kind), f"{kind} is not counted: {kinds}"
    assert seen["name"] == a["name"] and seen["home"] is False and seen["refused"] is None
    assert [x["app_name"] for x in seen["staging_apps"]] == ["helpdesk-ai-demo"]
    assert seen["staging_apps"][0]["host"] == "demo-host"
    assert seen["production_apps"] == [] and seen["previews_running"] == 1
    assert any("GitHub" in k for k in seen["keeps"]) and any("audit" in k for k in seen["keeps"])
    assert seen["export"]["available"] is False and seen["export"]["note"]
    assert hosts["calls"] == [], "reading what goes touches nothing"


def test_delete_leaves_nothing_naming_the_project_and_touches_no_other(world, owner, client,
                                                                        engine, hosts):
    a = _project(world, owner, client, engine)
    b = _project(world, owner, client, engine)
    before_b = _rows(engine, b["id"])
    assert sum(_rows(engine, a["id"]).values()) > 500

    done = _delete(world, owner, client, a["id"], confirm_name=a["name"])
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["deleted"] is True and body["rows"] > 500
    assert {c["key"] for c in body["counts"]} >= {"workflow_runs", "builds", "decisions"}

    # NOTHING NAMES IT: any text column of any table, but the audit log.
    left = {k: n for k, n in _naming(engine, a["id"]).items() if k.split(".")[0] not in KEPT}
    assert left == {}, f"rows still naming {a['id']}: {left}"
    with engine.connect() as db:
        assert db.execute(text("SELECT 1 FROM projects WHERE id = :p"), {"p": a["id"]}).first() is None
        try:
            from boss_engine.services.knowledge.age import graph_name

            assert db.execute(text("SELECT 1 FROM ag_catalog.ag_graph WHERE name = :n"),
                              {"n": graph_name(a["id"])}).first() is None
        except Exception as exc:  # noqa: BLE001 - only "no AGE here" is acceptable
            assert "ag_catalog" in str(exc), exc

    # ANOTHER PROJECT IS UNTOUCHED, row for row.
    assert _rows(engine, b["id"]) == before_b

    # ITS APP LEFT THE SERVER, ITS PREVIEW STOPPED, AND ONE AUDIT ROW SAYS SO.
    assert [x["app_name"] for x in hosts["calls"]] == ["helpdesk-ai-demo"]
    assert len(hosts["previews"]) == 1
    audited = _audit(engine, world.org, a["id"])
    assert len(audited) == 1 and audited[0]["actor_principal_id"] == owner.id
    details = audited[0]["details"] if isinstance(audited[0]["details"], dict) \
        else json.loads(audited[0]["details"])
    assert details["name"] == a["name"] and details["counts"]["workflow_runs"] >= 1
    assert details["rows"] == body["rows"]

    # Gone from the list, and asked again it is not found.
    with world.acting_as(world.ctx(owner)):
        listed = client.get("/v1/projects", params={"include_archived": "true"}).json()["items"]
        assert a["id"] not in {p["id"] for p in listed}
        assert client.get(f"/v1/projects/{a['id']}/delete").status_code in (403, 404)


def test_a_shared_github_connection_moves_to_the_project_that_uses_it(world, owner, client,
                                                                       engine, hosts):
    a = _project(world, owner, client, engine)
    b = _project(world, owner, client, engine)
    with engine.begin() as db:
        conn = db.execute(text("SELECT connection_id FROM github_repository_bindings WHERE id = :b"),
                          {"b": a["binding_id"]}).scalar()
        db.execute(text("UPDATE github_repository_bindings SET connection_id = :c WHERE id = :b"),
                   {"c": conn, "b": b["binding_id"]})
    done = _delete(world, owner, client, a["id"], confirm_name=a["name"])
    assert done.status_code == 200, done.text
    with engine.connect() as db:
        row = db.execute(text("SELECT b.project_id, c.project_id AS owner FROM "
                              "github_repository_bindings b JOIN github_connections c ON "
                              "c.id = b.connection_id WHERE b.id = :b"),
                         {"b": b["binding_id"]}).mappings().first()
    assert row is not None, "the other project's repository binding survived"
    assert row["project_id"] == b["id"] and row["owner"] == b["id"]


@pytest.mark.parametrize("case", ["wrong_name", "no_confirmation", "lapsed_second_factor", "home"])
def test_a_refusal_writes_nothing(world, owner, client, engine, hosts, case):
    a = _project(world, owner, client, engine)
    target = get_settings().default_project_id if case == "home" else a["id"]
    before = _rows(engine, target)
    with engine.connect() as db:
        home_name = db.execute(text("SELECT name FROM projects WHERE id = :p"),
                               {"p": target}).scalar()
    if case == "wrong_name":
        got = _delete(world, owner, client, target, confirm_name=a["name"] + "x")
        assert got.status_code == 422, got.text
        assert got.json()["detail"]["error"] == "name_mismatch"
        assert "Nothing was deleted" in got.json()["detail"]["message"]
    elif case == "no_confirmation":
        with world.acting_as(world.ctx(owner)):
            got = client.post(f"/v1/projects/{target}/delete", json={"confirm_name": a["name"]})
        assert got.status_code == 428, got.text
    elif case == "lapsed_second_factor":
        got = _delete(world, owner, client, target, mfa=False, confirm_name=a["name"])
        assert got.status_code == 401, got.text
        assert "step_up" in got.text
    else:
        # The install's home project is in BOSS's own org, not this world's: the service is asked
        # directly, with its name typed right.
        with pytest.raises(pd.DeleteRefused) as refused:
            pd.delete_project(engine, get_settings(), target, actor="ceo", confirm_name=home_name)
        assert refused.value.code == "delete_refused" and refused.value.status == 409
        assert "home project" in str(refused.value)
        with engine.connect() as db:
            assert pd.preview(db, get_settings(), target)["refused"] == str(refused.value)
    assert _rows(engine, target) == before
    assert _audit(engine, world.org, target) == []
    assert hosts["calls"] == [] and hosts["previews"] == []


def test_a_failure_half_way_rolls_everything_back(world, owner, client, engine, hosts,
                                                  monkeypatch):
    a = _project(world, owner, client, engine)
    before = _rows(engine, a["id"])

    def boom(*_a, **_k):
        raise RuntimeError("the database went away half-way")

    # After every row is deleted, before the project row and the audit entry.
    monkeypatch.setattr(pd, "_identity_scopes", boom)
    got = _delete(world, owner, client, a["id"], confirm_name=a["name"])
    assert got.status_code == 500, got.text
    after = _rows(engine, a["id"])
    # The work was stopped first (its cancel paths write a stop request, a scorecard, events);
    # not one row is gone.
    assert {t: n for t, n in before.items() if after.get(t, 0) < n} == {}
    with engine.connect() as db:
        assert db.execute(text("SELECT 1 FROM projects WHERE id = :p"), {"p": a["id"]}).first()
    assert _audit(engine, world.org, a["id"]) == []


def test_production_is_never_taken_down_without_the_tick(world, owner, client, engine, hosts):
    a = _project(world, owner, client, engine)
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
            "target_kind, target_ref, domains, status, created_by, host_id) VALUES (:i, :p, "
            "'helpdesk-prod', 'acme/helpdesk', 'production', 'host', 'deploy@203.0.113.20', "
            "CAST('[]' AS JSONB), 'active', 'seed', :h)"),
            {"i": f"dept_{uuid.uuid4().hex[:12]}", "p": a["id"], "h": a["host_id"]})
    before = _rows(engine, a["id"])
    with world.acting_as(world.ctx(owner)):
        seen = client.get(f"/v1/projects/{a['id']}/delete").json()
    assert [x["app_name"] for x in seen["production_apps"]] == ["helpdesk-prod"]

    refused = _delete(world, owner, client, a["id"], confirm_name=a["name"])
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["error"] == "production_running"
    assert detail["message"].startswith("Retire its production first")
    assert _rows(engine, a["id"]) == before and hosts["calls"] == []

    done = _delete(world, owner, client, a["id"], confirm_name=a["name"], also_production=True)
    assert done.status_code == 200, done.text
    assert sorted(x["app_name"] for x in hosts["calls"]) == ["helpdesk-ai-demo", "helpdesk-prod"]


def test_a_server_that_keeps_the_app_deletes_nothing(world, owner, client, engine, hosts):
    a = _project(world, owner, client, engine)
    before = _rows(engine, a["id"])
    hosts["answer"].update(status="failed", reason="the server did not answer")
    got = _delete(world, owner, client, a["id"], confirm_name=a["name"])
    assert got.status_code == 502, got.text
    detail = got.json()["detail"]
    assert detail["error"] == "host_cleanup_failed"
    assert "helpdesk-ai-demo" in detail["message"] and "Nothing was deleted" in detail["message"]
    assert _rows(engine, a["id"]) == before and hosts["previews"] == []


def test_only_a_person_deletes_a_project():
    """The agent can OFFER delete_project and never press it (Where people decide › Deleting
    data); a decision about it is on the list."""
    from boss_engine.services import decide_list
    from boss_engine.tools import build_default_registry

    spec = build_default_registry().get("delete_project")
    assert spec.kind == "write" and spec.operator_only and spec.reversibility == "irreversible"
    assert spec.permission == "project:delete" and spec.org_scope
    assert not spec.self_executable
    assert decide_list.classify({"recommendation": {"action": "delete_project"},
                                 "question": "Delete northpeak-bank-b?"}) == "delete"


# ── ITS FILES ON THIS MACHINE (BOSSAPP-915 follow-up) ─────────────────────────────────────────

@pytest.fixture
def disk(tmp_path, monkeypatch):
    """Every BOSS-owned root under tmp_path, so the test sees exactly what goes."""
    from boss_engine.services import agent_shell, local_repo, preview_service, project_files

    roots = {name: tmp_path / name for name in (
        "repos", "artifacts", "previews", "sandboxes", "sandbox-runs", "agent-ws", "kg", "toolchains")}
    for path in roots.values():
        path.mkdir()
    monkeypatch.setattr(local_repo, "root", lambda settings=None: roots["repos"])
    monkeypatch.setattr(preview_service, "PREVIEW_ROOT", roots["previews"])
    monkeypatch.setattr(agent_shell, "WORKSPACE_ROOT", roots["agent-ws"])
    monkeypatch.setattr(project_files, "_artifact_root", lambda settings: roots["artifacts"])
    monkeypatch.setattr(project_files, "_sandbox_root", lambda settings: roots["sandboxes"])
    monkeypatch.setattr(project_files, "_kg_root", lambda settings: roots["kg"])
    monkeypatch.setenv("BOSS_SANDBOX_RUN_ROOT", str(roots["sandbox-runs"]))
    monkeypatch.setenv("BOSS_TOOLCHAIN_CACHE_ROOT", str(roots["toolchains"]))
    roots["outside"] = tmp_path / "not-boss"
    roots["outside"].mkdir()
    return roots


def _files_of(engine, disk, project: dict) -> dict:
    """A local repository, a run's retained workspaces, a preview's tree and logs, artifacts,
    and an outage mirror of a repository only this project uses."""
    from pathlib import Path

    from boss_engine.services import agent_shell, local_projects, local_repo, sandbox_identity

    pid = project["id"]
    run_id = f"crun_{uuid.uuid4().hex[:10]}"
    only = f"only-{pid}"
    with engine.begin() as db:
        local_projects.create_for_project(db, get_settings(), pid, name="notes", actor="ceo")
        db.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, phase) VALUES "
                        "(:i, :p, 'files', 'completed', 'done')"), {"i": run_id, "p": pid})
        preview_id = db.execute(text("SELECT id FROM app_previews WHERE project_id = :p LIMIT 1"),
                                {"p": pid}).scalar()
        db.execute(text(
            "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, "
            "full_name, role, host, created_by) VALUES (:i, :p, (SELECT connection_id FROM github_repository_bindings WHERE project_id = :p AND connection_id IS NOT NULL LIMIT 1), 'acme', :n, :f, 'source', "
            "'github', 'ceo')"),
            {"i": f"ghrepo_{uuid.uuid4().hex[:10]}", "p": pid, "n": only, "f": f"acme/{only}"})
    mirror = local_repo.mirror_path("acme", only)
    local_repo.init_bare(mirror)
    paths = {
        "bare": Path(local_repo.bare_path(local_repo.owner_for(pid), "notes")),
        "mirror": mirror,
        "sandbox": disk["sandboxes"] / f"{run_id}-x1y2",
        "agent_ws": agent_shell.workspace_for(run_id),
        "uid_run": disk["sandbox-runs"] / sandbox_identity.owner_key(run_id),
        "preview_tree": disk["previews"] / f"{preview_id}-abc",
        "preview_log": disk["previews"] / f"{preview_id}.log",
        "artifacts": disk["artifacts"] / pid,
    }
    for name, path in paths.items():
        if name in ("bare", "mirror"):
            continue
        if name == "preview_log":
            path.write_text("log\n")
        else:
            path.mkdir(parents=True, exist_ok=True)
            (path / "kept.txt").write_text(name)
    assert (paths["bare"] / "HEAD").is_file()
    return paths


def test_delete_removes_its_repository_workspaces_and_logs_and_no_one_elses(
        world, owner, client, engine, hosts, disk):
    a = _project(world, owner, client, engine)
    b = _project(world, owner, client, engine)
    mine, theirs = _files_of(engine, disk, a), _files_of(engine, disk, b)
    # A row pointing outside BOSS's directories is refused, never followed.
    stray = disk["outside"] / "precious.log"
    stray.write_text("not BOSS's\n")
    with engine.begin() as db:
        db.execute(text("UPDATE app_previews SET log_path = :l WHERE project_id = :p"),
                   {"l": str(stray), "p": a["id"]})

    done = _delete(world, owner, client, a["id"], confirm_name=a["name"])
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["files"]["local_repo"] is True and body["files"]["failed"] == 0
    assert body["files_refused"] == [str(stray)]

    for name, path in mine.items():
        assert not path.exists(), f"{name} of the deleted project is still on disk: {path}"
    for name, path in theirs.items():
        assert path.exists(), f"{name} of ANOTHER project was removed: {path}"
    assert stray.read_text() == "not BOSS's\n"
    # The roots themselves stay.
    assert all(disk[k].is_dir() for k in ("repos", "artifacts", "previews", "sandboxes"))


def test_a_mirror_another_project_uses_is_kept(world, owner, client, engine, hosts, disk):
    from boss_engine.services import local_repo

    a = _project(world, owner, client, engine)
    b = _project(world, owner, client, engine)
    with engine.begin() as db:
        for pid in (a["id"], b["id"]):
            db.execute(text(
                "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, "
                "name, full_name, role, host, created_by) VALUES (:i, :p, (SELECT connection_id FROM github_repository_bindings WHERE project_id = :p AND connection_id IS NOT NULL LIMIT 1), 'acme', 'shared', "
                "'acme/shared', 'source', 'github', 'ceo')"),
                {"i": f"ghrepo_{uuid.uuid4().hex[:10]}", "p": pid})
    mirror = local_repo.mirror_path("acme", "shared")
    local_repo.init_bare(mirror)
    done = _delete(world, owner, client, a["id"], confirm_name=a["name"])
    assert done.status_code == 200, done.text
    assert (mirror / "HEAD").is_file()
