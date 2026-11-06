"""The squad demo: four agents patch a CVE together, with a critique and a dissent (BOSSAPP-908).

NON-PRODUCTION SEED. It exists so the Agent Operations diagram (906) and the walkthrough video
have one rich, honest item to draw: everything below runs through the REAL squad machinery —
squads.assemble, the turn runner on the one agent pipeline, collab's consensus, the gate, the
ledger, squad memory, the panel and workflow plug-ins — with two stand-ins, both labelled:

    ScriptedSquadModel   answers each member's turn from a script (no model is called, nothing
                         is spent); every answer still passes the pipeline's contract, the
                         caller's check and the board's validation, exactly as a model's would
    DemoLaunchers        stands in for request_meeting / start_workflow: it writes a finished
                         panel and a finished run marked `demo`, so the results come back onto
                         the board through the real collect_results

Everything it writes is marked: the item's title starts "[demo]", its fingerprint is
`demo:squad-cve`, the run's input and the panel's constraints carry `demo: true`. It refuses
unless the caller passes `confirm_demo=True`, and it seats agents only from roles the project
has, unless `ensure_roles=True` adds the four it needs (spec.seeded_by = "squad_demo").

THE STORY (dependency CVE, task class security_patch, A2):
    osv signal → Chief of Staff opens the item → CVE squad seated (security lead, engineer, QA,
    adversarial critic) → findings and a hypothesis → lead proposes "bump only" → the critic
    OBJECTS → rejected, into tried-and-failed memory → the lead convenes a PANEL (deliberate) →
    the panel's verdict comes back on the board → lead proposes "bump + escape /emails" → two
    agree, the critic objects again (DISSENT, kept) → approved by maker-checker → handed to a
    build WORKFLOW with the packet (execute) → the run's PR comes back → lead proposes merge →
    squad votes 2–1 (dissent) → merge is always a person: NEEDS YOU, recommendation approve.
It stops at Needs you on purpose: the person's press is the operator's to make
(squad_turns.person_answered).
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable, Mapping, Union

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from . import board, read_model, squad_turns, squads
from ._common import new_id

Move = Union[Mapping[str, Any], Callable[[str], Mapping[str, Any]]]
_REF = re.compile(r'"((?:signal|workflow_run|meeting|pull_request|message):[^"\s]+)"')


class ScriptedSquadModel:
    """A stand-in model that answers each member's turn from its own script (by the `SEAT:` line
    every squad turn carries). A move is {"action", "arguments"?, "confidence"?, "reason"?};
    "$signal", "$meeting", "$workflow_run", "$pull_request" in an argument become the first ref
    of that kind the member was shown as citable. An exhausted script passes."""

    def __init__(self, script: Mapping[str, list[Move]]):
        self.script = {k: list(v) for k, v in script.items()}
        self.prompts: list[tuple[str, str]] = []

    def create_text_response(self, prompt, system_prompt=None, model=None):
        found = re.search(r"SEAT: (agent:[A-Za-z0-9_\-]+)", str(prompt))
        who = found.group(1) if found else "?"
        self.prompts.append((who, str(prompt)))
        queue = self.script.get(who) or []
        move = queue.pop(0) if queue else {"action": "pass"}
        if callable(move):
            move = move(str(prompt))
        refs = _REF.findall(str(prompt))

        def fill(value: Any) -> Any:
            if isinstance(value, str) and value.startswith("$"):
                kind = value[1:]
                return next((r for r in refs if r.startswith(kind + ":")), value)
            if isinstance(value, list):
                return [fill(v) for v in value]
            if isinstance(value, dict):
                return {k: fill(v) for k, v in value.items()}
            return value

        answer = {"action": move["action"], "confidence": move.get("confidence", 0.9),
                  "reason": move.get("reason", "scripted demo turn"), "question": "",
                  "arguments": fill(dict(move.get("arguments") or {}))}
        return "resp", json.dumps(answer), {"model": "scripted-squad-demo"}


class DemoLaunchers:
    """Stand-in launchers: a finished panel and a finished run, both marked demo."""

    def __init__(self, *, panel_verdict: str, pr: str):
        self.panel_verdict, self.pr = panel_verdict, pr
        self.convened: list[str] = []
        self.started: list[str] = []

    def convene(self, connection, *, project_id, actor, directive, seats, objective):
        meeting_id = new_id("mrun")
        connection.execute(text(
            "INSERT INTO meeting_runs (id, project_id, status, phase, directive, role_keys, "
            "constraints, state, title) VALUES (:id, :p, 'completed', 'done', :d, "
            "CAST(:r AS JSONB), CAST(:c AS JSONB), CAST(:s AS JSONB), :t)"),
            {"id": meeting_id, "p": project_id, "d": directive, "r": json.dumps(seats),
             "c": json.dumps({"demo": True, "convened_by": actor}),
             "s": json.dumps({"synthesis": self.panel_verdict}), "t": "[demo] squad panel"})
        self.convened.append(meeting_id)
        return {"meeting_run_id": meeting_id}

    def start(self, connection, *, project_id, actor, workflow_key, directive, title, inputs):
        definition = connection.execute(text(
            "SELECT id FROM workflow_definitions WHERE project_id = :p AND workflow_key = :k"),
            {"p": project_id, "k": "squad_demo_build"}).scalar()
        if definition is None:
            definition = f"wfd_demo_{uuid.uuid4().hex[:10]}"
            connection.execute(text(
                "INSERT INTO workflow_definitions (id, project_id, workflow_key, name, "
                "description, created_by) VALUES (:id, :p, 'squad_demo_build', "
                "'[demo] squad build', 'non-production seed for the squad demo', 'squad_demo')"),
                {"id": definition, "p": project_id})
        version = connection.execute(text(
            "SELECT id FROM workflow_versions WHERE workflow_definition_id = :d"),
            {"d": definition}).scalar()
        if version is None:
            version = f"wfv_demo_{uuid.uuid4().hex[:10]}"
            connection.execute(text(
                "INSERT INTO workflow_versions (id, project_id, workflow_definition_id, version, "
                "spec, content_hash, created_by) VALUES (:id, :p, :d, 1, '{}'::jsonb, :h, "
                "'squad_demo')"), {"id": version, "p": project_id, "d": definition,
                                   "h": uuid.uuid4().hex})
        run_id = new_id("wfr")
        connection.execute(text(
            "INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
            "workflow_version_id, actor, status, input, output, completed_at) VALUES (:id, :p, "
            ":d, :v, :a, 'completed', CAST(:i AS JSONB), CAST(:o AS JSONB), now())"),
            {"id": run_id, "p": project_id, "d": definition, "v": version, "a": actor,
             "i": json.dumps({**dict(inputs), "workflow_key": workflow_key, "demo": True},
                             default=str),
             "o": json.dumps({"pr_url": self.pr, "demo": True})})
        self.started.append(run_id)
        return {"run_id": run_id}


LEAD, ENGINEER, QA, CRITIC = ("agent:security_reviewer", "agent:software_engineer", "agent:qa",
                              "agent:adversarial_reviewer")
DEMO_ROLES = {"security_reviewer": "Security Reviewer", "software_engineer": "Software Engineer",
              "qa": "QA", "adversarial_reviewer": "Adversarial Reviewer"}
ADVISORY = "url:https://osv.dev/vulnerability/GHSA-35jh-r3h4-6jhm"


def _p(move: str, **arguments: Any) -> dict[str, Any]:
    return {"action": move, "arguments": arguments}


def demo_script() -> dict[str, list[Move]]:
    """Five rounds, one move per member per round, in seat order."""
    bump = "Bump lodash to 4.17.21 in a draft PR"
    both = "Bump lodash to 4.17.21 and escape user names in the /emails templates, with a test"
    return {
        LEAD: [
            _p("hypothesis", summary="A patch-level bump to lodash 4.17.21 closes "
               "CVE-2021-23337 with no API change", confidence=0.8,
               evidence=["$signal", ADVISORY]),
            _p("proposal", action="open_draft_pr", summary=bump,
               verify="CI green on the draft PR", workflow="squad_demo_build"),
            _p("proposal", action="convene_meeting",
               summary="Ask a panel: bump only, or bump plus escaping /emails in one PR?"),
            _p("proposal", action="open_draft_pr", summary=both,
               verify="CI green including the new /emails template test",
               workflow="squad_demo_build"),
            _p("proposal", action="merge", summary="Merge the draft PR once CI is green",
               verify="the advisory no longer matches the lockfile"),
        ],
        ENGINEER: [
            _p("finding", summary="Three call sites use _.template; /emails renders it with "
               "user-supplied names", evidence=["$signal", "log:grep _.template apps/web: 3 hits"]),
            _p("pass"),
            _p("pass"),
            _p("critique", position="agree",
               summary="Escaping at the one render call covers all three sites"),
            _p("vote", vote="approve", reason="CI is green and the change is small"),
        ],
        QA: [
            _p("finding", summary="Template tests cover two of the three call sites; /emails "
               "has none", evidence=["log:coverage report apps/web/emails: 0 tests"]),
            _p("critique", position="concern",
               summary="No test covers /emails, so a green CI proves little there"),
            _p("pass"),
            _p("critique", position="agree", summary="The new /emails test closes the gap"),
            _p("vote", vote="approve", reason="The new /emails test passes"),
        ],
        CRITIC: [
            _p("finding", summary="The CVE is reachable only through _.template with untrusted "
               "input", evidence=[ADVISORY]),
            _p("critique", position="object",
               summary="A bump alone leaves /emails rendering untrusted names; escape them too"),
            _p("pass"),
            _p("critique", position="object",
               summary="Two changes in one PR widen the blast radius; ship the bump first"),
            _p("vote", vote="reject",
               reason="I would still split the PR; the risk is the owner's call"),
        ],
    }


def ensure_demo_roles(connection: Connection, project_id: str) -> list[str]:
    """A SEED (Load demo on a demo project): adds each demo role the project lacks, never one an
    operator removed (seed_exclusions, kind `role`), never touching a role that exists."""
    from ..seed_exclusions import excluded

    removed = excluded(connection, project_id, "role")
    added = []
    for key, name in DEMO_ROLES.items():
        if key in removed:
            continue
        got = connection.execute(text(
            "INSERT INTO roles (id, project_id, role_key, display_name, spec) VALUES "
            "(:id, :p, :k, :n, CAST(:s AS JSONB)) ON CONFLICT (project_id, role_key) DO NOTHING "
            "RETURNING role_key"),
            {"id": f"role_demo_{uuid.uuid4().hex[:10]}", "p": project_id, "k": key, "n": name,
             "s": json.dumps({"seeded_by": "squad_demo"})}).scalar()
        if got:
            added.append(got)
    return added


def seed(bind: Union[Engine, Connection], settings: Any, *, project_id: str,
         confirm_demo: bool = False, ensure_roles: bool = False) -> dict[str, Any]:
    """Seed the demo item and run its squad to Needs you. Returns {item_id, graph, run}."""
    if not confirm_demo:
        raise PermissionError("the squad demo writes demo rows; pass confirm_demo=True")
    from .squad_turns import _tx
    from . import signals

    with _tx(bind) as c:
        if ensure_roles:
            ensure_demo_roles(c, project_id)
        sig = signals.record_signal(
            c, project_id=project_id, source="osv", kind="osv.vulnerability",
            title="[demo] lodash 4.17.20: prototype pollution (CVE-2021-23337)",
            fingerprint_value="demo:squad-cve", subject_kind="package",
            subject_id="lodash@4.17.20", app="web", severity="high",
            evidence=[{"kind": "url", "ref": ADVISORY.split(":", 1)[1]}],
            source_ref=f"demo:{uuid.uuid4().hex[:8]}")["signal"]
        opened = board.open_item(
            c, settings, project_id=project_id, fingerprint="demo:squad-cve",
            title="[demo] lodash CVE-2021-23337 in web", task_class="security_patch",
            subject_ref="package:lodash@4.17.20", app="web", signal_ids=[sig["id"]],
            severity="high", actor="agent:chief_of_staff")
        if opened["outcome"] != "opened":
            raise RuntimeError(f"the demo item could not open: {opened.get('reason') or opened['outcome']}")
        item_id = opened["item"]["id"]
        squads.assemble(c, item_id, template="dependency_cve")
    launchers = DemoLaunchers(
        panel_verdict="Bump and escape in one PR, with an /emails test (2 for, 1 against)",
        pr="demo/web#88")
    run = squad_turns.collaborate(bind, settings, item_id, rounds=6,
                                  model=ScriptedSquadModel(demo_script()), launchers=launchers)
    with _tx(bind) as c:
        graph = read_model.graph(c, item_id, project_id=project_id)
    return {"item_id": item_id, "graph": graph, "run": run}


__all__ = ["ScriptedSquadModel", "DemoLaunchers", "demo_script", "ensure_demo_roles", "seed"]
