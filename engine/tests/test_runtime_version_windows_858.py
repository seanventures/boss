"""BOSSAPP-858: a CLI engine's release window and model fallback.

An old CLI in the image used to fail hours into a build. An engine may now declare the window it
is driven within; the pre-check reads it, and a routed model the installed CLI cannot run falls
back to the nearest one it can, with an event. With nothing declared nothing changes."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from boss_engine.services import runtime_versions as rv
from boss_engine.services.cli_harness import (BUILTIN_CLI_HARNESSES, CliHarnessSpec,
                                              CliHarnessUnavailable, GenericCliCodeAgent,
                                              load_specs)

WINDOW = rv.VersionWindow(binary="fakecli", min_version="1.2.0", max_version="2.0.0",
                          model_minimums=(("big-*", "1.5.0"), ("big-2", "1.8.0")),
                          model_fallbacks=("big-2", "big-1", "small-1"),
                          install_hint="npm install -g fakecli@latest")


def test_versions_are_read_from_whatever_the_cli_prints():
    assert rv.parse_version("codex-cli 0.154.0") == (0, 154, 0)
    assert rv.parse_version("2.1.273 (Claude Code)") == (2, 1, 273)
    assert rv.parse_version("no digits") is None
    assert rv.parse_version(None) is None


@pytest.mark.parametrize("installed,model,status,fallback", [
    ("fakecli 1.6.0", "small-1", "ok", None),
    ("fakecli 1.1.9", None, "too_old", None),
    ("fakecli 2.0.0", None, "too_new", None),
    ("garbage", None, "unknown", None),
    ("fakecli 1.6.0", "big-2", "model_fallback", "big-1"),   # big-2 needs 1.8, big-1 needs 1.5
    ("fakecli 1.3.0", "big-2", "model_fallback", "small-1"),  # nearest it can run, skipping big-1
    ("fakecli 1.9.0", "big-2", "ok", None),
])
def test_the_window_decides(installed, model, status, fallback):
    v = rv.check(WINDOW, installed, model)
    assert v.status == status
    assert v.fallback == fallback
    if status in ("too_old", "too_new"):
        assert v.blocks and "fakecli" in v.hint


def test_a_model_with_no_fallback_it_can_run_blocks_with_the_install_hint():
    w = rv.VersionWindow(binary="x", model_minimums=(("m", "3.0"),))
    v = rv.check(w, "x 2.0", "m")
    assert v.status == "model_needs_newer" and v.blocks
    assert ">= 3.0" in v.hint


def test_a_bad_window_is_refused_where_it_is_declared():
    with pytest.raises(ValueError):
        rv.VersionWindow(binary="x", min_version="2.0", max_version="1.0")
    with pytest.raises(ValueError):
        rv.VersionWindow(binary="x", min_version="latest")


def test_nothing_declared_is_byte_identical():
    for spec in BUILTIN_CLI_HARNESSES.values():
        assert spec.version_window() is None
    assert rv.verdict_for(None, "any") is None
    calls = []
    assert rv.apply_fallback("x", None, "m", run=lambda a: calls.append(a) or "") == "m"
    assert calls == []          # no `--version` probe when nothing is declared
    from boss_engine.services.harness_registry import available_harnesses, version_verdict

    assert version_verdict("codex", "gpt-6") is None
    assert all("version_window" not in row for row in available_harnesses())


def test_an_operator_override_declares_a_window_from_json():
    settings = SimpleNamespace(cli_harnesses_json=json.dumps({"codex": {
        "min_version": "0.150.0", "model_minimums": {"gpt-6*": "0.154.0"},
        "model_fallbacks": ["gpt-6-codex", "gpt-5.2-codex"]}}))
    spec = load_specs(settings)["codex"]
    w = spec.version_window()
    assert w is not None and w.binary == "codex"
    assert w.model_minimums == (("gpt-6*", "0.154.0"),)
    assert rv.check(w, "codex-cli 0.152.0", "gpt-6-codex").fallback == "gpt-5.2-codex"


def _agent(**window) -> GenericCliCodeAgent:
    spec = CliHarnessSpec(name="fakecli", binary="fakecli", **window)
    return GenericCliCodeAgent(SimpleNamespace(), spec)


def test_a_session_falls_back_and_says_so(monkeypatch):
    events = []
    monkeypatch.setattr(rv, "record_fallback_event", lambda engine, v: events.append((engine, v)))
    monkeypatch.setattr(rv, "installed_version", lambda w, **_: "fakecli 1.6.0")
    agent = _agent(min_version="1.2.0", model_minimums=(("big-2", "1.8.0"),),
                   model_fallbacks=("big-2", "big-1"))
    assert agent._versioned_model("big-2") == "big-1"
    assert events and events[0][0] == "fakecli" and events[0][1].fallback == "big-1"
    assert agent._versioned_model("big-1") == "big-1"


def test_a_cli_outside_its_window_refuses_to_start(monkeypatch):
    monkeypatch.setattr(rv, "installed_version", lambda w, **_: "fakecli 1.0.0")
    with pytest.raises(CliHarnessUnavailable, match="older than 1.2.0"):
        _agent(min_version="1.2.0")._versioned_model(None)


def test_the_probe_runs_the_version_args_once_per_binary(tmp_path):
    seen = []
    w = rv.VersionWindow(binary="fakecli", min_version="1.0", version_args=("version",))
    out = rv.installed_version(w, run=lambda argv: seen.append(argv) or "fakecli 1.4.2",
                               which=lambda b: str(tmp_path / b))
    assert out == "fakecli 1.4.2" and seen == [[str(tmp_path / "fakecli"), "version"]]
    assert rv.installed_version(w, which=lambda b: None) is None


def test_the_registry_answers_for_a_declared_engine():
    from boss_engine.services import harness_registry as hr

    hr._ensure_builtins()
    hr.register_harness("fake_windowed", lambda s: None, kind="cli", replace=True,
                        version=WINDOW)
    try:
        v = hr.version_verdict("fake_windowed", "big-2",
                               run=lambda a: "fakecli 1.6.0", which=lambda b: "/bin/" + b)
        assert v.status == "model_fallback" and v.fallback == "big-1"
        row = next(r for r in hr.available_harnesses() if r["name"] == "fake_windowed")
        assert row["version_window"]["min_version"] == "1.2.0"
    finally:
        hr._REGISTRY.pop("fake_windowed", None)


def test_an_acp_plugin_may_declare_a_window():
    from boss_engine.services.harness_plugins import _version_window, validate_manifest

    m = validate_manifest({
        "apiVersion": "boss.harness/v1", "kind": "Harness",
        "metadata": {"id": "acme.agent", "name": "acme_agent", "version": "1.0.0",
                     "description": "d"},
        "spec": {"kind": "acp", "acp": {"binary": "acme", "minVersion": "1.4.0"}}})
    w = _version_window(m)
    assert w is not None and w.min_version == "1.4.0" and w.binary == "acme"
    assert rv.check(w, "acme 1.3", None).status == "too_old"
