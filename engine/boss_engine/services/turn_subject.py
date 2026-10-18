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
    # Infra's Ask BOSS (infra-observability spec, operator decision 2026-10-04): the machines,
    # what is placed on them and how their last deploys went. The id is the project id.
    "fleet",
)

#: An id prefix is enough to know the kind of an untyped legacy reference.
_PREFIX_KIND = (
    ("crun_", "code_task"),
    ("wfr_", "workflow_run"),
    ("prev_", "preview"),
    ("drun_", "discovery"),
    ("deprun_", "deployment"),
    # A stack deployment (boss.deploy.yaml) is a deployment too: a deploy step's Ask that names
    # its sdep_ is on subject (deploy_repair).
    ("sdep_", "deployment"),
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


#: An id the operator TYPED. Only the kinds whose ids are unmistakable in prose.
_TYPED_ID = re.compile(r"\b((?:crun|wfr|prev|deprun|dec)_[a-z0-9]{6,})\b", re.IGNORECASE)


def subject_named_in(question: str) -> Optional[SubjectRef]:
    """The one piece of work a free-typed question names by id, or None.

    THE OWNER'S COMPLAINT, 2026-10-02: "it takes so long to find the issue and come up with the
    recommendation to fix". Asked "why did crun_… fail?" in the general chat, the model spent its
    first round-trips finding the run it had just been handed, then reading it, and only then
    answering — each round-trip a minute or more on a CLI brain — while an "Ask about this" door
    opened the same question with the run's whole record already in the prompt and answered in
    one. Same question, same evidence; only the door differed. An id in the question is the same
    typed reference the door sends, so it gets the same first call.

    Exactly one distinct id, or nothing: two ids is a comparison, and picking one would answer
    about the wrong thing.
    """
    found = {m.group(1).lower() for m in _TYPED_ID.finditer(question or "")}
    if len(found) != 1:
        return None
    return parse_subject(found.pop())


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


#: Ids BOSS mints, by prefix — in the SHAPE it mints them: the prefix, then a long lowercase
#: hex tail (`make_id` gives twenty). A reply naming one of these names an object of that kind.
#:
#: NOT ANY WORD THAT STARTS WITH A PREFIX. This matched `[0-9a-z]+` after the prefix,
#: case-insensitively, so ordinary code names — `dec_rate`, `dept_id`, `prev_state`,
#: `dec_threshold` — were read as a decision, a deployment and a preview "the record does not
#: link to", and a correct ImportError fix that mentioned one was thrown away (review of
#: 374ba07). Case-sensitive, eight hex characters at least, and not glued to more identifier.
_ID_IN_TEXT = re.compile(
    r"(?<![\w`])(" + "|".join(re.escape(p) for p, _ in _PREFIX_KIND)
    + r")[0-9a-f]{8,}(?![\w`])")

#: The deployment chain's INVESTIGATION vocabulary — hosts, targets, the tools that list them.
#: The incident's off-subject answer was made of it: "describe_deployment_target and
#: list_deployments both return empty … list_deployment_hosts returns zero hosts".
_DEPLOY_CHAIN = re.compile(
    r"deployment (?:target|host|record)s?|deployment hosts?|zero hosts|no hosts|"
    r"list_deployment_hosts|describe_deployment_target|list_deployments|"
    r"preflight_deployment_target", re.I)

#: A statement of whether the thing is LIVE. On the incident's Preview step it was a swap of
#: subject ("has never been deployed"); on a build it is often the answer — "not deployed yet,
#: the PR is open" to "Is it live yet?".
_LIVENESS = re.compile(r"never (?:been )?deployed|not (?:been |yet )?deployed|not (?:yet )?live",
                       re.I)

#: Subjects whose own next state is being shipped. Whether they are live is about THEM.
_SHIPS = {"code_task", "workflow_run"}

#: What makes the operator's question about shipping — then deploy talk is the answer.
_ASKS_ABOUT_SHIPPING = re.compile(
    r"\bdeploy|\blive\b|\bship|\brelease|\bproduction\b|\bcustomers?\b|\bgo(?:ne)? live|"
    r"\blaunch|\bin prod\b|\bavailable\b", re.I)

#: Where each kind of minted id lives, for "does this object exist in this project?".
_ID_TABLE = {
    "code_task": "code_task_runs", "workflow_run": "workflow_runs", "preview": "app_previews",
    "discovery": "discovery_runs", "decision": "decisions",
}


def id_exists(connection, project_id: str, token: str) -> Optional[bool]:
    """Whether a minted id names a real object in this project — None when that cannot be
    read (then the caller keeps its stricter rule rather than waving the id through)."""
    kind = _kind_of_id(token)
    tables = (["deployment_runs", "deployment_targets"] if kind == "deployment"
              else [_ID_TABLE[kind]] if kind in _ID_TABLE else [])
    if not tables or connection is None:
        return None
    seen = False
    for table in tables:
        try:
            with connection.begin_nested():
                row = connection.execute(
                    text(f"SELECT 1 FROM {table} WHERE id = :i AND project_id = :p"),  # noqa: S608
                    {"i": token, "p": project_id}).first()
        except Exception:  # noqa: BLE001 — unreadable is "unknown", not "absent"
            return None
        seen = seen or row is not None
    return seen


def _kind_of_id(token: str) -> str:
    low = token.lower()
    for prefix, kind in _PREFIX_KIND:
        if low.startswith(prefix):
            return kind
    return ""


def subject_marks(evidence: SubjectEvidence) -> set[str]:
    """What identifies this subject in prose: its ids, its name where the name is the object's
    own, and the distinctive tokens of its recorded failure."""
    marks = {evidence.ref.id.lower()}
    if evidence.ref.step:
        marks.add(evidence.ref.step.lower())
    # The title only where it IS the object's name. A step's or a preview's title is the APP's
    # — and the incident's off-subject answer named the app ("Lead Triage Inbox has never been
    # deployed"), so counting it would have waved that answer through.
    if evidence.title and len(evidence.title) >= 4 and evidence.ref.kind in {
            "decision", "discovery", "deployment", "code_task"}:
        marks.add(evidence.title.lower())
    for value in evidence.facts.values():
        if isinstance(value, str) and re.match(r"^[a-z]+_[0-9a-z]{6,}$", value):
            marks.add(value.lower())
    return {m for m in marks if m} | cause_marks(evidence)


def checker_phrases() -> tuple[str, ...]:
    """Sentences that are the CHECKER'S words, not the failure's: the label a check wraps every
    failure in, and the advice it attaches. Read from where the checker keeps them
    (`preview_service.START_FAILURE_ADVICE`, `CHECKER_LABELS`), plus the generic wrappers every
    check uses."""
    from .preview_service import CHECKER_LABELS, START_FAILURE_ADVICE

    advice = tuple(START_FAILURE_ADVICE.values())
    # Each advice sentence is matched clause by clause too: an answer paraphrases "Inspect
    # startup logs, ensure HOST/PORT are honored, or retry in Docker mode" one clause at a time.
    clauses = tuple(c.strip(" .") for a in advice for c in re.split(r"[,;]| or | and |\.", a)
                    if len(c.strip(" .")) >= 8)
    generic = ("health check", "within timeout", "timed out", "finished as", "log tail",
               "most recent call last")
    return tuple(dict.fromkeys((*CHECKER_LABELS, *advice, *clauses, *generic)))


def _without_checker_words(text_: str) -> str:
    out = text_
    for phrase in sorted(checker_phrases(), key=len, reverse=True):
        out = re.sub(re.escape(phrase), " ", out, flags=re.I)
    return out


def cause_marks(evidence: SubjectEvidence) -> set[str]:
    """The distinctive tokens of the recorded CAUSE — what an answer that USED the failure would
    repeat: the exception and its detail, a file it names, an error code.

    NEVER THE CHECKER'S OWN WORDS. The first version took every two-word phrase with one long
    word from the whole error, and the error is mostly the checker's wrapper — "the preview it
    started finished as 'failed' — app did not serve HTTP within timeout … Traceback (most recent
    call last)". So "within timeout", "preview started" and "traceback most" were marks, and the
    incident's own wrong answer — "The app did not serve HTTP within timeout. Ensure HOST/PORT
    are honored and retry in Docker mode." — counted as grounded, switching off the lookup floor
    and the role re-ask: one model call, the port_timeout misdiagnosis delivered (review of
    374ba07). Marks now come from the exception lines, then from paths and codes in what is left
    once the checker's sentences are removed; the plain two-word fallback ("connection refused")
    only when the record names no exception and no such token.
    """
    error = evidence.error or ""
    exceptions = [m.group(0) for m in _EXCEPTION_LINE.finditer(error)]
    # The exception line of a log tail counts as the record of the failure too: a preview
    # whose error column is empty still says why in its log.
    exceptions += [m.group(0) for m in _EXCEPTION_LINE.finditer(evidence.log_tail or "")]
    cause = _without_checker_words("\n".join([error, *exceptions]))
    marks: set[str] = set()
    for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9_./-]{3,}", cause):
        w = word.strip("./-")
        if len(w) < 5:
            continue
        if (re.search(r"[._/]", w) or re.match(r"[A-Z][a-z]+[A-Z]", w)
                or re.search(r"(?:Error|Exception)$", w) or re.match(r"^[A-Z][A-Z0-9_]{4,}$", w)):
            marks.add(w.lower())
    # The exception's own words: "attempted relative import with no known parent package"
    # yields "relative import" and "parent package" — what an answer about THIS failure says.
    def pairs(blob: str) -> set[str]:
        words = re.findall(r"[a-z]{4,}", blob.lower())
        return {f"{a} {b}" for a, b in zip(words, words[1:]) if max(len(a), len(b)) >= 7}

    for line in exceptions:
        detail = _EXCEPTION_LINE.match(line)
        marks |= pairs(_without_checker_words(detail.group(2) if detail else line))
    # Plain-words failures ("health check: connection refused") have no exception and no
    # token; there a two-word phrase from what is left after the checker's words is the mark.
    if not marks:
        marks |= pairs(cause)
    return marks


def cites_the_record(reply: str, evidence: Optional[SubjectEvidence]) -> bool:
    """Whether an answer was DRAWN FROM the subject's recorded failure.

    The record being found is not enough: a build with no recorded error, a deployment whose
    outage lives in its health samples, a quiet build asked "still working or stuck?" — the
    record is there and holds no answer, and an answer to those still has to look. So: the
    record holds a failure (an error or a finding), and the reply repeats something distinctive
    from it.
    """
    if evidence is None or not evidence.found or not reply:
        return False
    if not (evidence.error or evidence.findings):
        return False
    low = reply.lower()
    return any(m in low for m in cause_marks(evidence))


def strays_from_subject(reply: str, evidence: SubjectEvidence, question: str = "",
                        exists: Optional[Callable[[str], Optional[bool]]] = None) -> str:
    """Why a replacement answer is about some OTHER object, or "" when it is not.

    THIS IS A NEGATIVE TEST, deliberately. The first version demanded a positive match — the
    subject's id, an id-shaped fact or a word of its error — and on a build thread ("which
    criteria are untested?") a correct continuation names none of those: builds rarely have an
    error and nobody quotes a run id. It refused the better answer and kept the hand-back,
    which is the incident's failure in reverse.

    A reply strays only when it names something else:
      * an id of a KIND the subject's record does not link to (a `deprun_…` on a preview step); or
      * the deployment chain's vocabulary on a subject that is not, and does not link to, a
        deployment — and nothing of the subject itself (boss-1's "never been deployed … zero
        hosts", which named neither the step, the preview nor the error).
    The operator's own question widens what is on subject: asked about the deploy — or whether
    it is live, shipped, in front of customers — deploy talk is the answer. And a subject that
    ships (a build, a run) may say it is not live yet; only the deployment chain's
    investigation vocabulary needs that link. `exists`, when given, confirms a named id is a
    real object in the project before it counts against the reply.
    """
    if not (reply or "").strip():
        return "empty"
    low = reply.lower()
    record = (evidence.render() + "\n" + (question or "")).lower()
    linked_kinds = {evidence.ref.kind} | {
        _kind_of_id(m.group(0)) for m in _ID_IN_TEXT.finditer(record)}
    for m in _ID_IN_TEXT.finditer(reply):
        token = m.group(0)
        kind = _kind_of_id(token)
        if not kind or kind in linked_kinds or token.lower() in record:
            continue
        if exists is not None and exists(token) is False:
            continue          # not an object anywhere in this project, so not another subject
        return f"names {kind} {token}, which {evidence.ref.label} does not link to"
    deploy_linked = ("deployment" in linked_kinds
                     or bool(_ASKS_ABOUT_SHIPPING.search(question or "")))
    if not deploy_linked:
        chain = _DEPLOY_CHAIN.search(reply)
        liveness = _LIVENESS.search(reply) and evidence.ref.kind not in _SHIPS
        if (chain or liveness) and not any(mark in low for mark in subject_marks(evidence)):
            return (f"answers about deployments, and says nothing of {evidence.ref.label} "
                    "or its recorded failure")
    return ""


def stays_on_subject(reply: str, evidence: SubjectEvidence, question: str = "",
                     exists: Optional[Callable[[str], Optional[bool]]] = None) -> bool:
    """A re-ask may refine an answer; it may not swap it for one about another object."""
    return not strays_from_subject(reply, evidence, question, exists)


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


#: A build tool reporting that the build itself failed (Vite/Rollup, webpack, npm, tsc).
_BUILD_FAILED = re.compile(
    r"Could not resolve entry module[^\n]*|error during build[^\n]*|Build failed[^\n]*|"
    r"Failed to compile[^\n]*|Module not found: [^\n]*|npm ERR! [^\n]*", re.I)


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
        # A BUILD THAT FAILED IS THE SAME CLASS (boss-1, wfr_475bf68eb18f485b8c52): Vite's
        # `Could not resolve entry module "index.html"` under the same port_timeout label, and
        # no Python-style exception line for the rule above to find.
        build = _BUILD_FAILED.search(blob)
        if build is None:
            return []
        line = blob[build.start():].split(" log tail:")[0].split("\n")[0][:240].strip()
        out = [f"The app's build failed before anything could serve ({line}) — a build or "
               "configuration failure, not a slow start."]
        if failure_kind in {"port_timeout", "timeout"}:
            out.append(f"failure_kind '{failure_kind}' is the checker's label for 'nothing "
                       "answered in time'; the failed build above is why nothing answered.")
        return out
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


def _stack_deployment_of(out: dict) -> str:
    """The stack deployment a deploy step planned, waited on, or was failed by."""
    handle = out.get("await") if isinstance(out.get("await"), dict) else {}
    reached = out.get("reached_before_failing") \
        if isinstance(out.get("reached_before_failing"), dict) else {}
    fixes = [f for f in out.get("deploy_fixes") or [] if isinstance(f, dict)]
    failure = out.get("deploy_failure") if isinstance(out.get("deploy_failure"), dict) else {}
    return str(out.get("stack_deployment_id")
               or (handle.get("id") if handle.get("kind") == "stack_deployment" else "")
               or reached.get("stack_deployment_id") or failure.get("deployment_id")
               or next((f.get("deployment_id") for f in reversed(fixes)
                        if f.get("deployment_id")), "") or "")


def _with_the_deployment(connection, ev: SubjectEvidence, out: dict,
                         deployment_id: str) -> None:
    """A DEPLOY STEP'S ASK KNOWS ITS DEPLOYMENT (deploy_repair): the deployment's steps and
    states, the failing command and its redacted log tail, what the machine has, the manifest,
    and every fix BOSS started for it — so the deploy agent answers from the record, and the
    operator talks to it about the failure as about anything else."""
    from . import deploy_repair

    facts: dict = {}
    if deployment_id and connection is not None:
        try:
            with connection.begin_nested():
                facts = deploy_repair.deployment_facts(connection, deployment_id)
        except Exception as exc:  # noqa: BLE001 — evidence is an aid
            _log.info("deployment evidence for %s not read: %s", deployment_id, exc)
            facts = {}
    tail = str(facts.pop("_log_tail", "") or "")
    if tail and not ev.log_tail:
        ev.log_tail = tail[-_LOG_TAIL_CHARS:]
    for key, value in facts.items():
        ev.facts[key] = value
    failure = out.get("deploy_failure") if isinstance(out.get("deploy_failure"), dict) else {}
    if failure and "deployment failure" not in ev.facts:
        ev.facts["deploy refused before anything ran"] = (
            f"{failure.get('headline') or ''} {failure.get('action') or ''}".strip()
            + f" (cause: {failure.get('cause') or 'unplaced'})")
        machine = deploy_repair.facts_sentence(failure.get("host_facts"))
        if machine and "the machine" not in ev.facts:
            ev.facts["the machine"] = machine
    fixes = [f for f in out.get("deploy_fixes") or [] if isinstance(f, dict)]
    if fixes:
        ev.facts["automatic fixes"] = deploy_repair.fixes_sentence(fixes)
        for fix in fixes:
            ev.linked.append(f"build {fix.get('build_run_id')} — BOSS's fix {fix.get('attempt')} "
                             f"for this deploy ({fix.get('cause')})")
    ev.facts.pop("output.deploy_fixes", None)
    ev.facts.pop("output.deploy_failure", None)


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
    # WHAT THE STEP AWAITED, AS THE STEP RECORDS IT. A step that started something nests that
    # thing's result under its own kind — the real boss-1 row is
    # {"tool": "start_preview", "preview": {"id", "error", "failure_kind", "suggested_action"}}.
    # Reading only the top level lost the checker's label and its advice, so the "port_timeout
    # is only the label" finding never fired and "ensure HOST/PORT are honored" reached the
    # model unlabelled, inside a raw dump.
    nested = out.get("preview") if isinstance(out.get("preview"), dict) else {}
    failure_kind = str(out.get("failure_kind") or nested.get("failure_kind") or "")
    suggested = out.get("suggested_action") or nested.get("suggested_action")
    preview_id = str(out.get("preview_id") or nested.get("id") or "")
    ev.status = str(row.get("status") or "")
    ev.facts.update({
        "step kind": row.get("step_kind"), "attempt": row.get("attempt"),
        "failure_kind": failure_kind or None,
        "suggested_action (the checker's, not verified)": suggested,
        "preview_id": preview_id or None,
        "run_id": out.get("run_id") or out.get("build_run_id"),
        "decision_id": row.get("decision_id"),
    })
    for key in sorted(out):
        if key not in {"error", "note", "failure_kind", "suggested_action", "preview_id",
                       "run_id", "build_run_id", "preview"} and len(ev.facts) < 24:
            ev.facts[f"output.{key}"] = out[key]
    preview = {}
    if preview_id:
        preview = _preview_row(connection, project_id, preview_id)
    # THE ERROR, WHOLE. `last_error` is the workflow runtime's summary and may be cut; the
    # preview's own row (or the copy nested in the step's output) is the full text. Prefer the
    # longest record of the same failure.
    ev.error = _cap(max(
        (str(x) for x in (preview.get("error"), nested.get("error"), out.get("error"),
                          row.get("last_error"), out.get("note")) if x),
        key=len, default=""))
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
    deployment_id = _stack_deployment_of(out)
    if deployment_id or out.get("deploy_failure") or out.get("deploy_fixes"):
        _with_the_deployment(connection, ev, out, deployment_id)
    # WHAT THE STEP'S AGENT DECIDED to recover it (step_recovery): each decision in its own
    # words, so asking the step "what happened?" reads the loop, not a raw dump of it.
    recovery = out.get("recovery") if isinstance(out.get("recovery"), dict) else {}
    decided = [d for d in recovery.get("decisions") or []
               if isinstance(d, dict) and d.get("sentence")]
    if decided:
        ev.facts["recovery decisions"] = " | ".join(
            f"{d.get('n')}. {d['sentence']}" for d in decided[-6:])[:1500]
        if out.get("recovery_question"):
            ev.facts["recovery question for a person"] = out["recovery_question"]
    ev.facts.pop("output.recovery", None)
    ev.findings = _crash_findings(ev.error, ev.log_tail,
                                  failure_kind=failure_kind or str(preview.get("failure_kind") or ""))
    ev.open_points = [("the run's other steps and its way out",
                       f"describe_workflow_run(run_id={ref.id})")]
    if deployment_id:
        ev.open_points.append(("the deployment in full, its log tail and steps",
                               f"list_deployments(deployment_id={deployment_id})"))
        ev.open_points.append(("what the machine has",
                               "describe_deployment_host(host=<the host above>)"))
    if preview_id:
        ev.open_points.append(("the full start-up log",
                               f"read_preview_log(preview_id={preview_id})"))
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
    # WHAT THE BUILD DID, AS IT RECORDED IT. Asked why a "dark mode" follow-up changed one
    # route, the agent read the summary and guessed; the build's own event said it resumed the
    # PREVIOUS build's design and skipped planning (code_task.design_resumed). The run's events
    # are the record of its path, so they travel with it — newest last, bounded.
    events = _rows(connection,
                   "SELECT event_type, data FROM events WHERE entity_id = :r AND project_id = :p "
                   "ORDER BY created_at DESC LIMIT 12", {"r": row["id"], "p": project_id})
    step = _rows(connection,
                 "SELECT workflow_run_id, step_id, status FROM workflow_step_runs "
                 "WHERE project_id = :p AND (output->>'build_run_id' = :r OR "
                 "output->>'run_id' = :r) ORDER BY updated_at DESC LIMIT 1",
                 {"r": row["id"], "p": project_id})
    if step:
        ev.linked.append(
            f"this build is step '{step[0]['step_id']}' ({step[0]['status']}) of workflow run "
            f"{step[0]['workflow_run_id']}: a retry goes to that step "
            f"(retry_workflow_step), never retry_code_task, or the run does not follow it")
    for e in reversed(events):
        data = _json(e.get("data"))
        ev.linked.append(f"event {e['event_type']}"
                         + (f": {_short(json.dumps(data, default=str), 240)}" if data else ""))
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
    if ref.id.startswith("sdep_"):
        rows = _rows(connection, "SELECT id, status, failure_reason FROM stack_deployments "
                                 "WHERE id = :i AND project_id = :p",
                     {"i": ref.id, "p": project_id})
        if not rows:
            return ev
        ev.found = True
        ev.status = str(rows[0].get("status") or "")
        ev.error = _cap(rows[0].get("failure_reason"))
        _with_the_deployment(connection, ev, {}, ref.id)
        ev.findings = _crash_findings(ev.error, ev.log_tail)
        ev.open_points = [("the deployment's full record",
                           f"list_deployments(deployment_id={ref.id})")]
        return ev
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
    # IS IT UP, AS THE HEALTH CHECKER LAST SAW IT. A deployment's outage is not in its run
    # record — a run that SUCCEEDED last week says nothing about the app refusing connections
    # now. The Monitoring door's "not answering" lives in deployment_health_samples, so that
    # is part of this record; without it the answer had to come from lookups, and the gate that
    # counts lookups was switched off for any subject that merely resolved.
    samples = _rows(connection,
                    "SELECT ok, status_code, error, checked_at FROM deployment_health_samples "
                    "WHERE deployment_target_id = :t AND project_id = :p "
                    "ORDER BY checked_at DESC LIMIT 1", {"t": target_id, "p": project_id})
    if samples:
        s = samples[0]
        ev.facts["latest health check"] = (
            f"{'ok' if s.get('ok') else 'FAILING'} at {s.get('checked_at')}"
            + (f", HTTP {s['status_code']}" if s.get("status_code") else ""))
        if not s.get("ok") and s.get("error"):
            health = f"latest health check ({s.get('checked_at')}): {s['error']}"
            ev.error = _cap(f"{ev.error}\n{health}" if ev.error else health)
    # THE BUILD BEHIND IT. A deployment ships a build, and "build crun_… changed the port" is
    # the cause of the outage more often than anything about the host. The record never linked
    # one, so a correct continuation naming it was refused as off-subject (review of 374ba07).
    # The deploy endpoint records it in the release packet. Its own read: an older table without
    # the column must not cost the deployment its record.
    builds = _rows(connection,
                   "SELECT id, release_packet FROM deployment_runs "
                   "WHERE (id = :r OR deployment_target_id = :t) AND project_id = :p "
                   "ORDER BY created_at DESC LIMIT 5",
                   {"r": ref.id, "t": target_id, "p": project_id})
    for row in builds:
        packet = _json(row.get("release_packet")) or {}
        build = packet.get("code_task_run_id") if isinstance(packet, dict) else None
        if build and not any(str(build) in line for line in ev.linked):
            ev.linked.append(f"build {build} — shipped by deployment {row['id']}")
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
def _resolve_fleet(connection, project_id: str, ref: SubjectRef) -> SubjectEvidence:
    """The project's fleet: every machine with its state and last check, what is placed on
    each, and the deploys that failed lately. Read from stored rows only — never a host check."""
    ev = SubjectEvidence(ref)
    # The fleet IS the project's: a ref naming another project is not this project's evidence.
    if ref.id != project_id:
        return ev
    hosts = _rows(connection,
                  "SELECT id, name, address, arch, status, last_preflight_at, last_preflight "
                  "FROM deployment_hosts WHERE project_id = :p AND status <> 'retired' "
                  "ORDER BY name", {"p": project_id})
    targets = _rows(connection,
                    "SELECT id, app_name, environment, host_id, target_kind, status "
                    "FROM deployment_targets WHERE project_id = :p AND status = 'active' "
                    "ORDER BY app_name", {"p": project_id})
    ev.found = True
    ev.title = "Your fleet"
    ev.facts["project"] = project_id
    ev.status = f"{len(hosts)} machines, {len(targets)} app targets"
    placed: dict[str, list[str]] = {}
    for t in targets:
        placed.setdefault(str(t.get("host_id") or ""), []).append(
            f"{t.get('app_name')} ({t.get('environment')}, {t.get('target_kind')})")
    unreachable = []
    for h in hosts:
        pf = _json(h.get("last_preflight")) or {}
        problems = [str(x) for x in (pf.get("problems") or [])][:3] if isinstance(pf, dict) else []
        line = (f"{h.get('address')} · {h.get('arch') or 'arch unknown'} · {h.get('status')}"
                f" · checked {h.get('last_preflight_at') or 'never'}")
        if problems:
            line += " · problems: " + "; ".join(problems)
        apps = placed.get(str(h["id"])) or []
        ev.facts[f"machine {h.get('name')} ({h['id']})"] = line + (
            f" · apps: {', '.join(apps)}" if apps else " · no apps")
        if h.get("status") == "degraded" or (isinstance(pf, dict) and pf.get("reachable") is False):
            unreachable.append(str(h.get("name")))
    if placed.get(""):
        ev.facts["not on a machine BOSS manages"] = ", ".join(placed[""])
    failed = _rows(connection,
                   "SELECT d.id, t.app_name, d.commit_sha, d.failure_reason, d.created_at "
                   "FROM stack_deployments d JOIN deployment_targets t ON t.id = d.target_id "
                   "WHERE d.project_id = :p AND d.status = 'failed' "
                   "ORDER BY d.created_at DESC LIMIT 3", {"p": project_id})
    for f in failed:
        ev.linked.append(f"stack deploy {f['id']} of {f.get('app_name')} at "
                         f"{str(f.get('commit_sha') or '')[:7]} — failed: {_short(f.get('failure_reason'), 200)}")
    if unreachable:
        ev.error = f"Unreachable: {', '.join(unreachable)}"
    return ev


RESOLVERS: dict[str, Callable[[Any, str, SubjectRef], SubjectEvidence]] = {
    "workflow_step": _resolve_workflow_step,
    "workflow_run": _resolve_workflow_run,
    "code_task": _resolve_code_task,
    "preview": _resolve_preview,
    "discovery": _resolve_discovery,
    "deployment": _resolve_deployment,
    "decision": _resolve_decision,
    "fleet": _resolve_fleet,
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
