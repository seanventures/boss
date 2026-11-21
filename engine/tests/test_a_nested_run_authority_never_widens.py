"""A NESTED RUN-AUTHORITY BLOCK NEVER WIDENS A DELEGATED AGENT TO ITS PERSON (X1, P1).

Found in review (2026-11-20), latent on main: inside a step's delegated run, a second
`_step_authority` / `_run_authority` block called `runs.delegated` (which keeps the run: it yields
None), then fell back to `runs.on_behalf(recorded)`. `_adoptable` let that through, because a run
token's Acting names the PERSON (`rt.sub`) as its principal. The inner block's tool identity
(CURRENT_TOOL_AUTH) became the person's FULL live principal with no run token, and
`_authorize_step_tool`'s run-token check was skipped: an agent's Passport-bounded authority turned
into everything the person holds.

The class rule, enforced centrally in `runs.on_behalf`: work nested inside a delegated run keeps
that run's token. Measured here against the real schema and real Passports (the `staffed`
fixture), not a mock of `_step_authority`.
"""
from __future__ import annotations

import json
import types

from boss_engine.identity import lineage, runs
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.tools.registry import CURRENT_TOOL_AUTH

from .test_identity_agents_workflow_consent import ALICE, env  # noqa: F401  (full-schema fixture)
from .test_no_step_is_blocked_by_consent import (  # noqa: F401
    RUN_INPUT, _publish, _start_consented, staffed)

SPEC = {"entry_step": "deploy", "steps": [
    {"id": "deploy", "kind": "capability", "captain": "release_manager", "next": [],
     "binding": {"capability": "deploy", "inputs": {"run_id": "crun_nested"}}}]}


def _who(auth) -> dict:
    rt = getattr(auth, "run_token", None)
    return {"principal": getattr(getattr(auth, "principal", None), "id", None),
            "run_token": rt is not None,
            "chain": list(rt.chain.to_boss_chain()) if rt is not None else []}


def _run(env):  # noqa: F811
    version = _publish(env, json.loads(json.dumps(SPEC)), "nested authority")
    return _start_consented(env, version, RUN_INPUT)


def test_a_nested_step_authority_keeps_the_agents_run_token(env, staffed):  # noqa: F811
    run = _run(env)
    ctx = types.SimpleNamespace(engine=env.engine)
    seen: dict = {}
    with env.engine.begin() as c:
        with runner._step_authority(ctx, c, env.project, {"run_id": run["id"]},
                                    {"id": "deploy"}, "release_manager"):
            seen["outer"] = _who(CURRENT_TOOL_AUTH.get())
            # Every nesting shape: no agent (the on_behalf fallback), another agent, the same one.
            for label, role in (("no agent", ""), ("other agent", "release_manager"), ("none", None)):
                with runner._step_authority(ctx, c, env.project, {"run_id": run["id"]},
                                            {"id": "inner"}, role):
                    seen[label] = _who(CURRENT_TOOL_AUTH.get())
            seen["after"] = _who(CURRENT_TOOL_AUTH.get())
    outer = seen["outer"]
    assert outer["run_token"] and outer["principal"] == staffed["release_manager"], seen
    assert any("step:deploy" in h for h in outer["chain"]), seen
    for label in ("no agent", "other agent", "none", "after"):
        assert seen[label] == outer, (label, seen)
        assert seen[label]["principal"] != ALICE, (label, seen)


def test_on_behalf_inside_a_delegated_run_never_installs_the_person(env, staffed):  # noqa: F811
    """The central rule, at the one function that hands out a person's full principal."""
    run = _run(env)
    from boss_engine.identity import consent

    with env.engine.begin() as c:
        recorded = consent.acting_for_workflow_run(c, run["id"])
    assert recorded is not None and recorded.principal_id == ALICE
    with runs.delegated(env.engine, kind="step", entity_id="deploy", project_id=env.project,
                        role_key="release_manager", scope=frozenset({"workflow_run:read"}),
                        acting=recorded) as agent_ctx:
        assert agent_ctx is not None and agent_ctx.run_token is not None
        with runs.on_behalf(env.engine, recorded) as inner:
            assert inner is agent_ctx
            assert CURRENT_TOOL_AUTH.get() is agent_ctx
            assert lineage.current().chain
        assert CURRENT_TOOL_AUTH.get() is agent_ctx
    # Outside any run, on_behalf still gives a built-in step its person (#256 unchanged).
    with runs.on_behalf(env.engine, recorded) as person:
        assert person is not None and person.principal.id == ALICE and person.run_token is None
