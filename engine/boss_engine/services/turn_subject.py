"""What an "Ask about this" turn is ABOUT, resolved into the evidence BOSS already holds.

THE INCIDENT (boss-1, 2026-10-02, workflow run wfr_9c78b289633348c68246).
-------------------------------------------------------------------------
A Preview step failed with "app did not serve HTTP within timeout. error: ImportError: attempted
relative import with no known parent package … lead_triage_core/approvals.py". The operator
pressed "Ask the Qa". The console folded the step into PROSE — its error cut to 400 characters,
mid-word, plus a stapled-on "Check the app's code and the host before answering." — and scoped
the turn to the BUILD (`crun_…`), because a code-task run was the only kind of thing a turn
could be scoped to. Nothing that identified the step travelled: not the workflow run, not the
step id, not the preview id, not the log path.

So the server told the model "THIS CONVERSATION IS ABOUT ONE APP … Status: completed … Call
read_spec FIRST"; the word "host" in the boilerplate tripped the deploy keyword gate; and after
276 seconds of deployment lookups the answer was "Lead Triage Inbox has never been deployed".
The true answer had been in the question the whole time: a package module was launched as a
script, and the fix is to run the app's real entry point (or `python -m`).

THE DESIGN, FOR THE WHOLE CLASS.
--------------------------------
A turn about a thing carries a typed reference to that thing — `(kind, id[, step])` — and the
operator's literal question. Nothing else. This module turns the reference into the record:
complete, untruncated, structured, with the linked objects (step → preview → build → PR) and
the tools that would verify each point the record leaves open. The router puts that block FIRST
in the model's context, under a contract that says: answer the question from this evidence,
call tools only to settle what it leaves open, stay on this subject.

ONE REGISTRY. Every kind the console can send has exactly one resolver here, and
`engine/tests/test_an_ask_about_this_is_answered_from_its_record.py` reads the kinds out of the
console's own source (`apps/web/lib/dock-subject.ts`) and fails if any of them has none. A new
door with a new kind cannot ship pointing at nothing.

The reference travels as the thread's `external_ref` — `workflow_step:wfr_…:preview` — so the
conversation about a step is keyed on that step, and no request body had to grow a field. A
bare `crun_…` (every code-task thread that predates this) still reads as a code_task subject.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy import text

_log = logging.getLogger(__name__)

#: Every kind of thing a console door can ask about. MIRRORED, deliberately and checked, by
#: `SUBJECT_KINDS` in apps/web/lib/dock-subject.ts — the enforcing test compares the two.
SUBJECT_KINDS: tuple[str, ...] = (
    "workflow_step",
    "workflow_run",
    "code_task",
    "preview",
    "discovery",
    "deployment",
    "decision",
)

#: An id prefix is enough to know the kind of an untyped legacy reference.
_PREFIX_KIND = (
    ("crun_", "code_task"),
    ("wfr_", "workflow_run"),
    ("prev_", "preview"),
    ("drun_", "discovery"),
    ("deprun_", "deployment"),
    ("deptgt_", "deployment"),
    ("dept_", "deployment"),
    ("dec_", "decision"),
)

#: How much of any one recorded text goes in. Generous on purpose: the incident's error was cut
#: at 400 characters, mid-word, and the cut fell exactly where the cause was.
_TEXT_CAP = 6000
_LOG_TAIL_CHARS = 4000


@dataclass(frozen=True)
class SubjectRef:
    kind: str
    id: str
    step: Optional[str] = None

    @property
    def external_ref(self) -> str:
        """The thread key for a conversation about this thing.

        A code task keeps its bare run id: every thread about a build since threads existed is
        keyed that way, and re-keying would orphan their history.
        """
        if self.kind == "code_task":
            return self.id
        return f"{self.kind}:{self.id}" + (f":{self.step}" if self.step else "")

    @property
    def label(self) -> str:
        noun = self.kind.replace("_", " ")
        return f"{noun} {self.id}" + (f" / {self.step}" if self.step else "")


def parse_subject(raw: Any) -> Optional[SubjectRef]:
    """A subject from a thread's external_ref (or a {kind, id, step} dict), or None.

    `role:<name>` refs and anything unrecognised are not subjects — a general or role
    conversation must not be given an invented object to talk about.
    """
    if isinstance(raw, dict):
        kind = str(raw.get("kind") or "").strip()
        ident = str(raw.get("id") or "").strip()
        step = str(raw.get("step") or "").strip() or None
        if kind in SUBJECT_KINDS and ident:
            return SubjectRef(kind, ident, step if kind == "workflow_step" else None)
        return None
    value = str(raw or "").strip()
    if not value:
        return None
    head, sep, rest = value.partition(":")
    if sep and head in SUBJECT_KINDS:
        ident, _, step = rest.partition(":")
        ident = ident.strip()
        if not ident:
            return None
        step = step.strip() or None
        if head == "workflow_step" and not step:
            # A step with no step id is the run.
            return SubjectRef("workflow_run", ident)
        return SubjectRef(head, ident, step if head == "workflow_step" else None)
    if sep:
        return None   # role:… and any other namespace
    for prefix, kind in _PREFIX_KIND:
        if value.startswith(prefix):
            return SubjectRef(kind, value)
    return None


@dataclass
class SubjectEvidence:
    """The record of one thing, as BOSS holds it — what the model reads before anything else."""

    ref: SubjectRef
    found: bool = False
    title: str = ""
    status: str = ""
    #: The recorded failure, whole. First in the rendered block, because it is usually the
    #: answer.
    error: str = ""
    facts: dict[str, Any] = field(default_factory=dict)
    log_tail: str = ""
    #: Other records this one points at, already resolved: "preview prev_… — failed …".
    linked: list[str] = field(default_factory=list)
    #: Observations derived from the record that the record's own labels get wrong.
    findings: list[str] = field(default_factory=list)
    #: What the record leaves open, each with the tool that settles it.
    open_points: list[tuple[str, str]] = field(default_factory=list)

    def render(self, question: str = "") -> str:
        """The SUBJECT block. Error first; the operator's words above it."""
        lines = [f"SUBJECT OF THIS TURN: {self.ref.label}"]
        if question:
            lines.append(f'THE OPERATOR ASKED: "{question.strip()}"')
        if not self.found:
            lines.append(
                f"No record of {self.ref.label} was found in this project. Say so in your "
                "first sentence; do not substitute a different object for it.")
        if self.error:
            lines.append("RECORDED ERROR (verbatim, untruncated):\n" + self.error)
        if self.findings:
            lines.append("WHAT THE RECORD SHOWS:\n" + "\n".join(f"- {f}" for f in self.findings))
        head = [f"Kind: {self.ref.kind}", f"Id: {self.ref.id}"]
        if self.ref.step:
            head.append(f"Step: {self.ref.step}")
        if self.title:
            head.append(f"Title: {self.title}")
        if self.status:
            head.append(f"Status: {self.status}")
        lines.append("\n".join(head))
        if self.facts:
            lines.append("RECORDED FACTS:\n" + "\n".join(
                f"- {k}: {_short(v)}" for k, v in self.facts.items()
                if v not in (None, "", [], {})))
        if self.log_tail:
            lines.append("LOG TAIL:\n" + self.log_tail)
        if self.linked:
            lines.append("LINKED RECORDS:\n" + "\n".join(f"- {x}" for x in self.linked))
        if self.open_points:
            lines.append("OPEN POINTS, AND THE TOOL THAT SETTLES EACH:\n" + "\n".join(
                f"- {point} -> {tool}" for point, tool in self.open_points))
        return "\n\n".join(lines)


def answer_contract(ref: SubjectRef) -> str:
    """The rules for a turn about one thing. Short, because it sits above a long persona."""
    return (
        "HOW TO ANSWER A QUESTION ABOUT THIS SUBJECT — these rules outrank everything below.\n"
        "1. Your FIRST SENTENCE answers the operator's question above, from the SUBJECT "
        "evidence. If the evidence already holds the cause, say it — that IS a grounded "
        "answer, and it needs no further lookups to be one.\n"
        "2. Call a tool only to settle a point the evidence leaves open, and say which point "
        "it settled. The OPEN POINTS list names the tool for each.\n"
        f"3. Stay on {ref.label}. Do not answer about a different object — a deployment, "
        "another run, the host in general — unless the evidence above links to it.\n"
        "4. If you could not check something, say what and why, in one sentence. Do not keep "
        "looking past the turn's budget to avoid saying it."
    )


def subject_context(evidence: SubjectEvidence, question: str) -> str:
    """The block that goes FIRST in the system prompt: the record, then the contract."""
    return evidence.render(question) + "\n\n" + answer_contract(evidence.ref)


def stays_on_subject(reply: str, evidence: SubjectEvidence) -> bool:
    """Whether a replacement answer is still about the subject.

    A re-ask may refine an answer; it may not swap it for one about another object. In the
    incident the continuation's reply — "has never been deployed … list_deployment_hosts
    returns zero hosts" — named neither the step, the preview nor the error, and REPLACED an
    answer drawn from the evidence. The test is objective: does the reply mention anything that
    identifies this subject (its ids, its title, a distinctive fragment of its error)?
    """
    low = (reply or "").lower()
    if not low.strip():
        return False
    marks = {evidence.ref.id.lower()}
    if evidence.ref.step:
        marks.add(evidence.ref.step.lower())
    # The title only where it IS the object's name. A step's or a preview's title is the APP's
    # — and the incident's off-subject answer named the app ("Lead Triage Inbox has never been
    # deployed"), so counting it would have waved that answer through.
    if evidence.title and len(evidence.title) >= 4 and evidence.ref.kind in {
            "decision", "discovery", "deployment"}:
        marks.add(evidence.title.lower())
    for value in evidence.facts.values():
        if isinstance(value, str) and re.match(r"^[a-z]+_[0-9a-z]{6,}$", value):
            marks.add(value.lower())
    for word in re.findall(r"[A-Za-z][A-Za-z_./]{5,}", evidence.error or "")[:40]:
        if any(c in word for c in "._/") or word[0].isupper():
            marks.add(word.lower())
    return any(m and m in low for m in marks)


# --------------------------------------------------------------------------------------------
# Resolvers — one per kind.
# --------------------------------------------------------------------------------------------

def _rows(connection, sql: str, params: dict) -> list[dict]:
    """A read that cannot poison the turn's transaction.

    The turn runs inside one transaction, and on Postgres a failed statement aborts it — so a
    resolver tripping over a missing column would take the whole answer down with it. Each
    read gets its own savepoint and degrades to nothing.
    """
    if connection is None:
        return []
    try:
        with connection.begin_nested():
            return [dict(r) for r in connection.execute(text(sql), params).mappings().all()]
    except Exception as exc:  # noqa: BLE001 — evidence is an aid; a missing table is "not found"
        _log.info("subject read failed (%s): %s", type(exc).__name__, exc)
        return []


def _json(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "ignore")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _short(value: Any, cap: int = 600) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, default=str)
    out = str(value)
    return out if len(out) <= cap else out[:cap] + f"… ({len(out)} chars)"


def _cap(value: Any) -> str:
    out = str(value or "").strip()
    return out if len(out) <= _TEXT_CAP else out[-_TEXT_CAP:]


def _log_tail(path: Any) -> str:
    if not path:
        return ""
    try:
        return Path(str(path)).read_text(errors="ignore")[-_LOG_TAIL_CHARS:].strip()
    except OSError:
        return ""


#: The last line of a Python or Node traceback: the exception that actually ended the process.
_EXCEPTION_LINE = re.compile(
    r"\b((?:[A-Z][A-Za-z]*(?:Error|Exception))|SyntaxError|ModuleNotFoundError|"
    r"ImportError|KeyboardInterrupt|SystemExit)\b:?\s*([^\n]{0,240})")


def _crash_findings(*texts: str, failure_kind: str = "") -> list[str]:
    """What the recorded text says that the recorded LABEL does not.

    The incident's preview row said failure_kind='port_timeout' and suggested "ensure HOST/PORT
    are honored" — while its own log held an ImportError raised before anything listened. A
    process that crashed on start is not a port problem, and an agent reading the label is sent
    to look at ports and hosts. The exception in the text outranks the label.
    """
    blob = "\n".join(t for t in texts if t)
    match = None
    for match in _EXCEPTION_LINE.finditer(blob):
        pass
    if match is None:
        return []
    exc, detail = match.group(1), match.group(2).strip()
    out = [f"The process raised {exc}" + (f": {detail}" if detail else "")
           + " — it crashed before serving, so this is a start-up failure, not a slow start."]
    if failure_kind in {"port_timeout", "timeout"}:
        out.append(f"failure_kind '{failure_kind}' is the checker's label for 'nothing "
                   "answered in time'; the exception above is why nothing answered.")
    return out


def _code_task_line(connection, project_id: str, run_id: str) -> tuple[str, dict]:
    rows = _rows(connection,
                 "SELECT id, title, status, phase, error, pull_request_url FROM code_task_runs "
                 "WHERE (id = :r OR task_id = :r) AND project_id = :p "
                 "ORDER BY created_at DESC LIMIT 1",
                 {"r": run_id, "p": project_id})
    if not rows:
        return "", {}
    row = rows[0]
    return (f"build {row['id']} — \"{row['title']}\", {row['status']}"
            f"{'/' + str(row['phase']) if row.get('phase') else ''}, "
            f"PR {row.get('pull_request_url') or 'none'}"), row


def _preview_row(connection, project_id: str, preview_id: str) -> dict:
    rows = _rows(connection,
                 "SELECT id, run_id, status, mode, command, port, url, failure_kind, error, "
                 "log_path, branch, repo FROM app_previews WHERE id = :i AND project_id = :p",
                 {"i": preview_id, "p": project_id})
    return rows[0] if rows else {}


def _resolve_preview(connection, project_id: str, ref: SubjectRef) -> SubjectEvidence:
    ev = SubjectEvidence(ref)
    row = _preview_row(connection, project_id, ref.id)
    if not row:
        return ev
    ev.found = True
    ev.status = str(row.get("status") or "")
    ev.error = _cap(row.get("error"))
    ev.log_tail = _log_tail(row.get("log_path"))
    ev.facts = {
        "failure_kind": row.get("failure_kind"), "mode": row.get("mode"),
        # An EMPTY command is a fact worth stating: it means the record does not say what ran.
        "launch command": row.get("command") or "(not recorded)",
        "port": row.get("port"), "url": row.get("url"), "branch": row.get("branch"),
        "log_path": row.get("log_path"), "run_id": row.get("run_id"),
    }
    ev.findings = _crash_findings(ev.error, ev.log_tail,
                                  failure_kind=str(row.get("failure_kind") or ""))
    if row.get("run_id"):
        line, _ = _code_task_line(connection, project_id, str(row["run_id"]))
        if line:
            ev.linked.append(line)
    ev.open_points = [
        ("the full start-up log", f"read_preview_log(preview_id={ref.id})"),
        ("which file is the app's real server entry",
         f"search_code / read_code with run_id={row.get('run_id') or '<the build>'}"),
    ]
    return ev


def _resolve_workflow_step(connection, project_id: str, ref: SubjectRef) -> SubjectEvidence:
    ev = SubjectEvidence(ref)
    rows = _rows(connection,
                 "SELECT step_id, step_kind, status, output, input, last_error, attempt, "
                 "iteration, decision_id FROM workflow_step_runs "
                 "WHERE workflow_run_id = :r AND step_id = :s AND project_id = :p "
                 "ORDER BY iteration DESC, attempt DESC LIMIT 1",
                 {"r": ref.id, "s": ref.step or "", "p": project_id})
    run = _rows(connection,
                "SELECT id, status, input, error FROM workflow_runs "
                "WHERE id = :r AND project_id = :p",
                {"r": ref.id, "p": project_id})
    if run:
        inp = _json(run[0].get("input")) or {}
        if isinstance(inp, dict):
            ev.title = str(inp.get("title") or inp.get("app_name") or "")[:200]
        ev.facts["workflow run status"] = run[0].get("status")
    if not rows:
        return ev
    row = rows[0]
    ev.found = True
    out = _json(row.get("output")) or {}
    out = out if isinstance(out, dict) else {"output": out}
    ev.status = str(row.get("status") or "")
    ev.error = _cap(row.get("last_error") or out.get("error") or out.get("note"))
    ev.facts.update({
        "step kind": row.get("step_kind"), "attempt": row.get("attempt"),
        "failure_kind": out.get("failure_kind"),
        "suggested_action (the checker's, not verified)": out.get("suggested_action"),
        "preview_id": out.get("preview_id"),
        "run_id": out.get("run_id") or out.get("build_run_id"),
        "decision_id": row.get("decision_id"),
    })
    for key in sorted(out):
        if key not in {"error", "note", "failure_kind", "suggested_action", "preview_id",
                       "run_id", "build_run_id"} and len(ev.facts) < 24:
            ev.facts[f"output.{key}"] = out[key]
    preview = {}
    if out.get("preview_id"):
        preview = _preview_row(connection, project_id, str(out["preview_id"]))
        if preview:
            ev.facts["launch command"] = preview.get("command") or "(not recorded)"
            ev.facts["log_path"] = preview.get("log_path")
            ev.log_tail = _log_tail(preview.get("log_path"))
            ev.linked.append(
                f"preview {preview['id']} — {preview.get('status')}, "
                f"failure_kind={preview.get('failure_kind') or 'none'}, mode={preview.get('mode')}")
    build_id = str(out.get("run_id") or out.get("build_run_id") or preview.get("run_id") or "")
    if build_id:
        line, _ = _code_task_line(connection, project_id, build_id)
        if line:
            ev.linked.append(line)
    ev.findings = _crash_findings(ev.error, ev.log_tail,
                                  failure_kind=str(out.get("failure_kind") or ""))
    ev.open_points = [("the run's other steps and its way out",
                       f"describe_workflow_run(run_id={ref.id})")]
    if out.get("preview_id"):
        ev.open_points.append(("the full start-up log",
                               f"read_preview_log(preview_id={out['preview_id']})"))
    if build_id:
        ev.open_points.append(("the app's code at the line the error names",
                               f"search_code / read_code with run_id={build_id}"))
    return ev


def _resolve_workflow_run(connection, project_id: str, ref: SubjectRef) -> SubjectEvidence:
    ev = SubjectEvidence(ref)
    run = _rows(connection,
                "SELECT id, status, input, error, blocked_reason FROM workflow_runs "
                "WHERE id = :r AND project_id = :p", {"r": ref.id, "p": project_id})
    if not run:
        return ev
    ev.found = True
    inp = _json(run[0].get("input")) or {}
    if isinstance(inp, dict):
        ev.title = str(inp.get("title") or inp.get("app_name") or "")[:200]
    ev.status = str(run[0].get("status") or "")
    ev.error = _cap(run[0].get("error") or run[0].get("blocked_reason"))
    steps = _rows(connection,
                  "SELECT step_id, status, last_error FROM workflow_step_runs "
                  "WHERE workflow_run_id = :r AND project_id = :p ORDER BY created_at",
                  {"r": ref.id, "p": project_id})
    for s in steps:
        if s.get("status") in {"failed", "deadlettered", "waiting", "running", "blocked"}:
            ev.linked.append(f"step {s['step_id']} — {s['status']}"
                             + (f": {_short(s.get('last_error'), 300)}"
                                if s.get("last_error") else ""))
    ev.findings = _crash_findings(ev.error, *[str(s.get("last_error") or "") for s in steps])
    ev.open_points = [("each step's state and the remedy", f"describe_workflow_run(run_id={ref.id})")]
    return ev


def _resolve_code_task(connection, project_id: str, ref: SubjectRef) -> SubjectEvidence:
    ev = SubjectEvidence(ref)
    rows = _rows(connection,
                 "SELECT id, title, status, phase, error, pull_request_url, state FROM "
                 "code_task_runs WHERE (id = :r OR task_id = :r) AND project_id = :p "
                 "ORDER BY created_at DESC LIMIT 1", {"r": ref.id, "p": project_id})
    if not rows:
        return ev
    row = rows[0]
    ev.found = True
    ev.title = str(row.get("title") or "")
    ev.status = f"{row.get('status')}/{row.get('phase')}"
    ev.error = _cap(row.get("error"))
    state = _json(row.get("state")) or {}
    state = state if isinstance(state, dict) else {}
    ev.facts = {"PR": row.get("pull_request_url") or "none opened",
                "summary": state.get("summary"), "tests_passed": state.get("tests_passed")}
    previews = _rows(connection,
                     "SELECT id, status, failure_kind FROM app_previews WHERE run_id = :r "
                     "AND project_id = :p ORDER BY created_at DESC LIMIT 3",
                     {"r": row["id"], "p": project_id})
    for p in previews:
        ev.linked.append(f"preview {p['id']} — {p.get('status')}"
                         + (f", failure_kind={p['failure_kind']}" if p.get("failure_kind") else ""))
    ev.findings = _crash_findings(ev.error)
    ev.open_points = [
        ("the PRD, acceptance criteria and review notes", f"read_spec(run_id={row['id']})"),
        ("what the build did and where it stopped", f"diagnose_run(run_id={row['id']})"),
    ]
    return ev


def _resolve_discovery(connection, project_id: str, ref: SubjectRef) -> SubjectEvidence:
    ev = SubjectEvidence(ref)
    rows = _rows(connection,
                 "SELECT id, title, status, phase, seed, error FROM discovery_runs "
                 "WHERE id = :r AND project_id = :p", {"r": ref.id, "p": project_id})
    if not rows:
        return ev
    row = rows[0]
    ev.found = True
    ev.title = str(row.get("title") or "")
    ev.status = f"{row.get('status')}/{row.get('phase')}"
    ev.error = _cap(row.get("error"))
    ev.facts = {"seed": row.get("seed")}
    ev.open_points = [("the dossier's findings", "list_discovery")]
    return ev


def _resolve_deployment(connection, project_id: str, ref: SubjectRef) -> SubjectEvidence:
    ev = SubjectEvidence(ref)
    runs = _rows(connection,
                 "SELECT id, deployment_target_id, app_name, environment, status, "
                 "failure_reason, target_ref, commit_ref FROM deployment_runs "
                 "WHERE id = :r AND project_id = :p", {"r": ref.id, "p": project_id})
    target_id = runs[0]["deployment_target_id"] if runs else ref.id
    targets = _rows(connection,
                    "SELECT id, app_name, environment, status, target_kind, target_ref, domains "
                    "FROM deployment_targets WHERE id = :t AND project_id = :p",
                    {"t": target_id, "p": project_id})
    if not runs and not targets:
        return ev
    ev.found = True
    if runs:
        run = runs[0]
        ev.title = f"{run.get('app_name')} ({run.get('environment')})"
        ev.status = str(run.get("status") or "")
        ev.error = _cap(run.get("failure_reason"))
        ev.facts.update({"commit": run.get("commit_ref"), "target_ref": run.get("target_ref")})
    else:
        latest = _rows(connection,
                       "SELECT id, status, failure_reason FROM deployment_runs "
                       "WHERE deployment_target_id = :t AND project_id = :p "
                       "ORDER BY created_at DESC LIMIT 1", {"t": target_id, "p": project_id})
        if latest:
            ev.error = _cap(latest[0].get("failure_reason"))
            ev.linked.append(f"latest deployment {latest[0]['id']} — {latest[0].get('status')}")
    if targets:
        t = targets[0]
        ev.title = ev.title or f"{t.get('app_name')} ({t.get('environment')})"
        ev.status = ev.status or str(t.get("status") or "")
        ev.facts.update({"target": t["id"], "target_kind": t.get("target_kind"),
                         "target_ref": t.get("target_ref"), "domains": _json(t.get("domains"))})
        app = t.get("app_name")
    else:
        app = runs[0].get("app_name")
    ev.findings = _crash_findings(ev.error)
    ev.open_points = [
        ("the target as configured", f"describe_deployment_target(app_name={app})"),
        ("whether it answers right now", "http_probe on its URL"),
    ]
    return ev


def _resolve_decision(connection, project_id: str, ref: SubjectRef) -> SubjectEvidence:
    ev = SubjectEvidence(ref)
    rows = _rows(connection,
                 "SELECT id, question, status, recommendation, options, risk_tier, "
                 "default_action, external_ref FROM decisions WHERE id = :r AND project_id = :p",
                 {"r": ref.id, "p": project_id})
    if not rows:
        return ev
    row = rows[0]
    ev.found = True
    ev.title = str(row.get("question") or "")[:300]
    ev.status = str(row.get("status") or "")
    ev.facts = {"recommendation": _json(row.get("recommendation")),
                "options": _json(row.get("options")), "risk_tier": row.get("risk_tier"),
                "default_action": row.get("default_action"),
                "gating": row.get("external_ref")}
    ev.open_points = [("what else is waiting on the same call", "list_decisions")]
    return ev


#: THE REGISTRY. A kind missing here is a door that opens onto nothing — the enforcing test
#: checks every kind the console can send against this mapping.
RESOLVERS: dict[str, Callable[[Any, str, SubjectRef], SubjectEvidence]] = {
    "workflow_step": _resolve_workflow_step,
    "workflow_run": _resolve_workflow_run,
    "code_task": _resolve_code_task,
    "preview": _resolve_preview,
    "discovery": _resolve_discovery,
    "deployment": _resolve_deployment,
    "decision": _resolve_decision,
}


def resolve_subject(connection, project_id: str, ref: SubjectRef) -> SubjectEvidence:
    """The record for `ref`. Never raises: an unreadable record is reported as not found."""
    resolver = RESOLVERS.get(ref.kind)
    if resolver is None:
        return SubjectEvidence(ref)
    try:
        return resolver(connection, project_id, ref)
    except Exception as exc:  # noqa: BLE001 — a turn must still be answerable
        _log.warning("resolving %s failed (%s): %s", ref.label, type(exc).__name__, exc)
        return SubjectEvidence(ref)
