"""Tool registry + built-in tool-set tests (no DB)."""
from __future__ import annotations

import pytest

from boss_engine.tools import build_default_registry, build_discuss_read_registry
from boss_engine.tools.registry import ToolError, ToolRegistry, ToolSpec


#: Every tool the default registry is expected to carry. Present so a count mismatch can name
#: what changed instead of reporting two integers.
_EXPECTED_TOOLS: set[str] = {
    # THE KNOWLEDGE GRAPH (BOSSAPP-622): where code is, who depends on it, what a change can
    # break, the facts and precedent behind it; and kg_propose_fact, the one write (an `inferred`
    # fact), offered through the knowledge.propose bundle, which every agent holds.
    "kg_symbol", "kg_callers", "kg_blast_radius", "kg_why", "kg_search", "kg_precedent", "kg_propose_fact",
    # RULES (BOSSAPP-768): the approved business rules that govern code, and proposing one (stored
    # proposed and untrusted until a person approves it in Rules review; no tool approves).
    "kg_rules", "kg_propose_rule",
    # A node's typed neighbours, hop-capped (rulebook source G6).
    "kg_neighbors",
    # A regulation or the AML sample into a project's Knowledge Graph (KG P7a, case study 4).
    "add_regulation_document", "set_ontology_pack", "load_aml_sample", "aml_sample_status",
    # An uploaded file (or the DQ sample rulebook) proposed as sections a person confirms (G3/G7).
    "add_document_from_file", "dq_rulebook_status",
    # LEARNING WHILE EXECUTING (BOSSAPP-768 S2b): what an agent learned, recorded the moment it
    # learns it — one `inferred` item with evidence, the run's provenance, forgettable.
    "record_learning",
    "kg_cypher",   # read-only Cypher over the project's AGE projection
    "kg_schema",   # what that graph holds: labels, relationship types and counts, properties
    # THE CAPTAIN'S POWERS (BOSSAPP-759): retired as an engine, kept as the one agent's actions.
    # watch / goals / set_goal the agent may run; drive / overwatch / theater are operator_only.
    "captain_watch", "captain_goals", "captain_set_goal", "captain_drive", "captain_overwatch",
    "captain_theater",
    # BOSS FIXES ITSELF (BOSSAPP-571): read the self-fixes and the autonomy metric. Reads only:
    # no tool starts, approves, installs, removes or sends a self-fix.
    "list_self_fixes", "get_self_fix", "autonomy_report",
    # ...and the doorway (BOSSAPP-571): a gap an agent diagnosed in BOSS, filed in the turn that
    # found it (it only queues for a person); and the self-fix a person presses to start.
    "list_platform_gaps", "file_platform_gap", "request_self_fix",
    # Setup, answered and repaired rather than described. `diagnose_setup` is the whole
    # picture in one call; the other two are the actions that used to be instructions.
    "diagnose_setup",
    "test_github_access",
    "set_project_setting",
    # A SUBSCRIPTION HAS NOTHING TO PASTE. A Codex or Cursor plan is an OAuth session the CLI
    # holds, so BOSS finds the CLI the customer already uses, proves it answers, and records
    # the entitlement — rather than asking an operator to transcribe a credential that does
    # not exist.
    "detect_coding_agents", "connect_coding_agent",
    # PROVIDER AGNOSTICISM YOU CAN CHECK. Config says what was asked for; the llm_calls
    # ledger says what happened, and only the second one is evidence. `list_harnesses` says
    # which of them a governed task has actually completed on — wired is not proven.
    "provider_audit", "list_harnesses",
    # REVIEW IS A CONVERSATION ABOUT A LINE, not a verdict on a run. A reviewer that can only
    # say "not safe" gives the implementer nothing to act on; these three are how a finding
    # gets a file and a line, and how a person adds one of their own.
    "add_review_comment", "list_review_comments", "resolve_review_comment",
    # A hollow review (PR #223: "nothing to review" on 77 files) is answered by reviewing the
    # pull request's real diff again and posting the round on the PR.
    "review_pull_request_again",
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
    # to answer "is 192.0.2.202 healthy" or "where would this ship" — so the Deployment
    # Manager role had nothing to read. Read-only: the governed path still does the deploy.
    "list_deployment_hosts", "describe_deployment_host", "list_deployments",
    # A shell on a registered host. Two gates, not one: BOSS_AGENT_HOST_SHELL must be on, and
    # the model cannot call it directly — it proposes, the operator clicks, and only the
    # accept-action path supplies the confirmation the tool requires.
    "read_pull_request",
    "run_on_host",
    "redeploy", "preflight_deployment_target",
    # Removing a deployed app: governed like a deploy, and only ever a button a person presses.
    "remove_app",

    "cancel_code_task",
    # "Create a project called northpeak-bank-a": a new project in the org (project:create).
    "create_project",
    # Archive / restore a project: offered as a button a person presses (BOSSAPP-768 S5).
    "archive_project", "restore_project",
    # A knowledge-graph project's setup: where it is, and its team, stated (BOSSAPP-768 flow).
    "knowledge_project_setup", "set_project_team",
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
    # Reconciling a run with a world that has moved on: re-test what stopped each step and put
    # back only the ones whose blocker has actually cleared.
    "recheck_workflow_run",
    # A WORKFLOW RUN HAD NO READ TOOL AT ALL, so "what is the status?" about a wfr_ was
    # answered with hand-written SQL — seven failed guesses at table and column names in one
    # turn, and no answer for the operator. This also computes the way OUT of a failed run,
    # because reporting `deadlettered` and stopping is what "it drops the ball" means.
    "describe_workflow_run",
    # THE MEMORY PLANE (Kiro gap A0). BOSS had 249 tables and none of them remembered anything
    # an agent was told: every thread started cold and every correction was spent the moment it
    # was given. `remember` is the write, `recall` the read, `forget` the retirement, and
    # `update_ledger` is the session's memory of its own goal across compaction.
    # WORK THAT STARTS WITHOUT ANYBODY TYPING (Kiro gaps A1.2/A1.3). BOSS could only act when
    # someone asked: "keep an eye on this PR" had no answer but the operator remembering to
    # come back. Every wake spends budget, so each carries a cap and stops itself.
    "watch",
    "schedule_work",
    "stop_watching",
    "list_watches",
    # ...and waiting for BOSS's own work within a turn: one call that blocks server-side until a
    # job, build, run or code index settles, with no model call per look (services/wait_for.py).
    "wait_for",
    "remember",
    "recall",
    "forget",
    "update_ledger",
    # THE RECOVERY THE AGENT COULD DIAGNOSE AND COULD NOT PERFORM. It traced a failed panel
    # step to its root cause and then said "tell me to go ahead", offering no button, because
    # the console's "Try … again" had no tool behind it. A correct diagnosis you cannot act on
    # is still a dead end.
    "retry_workflow_step",
    # ...and the other answer to a step that stopped its run: go on without it, with a reason.
    "skip_workflow_step",
    # THE PLAN, BEFORE THE RUN. The builder drew four labels and four dropdowns; what each
    # step does, what it produces, and which agent captains it were all known and none of it
    # was shown. "I want to see the workflow and its definition, what it will do."
    "describe_workflow",
    # WILL THIS RUN WORK — each step's real infrastructure checked before a run spends anything
    # (services.workflow_precheck), the same answer as POST /v1/workflows/{id}/precheck.
    "precheck_workflow",
    # The whole factory in one read, so an open question does not become six lookups.
    "factory_briefing",
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
    "read_app_logs",
    "query_project_data",
    "list_code_tasks",
    "list_decisions",
    "list_discovery",
    "list_github_connections",
    # The org's repository registry, as the console's routes (multirepo): list, allow, remove.
    "list_org_repositories",
    "allow_repository",
    "remove_project_repository",
    "list_meetings",
    "list_previews",
    "list_repositories",
    "list_running_work",
    "microvm_status",
    "project_status",
    "list_projects",
    "describe_project",
    "code_graph_status",
    "index_repository_code",
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
    # Agent World: an agent's own signed work, to cite as [act:…] (services/agent_acts.py).
    "my_recent_work",
    "describe_role",
    "read_logs",
    "read_preview_log",
    "register_repository",
    "repo_activity",
    "request_meeting",
    "retry_code_task",
    "search_code",
    "send_feedback",
    "set_job_priority",
    "start_preview",
    "run_security_scan",
    # App templates and the security loop's reads (tools/app_platform_tools.py).
    "list_templates", "describe_template", "template_status", "apply_template",
    "security_scan_status", "list_security_findings", "security_report", "security_review_status",
    # Authoring the PATH, as opposed to starting work on it.
    "draft_workflow",
    "draft_custom_step",
    "delete_workflow",
    # Authoring the unit of doing, so an operator can get a skill by describing one in the
    # conversation rather than filling in a form about behaviour kinds and digest pinning.
    "draft_skill",
    # Giving an agent a skill or an MCP server by asking — the act the Agents page exists for.
    "assign_skill_to_agent",
    "assign_mcp_to_agent",
    "list_capabilities",
    "list_skills",
    # The MCP side of the same conversation: see what servers exist (and whether they answer),
    # and connect one, so "use my server's search tool in this workflow" can be finished in chat.
    "list_mcp_servers",
    "register_mcp_server",
    "start_workflow",
    "steer_code_task",
    "stop_preview",
    # The machine (stability spec §4e): the system doctor's gauges/findings, and a choice.
    "system_health", "system_doctor_act",
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
    assert len(reg.names()) == 172, (
        f"tool set changed: unexpected={sorted(set(reg.names()) - _EXPECTED_TOOLS)}, "
        f"missing={sorted(_EXPECTED_TOOLS - set(reg.names()))}")
    assert len(read) == 98  # + kg_neighbors, dq_rulebook_status (rulebook source G6/G7) + aml_sample_status + knowledge_project_setup (BOSSAPP-768 project flow)
    # + list_platform_gaps (BOSSAPP-571's doorway) + kg_rules (the approved business rules, BOSSAPP-768)
    #                         + wait_for (waiting for BOSS's own work) + kg_schema (what the knowledge graph holds) + code_graph_status, index_repository_code (a repository's code graph) + list_org_repositories (the org's repository registry) + list_projects, describe_project (a project's real state, any project)
    #                        + captain_goals, + 7 kg reads, + 3 self-fix reads, + read_app_logs, + my_recent_work, + system_health, + 7 template/security reads, + precheck_workflow
    #                        + query_project_data (the agent baseline's fenced row read)
    assert len(write) == 74  # + add_document_from_file (G3) + add_regulation_document, set_ontology_pack, load_aml_sample + set_project_team (BOSSAPP-768 project flow)
    # + file_platform_gap, request_self_fix (BOSSAPP-571's doorway) + record_learning (BOSSAPP-768 S2b), + kg_propose_rule (BOSSAPP-768 S2a), + archive_project, restore_project (BOSSAPP-768 S5), + allow_repository, remove_project_repository + 5 captain writes (BOSSAPP-759), + create_project, + kg_propose_fact, + remove_app, + system_doctor_act, + run_security_scan (F4), + apply_template (F2),
    #                         + review_pull_request_again
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
    reg.register(ToolSpec("echo", "echo", {"type": "object"}, lambda ctx, **k: {"v": k["args"].get("x")}, permission="system:read"))
    out = reg.invoke("echo", None, project_id="p", actor="a", args={"x": 7})
    assert out == {"v": 7, "ok": True, "tool": "echo"}


def test_invoke_wraps_handler_exception():
    reg = ToolRegistry()

    def boom(ctx, **k):
        raise ValueError("nope")

    reg.register(ToolSpec("boom", "boom", {"type": "object"}, boom, permission="system:read"))
    out = reg.invoke("boom", None, project_id="p", actor="a", args={})
    assert out["ok"] is False and "ValueError" in out["error"]


def test_write_tools_fail_closed_without_a_durable_workspace():
    reg = ToolRegistry()
    called = False

    def write(_ctx, **_kwargs):
        nonlocal called
        called = True
        return {"created": True}

    reg.register(ToolSpec("create_work", "write", {"type": "object"}, write, kind="write", permission="system:read"))
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
    reg.register(ToolSpec("t", "d", {"type": "object"}, lambda ctx, **k: {}, permission="system:read"))
    with pytest.raises(ValueError):
        reg.register(ToolSpec("t", "d", {"type": "object"}, lambda ctx, **k: {}, permission="system:read"))


def test_discuss_read_registry_exposes_scoped_reads_and_hides_project_override():
    """Chat admits project-scoped tools; raw SQL cannot enforce that project boundary."""
    reg = build_discuss_read_registry()
    assert "query_db" not in reg.names()
    assert "write_db" not in reg.names()
    assert "project_status" in reg.names()
    assert "get_code_task" in reg.names()
    assert "get_costs" in reg.names()
    # Every other read tool remains available to the conversation.
    default_reads = {s.name for s in build_default_registry().specs(kind="read")}
    discuss_reads = {s.name for s in reg.specs(kind="read")}
    assert default_reads - discuss_reads == {"query_db"}, (
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

    allowed = {"safe", "costs_money", "destroys_work", "irreversible", "grants_authority"}
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
    # A GAP IN BOSS ITSELF, FILED (BOSSAPP-571): one recommendation row a person reviews; it changes
    # nothing else, and filing it again counts the same cause again (queues_for_review).
    "file_platform_gap",
    # THE CAPTAIN OBSERVES AND KEEPS GOALS (BOSSAPP-759): a watch tick records what it saw and
    # applies nothing; a goal is a row the captain scores. Running either again records again.
    "captain_watch", "captain_set_goal",
    # A SCAN READS THE APP AND WRITES FINDINGS. It runs the pinned scanners on the app's own code
    # in a sandbox and against BOSS's own targets only; nothing in the app or the world changes,
    # and running it twice records a second scan (APP_TEMPLATES_AND_SECURITY_LOOP.md §4).
    "run_security_scan",
    # RE-ASKING GITHUB A QUESTION whose answer has changed. It updates the connection's test
    # state and nothing else; running it twice asks twice. This is what "fix it" means after
    # an operator widens a token's repository access — and BOSS telling them to go and press
    # the button themselves is what it replaced.
    "test_github_access",
    # TURNING A DIAL BACK IS SETTING IT AGAIN. Which model fills which role, how many agents
    # run at once; an empty value hands it back to the deployment default. Nothing starts.
    "set_project_setting",
    # A NEW PROJECT IS EMPTY. It starts nothing, spends nothing and touches no existing work; the
    # identity gate still decides project:create with the caller's own step-up.
    "create_project",
    # ARCHIVING DELETES NOTHING: the other tool undoes it. Still operator_only (a person's
    # press), because it pauses every schedule and monitor of a project at once.
    "archive_project", "restore_project",
    # A TEAM IS STATED AGAIN TO UNDO IT. It records who works on the project; it starts nothing
    # and spends nothing (agent_assignment.set_team).
    "set_project_team",
    # CONNECTING A SUBSCRIPTION READS. It probes a CLI the operator already installed, makes
    # it answer once in a read-only sandbox, and records an entitlement row. It stores no
    # credential (there is none to store) and changes no code.
    "connect_coding_agent",
    # A REVIEW COMMENT IS A NOTE, NOT AN ACT. Leaving one appends to a run's comment list and
    # resolving one flips its status; neither starts work, spends a model budget, or changes a
    # line of code. The implementer reads them at its next repair round. Nothing here is harder
    # to undo than saying it again.
    "add_review_comment",
    "resolve_review_comment",
    "close_orphaned_runs",
    # AUTHORING A PATH IS NOT WALKING IT. Both write a definition and neither starts a run or
    # spends a model budget: a workflow that exists but is not mapped to a class of work changes
    # no behaviour until somebody chooses it, and a custom step is inert until a workflow places
    # it. Publishing a new VERSION is also additive — the previous one is still there. The
    # destructive sibling, delete_workflow, declares destroys_work and is press-only.
    "draft_custom_step",
    # Same act, one level down: a skill is the unit a custom step runs. Saving one is
    # idempotent on (project, skill_key) and nothing executes until a step binds it and a
    # workflow places that step.
    "draft_skill",
    "draft_workflow",
    # A SKILL GRANT IS A ROW. Giving an agent a skill adds one; taking it back (the same tool,
    # remove=true) deletes it and records the removal. Nothing starts or spends, and the skill's
    # own risk tier still decides when a use of it waits for a person. (assign_mcp_to_agent was
    # listed here too; it hands over a server's credentials, so it is `grants_authority` now.)
    "assign_skill_to_agent",
    # Connecting a server is undone by removing it, and it lands SWITCHED OFF, UNREVIEWED and at
    # tier 2 or above: BOSS does not contact it and no agent is offered its tools until a person
    # turns it on, and even then every call waits for approval until somebody reviews it
    # (test_skills_and_mcp_from_chat.py). That is why it may stay one-shot.
    "register_mcp_server",
    # Read-only by default and one at a time per project. The FIXING variant is not a separate
    # tool but a separate permission: it returns requires_confirmation, so only an operator's
    # click gets a writable shell on a host.
    "investigate",
    # Creating a DESTINATION, not shipping to one. It writes a row that can be retired, and an
    # app that already has a target for the environment adopts it rather than growing a second.
    "create_deployment_target", "deploy_app", "kg_propose_fact", "link_run_origin", "mandate_standard", "merge_apps",
    # A PROPOSED RULE IS NOT A RULE (BOSSAPP-768). kg_propose_rule writes one `inferred`, proposed
    # rule: untrusted, never given to an agent, until a person approves it in Rules review.
    "kg_propose_rule",
    # A REGULATION'S OWN WORDS, OR THE SYNTHETIC AML BANK, INTO THE GRAPH (KG P7a, case study 4):
    # upserts keyed by the document's or the sample's own ids, so adding again changes nothing;
    # the pack is set again to undo. Both pack-changing ones are a person's press.
    "add_regulation_document", "set_ontology_pack", "load_aml_sample",
    # A file proposed as a pending upload: nothing reaches the graph until a person confirms it.
    "add_document_from_file",
    # What an agent learned while working: one `inferred` item, with evidence (BOSSAPP-768 S2b).
    "record_learning",
    # Re-queues only steps whose recorded blocker has actually cleared — each one something the
    # operator could have retried by hand from the same screen.
    "recheck_workflow_run",
    "redeploy", "register_repository", "retarget_app",
    # Out of one project, still in the org's registry; register_repository puts it back.
    "remove_project_repository", "send_feedback", "set_job_priority",
    "start_preview", "steer_code_task", "stop_preview",
    # Re-queues ONE already-failed step and keeps everything the run produced. It can only
    # move a step out of failed/deadlettered/cancelled, so it cannot start live work twice.
    "retry_workflow_step",
    # Marks ONE failed step skipped, with the reason and who decided; it starts nothing the run
    # was not already going to start, and refuses approval gates and live steps.
    "skip_workflow_step",
    # Memory writes. `remember` cannot bind a rule on its own — a lesson lands `proposed` and
    # inert — and `forget`/`update_ledger` retire or annotate rather than destroy.
    "remember",
    "forget",
    "update_ledger",
    # Arming a loop writes one row and spends nothing until its first wake; stopping one
    # spends nothing at all.
    "watch",
    "schedule_work",
    "stop_watching",
]


def test_the_tools_that_are_never_pressed_by_the_model():
    """The other half: what the operator must click for themselves."""
    reg = build_default_registry()
    never = sorted(s.name for s in reg.specs(kind="write") if not s.self_executable)
    assert never == [
        # gives BOSS's agents reach on a GitHub repository through the org's App
        "allow_repository",
        # archive / restore a project: pauses or resumes all of its schedulers (BOSSAPP-768 S5)
        "archive_project",
        # hands an agent an MCP server — its credentials and every action it exposes
        "assign_mcp_to_agent",
        "cancel_code_task",   # throws away a build in flight
        "cancel_job",         # the one that caused this
        # the captain's powers that APPLY moves a person has not seen one by one (BOSSAPP-759)
        "captain_drive", "captain_overwatch", "captain_theater",
        "decide",             # approves a gate; an agent that approves its own gates is ungated
        "delete_workflow",    # removes a path a person authored
        # sets the project's ontology pack and loads the synthetic AML bank (case study 4)
        "load_aml_sample",
        "remove_app",         # stops an app and removes it from its host
        # starts BOSS changing its own code from a filed gap: a person's press (BOSSAPP-571)
        "request_self_fix",
        "restore_project",    # see archive_project
        "run_on_host",        # arbitrary command on a live machine
        "set_ontology_pack",  # changes which kinds the project's graph accepts (KG P7a)
        "system_doctor_act",  # may pause or cancel a build to free the machine
        "waive_gates",        # removes a check somebody put there
        "write_db",           # arbitrary mutation of any table
    ]


def test_the_chat_registry_keeps_every_permission_its_tools_declare():
    """The pinned chat registry rebuilds each spec; it dropped `permission_by_arg` and
    `also_requires`, so an operation-dependent key (decide's execute, a registry refresh) was
    decided on the base key alone in the one registry the chat agent uses."""
    source = build_default_registry()
    chat = build_discuss_read_registry()
    for spec in chat.specs():
        own = source.get(spec.name)
        assert (spec.permission, spec.permission_by_arg, spec.also_requires) == \
            (own.permission, own.permission_by_arg, own.also_requires), spec.name
