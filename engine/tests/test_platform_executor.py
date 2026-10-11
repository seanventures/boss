"""Execution through receipts: idempotent replay, the rules the executor enforces whatever the
plugin does, the ledger, and an approved plan applied in order. Real Postgres
(BOSS_TEST_DATABASE_URL); the secret broker is the in-memory one, so no vault key is needed."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from ._multirepo_support import make_engine
from boss_engine.platform import executor, planner
from boss_engine.platform import registry as reg
from boss_engine.platform.bindings import MemoryBroker
from boss_engine.platform.contract import Hook, InfraPlugin, OperationStatus, ResourceRequest, validate_resources

from .test_platform_planner import _plugin


class Counting(InfraPlugin):
    def __init__(self, *, connect=True, leak=False):
        self.calls: list[str] = []
        self.connect, self.leak = connect, leak

    def validate(self, ctx):
        return ctx.result()

    def plan(self, ctx):
        from boss_engine.platform.contract import PlannedChange, ResourcePlan
        return ctx.result(plan=ResourcePlan(resourceId="x", pluginId=ctx.plugin_id, pluginVersion=ctx.plugin_version,
                                            mode="owned", changes=[PlannedChange(action="create", target="c")]))

    def provision(self, ctx):
        self.calls.append("provision")
        ref = ctx.secrets.put("DATABASE_URL", "postgresql://u:pw-very-secret@h/db")
        ctx.log("started with postgresql://u:pw-very-secret@h/db")
        state = {"leaked": "pw-very-secret"} if self.leak else {"container": "c1"}
        return ctx.result(bindings={"DATABASE_URL": ref}, state=state)

    def verify(self, ctx):
        self.calls.append("verify")
        return ctx.result(evidence=["connection:passed" if self.connect else "healthcheck:passed"])

    def destroy(self, ctx):
        self.calls.append("destroy")
        return ctx.result(evidence=[{"check": "removed", "passed": True, "kind": "cleanup"}])


@pytest.fixture
def world(tmp_path, monkeypatch):
    engine = make_engine()
    monkeypatch.delenv(reg.EXTRA_PATHS_ENV, raising=False)
    _plugin(tmp_path, "official", "postgres", "official.postgres")
    registry = reg.Registry(reg.discover(tmp_path))
    tag = uuid.uuid4().hex[:8]
    org, ws, pid = f"org_ip_{tag}", f"ws_ip_{tag}", f"p_ip_{tag}"
    with engine.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'Acme')"), {"o": org})
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'Acme')"), {"w": ws, "o": org})
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, 'Shop', :p)"),
                  {"p": pid, "w": ws})
    return {"engine": engine, "registry": registry, "project": pid, "reg": registry.get("official.postgres")}


def _run(world, hook, *, plugin, digest="sha256:approved", request=None, broker=None, **kw):
    request = request or ResourceRequest(name="db", capability="database.relational")
    return executor.run_hook(world["engine"], project_id=world["project"], app_key="shop", reg=world["reg"],
                             request=request, hook=hook, plan=None, digest=digest, actor_id="t",
                             plugin=plugin, broker=broker or MemoryBroker(app_key="shop"), **kw)


def _receipts(world):
    with world["engine"].begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT effect_kind, status, result FROM receipts WHERE project_id = :p ORDER BY created_at"),
            {"p": world["project"]}).mappings().all()]


def test_provision_twice_replays_the_receipt_and_never_reruns_the_plugin(world):
    p = Counting()
    first = _run(world, Hook.PROVISION, plugin=p)
    second = _run(world, Hook.PROVISION, plugin=p)
    assert first.ok and second.ok and p.calls == ["provision"]
    assert first.bindings == second.bindings
    [r] = _receipts(world)
    assert r["effect_kind"] == "infra.provision" and r["status"] == "succeeded"
    res = r["result"]
    assert res["pluginVersion"] == "1.0.0" and res["planDigest"] == "sha256:approved"
    assert res["outcome"] == "provisioned" and res["pluginChecksum"].startswith("sha256:")
    assert "pw-very-secret" not in str(res) and "[redacted]" in " ".join(res["log"])
    with world["engine"].begin() as c:
        row = executor.ledger_row(c, world["project"], "deploy/shop/db")
    assert row["status"] == "provisioned" and row["mode"] == "owned"
    assert row["bindings"]["DATABASE_URL"]["secretRef"] == "app_secret://deploy/shop/DATABASE_URL"


def test_a_mutating_hook_without_an_approved_digest_is_refused(world):
    p = Counting()
    out = _run(world, Hook.PROVISION, plugin=p, digest=None)
    assert out.status is OperationStatus.REFUSED and p.calls == []


def test_an_attached_resource_is_never_destroyed(world):
    p = Counting()
    req = validate_resources({"db": {"capability": "database.relational", "mode": "attached",
                                     "attach": {"bindings": {"DATABASE_URL": "OLD_URL"}}}})[0]
    out = _run(world, Hook.DESTROY, plugin=p, request=req)
    assert out.status is OperationStatus.REFUSED and "never destroyed" in out.errors[0].message
    assert p.calls == []


def test_verify_without_a_real_connection_fails(world):
    out = _run(world, Hook.VERIFY, plugin=Counting(connect=False))
    assert out.status is OperationStatus.FAILED and out.errors[0].code == "verify_not_connected"


def test_a_result_carrying_a_secret_is_discarded(world):
    out = _run(world, Hook.PROVISION, plugin=Counting(leak=True))
    assert out.status is OperationStatus.FAILED and out.errors[0].code == "secret_leak"
    assert "pw-very-secret" not in str(_receipts(world))


def test_an_approved_plan_applies_in_order_and_refuses_a_changed_one(world):
    reqs = validate_resources({"db": {"capability": "database.relational"}})
    p = Counting()
    doc = planner.build_plan(planner.resolve(reqs, world["registry"]), app_key="shop", environment="staging",
                             plugin_for=lambda _r: p)
    with pytest.raises(executor.ApplyRefused, match="changed after it was approved"):
        executor.apply_plan(world["engine"], project_id=world["project"], app_key="shop", doc=doc, requests=reqs,
                            registry=world["registry"], actor_id="t", approved_digest="sha256:other",
                            plugin_for=lambda _r: p)
    tampered = {**doc, "order": ["db", "db"]}
    with pytest.raises(executor.ApplyRefused, match="digest"):
        executor.check_plan(tampered, None, world["registry"])
    with pytest.raises(executor.ApplyRefused, match="cancelled"):
        executor.apply_plan(world["engine"], project_id=world["project"], app_key="shop", doc=doc, requests=reqs,
                            registry=world["registry"], actor_id="t", is_cancelled=lambda: True,
                            plugin_for=lambda _r: p)
    # run_hook builds the vault broker by default; give the apply an in-memory one.
    orig = executor.VaultBroker
    executor.VaultBroker = lambda *a, **k: MemoryBroker(app_key="shop")  # type: ignore[assignment]
    try:
        results = executor.apply_plan(world["engine"], project_id=world["project"], app_key="shop", doc=doc,
                                      requests=reqs, registry=world["registry"], actor_id="t",
                                      approved_digest=doc["digest"], plugin_for=lambda _r: p)
    finally:
        executor.VaultBroker = orig  # type: ignore[assignment]
    assert [r.hook.value for r in results] == ["provision", "verify"] and p.calls == ["provision", "verify"]
    assert {r["effect_kind"] for r in _receipts(world)} == {"infra.provision", "infra.verify"}
