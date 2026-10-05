"""Infra's Ask BOSS asks about the fleet as a RECORD (operator decision 2026-10-04).

The header door opened the dock with no subject, so a question about "my machines" was answered
from whatever the model remembered. `fleet:<project>` resolves to every machine with its state,
last check and problems, what is placed on each, what is on no machine, and the stack deploys
that failed lately — stored rows only, never a host check.
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text

from ._obs_seed import pg_engine, project
from boss_engine.services.turn_subject import RESOLVERS, SUBJECT_KINDS, parse_subject, resolve_subject


def test_fleet_is_a_kind_with_a_resolver():
    assert "fleet" in SUBJECT_KINDS and "fleet" in RESOLVERS
    ref = parse_subject("fleet:p1")
    assert ref is not None and ref.kind == "fleet" and ref.id == "p1"


def test_the_fleet_record_names_every_machine_and_what_runs_on_it():
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
        for hid, name, status, pf in (("h_ok", "edge-ready", "verified", '{"reachable": true}'),
                                      ("h_bad", "edge-down", "degraded",
                                       '{"reachable": false, "problems": ["ssh: connection refused"]}')):
            c.execute(text(
                "INSERT INTO deployment_hosts (id, project_id, name, address, status, last_preflight) "
                "VALUES (:i, :p, :n, '203.0.113.7', :s, CAST(:pf AS jsonb))"),
                {"i": f"{pid}_{hid}", "p": pid, "n": name, "s": status, "pf": pf})
        c.execute(text(
            "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
            " target_ref, status, created_by, host_id) VALUES (:t, :p, 'invoice', 'acme/app', 'prod', "
            " 'stack', 'invoice', 'active', 'test', :h)"), {"t": f"{pid}_t", "p": pid, "h": f"{pid}_h_ok"})
        ev = resolve_subject(c, pid, parse_subject(f"fleet:{pid}"))
    assert ev.found and ev.title == "Your fleet"
    block = ev.render("What is wrong with my machines?")
    assert "edge-ready" in block and "edge-down" in block
    assert "invoice (prod, stack)" in block
    assert "ssh: connection refused" in block
    assert "Unreachable: edge-down" in ev.error
