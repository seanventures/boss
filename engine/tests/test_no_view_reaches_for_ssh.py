"""No Observability view reaches a host on a poll (infra-observability spec §6.8, §9.1).

obs_view never imports the SSH adapter or the deploy service's SSH helpers. The monitoring
stack's status is asked of the host (SSH, plus Grafana over HTTP) ONLY on an explicit
`Check now` (fresh=True); a poll reuses the composed Sources body for 60 s and shows the last
check with its time. Pulse, Models and System never ask at all.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from sqlalchemy import text

from ._obs_seed import pg_engine, project, target
from boss_engine.config import get_settings
from boss_engine.services import obs_view, observability_stack

SOURCE = Path(obs_view.__file__)
BANNED = ("ssh_host_adapter", "deploy_service", "paramiko", "subprocess")


def test_the_module_never_imports_ssh():
    tree = ast.parse(SOURCE.read_text())
    named = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            named.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            named.add(node.module or "")
            named.update(a.name for a in node.names)
    hits = [n for n in named for b in BANNED if b in n]
    assert not hits, f"obs_view imports {hits}"


@pytest.fixture
def stack(monkeypatch):
    calls = {"ssh": 0, "grafana": 0}

    def fake_status(engine, settings, *, host_target_id):
        calls["ssh"] += 1
        return {"running": True, "containers": [{"name": "boss-grafana", "status": "Up 2h"},
                                                {"name": "boss-prometheus", "status": "Up 2h"}],
                "missing": [], "host": "10.0.0.9"}

    def fake_dashboards(base, timeout=4.0):
        calls["grafana"] += 1
        return {"boss-invoice"}

    monkeypatch.setattr(observability_stack, "stack_status", fake_status)
    monkeypatch.setattr(observability_stack, "provisioned_dashboards", fake_dashboards)
    obs_view.forget_sources()
    obs_view._STACK_PROBES.clear()
    yield calls
    obs_view.forget_sources()
    obs_view._STACK_PROBES.clear()


def test_sources_ask_the_host_only_on_check_now(stack):
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
        target(c, pid, f"{pid}_t", "invoice")
        c.execute(text(
            "INSERT INTO telemetry_sources (id, project_id, kind, name, config, enabled, created_at) "
            "VALUES (:id, :p, 'prometheus', 'boss-prometheus', CAST(:c AS jsonb), true, now())"),
            {"id": f"ts_{pid}", "p": pid, "c": json.dumps({"base_url": "http://10.0.0.9:9091"})})
    s = get_settings()
    with eng.begin() as c:
        first = obs_view.sources(c, s, pid)
        again = obs_view.sources(c, s, pid)
    assert stack["ssh"] == 0 and stack["grafana"] == 0
    assert first["stack"]["state"] == "unknown" and first["stack"]["word"] == "Status unknown"
    assert first["stack"]["checked_at"] is None
    assert again is first, "a poll within 60 s reuses the composed body"
    # Grafana has not been asked yet: no link, and the reason (operator decision 2026-10-18)
    assert first["stack"]["grafana_url"] is None
    assert first["stack"]["grafana"]["state"] == "unchecked"
    assert first["stack"]["can_deploy"] is True and first["stack"]["host_target_id"] == f"{pid}_t"

    with eng.begin() as c:
        checked = obs_view.sources(c, s, pid, fresh=True)
    assert stack == {"ssh": 1, "grafana": 1}
    assert checked["stack"]["state"] == "running" and checked["stack"]["word"] == "Running"
    assert checked["stack"]["checked_at"]
    assert len(checked["stack"]["containers"]) == 2
    # ...and Check now asked Grafana, which answered: the link is back
    assert checked["stack"]["grafana_url"] == "http://10.0.0.9:3006"

    with eng.begin() as c:
        polled = obs_view.sources(c, s, pid)
        obs_view.forget_sources(pid)
        rebuilt = obs_view.sources(c, s, pid)
        obs_view.sources(c, s, pid, fresh=True)
    assert polled is checked, "the check's body is what the next poll sees"
    assert rebuilt["stack"]["state"] == "running", "a rebuilt poll shows the last check"
    assert stack == {"ssh": 2, "grafana": 2}, "only the two Check now presses reached the host"


def test_the_other_views_never_ask(stack, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a view reached for the host on a poll")

    monkeypatch.setattr(observability_stack, "stack_status", refuse)
    monkeypatch.setattr(observability_stack, "provisioned_dashboards", refuse)
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
        target(c, pid, f"{pid}_t", "invoice")
        s = get_settings()
        obs_view.pulse(c, s, pid)
        obs_view.app_detail(c, s, pid, f"{pid}_t")
        obs_view.models(c, s, pid)
        obs_view.system(c, s, pid)
        obs_view.sources(c, s, pid)
