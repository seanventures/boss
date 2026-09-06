"""Tool registry + built-in tool-set tests (no DB)."""
from __future__ import annotations

import pytest

from boss_engine.tools import build_default_registry, build_discuss_read_registry
from boss_engine.tools.registry import ToolError, ToolRegistry, ToolSpec


#: Every tool the default registry is expected to carry. Present so a count mismatch can name
#: what changed instead of reporting two integers.
_EXPECTED_TOOLS: set[str] = {
    # Rules the WORKFORCE mandates, as opposed to the ones the platform hardcodes. Security
    # concluding in a panel that ports must be checked for public exposure used to change
    # nothing — the conclusion lived in minutes no build ever read. `check_port_exposure` is
    # here for the same reason: a role that mandates a check it cannot itself run has filed a
    # request, not applied a control.
    "mandate_standard", "list_standards", "check_port_exposure",
    # The turn-by-turn record of what a build DID. 101 rows existed for a run reported as
    # "stuck" and nothing could read them: get_run_activity reads llm_calls, which was empty.
    # So a question about progress got answered from queue statistics, and the build was
    # cancelled on the strength of it.
    "read_run_turns",
    # The whole investigation as one call, in the right order, with the comparison that
    # makes a duration mean something. A prompt can be skipped under pressure; a routine
    # cannot.
    "diagnose_run",
    # Choosing a port without guessing. BOSS worked out "8000 is in use, 8001 is free" by
    # reading raw socket output; the next number up is a guess that happened to be right.
    "find_free_port",
    # The spec an app was built from — PRD, criteria, review notes. BOSS could read
    # its own source, its logs and its hosts, and could not read the one document
    # that says what an app was FOR.
    "read_spec",
    # The machines BOSS deploys onto. The agent could describe every build and had no way
    # to answer "is 2.29.2.202 healthy" or "where would this ship" — so the Deployment
    # Manager role had nothing to read. Read-only: the governed path still does the deploy.
    "list_deployment_hosts", "describe_deployment_host", "list_deployments",
    # A shell on a registered host. Two gates, not one: BOSS_AGENT_HOST_SHELL must be on, and
    # the model cannot call it directly — it proposes, the operator clicks, and only the
    # accept-action path supplies the confirmation the tool requires.
    "read_pull_request",
    "run_on_host",
    "redeploy", "preflight_deployment_target",

    "cancel_code_task",
    # The first deployment. BOSS could create hosts and targets and could redeploy, and had
    # nothing that performs a deployment which never happened — so "deploy it" had no
    # executable action at all.
    "deploy_app",
    "cancel_job",
    "close_orphaned_runs",
    "consistency_report",
    "create_code_task",
    # Somewhere to deploy TO. Its absence sent BOSS to write_db with a hand-composed INSERT
    # against a table name it guessed wrong.
    "create_deployment_target",
    # A real debugging session — a shell, the repo, read-only SQL and the fleet — for the
    # questions a registry of fixed shapes cannot answer.
    "investigate",
    "get_investigation",
    "create_discovery",
    "decide",
    "describe_schema",
    "explain_run",
    "extend_code_task",
    "find_starved_work",
    "get_code_task",
    "get_config",
    "get_costs",
    "get_meeting",
    "get_run_activity",
    "get_task_preview",
    "git_history",
    "http_probe",
    "inspect_host",
    "inspect_self",
    "link_run_origin",
    "list_code_tasks",
    "list_decisions",
    "list_discovery",
    "list_github_connections",
    "list_meetings",
    "list_previews",
    "list_repositories",
    "list_running_work",
    "microvm_status",
    "project_status",
    "propose_action",
    "write_db",
    "query_db",
    "read_code",
    "list_source",
    "find_split_apps",
    "merge_apps",
    "generate_image",
    "describe_deployment_target",
    "retarget_app",
    "recall_role_experience",
    "describe_role",
    "read_logs",
    "register_repository",
    "repo_activity",
    "request_meeting",
    "retry_code_task",
    "search_code",
    "send_feedback",
    "set_job_priority",
    "start_preview",
    "steer_code_task",
    "stop_preview",
    "waive_gates",
    "watch_code_task",
}


def test_default_registry_shape():
    reg = build_default_registry()
    # The governed catalog has expanded since the original Phase 1 tool layer;
    # keep this explicit so accidental tool removal is visible in OSS CI.
    #
    # Counts are a tripwire, not a specification — and a bare `== 46` fails saying only
    # "47 == 46", which does not name the tool that appeared or vanished. Adding
    # `inspect_self` broke this and the message pointed at neither the tool nor the change.
    # The set difference below does say, so a legitimate addition is a one-line edit and an
    # accidental REMOVAL still fails loudly, which is what the tripwire is for.
    read = {s.name for s in reg.specs(kind="read")}
    write = {s.name for s in reg.specs(kind="write")}
    assert read | write == set(reg.names())
    assert len(reg.names()) == 75, (
        f"tool set changed: unexpected={sorted(set(reg.names()) - _EXPECTED_TOOLS)}, "
        f"missing={sorted(_EXPECTED_TOOLS - set(reg.names()))}")
    assert len(read) == 48
    assert len(write) == 27
    # the capabilities the user explicitly asked the agent to cover
    for name in (
        "project_status", "list_running_work", "get_task_preview",
        "microvm_status", "query_db", "create_code_task", "retry_code_task",
        "extend_code_task", "cancel_code_task", "waive_gates", "decide", "get_costs",
        "steer_code_task", "watch_code_task",
        # Investigation: the console is meant to answer any question about the running
        # system, not only the ones somebody wrote a report for. Data, code (BOSS's own
        # and the customer's), logs, live hosts, live HTTP, history, config.
        "describe_schema", "search_code", "read_code", "read_logs", "explain_run",
        "inspect_host", "http_probe", "git_history", "get_config",
        # Operations: BOSS running BOSS. Reading its own stuck queue and being unable to
        # touch it is not operating anything.
        "find_starved_work", "cancel_job", "set_job_priority", "close_orphaned_runs",
        "consistency_report",
        # The spec: what an app was supposed to do, and what it actually got.
        "read_spec",
    ):
        assert name in reg.names()


def test_schema_shapes():
    reg = build_default_registry()
    for s in reg.openai_schema():
        assert s["type"] == "function"
        assert set(s["function"]) >= {"name", "description", "parameters"}
    for s in reg.mcp_schema():
        assert set(s) >= {"name", "description", "inputSchema"}


def test_read_only_schema_excludes_writes():
    reg = build_default_registry()
    read_names = {s["name"] for s in reg.mcp_schema(kind="read")}
    assert "create_code_task" not in read_names
    assert "project_status" in read_names


def test_invoke_normalizes_result_and_defaults():
    reg = ToolRegistry()
    reg.register(ToolSpec("echo", "echo", {"type": "object"}, lambda ctx, **k: {"v": k["args"].get("x")}))
    out = reg.invoke("echo", None, project_id="p", actor="a", args={"x": 7})
    assert out == {"v": 7, "ok": True, "tool": "echo"}


def test_invoke_wraps_handler_exception():
    reg = ToolRegistry()

    def boom(ctx, **k):
        raise ValueError("nope")

    reg.register(ToolSpec("boom", "boom", {"type": "object"}, boom))
    out = reg.invoke("boom", None, project_id="p", actor="a", args={})
    assert out["ok"] is False and "ValueError" in out["error"]


def test_write_tools_fail_closed_without_a_durable_workspace():
    reg = ToolRegistry()
    called = False

    def write(_ctx, **_kwargs):
        nonlocal called
        called = True
        return {"created": True}

    reg.register(ToolSpec("create_work", "write", {"type": "object"}, write, kind="write"))
    ctx = type("CatalogOnlyContext", (), {"durable_workspace_configured": False})()

    out = reg.invoke("create_work", ctx, project_id="p", actor="a", args={})

    assert out["ok"] is False
    assert out["status"] == "blocked"
    assert out["error"] == "durable_workspace_required"
    assert "DATABASE_URL" in out["message"]
    assert called is False


def test_unknown_tool_raises():
    reg = ToolRegistry()
    with pytest.raises(ToolError):
        reg.get("missing")


def test_duplicate_registration_rejected():
    reg = ToolRegistry()
    reg.register(ToolSpec("t", "d", {"type": "object"}, lambda ctx, **k: {}))
    with pytest.raises(ValueError):
        reg.register(ToolSpec("t", "d", {"type": "object"}, lambda ctx, **k: {}))


def test_discuss_read_registry_exposes_everything_and_hides_project_override():
    """The discuss path is an operator console: it withholds no READ capability.

    This previously asserted the opposite — that `query_db` was hidden from chat. That
    fence existed for cross-tenant isolation, and in a single-tenant product it bought
    nothing while costing the console its ability to answer any question nobody had
    written a report for. When multi-tenant arrives the fence belongs in the database
    (Postgres RLS on the authorised project), not in a hand-maintained deny-list that
    would also have to stay right about two dozen other tools.

    What still holds, and is the real invariant here: no tool takes `project_id` from the
    model, so it cannot reach outside the project the API already authorised.
    """
    reg = build_discuss_read_registry()
    assert "query_db" in reg.names()
    assert "project_status" in reg.names()
    assert "get_code_task" in reg.names()
    assert "get_costs" in reg.names()
    # Every read tool, nothing withheld — asserted as "the same set the default registry has"
    # rather than a count, so adding a read tool cannot silently fail to reach a question.
    # That is the actual invariant: `inspect_self` was built for exactly this path, and a bare
    # count would have failed here while proving nothing about reachability.
    default_reads = {s.name for s in build_default_registry().specs(kind="read")}
    discuss_reads = {s.name for s in reg.specs(kind="read")}
    assert default_reads - discuss_reads == set(), (
        f"these read tools cannot be reached from a question: "
        f"{sorted(default_reads - discuss_reads)}")
    assert "inspect_self" in discuss_reads
    # …plus the operational writes. BOSS could describe its own stuck queue and not touch it;
    # an operator had to reach in from outside to cancel a job. The line is OPERATIONAL STATE
    # (a priority, a wedged job, an orphaned run) versus CREATING WORK that spends money.
    # Compared against the CONSTANT, not a copy of it: a hardcoded list here meant that
    # widening what BOSS may do required editing the policy in two places, and the test would
    # fail describing a difference it was itself creating.
    from boss_engine.tools.builtin import _DISCUSS_OPERATIONS_TOOLS
    assert {s.name for s in reg.specs(kind="write")} == set(_DISCUSS_OPERATIONS_TOOLS)
    # create_code_task IS reachable now, deliberately: a bug in a customer's app is repaired by
    # writing code, so withholding it meant BOSS could diagnose an app bug and had no way to fix
    # it. The only thing BOSS does not do is patch its OWN foundation, and that is enforced by
    # there being no tool for it plus an explicit instruction in the prompt — not by disarming
    # the tools that fix customer work.
    assert "create_code_task" in reg.names(), "BOSS must be able to write the fix"
    for schema in reg.openai_schema():
        props = schema["function"]["parameters"].get("properties") or {}
        assert "project_id" not in props, schema["function"]["name"]


def test_discuss_read_registry_drops_project_id_override_from_args():
    """Pinned discuss handlers must ignore a model-supplied project_id arg."""
    seen: dict = {}

    def spy(ctx, *, project_id, actor, args):
        seen["project_id"] = project_id
        seen["args"] = dict(args)
        return {"ok": True}

    def _pin(inner):
        def handler(ctx, *, project_id, actor, args):
            safe_args = dict(args or {})
            safe_args.pop("project_id", None)
            return inner(ctx, project_id=project_id, actor=actor, args=safe_args)

        return handler

    # Same pin helper shape as build_discuss_read_registry.
    wrapped = _pin(spy)
    wrapped(
        None,
        project_id="project-a",
        actor="a",
        args={"project_id": "project-b", "limit": 3},
    )
    assert seen["project_id"] == "project-a"
    assert "project_id" not in seen["args"]
    assert seen["args"]["limit"] == 3


def test_every_write_declares_what_pressing_it_costs():
    """A new destructive tool must not be able to arrive as an ordinary write.

    `cancel_job` was exactly that — a write like any other until it threw away twenty-seven
    minutes of a build to answer a question. The membership lived in a hand-maintained name set
    inside the agent, so anything added later was silently in neither set.

    The declaration is now on the tool. This test is what stops the default from quietly
    covering the next one: every write names its reversibility, and adding a write means
    choosing.
    """
    from boss_engine.tools.registry import Reversibility  # noqa: F401 — documents the values

    allowed = {"safe", "costs_money", "destroys_work", "irreversible"}
    reg = build_default_registry()
    undeclared = []
    for spec in reg.specs(kind="write"):
        assert spec.reversibility in allowed, f"{spec.name}: {spec.reversibility!r}"
        if spec.reversibility == "safe":
            undeclared.append(spec.name)
    # `safe` is a real answer AND the default, so it is the one that can be reached by
    # accident. Pinning the set means a new write landing on the default has to be added here
    # deliberately, in front of a reviewer.
    assert sorted(undeclared) == sorted(_SAFE_WRITES), (
        "a write is relying on the `safe` default. Declare its reversibility explicitly, then "
        f"add it here: {sorted(set(undeclared) - set(_SAFE_WRITES))}")


#: Writes whose effect can be undone or simply redone. These are what "fix it" legitimately
#: means, and they stay one-shot when the operator asks for them.
_SAFE_WRITES = [
    "close_orphaned_runs",
    # Read-only by default and one at a time per project. The FIXING variant is not a separate
    # tool but a separate permission: it returns requires_confirmation, so only an operator's
    # click gets a writable shell on a host.
    "investigate",
    # Creating a DESTINATION, not shipping to one. It writes a row that can be retired, and an
    # app that already has a target for the environment adopts it rather than growing a second.
    "create_deployment_target", "deploy_app", "link_run_origin", "mandate_standard", "merge_apps",
    "redeploy", "register_repository", "retarget_app", "send_feedback", "set_job_priority",
    "start_preview", "steer_code_task", "stop_preview",
]


def test_the_tools_that_are_never_pressed_by_the_model():
    """The other half: what the operator must click for themselves."""
    reg = build_default_registry()
    never = sorted(s.name for s in reg.specs(kind="write") if not s.self_executable)
    assert never == [
        "cancel_code_task",   # throws away a build in flight
        "cancel_job",         # the one that caused this
        "decide",             # approves a gate; an agent that approves its own gates is ungated
        "run_on_host",        # arbitrary command on a live machine
        "waive_gates",        # removes a check somebody put there
        "write_db",           # arbitrary mutation of any table
    ]
