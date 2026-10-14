"""Every capability runs as a role whose Passport holds what its tools are checked against.

The 2026-10-13 pre-check of the HelpDesk workflow: the preview step ran as QA (Vera, Reviewer
Passport), which has no preview:manage, so every preview was refused and skipped; More tests ran
its extend_code_task as QA too, which has no code_task:retry. A consent that asks for a permission
is not enough: the delegated token is person ∩ Passport ceiling ∩ grant, so the agent a step runs
as must hold it as well.

  * a capability's own `tool` runs as the step's default agent (DEFAULT_STEP_AGENTS, else the
    capability's captain);
  * its in-process `calls` (work on the build's branch) run as the builder
    (workflow_capability_runner._start_on_the_branch).
A permission no agent may ever hold (not delegable) is only ever exercised as the person.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from boss_engine.identity import catalogue, passports
from boss_engine.identity.consent import DEFAULT_BUILDER
from boss_engine.identity.permissions_gen import PASSPORT_PRESETS
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services.workflow_capabilities import CAPABILITIES
from boss_engine.services.workflow_catalogue import DEFAULT_STEP_AGENTS
from boss_engine.tools.builtin import build_default_registry

ROLES = Path(__file__).resolve().parents[2] / "packages" / "specpack" / "roles"


def _preset(role_key: str):
    f = ROLES / f"{role_key}.yaml"
    callsign = str(((yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("persona") or {})
                   .get("callsign") or "") if f.is_file() else ""
    key = passports.preset_for_role(callsign) or passports.preset_for_role(role_key)
    return PASSPORT_PRESETS.get(key) if key else None


def test_every_capabilitys_tools_are_held_by_the_role_it_runs_as():
    specs = {s.name: s for s in build_default_registry().specs()}
    problems = []
    for key, cap in CAPABILITIES.items():
        runs_as = [(DEFAULT_STEP_AGENTS.get(key) or cap.captain, cap.tool)]
        runs_as += [(DEFAULT_BUILDER, tool) for tool in cap.calls]
        for role, tool in runs_as:
            if not tool or not role:
                continue
            perm = str(getattr(specs.get(tool), "permission", "") or "")
            if not perm or not catalogue.is_known(perm) or not catalogue.meta(perm).delegable:
                continue  # never an agent's: the step's tool runs as the person
            preset = _preset(role)
            if preset is None or perm not in preset.permissions:
                problems.append(f"{key}: {tool} needs {perm}; it runs as {role} "
                                f"({preset.key if preset else 'no Passport preset'})")
    assert not problems, "\n".join(problems)


def test_work_on_the_branch_runs_as_the_builder_not_the_steps_agent():
    """More tests is QA's step, and QA does not extend builds: the builder does."""
    source = Path(runner.__file__).read_text(encoding="utf-8")
    body = source[source.index("def _start_on_the_branch"):]
    body = body[:body.index("\ndef ", 10)]
    assert "resolved_builder(" in body and "_step_authority(ctx, connection, project_id, context, step, builder)" in body
    assert "code_task:retry" in _preset(DEFAULT_BUILDER).permissions
    assert "code_task:retry" not in _preset("qa").permissions, "QA's Passport was widened"


def test_a_skipped_preview_is_said_on_the_run():
    from boss_engine.tools.registry import permission_refusal_words

    out = runner._preview_not_permitted("preview", {
        "ok": False, "error": "forbidden", "permission": "preview:manage",
        "message": permission_refusal_words("start_preview", "preview:manage")}, "qa")
    assert out["note"].startswith("skipped: ")
    source = Path(runner.__file__).read_text(encoding="utf-8")
    at = source.index("skipped = _preview_not_permitted(key, result, step_actor)")
    assert '"workflow.step_skipped"' in source[at:at + 600]
