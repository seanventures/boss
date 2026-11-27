"""An agent never offers a button this install will refuse.

Harbor offered "run on host" to fix a deploy's directory; the click answered "Community host
shell is disabled". `action_unavailable` is the one rule: the tool and `propose_action` both read it.
"""
from __future__ import annotations

from types import SimpleNamespace

from boss_engine.tools import builtin
from boss_engine.tools.builtin import action_unavailable


def _settings(**over):
    return SimpleNamespace(**{"boss_edition": "community", "agent_host_shell": False, **over})


def test_host_shell_off_refuses_identically_across_editions():
    said = action_unavailable("run_on_host", _settings())
    assert "`BOSS_AGENT_HOST_SHELL`" in said, "a setting name is written as code, not prose"
    for jargon in ("ChangeSet", "ResourceReceipt", "budget reservation", "Resource Plane"):
        assert jargon not in said
    assert action_unavailable("run_on_host", _settings(boss_edition="enterprise")) == said
    assert action_unavailable("run_on_host", _settings(agent_host_shell=True)) is None
    assert action_unavailable("redeploy", _settings()) is None


def test_propose_action_refuses_a_button_that_would_refuse():
    reg = builtin.build_default_registry()
    spec = next(s for s in reg.specs() if s.name == "propose_action")
    out = spec.handler(SimpleNamespace(settings=_settings()), project_id="p", actor="ceo",
                       args={"tool": "run_on_host", "label": "Prepare the host",
                             "args": {"host": "h", "command": "mkdir -p /opt/x"}})
    assert out["ok"] is False and "cannot propose 'run_on_host' here" in out["error"]


def test_the_tool_itself_reads_the_same_rule():
    import inspect
    src = inspect.getsource(builtin)
    body = src[src.index('"run_on_host",\n        "Run ANY command'):]
    body = body[:body.index("raw = (args.get(\"command\")")]
    assert 'action_unavailable("run_on_host", ctx.settings)' in body
