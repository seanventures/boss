"""A verified host belongs to the organisation, not to the one project that registered it.

HelpDesk AI was imported as its own project and could not deploy anywhere: the shared server was
registered and verified under another project of the same organisation, and BOSS's agent found
"attach refused across projects" with no way on. Every project of an org may deploy onto the org's
hosts, as every project may use the org's repositories; another organisation never sees them.
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text

from ._obs_seed import pg_engine
from boss_engine.services import deployment_fleet as fleet


def _host(c, pid: str, status: str = "verified") -> str:
    hid = f"dh_{uuid4().hex[:12]}"
    c.execute(text(
        "INSERT INTO deployment_hosts (id, project_id, name, address, ssh_user, status) "
        "VALUES (:i, :p, :n, :a, 'root', :s)"),
        {"i": hid, "p": pid, "n": f"edge-{hid[-4:]}", "a": f"198.51.100.{int(hid[-2:], 16) % 250 + 1}", "s": status})
    return hid


def _org(c) -> str:
    """A fresh organisation and workspace, so no other test's hosts are in it."""
    org = f"org_{uuid4().hex[:8]}"
    c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, :o)"), {"o": org})
    c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, :o)"), {"w": f"ws_{org}", "o": org})
    return org


def _project(c, org: str) -> str:
    pid = f"p_{uuid4().hex[:8]}"
    c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :p, :p)"),
              {"p": pid, "w": f"ws_{org}"})
    return pid


def _other_org_project(c) -> str:
    return _project(c, _org(c))


def test_a_sibling_project_sees_and_deploys_onto_the_orgs_host():
    eng = pg_engine()
    with eng.begin() as c:
        org = _org(c)
        owner, imported, stranger = _project(c, org), _project(c, org), _other_org_project(c)
        hid = _host(c, owner)
        assert hid in {h["id"] for h in fleet.list_hosts(c, imported)}
        assert fleet.host_usable_by(c, hid, imported)
        assert not fleet.host_usable_by(c, hid, stranger)
        assert hid not in {h["id"] for h in fleet.list_hosts(c, stranger)}


def test_a_sibling_project_gets_a_target_on_the_shared_host_and_can_attach_to_it():
    eng = pg_engine()
    with eng.begin() as c:
        org = _org(c)
        owner, imported = _project(c, org), _project(c, org)
        hid = _host(c, owner)
        made = fleet.create_deployment_target(c, project_id=imported, app_name=f"HelpDesk {uuid4().hex[:4]}",
                                              repo="paramg/helpdesk-ai")
        assert made.get("ok") is not False, made
        row = c.execute(text("SELECT host_id, project_id FROM deployment_targets WHERE id = :t"),
                        {"t": made["target_id"]}).mappings().first()
        assert row["host_id"] == hid and row["project_id"] == imported
        assert fleet.attach_target(c, target_id=made["target_id"], host_id=hid)["host_id"] == hid


def test_another_organisations_host_is_never_attached_or_chosen():
    eng = pg_engine()
    with eng.begin() as c:
        stranger, mine = _other_org_project(c), _other_org_project(c)
        theirs = _host(c, stranger)
        made = fleet.create_deployment_target(c, project_id=mine, app_name="Lonely", host_id=theirs)
        assert made.get("ok") is False
        c.execute(text(
            "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
            " target_ref, status, created_by) VALUES (:t, :p, 'Lonely', 'a/b', 'staging', 'ssh', 'x', "
            " 'active', 'test')"), {"t": f"deptgt_{uuid4().hex[:8]}", "p": mine})
        tid = c.execute(text("SELECT id FROM deployment_targets WHERE project_id = :p"), {"p": mine}).scalar()
        try:
            fleet.attach_target(c, target_id=tid, host_id=theirs)
        except ValueError as exc:
            assert "attach refused" in str(exc)
        else:
            raise AssertionError("another organisation's host was attached")


def test_no_host_at_all_is_said_plainly():
    eng = pg_engine()
    with eng.begin() as c:
        lone = _other_org_project(c)
        made = fleet.create_deployment_target(c, project_id=lone, app_name="Nowhere")
        assert made.get("ok") is False and "no verified deployment host" in made["error"]
