"""The self-fix routes (BOSSAPP-571, docs/product/BOSS_FIXES_ITSELF.md §11.4).

  * People read, start (from an open recommendation), stop, and switch self-fix; approval stays
    the decisions API.
  * The host self-installer's routes answer ONLY `sys_self_installer` — not an Owner, not the
    worker, not an anonymous caller — under every authz mode.
  * A ticket the API issues is signed with the install's REAL signing key, published in its
    JWKS, and the host installer's own dependency-free verifier accepts it.
"""
from __future__ import annotations

import base64
import importlib.util
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.identity.principal import (AuthContext, AuthMethod, Principal, PrincipalKind,
                                            PrincipalStatus)

HOME = "boss-dogfood"
ROOT = Path(__file__).resolve().parents[3]


def _world(engine, project=HOME):
    from apps.api.tests.identity_fixtures import IdentityWorld

    with engine.connect() as c:
        row = c.execute(text("SELECT p.workspace_id, w.org_id FROM projects p JOIN workspaces w "
                             "ON w.id = p.workspace_id WHERE p.id = :p"),
                        {"p": project}).mappings().one()
    return IdentityWorld(engine, row["org_id"], row["workspace_id"], project, project)


def _system(world, pid: str) -> AuthContext:
    return AuthContext(Principal(pid, PrincipalKind.SYSTEM, world.org, PrincipalStatus.ACTIVE,
                                 pid), AuthMethod.SYSTEM)


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                           "-c", "commit.gpgsign=false", *args], cwd=str(cwd),
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.fixture()
def self_fixable(engine, tmp_path, monkeypatch):
    """A git install of BOSS (a tiny one): its source sha and its self-source mirror."""
    repo = tmp_path / "src"
    (repo / "engine/boss_engine/services").mkdir(parents=True)
    (repo / "engine/boss_engine/services/widget.py").write_text("def double(x):\n    return x+2\n")
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    mirror = tmp_path / "mirror.git"
    _git(tmp_path, "clone", "-q", "--mirror", str(repo), str(mirror))
    monkeypatch.setenv("BOSS_SOURCE_SHA", _git(repo, "rev-parse", "HEAD"))
    monkeypatch.setattr(api_main.settings, "self_fix_source", str(mirror), raising=False)
    yield
    with engine.begin() as c:
        ids = [r[0] for r in c.execute(text("SELECT id FROM self_fixes WHERE project_id = :p"),
                                       {"p": HOME})]
        for table in ("self_fix_tickets", "self_fix_waiters", "self_fix_claims"):
            c.execute(text(f"DELETE FROM {table} WHERE self_fix_id = ANY(:i)"), {"i": ids})
        c.execute(text("DELETE FROM self_fixes WHERE project_id = :p"), {"p": HOME})
        c.execute(text("DELETE FROM jobs WHERE kind = 'self_fix.build'"))
        c.execute(text("DELETE FROM system_state WHERE key = 'self_fix'"))


def test_the_installer_routes_answer_only_the_self_installer(client, engine):
    world = _world(engine)
    for path in ("/v1/self-fixes/tickets", "/v1/self-fixes/health"):
        assert client.get(path).status_code == 403, "anonymous, shadow mode: refused"
    owner = world.person(f"owner-{uuid4().hex[:6]}")
    world.bind(owner, "owner")
    with world.acting_as(world.ctx(owner)):
        assert client.get("/v1/self-fixes/tickets").status_code == 403, "an Owner: refused"
    with world.acting_as(_system(world, "sys_worker")):
        assert client.get("/v1/self-fixes/tickets").status_code == 403, "the worker: refused"
    with world.acting_as(_system(world, "sys_self_installer")):
        listed = client.get("/v1/self-fixes/tickets")
        assert listed.status_code == 200 and listed.json() == {"tickets": []}
        health = client.get("/v1/self-fixes/health").json()
        assert {"stack_head", "boss_build", "worker_heartbeat_age_seconds"} <= set(health)
        assert client.post("/v1/self-fixes/tickets/sft_nope/take", json={}).status_code == 409


def test_people_switch_start_follow_and_stop_a_self_fix(client, engine, self_fixable):
    assert client.put("/v1/self-fix/settings", json={
        "project_id": HOME, "actor": "ceo", "on": True,
        "settings": {"self_fix_budget_cents": 300}}).status_code == 200
    state = client.get("/v1/self-fix/settings", params={"project_id": HOME}).json()
    assert state["switch"]["on"] is True and state["settings"]["self_fix_budget_cents"] == 300
    assert state["can_install"] is True and state["auto_install"] == "off"
    from boss_engine.services import boss_recommendations

    with engine.begin() as c:
        rec = boss_recommendations.record(
            c, api_main.settings, project_id=HOME, signature=f"build:boss:x{uuid4().hex[:6]}",
            title="the widget label is wrong", symptom="5 for 3",
            files=["engine/boss_engine/services/widget.py"], actor="test")
    started = client.post("/v1/self-fixes", json={"project_id": HOME, "actor": "ceo",
                                                  "recommendation_id": rec["id"]})
    assert started.status_code == 201, started.text
    sfx_id = started.json()["self_fix_id"]
    assert started.json()["status"] == "claimed"
    listed = client.get("/v1/self-fixes", params={"project_id": HOME}).json()
    assert sfx_id in [s["id"] for s in listed["self_fixes"]]
    assert all(s["line"] for s in listed["self_fixes"])
    one = client.get(f"/v1/self-fixes/{sfx_id}", params={"project_id": HOME}).json()
    assert one["self_fix"]["chip"] == "Building"
    assert [e["event_type"] for e in one["events"]][:2] == ["self_fix.proposed", "self_fix.claimed"]
    assert client.get(f"/v1/self-fixes/{sfx_id}/diff", params={"project_id": HOME}
                      ).json()["diff"] == ""
    assert client.get(f"/v1/self-fixes/{sfx_id}", params={"project_id": "elsewhere"}
                      ).status_code in (403, 404)
    stopped = client.post(f"/v1/self-fixes/{sfx_id}/cancel",
                          json={"project_id": HOME, "actor": "ceo", "reason": "not now"})
    assert stopped.json()["status"] == "cancelled"
    with engine.begin() as c:
        assert c.execute(text("SELECT status FROM boss_recommendations WHERE id = :i"),
                         {"i": rec["id"]}).scalar() == "open"


def test_a_ticket_is_signed_with_the_installs_key_and_the_host_verifies_it(client, engine,
                                                                          monkeypatch):
    from boss_engine.identity import signing_keys, vault
    from boss_engine.services import self_fix_install as sfi

    world = _world(engine)
    monkeypatch.delenv("BOSS_MASTER_KEY_FILE", raising=False)
    monkeypatch.setenv("BOSS_MASTER_KEY", base64.b64encode(b"k" * 32).decode())
    vault.reset_master_key_cache()
    signing_keys.clear_cache()
    try:
        with engine.begin() as c:
            vault.check_master_key(db=c)
            if c.execute(text("SELECT count(*) FROM identity_signing_keys WHERE family = 'actor' "
                              "AND status = 'active'")).scalar() == 0:
                signing_keys.provision(org_id=world.org, db=c)
        signing_keys.clear_cache()
        claims = {"typ": "self_fix_ticket", "kind": "install", "ticket_id": "sft_test",
                  "sfx_id": "sfx_test", "decision_id": "dec_test", "base_sha": "a" * 40,
                  "fix_commit_sha": "b" * 40, "fix_tree_sha": "c" * 40,
                  "stack_ref": "refs/boss/stack/v1", "release": "v1", "iat": 1,
                  "exp": 4102444800}
        with engine.begin() as c:
            token = sfi.IdentitySigner().sign(c, claims)
        jwks = client.get("/.well-known/boss-jwks.json").json()
    finally:
        signing_keys.clear_cache()
        vault.reset_master_key_cache()
    spec = importlib.util.spec_from_file_location(
        "sfi_host", ROOT / "scripts/install/self_fix_installer.py")
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    assert host.verify_ticket(token, jwks)["fix_commit_sha"] == "b" * 40
    tampered = token.rsplit(".", 1)[0][:-2] + "AA." + token.rsplit(".", 1)[1]
    with pytest.raises(host.Refused):
        host.verify_ticket(tampered, jwks)


def _verified_row(engine) -> str:
    import json as _json

    sfx_id = f"sfx_{uuid4().hex[:20]}"
    diff = ("diff --git a/engine/x.py b/engine/x.py\n--- a/engine/x.py\n+++ b/engine/x.py\n"
            "@@ -1 +1 @@\n-a = 1\n+a = 2  # 203.0.113.10\n")
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO self_fixes (id, project_id, signature, class_signature, area, status, "
            "confidence, base_sha, fix_commit_sha, fix_tree_sha, branch, location, engine, "
            "created_by, pr, red_green, diagnosis, evidence) VALUES (:i, :p, "
            "'deploy:port_held:8000', 'deploy:port_held', 'deploy', 'verified', 'high', :b, :f, "
            ":t, :br, 'engine/x.py::plan', 'scripted', 'boss_maintainer', CAST(:pr AS JSONB), "
            "CAST(:rg AS JSONB), CAST(:d AS JSONB), CAST(:e AS JSONB))"),
            {"i": sfx_id, "p": HOME, "b": "a" * 40, "f": "b" * 40, "t": "c" * 40,
             "br": f"boss/self-fix/{sfx_id}-x",
             "pr": _json.dumps({"title": "fix(deploy): x", "diff": diff, "labels": []}),
             "rg": _json.dumps({"tests": ["engine/tests/test_x.py"]}),
             "d": _json.dumps({"hypothesis": "x"}), "e": _json.dumps({"symptom": "port held"})})
    return sfx_id


def test_send_to_boss_team_is_previewed_ticked_and_exportable(client, engine, monkeypatch):
    """The upstream routes (R9): preview stores the exact send, Send refuses without the tick,
    a patch export is a download, and with telemetry off the preview offers only the export."""
    from boss_engine.services import self_fix_upstream as up

    monkeypatch.setenv("BOSS_TELEMETRY", "off")
    monkeypatch.setattr(api_main.settings, "telemetry", "off", raising=False)
    monkeypatch.setattr(up, "_names", lambda c, p: [])
    sfx_id = _verified_row(engine)
    try:
        assert client.get(f"/v1/self-fixes/{sfx_id}/upstream", params={"project_id": HOME}
                          ).json() == {"send": None, "telemetry_off": "BOSS_TELEMETRY=off"}
        got = client.post(f"/v1/self-fixes/{sfx_id}/upstream/preview",
                          json={"project_id": HOME, "actor": "ceo"})
        assert got.status_code == 200, got.text
        send = got.json()
        assert send["channel"] == "patch_export" and send["check"]["ok"] is True
        assert send["sha256"] and send["checked"] is False
        refused = client.post(f"/v1/self-fixes/{sfx_id}/upstream/send",
                              json={"project_id": HOME, "actor": "ceo",
                                    "send_id": send["send_id"], "checked": False})
        assert refused.status_code == 409 and "I checked this" in refused.text
        edited = client.put(f"/v1/self-fixes/{sfx_id}/upstream/preview",
                            json={"project_id": HOME, "actor": "ceo", "send_id": send["send_id"],
                                  "body": "my words"}).json()
        assert edited["send_id"] != send["send_id"] and edited["checked"] is False
        patch = client.get(f"/v1/self-fixes/{sfx_id}/patch", params={"project_id": HOME})
        assert patch.status_code == 200 and patch.content[:2] == b"\x1f\x8b"
        assert "attachment" in patch.headers["content-disposition"]
    finally:
        with engine.begin() as c:
            c.execute(text("DELETE FROM self_fixes WHERE id = :i"), {"i": sfx_id})


def test_removal_and_autonomy_routes(client, engine):
    sfx_id = _verified_row(engine)
    try:
        nothing = client.post(f"/v1/self-fixes/{sfx_id}/remove",
                              json={"project_id": HOME, "actor": "ceo"})
        assert nothing.status_code == 409 and "not on the local stack" in nothing.text
        assert client.post("/v1/self-fixes/remove-all", json={"project_id": HOME, "actor": "ceo"}
                           ).status_code == 409
        report = client.get("/v1/autonomy", params={"project_id": HOME, "window": "7d"}).json()
        assert {"totals", "boss_side", "trend", "runs", "window"} <= set(report)
        one = client.get("/v1/autonomy/runs/wfr_none", params={"project_id": HOME}).json()
        assert one["sentence"] == "No problems" and one["counts"]["problems"] == 0
        settings = client.get("/v1/self-fix/settings", params={"project_id": HOME}).json()
        assert settings["auto_install"] == "off" and settings["auto_install_why"]
        assert {"last_seen_at", "watching"} <= set(settings["installer"])
        one_fix = client.get(f"/v1/self-fixes/{sfx_id}", params={"project_id": HOME}).json()
        assert [s["key"] for s in one_fix["lineage"]][0] == "problem"
        assert "bundle" not in one_fix["self_fix"]
    finally:
        with engine.begin() as c:
            c.execute(text("DELETE FROM self_fixes WHERE id = :i"), {"i": sfx_id})
