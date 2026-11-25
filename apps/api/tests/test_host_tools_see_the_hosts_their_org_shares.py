"""A HOST THE PROJECT CAN SEE IS A HOST EVERY TOOL CAN FIND (2026-11-18).

THE CASE: API onboarding on northgate-gateway. list_deployment_hosts showed 203.0.113.10 (a
verified host registered in boss-dogfood, shared with the org) and BOSS's agent then got
"no registered host '203.0.113.10'" from inspect_host and "no host matching" from
describe_deployment_host: those tools looked hosts up in the project's own rows only. Every
id/name/address lookup now goes through deployment_fleet.match_host (org-shared, verified)."""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from sqlalchemy import text

from boss_engine.services.deployment_fleet import host_by_hint_sql, match_host

ROOT = Path(__file__).resolve().parents[3]
OWNER = "boss-dogfood"


def _uid() -> str:
    return uuid.uuid4().hex[:10]


def test_a_sibling_project_finds_the_shared_host_by_address_name_and_id(engine):
    sibling, host_id, addr = f"proj-{_uid()}", f"dh_{_uid()}", f"198.51.100.{uuid.uuid4().int % 250}"
    with engine.begin() as c:
        ws = c.execute(text("SELECT workspace_id FROM projects WHERE id = :p"), {"p": OWNER}).scalar()
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:i, :w, :i, :i)"),
                  {"i": sibling, "w": ws})
        c.execute(text(
            "INSERT INTO deployment_hosts (id, project_id, name, address, ssh_user, ssh_port, status) "
            "VALUES (:i, :p, :n, :a, 'root', 22, 'verified')"),
            {"i": host_id, "p": OWNER, "n": f"box-{host_id}", "a": addr})
    try:
        with engine.begin() as c:
            for hint in (addr, f"box-{host_id}", host_id, addr.upper()):
                row, refusal = match_host(c, sibling, hint)
                assert row and row["id"] == host_id, (hint, refusal)
                # The SQL the tools run (inspect_host, run_on_host, describe_deployment_host,
                # the port probe, debug sessions) finds it too.
                hit = c.execute(text(host_by_hint_sql()), {"p": sibling, "h": hint}).mappings().first()
                assert hit and hit["id"] == host_id, hint
                # run_on_host changes a machine: it stays on the project's own hosts.
                own = c.execute(text(host_by_hint_sql(own_only=True)),
                                {"p": sibling, "h": hint}).mappings().first()
                assert own is None, hint
    finally:
        with engine.begin() as c:
            c.execute(text("DELETE FROM deployment_hosts WHERE id = :i"), {"i": host_id})
            c.execute(text("DELETE FROM projects WHERE id = :i"), {"i": sibling})


# A host looked up by what a person or agent typed (id, name or address), scoped to the
# project's own rows: the shape that hid 203.0.113.10. Every such lookup is match_host now.
_OWN_ROWS_LOOKUP = re.compile(
    r"FROM deployment_hosts[^;]{0,200}?project_id\s*=\s*:p",
    re.S)


def test_no_tool_looks_a_host_up_in_the_projects_own_rows_only():
    offenders = []
    for base in ("engine/boss_engine/tools", "engine/boss_engine/services/debug_session.py"):
        paths = [ROOT / base] if base.endswith(".py") else sorted((ROOT / base).rglob("*.py"))
        for path in paths:
            for m in _OWN_ROWS_LOOKUP.finditer(path.read_text(encoding="utf-8")):
                offenders.append(f"{path.relative_to(ROOT)}: {m.group(0)[:90]!r}")
    assert not offenders, "use deployment_fleet.match_host:\n" + "\n".join(offenders)
