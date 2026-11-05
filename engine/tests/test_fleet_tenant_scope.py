"""Fleet attach/retire must not cross an organisation boundary or sweep a neighbor's keys.

Hosts are shared across the projects of ONE organisation (deployment_fleet.usable_hosts_sql):
proj_a and proj_c share org_1; proj_b belongs to org_2."""

from __future__ import annotations

import sqlalchemy as sa
import pytest

from boss_engine.services.deployment_fleet import attach_target, retire_host, target_ref_for


@pytest.fixture()
def conn():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(sa.text(
            "CREATE TABLE deployment_hosts ("
            "id TEXT PRIMARY KEY, project_id TEXT, name TEXT, address TEXT, "
            "ssh_user TEXT, ssh_port INT, status TEXT, key_public TEXT, "
            "key_secret_ref TEXT)"
        ))
        c.execute(sa.text(
            "CREATE TABLE deployment_targets ("
            "id TEXT PRIMARY KEY, project_id TEXT, host_id TEXT, target_ref TEXT, "
            "updated_at TEXT)"
        ))
        c.execute(sa.text("CREATE TABLE workspaces (id TEXT PRIMARY KEY, org_id TEXT)"))
        c.execute(sa.text("CREATE TABLE projects (id TEXT PRIMARY KEY, workspace_id TEXT)"))
        c.execute(sa.text("INSERT INTO workspaces VALUES ('ws_1', 'org_1'), ('ws_2', 'org_2')"))
        c.execute(sa.text("INSERT INTO projects VALUES ('proj_a', 'ws_1'), ('proj_c', 'ws_1'), "
                          "('proj_b', 'ws_2')"))
        yield c


def test_attach_target_refuses_a_host_from_another_organisation(conn):
    """host_id is a global primary key. Without an organisation check, project A can attach
    another organisation's VM and the next deploy follows that host's SSH key."""
    conn.execute(sa.text(
        "INSERT INTO deployment_hosts (id, project_id, name, address, ssh_user, ssh_port, status) "
        "VALUES ('dh_victim', 'proj_b', 'prod', '10.0.0.9', 'root', 22, 'verified')"
    ))
    conn.execute(sa.text(
        "INSERT INTO deployment_targets (id, project_id, host_id, target_ref) "
        "VALUES ('dt_attacker', 'proj_a', NULL, '')"
    ))
    with pytest.raises(ValueError, match="attach refused"):
        attach_target(conn, target_id="dt_attacker", host_id="dh_victim")
    host_id = conn.execute(
        sa.text("SELECT host_id FROM deployment_targets WHERE id = 'dt_attacker'")
    ).scalar()
    assert host_id in (None, "")


def test_attach_target_onto_a_host_its_organisation_shares(conn):
    conn.execute(sa.text(
        "INSERT INTO deployment_hosts (id, project_id, name, address, ssh_user, ssh_port, status) "
        "VALUES ('dh_shared', 'proj_c', 'box', '10.0.0.3', 'root', 22, 'verified')"
    ))
    conn.execute(sa.text(
        "INSERT INTO deployment_targets (id, project_id, host_id, target_ref) "
        "VALUES ('dt_sibling', 'proj_a', NULL, '')"
    ))
    assert attach_target(conn, target_id="dt_sibling", host_id="dh_shared")["host_id"] == "dh_shared"


def test_attach_target_same_project_still_works(conn):
    conn.execute(sa.text(
        "INSERT INTO deployment_hosts (id, project_id, name, address, ssh_user, ssh_port, status) "
        "VALUES ('dh_1', 'proj_a', 'box', '10.0.0.2', 'root', 22, 'verified')"
    ))
    conn.execute(sa.text(
        "INSERT INTO deployment_targets (id, project_id, host_id, target_ref) "
        "VALUES ('dt_1', 'proj_a', NULL, '')"
    ))
    out = attach_target(conn, target_id="dt_1", host_id="dh_1")
    assert out["host_id"] == "dh_1"
    assert out["target_ref"] == target_ref_for(
        {"ssh_user": "root", "address": "10.0.0.2", "ssh_port": 22}
    )


def test_retire_script_does_not_substring_match_a_longer_host_name():
    """`grep -v ' boss@prod'` also drops a line commented `boss@production`.

    Hosts named `prod` and `production` (or `web` / `web-2`) share a prefix. Retire must
    drop only lines that end with the exact ` boss@<name>` comment, plus the recorded
    public key itself.
    """
    import inspect

    src = inspect.getsource(retire_host)
    assert "grep -vxF" in src
    # A bare substring grep -v of the marker is the defect.
    assert 'grep -v {shlex.quote(" " + marker)}' not in src
    assert "boss@" in src
