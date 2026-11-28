"""Regression tests for BOSSAPP-1481, BOSSAPP-1489, BOSSAPP-1490, and BOSSAPP-1502.

Covers:
- BOSSAPP-1481: atomic one-use claim row on generic deploy approval, wrong-run/wrong-capability
  gate refusal, rejected onboarding workflow run status, and hidden approval_decision_id in
  deploy_config/promote_config model schemas.
- BOSSAPP-1489: injective ref_key_for, atomic owner claim & race in DatabaseBackend and
  LocalVaultBackend, cross-project & system-material read refusal, provable backfill migration,
  flagged legacy refs, and pre-upgrade subscription/gateway key continuity.
- BOSSAPP-1490: host-bound GHES connection slots (repoint to new host refused, host mismatch
  refused at send) and aliasing key refusal (.. and embedded ghconn-).
- BOSSAPP-1502: scoped hard caps count active reservations and acquire org/team scope-level
  advisory locks across concurrent cross-project reservations.
"""
from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not URL or "test" not in URL.lower(),
    reason="needs BOSS_TEST_DATABASE_URL naming a disposable database",
)

DOLLAR = 1_000_000


@pytest.fixture(scope="module")
def eng():
    from boss_engine.config import Settings
    from boss_engine.db import ensure_schema

    engine = create_engine(URL, future=True)
    ensure_schema(engine, Settings())
    yield engine
    engine.dispose()


def _mk_org_world(eng, prefix: str = "sb"):
    sfx = uuid4().hex[:8]
    org = f"org_{prefix}_{sfx}"
    ws = f"ws_{prefix}_{sfx}"
    p1 = f"proj_{prefix}_a_{sfx}"
    p2 = f"proj_{prefix}_b_{sfx}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, :o)"), {"o": org})
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, :w)"),
                  {"w": ws, "o": org})
        for p in (p1, p2):
            c.execute(
                text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :p, :p)"),
                {"p": p, "w": ws},
            )
    return {"org": org, "ws": ws, "p1": p1, "p2": p2, "sfx": sfx}


# ── BOSSAPP-1481 ──────────────────────────────────────────────────────────────────────────────


def _insert_approved_deploy_decision(eng, project_id: str, *, run_id: str, target_id: str,
                                     capability: str = "deploy", target_ref: str = "app-prod") -> str:
    dec_id = f"dec_{uuid4().hex[:12]}"
    wfd_id = f"wfd_{uuid4().hex[:10]}"
    wfv_id = f"wfv_{uuid4().hex[:10]}"
    wfs_id = f"wfs_{uuid4().hex[:10]}"
    rec = {
        "action": "approve_deploy",
        "capability": capability,
        "run_id": run_id,
        "target_id": target_id,
        "target_ref": target_ref,
        "environment": "production",
    }
    snap = {"released_for": dec_id, "binding": {"capability": capability}}
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO decisions (id, project_id, status, question, recommendation, "
                "cost_summary, approved_by, approved_at) "
                "VALUES (:id, :p, 'approved', 'Approve deploy?', CAST(:rec AS JSONB), "
                "'{}'::jsonb, 'ceo', now())"
            ),
            {"id": dec_id, "p": project_id, "rec": json.dumps(rec)},
        )
        c.execute(
            text(
                "INSERT INTO workflow_definitions (id, project_id, workflow_key, name, created_by) "
                "VALUES (:i, :p, :i, 'Deploy', 'ceo')"
            ),
            {"i": wfd_id, "p": project_id},
        )
        c.execute(
            text(
                "INSERT INTO workflow_versions (id, project_id, workflow_definition_id, version, "
                "spec, content_hash, created_by) VALUES (:v, :p, :d, 1, '{}'::jsonb, 'h', 'ceo')"
            ),
            {"v": wfv_id, "p": project_id, "d": wfd_id},
        )
        c.execute(
            text(
                "INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
                "workflow_version_id, status, actor) VALUES (:r, :p, :d, :v, 'running', 'ceo') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"r": run_id, "p": project_id, "d": wfd_id, "v": wfv_id},
        )
        c.execute(
            text(
                "INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, "
                "step_kind, status, decision_id, binding_snapshot) "
                "VALUES (:s, :p, :r, :sid, 'capability', 'running', :dec, CAST(:snap AS JSONB))"
            ),
            {"s": wfs_id, "p": project_id, "r": run_id, "sid": f"deploy_{wfs_id}",
             "dec": dec_id, "snap": json.dumps(snap)},
        )
    return dec_id


def test_1481_generic_deploy_gate_concurrent_claim_only_allows_one(eng):
    from boss_engine.services.deploy_policy import pre_approval

    w = _mk_org_world(eng, "1481a")
    target_id = f"tgt_{w['sfx']}"
    run_id = f"run_{w['sfx']}"
    dec_id = _insert_approved_deploy_decision(
        eng, w["p1"], run_id=run_id, target_id=target_id, capability="deploy",
    )
    barrier = threading.Barrier(2)

    def _claim(_idx: int):
        with eng.begin() as c:
            barrier.wait(timeout=10)
            return pre_approval(
                c, w["p1"], "production",
                gate_decision_id=dec_id, gate_run_id=run_id, target_id=target_id,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_claim, (1, 2)))

    allowed = [r for r in results if r is not None]
    denied = [r for r in results if r is None]
    assert len(allowed) == 1, results
    assert len(denied) == 1, results
    with eng.begin() as c:
        uses = c.execute(
            text("SELECT count(*) FROM deployment_gate_uses WHERE decision_id = :d"),
            {"d": dec_id},
        ).scalar_one()
    assert int(uses) == 1


def test_1481_generic_deploy_gate_refuses_wrong_run_or_capability(eng):
    from boss_engine.services.deploy_policy import pre_approval

    w = _mk_org_world(eng, "1481b")
    target_id = f"tgt_{w['sfx']}"
    dec_wrong_cap = _insert_approved_deploy_decision(
        eng, w["p1"], run_id="run_a", target_id=target_id, capability="config_deploy",
    )
    dec_wrong_run = _insert_approved_deploy_decision(
        eng, w["p1"], run_id="run_a", target_id=target_id, capability="deploy",
    )
    with eng.begin() as c:
        r_cap = pre_approval(
            c, w["p1"], "production",
            gate_decision_id=dec_wrong_cap, gate_run_id="run_a", target_id=target_id,
        )
        r_run = pre_approval(
            c, w["p1"], "production",
            gate_decision_id=dec_wrong_run, gate_run_id="run_other", target_id=target_id,
        )
    assert r_cap is None
    assert r_run is None


def test_1481_onboarding_rejection_run_ends_rejected(eng):
    from boss_engine.services.workflows import _maybe_finish_run

    w = _mk_org_world(eng, "1481c")
    wfd_id = f"wfd_{w['sfx']}"
    wfv_id = f"wfv_{w['sfx']}"
    wfr_id = f"wfr_{w['sfx']}"
    step_id = f"wfs_{w['sfx']}"
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO workflow_definitions (id, project_id, workflow_key, name, created_by) "
                "VALUES (:i, :p, :i, 'Onboarding', 'ceo')"
            ),
            {"i": wfd_id, "p": w["p1"]},
        )
        c.execute(
            text(
                "INSERT INTO workflow_versions (id, project_id, workflow_definition_id, version, "
                "spec, content_hash, created_by) VALUES (:v, :p, :d, 1, CAST(:spec AS JSONB), "
                "'h', 'ceo')"
            ),
            {
                "v": wfv_id,
                "p": w["p1"],
                "d": wfd_id,
                "spec": json.dumps({
                    "steps": [{"id": "rejected", "kind": "action", "run_outcome": "rejected"}],
                }),
            },
        )
        c.execute(
            text(
                "INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
                "workflow_version_id, status, actor) VALUES (:r, :p, :d, :v, 'running', 'ceo')"
            ),
            {"r": wfr_id, "p": w["p1"], "d": wfd_id, "v": wfv_id},
        )
        c.execute(
            text(
                "INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, "
                "step_kind, status) VALUES (:s, :p, :r, 'rejected', 'action', 'succeeded')"
            ),
            {"s": step_id, "p": w["p1"], "r": wfr_id},
        )
        _maybe_finish_run(c, wfr_id)
        status = c.execute(
            text("SELECT status FROM workflow_runs WHERE id = :r"), {"r": wfr_id}
        ).scalar_one()
        ev = c.execute(
            text(
                "SELECT count(*) FROM events WHERE project_id = :p "
                "AND event_type = 'workflow.run_rejected' AND entity_id = :r"
            ),
            {"p": w["p1"], "r": wfr_id},
        ).scalar_one()
    assert status == "rejected"
    assert int(ev) == 1


def test_1481_deploy_config_and_promote_config_hide_approval_decision_id():
    from boss_engine.tools.builtin import build_default_registry

    reg = build_default_registry()
    for name in ("deploy_config", "promote_config"):
        spec = reg.get(name)
        assert spec is not None
        props = (spec.parameters or {}).get("properties") or {}
        assert "approval_decision_id" not in props, f"{name} exposes approval_decision_id"
        assert "person_required" not in props, f"{name} exposes person_required"


# ── BOSSAPP-1489 ──────────────────────────────────────────────────────────────────────────────


def test_1489_ref_key_for_is_injective():
    from boss_engine.services.secrets_service import ref_key_for

    assert ref_key_for("acme", "prod_key") != ref_key_for("acme-prod", "key")
    assert ref_key_for("acme-prod", "token") != ref_key_for("acme_prod", "token")
    assert ref_key_for("llm", "gateway_x") != ref_key_for("llm_gateway", "x")
    # Still deterministic for rotation in place.
    assert ref_key_for("acme-prod", "token") == ref_key_for("acme-prod", "token")


def test_1489_db_atomic_owner_claim_squatting_and_race(eng):
    from boss_engine.services.secret_backends import DatabaseBackend, SecretUnavailable

    backend = DatabaseBackend(engine=eng)
    key = f"race_key_{uuid4().hex[:10]}"
    barrier = threading.Barrier(2)

    def _put(scope: str):
        barrier.wait(timeout=10)
        try:
            backend.put(key, f"secret-from-{scope}", owner_scope=scope)
            return ("ok", scope)
        except SecretUnavailable:
            return ("denied", scope)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(_put, ("project:alpha", "project:beta")))

    winners = [s for st, s in outcomes if st == "ok"]
    losers = [s for st, s in outcomes if st == "denied"]
    assert len(winners) == 1 and len(losers) == 1
    assert backend.owner_of(key) == winners[0]
    # Winner can rotate in place; loser still cannot squat.
    backend.put(key, "rotated-value", owner_scope=winners[0])
    assert backend.get(key) == "rotated-value"
    with pytest.raises(SecretUnavailable):
        backend.put(key, "squatted", owner_scope=losers[0])


def test_1489_local_vault_owner_isolation_and_squat_refusal(tmp_path):
    from boss_engine.services.secret_backends import LocalVaultBackend, SecretUnavailable

    vault = LocalVaultBackend(root=str(tmp_path), master_key=b"k" * 32)
    vault.put("sys_key", "install-secret")  # default owner_scope="system"
    assert vault.owner_of("sys_key") == "system"
    with pytest.raises(SecretUnavailable):
        vault.put("sys_key", "hijacked", owner_scope="project:p1")
    vault.put("p1_key", "p1-secret", owner_scope="project:p1")
    assert vault.owner_of("p1_key") == "project:p1"
    with pytest.raises(SecretUnavailable):
        vault.put("p1_key", "p2-hijack", owner_scope="project:p2")
    # Same owner can rotate.
    vault.put("p1_key", "p1-rotated", owner_scope="project:p1")
    assert vault.get("p1_key") == "p1-rotated"


def test_1489_project_ref_cannot_read_system_or_other_project_material(eng):
    from boss_engine.config import Settings
    from boss_engine.services.secret_backends import (
        DatabaseBackend,
        SecretUnavailable,
        resolve_secret_ref,
    )
    from boss_engine.services.secrets_service import put_secret

    w = _mk_org_world(eng, "1489iso")
    settings = Settings()
    db_backend = DatabaseBackend(engine=eng)

    # 1. System material (e.g. llm_gateway / subscription) cannot be read via a project ref.
    sys_key = f"BOSS_LLM_GATEWAY_{w['sfx'].upper()}"
    db_backend.put(sys_key, "sk-system-only")  # owner_scope defaults to 'system'
    rogue_sys_ref = f"sref_sys_{w['sfx']}"
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO secret_refs (id, project_id, scope_type, scope_id, ref_kind, ref_key, "
                "provider, storage_backend, status, created_by) "
                "VALUES (:i, :p, 'project', :p, 'database', :k, 'openai', 'database', 'active', 'u')"
            ),
            {"i": rogue_sys_ref, "p": w["p1"], "k": sys_key},
        )
    with eng.begin() as c, pytest.raises(SecretUnavailable, match="system"):
        resolve_secret_ref(c, rogue_sys_ref, settings, project_id=w["p1"])

    # 2. Project 1 writes material via put_secret; Project 2 creates a ref naming that key -> refused.
    with eng.begin() as c:
        stored = put_secret(
            c, settings, project_id=w["p1"], name="deploy_token", value="p1-real-token",
            backend="database", actor="ceo",
        )
        assert resolve_secret_ref(c, stored["secret_ref_id"], settings, project_id=w["p1"]) == "p1-real-token"
        rogue_p2_ref = f"sref_p2_{w['sfx']}"
        c.execute(
            text(
                "INSERT INTO secret_refs (id, project_id, scope_type, scope_id, ref_kind, ref_key, "
                "provider, storage_backend, status, created_by) "
                "VALUES (:i, :p, 'project', :p, 'database', :k, 'custom', 'database', 'active', 'u')"
            ),
            {"i": rogue_p2_ref, "p": w["p2"], "k": stored["ref_key"]},
        )
    with eng.begin() as c, pytest.raises(SecretUnavailable, match="another scope"):
        resolve_secret_ref(c, rogue_p2_ref, settings, project_id=w["p2"])


def test_1489_backfill_and_legacy_subscription_upgrade_continuity(eng, monkeypatch):
    from boss_engine.config import Settings
    from boss_engine.services.cli_harness import SUBSCRIPTION_SECRET_SCOPE, stored_credential
    from boss_engine.services.secret_backends import (
        DatabaseBackend,
        SecretUnavailable,
        _aes_gcm_encrypt,
        backfill_secret_material_owners,
        flagged_legacy_secret_refs,
        resolve_secret_ref,
    )
    from boss_engine.services.secrets_service import legacy_ref_key_for

    monkeypatch.setenv("BOSS_COMMUNITY_SECRET_STORE", "database")
    w = _mk_org_world(eng, "1489mig")
    settings = Settings()
    db_backend = DatabaseBackend(engine=eng)
    key_bytes = db_backend._write_key()  # noqa: SLF001

    # Simulate pre-1489 unowned rows in secret_material:
    # (a) legacy subscription key, (b) single-scope write-through key, (c) ambiguous shared key
    legacy_sub_key = legacy_ref_key_for(SUBSCRIPTION_SECRET_SCOPE, f"TEST_SUB_{w['sfx']}")
    wt_key = f"BOSS_WT_{w['sfx'].upper()}"
    ambig_key = f"BOSS_AMBIG_{w['sfx'].upper()}"
    wt_ref = f"sref_wt_{w['sfx']}"
    amb_ref_1 = f"sref_amb1_{w['sfx']}"
    amb_ref_2 = f"sref_amb2_{w['sfx']}"

    with eng.begin() as c:
        for k, val in ((legacy_sub_key, "sub-tok-ok"), (wt_key, "wt-val"), (ambig_key, "amb-val")):
            c.execute(
                text(
                    "INSERT INTO secret_material (ref_key, ciphertext, owner_scope) "
                    "VALUES (:k, :v, NULL) ON CONFLICT (ref_key) DO UPDATE "
                    "SET ciphertext = EXCLUDED.ciphertext, owner_scope = NULL"
                ),
                {"k": k, "v": _aes_gcm_encrypt(key_bytes, val.encode("utf-8"))},
            )
        c.execute(
            text(
                "INSERT INTO secret_refs (id, project_id, scope_type, scope_id, ref_kind, ref_key, "
                "provider, storage_backend, status, metadata, created_by) "
                "VALUES (:i, :p, 'project', :p, 'database', :k, 'custom', 'database', 'active', "
                "'{\"raw_secret_stored\": false}'::jsonb, 'ceo')"
            ),
            {"i": wt_ref, "p": w["p1"], "k": wt_key},
        )
        for rid, proj in ((amb_ref_1, w["p1"]), (amb_ref_2, w["p2"])):
            c.execute(
                text(
                    "INSERT INTO secret_refs (id, project_id, scope_type, scope_id, ref_kind, ref_key, "
                    "provider, storage_backend, status, metadata, created_by) "
                    "VALUES (:i, :p, 'project', :p, 'database', :k, 'custom', 'database', 'active', "
                    "'{\"raw_secret_stored\": false}'::jsonb, 'ceo')"
                ),
                {"i": rid, "p": proj, "k": ambig_key},
            )

        summary = backfill_secret_material_owners(c)
        assert summary["system"] >= 1
        assert summary["write_through"] >= 1
        assert db_backend.owner_of(legacy_sub_key, connection=c) == "system"
        assert db_backend.owner_of(wt_key, connection=c) == f"project:{w['p1']}"
        assert db_backend.owner_of(ambig_key, connection=c) is None  # never guessed!

    # Committed above so DatabaseBackend.get (own connection) sees the migrated rows.
    with eng.begin() as c:
        # Single-scope write-through still resolves; ambiguous cross-scope is refused & flagged.
        assert resolve_secret_ref(c, wt_ref, settings, project_id=w["p1"]) == "wt-val"
        with pytest.raises(SecretUnavailable, match="Setup"):
            resolve_secret_ref(c, amb_ref_1, settings, project_id=w["p1"])
        flagged = flagged_legacy_secret_refs(c, w["p1"], settings)
        assert any(item["secret_ref_id"] == amb_ref_1 for item in flagged)

    # Pre-upgrade install subscription key keeps working through stored_credential!
    got = stored_credential(settings, f"TEST_SUB_{w['sfx']}")
    assert got == "sub-tok-ok"


# ── BOSSAPP-1490 ──────────────────────────────────────────────────────────────────────────────


def test_1490_connection_slot_bound_to_host_and_repoint_refused(eng, monkeypatch):
    from boss_engine.config import Settings
    from boss_engine.identity import github_urls
    from boss_engine.services.github_connections import (
        GitHubConnectionService,
        credential_host_for,
    )

    monkeypatch.setenv("BOSS_COMMUNITY_SECRET_STORE", "database")
    w = _mk_org_world(eng, "1490a")
    svc = GitHubConnectionService(Settings())
    ghes_a = "https://git-a.example.internal/api/v3"
    ghes_b = "https://git-b.example.internal/api/v3"

    with eng.begin() as c:
        ref = svc.store_token_credential(
            c, project_id=w["p1"], token="ghp_test1490token", auth_method="personal_access_token",
            label="corp-a", actor="ceo",
        )
        conn = svc.create_connection(
            c, project_id=w["p1"], connection_label="corp-a",
            auth_method="personal_access_token", credential_ref=ref,
            app_id=None, installation_id=None, api_base_url=ghes_a,
            required_permissions=["pull"], metadata={}, actor="ceo",
        )
        kind, _, key = ref.partition(":")
        row = dict(c.execute(
            text(
                "SELECT id, ref_kind, ref_key, storage_backend, project_id, scope_type, metadata "
                "FROM secret_refs WHERE project_id = :p AND ref_kind = :rk AND ref_key = :k"
            ),
            {"p": w["p1"], "rk": kind, "k": key},
        ).mappings().one())

    assert row["metadata"]["connection_slot"]["api_base"] == github_urls.api_base(ghes_a)
    assert credential_host_for(row, ghes_a, w["p1"], conn["id"]) == github_urls.api_base(ghes_a)
    assert credential_host_for(row, ghes_b, w["p1"], conn["id"]) == ""

    # Repointing the same kept slot to a different host is refused!
    with eng.begin() as c, pytest.raises(ValueError):
        svc.repoint_connection(
            c, project_id=w["p1"], connection_id=conn["id"],
            credential_ref=ref, auth_method="personal_access_token",
            app_id=None, installation_id=None, api_base_url=ghes_b, actor="ceo",
        )


def test_1490_aliasing_keys_refused(eng):
    from boss_engine.services.secret_backends import _material_belongs_to
    from boss_engine.services.secret_refs import SecretRefService

    w = _mk_org_world(eng, "1490b")
    svc = SecretRefService()
    bad_keys = (
        "x/../ghconn-sref_1",
        "../secret",
        "prefix-ghconn-sref_1",
        "GHCONN-sref_1",
    )
    for bad in bad_keys:
        with eng.begin() as c, pytest.raises(ValueError):
            svc.create_secret_ref(
                c, project_id=w["p1"], ref_kind="local_vault", ref_key=bad,
                provider="github", status="active", metadata={}, actor="ceo",
            )
        assert not _material_belongs_to(
            {"ref_key": bad, "storage_backend": "local_vault", "ref_kind": "local_vault"},
            "sref_other",
        )


# ── BOSSAPP-1502 ──────────────────────────────────────────────────────────────────────────────


def test_1502_scoped_hard_cap_counts_active_reservations(eng):
    from boss_engine.config import Settings
    from boss_engine.services import scoped_budgets as sb
    from boss_engine.services.budget import BudgetGovernor

    sb.reset_for_tests()
    w = _mk_org_world(eng, "1502a")
    gov = BudgetGovernor(Settings())
    with eng.begin() as c:
        sb.set_policy(
            c, org_id=w["org"], scope_type="org", scope_id=w["org"],
            cap_usd_micros=10 * DOLLAR, period="month", hard_stop=True, actor="ceo",
        )
        r1 = gov.reserve(c, w["p1"], "model", 7 * DOLLAR, "first build", 30)
        assert r1["status"] == "reserved"
        # No llm_calls yet — only the active $7 reservation! A $5 reservation on p2 must be denied.
        r2 = gov.reserve(c, w["p2"], "model", 5 * DOLLAR, "second build", 30)
        assert r2["status"] == "denied"
        assert r2["refusal"] == "scoped_cap_exceeded"
        assert r2["scope_type"] == "org"
        # Once released, p2 fits cleanly.
        gov.release(c, w["p1"], r1["reservation_id"], "done")
        r3 = gov.reserve(c, w["p2"], "model", 5 * DOLLAR, "second build retry", 30)
        assert r3["status"] == "reserved"


def test_1502_concurrent_cross_project_reservations_under_shared_org_and_team_caps(eng):
    from boss_engine.config import Settings
    from boss_engine.services import scoped_budgets as sb
    from boss_engine.services.budget import BudgetGovernor

    sb.reset_for_tests()
    w = _mk_org_world(eng, "1502b")
    team_id = f"grp_1502_{w['sfx']}"
    role_id = f"arl_1502_{w['sfx']}"
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO identity_access_roles (id, org_id, key, name, builtin, bindable_scopes) "
                "VALUES (:r, :o, :r, 'Dev', false, ARRAY['project'])"
            ),
            {"r": role_id, "o": w["org"]},
        )
        c.execute(
            text(
                "INSERT INTO identity_groups (id, org_id, key, name, source) "
                "VALUES (:g, :o, :g, 'Core', 'manual')"
            ),
            {"g": team_id, "o": w["org"]},
        )
        for idx, proj in enumerate((w["p1"], w["p2"])):
            c.execute(
                text(
                    "INSERT INTO identity_role_bindings "
                    "(id, org_id, subject_type, subject_id, role_id, scope_type, scope_id, granted_by) "
                    "VALUES (:id, :o, 'group', :g, :r, 'project', :p, 'ceo')"
                ),
                {"id": f"rb_1502_{idx}_{w['sfx']}", "o": w["org"], "g": team_id,
                 "r": role_id, "p": proj},
            )
        sb.set_policy(
            c, org_id=w["org"], scope_type="org", scope_id=w["org"],
            cap_usd_micros=10 * DOLLAR, period="month", hard_stop=True, actor="ceo",
        )
        sb.set_policy(
            c, org_id=w["org"], scope_type="team", scope_id=team_id,
            cap_usd_micros=10 * DOLLAR, period="month", hard_stop=True, actor="ceo",
        )

    gov = BudgetGovernor(Settings())
    barrier = threading.Barrier(2)

    def _attempt(proj_id: str):
        with eng.begin() as c:
            barrier.wait(timeout=10)
            return gov.reserve(c, proj_id, "model", 7 * DOLLAR, f"concurrent-{proj_id}", 30)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_attempt, (w["p1"], w["p2"])))

    reserved_results = [r for r in results if r["status"] == "reserved"]
    denied_results = [r for r in results if r["status"] == "denied"]
    assert len(reserved_results) == 1, results
    assert len(denied_results) == 1, results
    assert denied_results[0]["refusal"] in ("scoped_cap_exceeded", "admission_busy")
    with eng.begin() as c:
        total = c.execute(
            text(
                "SELECT COALESCE(SUM(amount_usd_micros), 0) FROM budget_reservations "
                "WHERE project_id IN (:p1, :p2) AND status = 'active'"
            ),
            {"p1": w["p1"], "p2": w["p2"]},
        ).scalar_one()
    assert int(total) == 7 * DOLLAR
