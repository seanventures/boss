"""A BUTTON'S ARGUMENTS ARE ITS TOOL'S ARGUMENTS (laptop 2026-11-20).

Ask BOSS offered "Create config target … dh_9c480ba29fdb …" as create_config_target with `host`
and `reload` — names the tool does not take (`host_id`, `reload_command`, `reload_url`). The
button was unpressable, and had it run, the host would have been dropped silently and the
config target made on the BOSS machine. propose_action now refuses such an offer, naming the
arguments the tool does take, so the model offers it again correctly.
"""
from __future__ import annotations

from types import SimpleNamespace

from boss_engine.tools.builtin import build_default_registry


def _propose(reg, tool, args):
    spec = reg.get("propose_action")
    return spec.handler(SimpleNamespace(settings=None), project_id="p", actor="ceo",
                        args={"tool": tool, "args": args, "label": "Do it"})


def test_an_offer_with_names_its_tool_does_not_take_is_refused_with_the_real_names():
    reg = build_default_registry()
    got = _propose(reg, "create_config_target", {
        "app_name": "Northgate gateway config", "environment": "staging",
        "host": "dh_9c480ba29fdb", "reload": False,
        "directory": "/opt/boss-apps/northgate-gateway/current/config/tenants",
        "health_url": "http://203.0.113.10:8080/readyz"})
    assert got["ok"] is False and got.get("proposed") is False
    assert "does not take host, reload" in got["error"]
    assert "host_id" in got["error"] and "reload_command" in got["error"]


def test_an_offer_missing_a_required_argument_is_refused():
    reg = build_default_registry()
    got = _propose(reg, "create_config_target", {"app_name": "x", "directory": "/srv/x"})
    assert got["ok"] is False and "needs health_url" in got["error"]


def test_a_correct_offer_still_becomes_a_button():
    reg = build_default_registry()
    got = _propose(reg, "create_config_target", {
        "app_name": "Northgate gateway config", "environment": "staging",
        "host_id": "dh_9c480ba29fdb",
        "directory": "/opt/boss-apps/northgate-gateway/current/config/tenants",
        "health_url": "http://203.0.113.10:8080/readyz", "project_id": "p"})
    assert got["ok"] is True and got["proposed"] is True and got["tool"] == "create_config_target"
