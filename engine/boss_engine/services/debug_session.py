"""An investigation: BOSS debugging the way an engineer debugs, instead of answering.

WHY THIS EXISTS

Asked repeatedly why a deployment was broken, BOSS produced correct diagnoses and no fixes,
then hand-wrote an INSERT against a table name it had guessed. The operator's summary was
exact: "it is working based on the tools... it is not debugging the issue like you do".

That is not a prompt problem and it was never really a model problem. Compare the harnesses:

    an engineer (or Claude Code)          the chat agent
    ------------------------------        --------------------------------
    grep the tree, read any file          75 fixed-shape tools
    write a throwaway script and run it   no shell, no scratch space
    call an endpoint nobody wrapped       only the 75
    run the tests, see them fail, retry   no verification loop
    forty tool calls, several hypotheses  a step budget in the teens

Every question is multiple-choice and every answer arrives in a shape somebody anticipated. A
frontier model in that harness is a very good engineer permitted only to point at things.

WHAT THIS IS

The same runner the coding pipeline already uses — `claude -p` with its own Bash, Read, Grep
and Glob tools in an ephemeral sandbox — pointed at a DIAGNOSIS instead of a diff. It gets:

  * the app's repo checked out, so it can read the code that actually shipped;
  * `boss-db`, read-only SQL over BOSS's own database;
  * `boss-host`, a shell on any registered fleet host;
  * `boss-probe`, HTTP and TCP against anything it wants to test;
  * a briefing of what BOSS already knows, so it starts oriented rather than guessing;
  * and a requirement to end with a verdict it has EVIDENCE for.

WHAT IT MAY CHANGE

Nothing, by default. A default investigation is read-only everywhere: `boss-db` refuses
anything but SELECT, `boss-host` refuses anything outside an enumerated set of read commands,
and the agent has no Edit or Write tool. Freedom to look is not freedom to act, and that
distinction is the whole reason this can be turned loose at all.

An investigation opened with `fix=True` gets a real shell on the host. That one is reachable
only by an operator pressing a button — `investigate` with fix set returns
`requires_confirmation`, so a model cannot license it for itself. After 2026-09-02, when an
agent holding production credentials deleted 204 tables, this is the line that matters most.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import text as sql

from ..config import Settings
from .core import emit_event

_log = logging.getLogger(__name__)

#: Read commands an investigation may run on a host without an operator's say-so.
#:
#: An allow-list, because a deny-list on shell input is a promise nobody can keep — `docker`
#: alone can start, stop and delete. Naming the safe verbs is the only version of this that
#: stays true when the tool is used in ways nobody anticipated.
_HOST_READ_VERBS = (
    "docker ps", "docker logs", "docker inspect", "docker images", "docker stats --no-stream",
    "docker compose ps", "docker compose logs", "docker port",
    "systemctl status", "systemctl list-units", "journalctl",
    "ss ", "netstat", "ps aux", "ps -", "df", "du ", "free", "uptime", "uname", "whoami",
    "ls ", "cat ", "head ", "tail ", "stat ", "find ", "readlink", "getent", "id",
    "curl -s", "curl -I", "nslookup", "dig ", "ping -c",
    "env", "printenv", "date", "hostname",
)

#: Filters an investigation may pipe INTO. Enumerated rather than pattern-matched: every one of
#: these reads stdin and writes stdout, and none can touch the machine. This is what makes the
#: read mode usable — `docker logs app | tail -100` is the single most useful command in a
#: deployment investigation, and an allow-list without pipes cannot express it.
_HOST_FILTERS = ("grep", "egrep", "fgrep", "tail", "head", "wc", "sort", "uniq", "cut", "tr",
                 "awk", "jq", "column", "nl", "rev", "tac", "strings")

#: Refused in EVERY mode, because they are irreversible and no diagnosis needs them.
#: `fix=True` opens the shell; it does not open these.
_NEVER = re.compile(
    r"(?:^|[\s;&|])(?:rm\s+-[rf]|mkfs|dd\s+if=|shutdown|reboot|halt|poweroff|"
    r"docker\s+system\s+prune|docker\s+volume\s+rm|userdel|passwd|chpasswd|"
    r":\(\)\s*\{|>\s*/dev/sd)", re.I)


def _new_id() -> str:
    return f"inv_{uuid.uuid4().hex[:16]}"


def host_command_allowed(command: str, *, allow_write: bool) -> tuple[bool, str]:
    """Whether an investigation may run this on a real machine.

    Returns (allowed, why-not). Split out from the runner so the rule is testable on its own:
    this is the boundary between "BOSS looked at production" and "BOSS changed production", and
    it should not be reachable only through a subprocess.
    """
    raw = (command or "").strip()
    if not raw:
        return False, "empty command"
    if _NEVER.search(raw):
        return False, ("this command destroys state irreversibly and is refused in every mode, "
                       "including a fix investigation")
    if allow_write:
        return True, ""

    # Redirection and command substitution are how a read command becomes a write one.
    if any(marker in raw for marker in (">", "`", "$(", ";", "&&", "||", "\n")):
        return False, ("read-only investigation: no redirection, chaining or command "
                       "substitution. Open a fix investigation if you need to change the host.")
    segments = [seg.strip() for seg in raw.split("|")]
    if not any(segments[0].startswith(verb) for verb in _HOST_READ_VERBS):
        first = segments[0].split()[0] if segments[0].split() else segments[0]
        return False, (f"read-only investigation: {first!r} is not one of the read commands. "
                       f"Allowed: {', '.join(v.strip() for v in _HOST_READ_VERBS[:12])}...")
    for seg in segments[1:]:
        head = seg.split()[0] if seg.split() else ""
        if head not in _HOST_FILTERS:
            return False, (f"read-only investigation: {head!r} is not a permitted filter. "
                           f"After a pipe: {', '.join(_HOST_FILTERS)}")
    return True, ""


def sql_is_read_only(statement: str) -> tuple[bool, str]:
    """SELECT and WITH...SELECT only, one statement.

    An investigation reads the database to understand it. Writing to it is a different act with
    a different tool and a different audit trail — `write_db` exists, asks the operator first,
    and records who wanted it.
    """
    body = (statement or "").strip().rstrip(";")
    if not body:
        return False, "empty statement"
    if ";" in body:
        return False, "one statement at a time"
    head = body.split(None, 1)[0].lower() if body.split() else ""
    if head not in ("select", "with", "explain", "table", "show"):
        return False, (f"this connection is read-only ({head!r} refused). To change a row, tell "
                       "the operator what needs changing — write_db asks them first.")
    if re.search(r"\b(insert|update|delete|drop|alter|truncate|create|grant|copy)\b", body, re.I):
        return False, "read-only: the statement contains a write keyword"
    return True, ""


# --------------------------------------------------------------------------- #
#  The sandbox toolbelt                                                        #
# --------------------------------------------------------------------------- #
#: Configured at generation time, NOT through the environment.
#:
#: `agent_environment` strips DATABASE_URL and every credential from the agent's subprocess.
#: That filter exists because an agent that inherited production credentials deleted 204 tables
#: on 2026-09-02, and it must not be weakened to make a feature convenient. So each command
#: carries what it needs, in a file only its owner can read.
_DB_SCRIPT = '''#!/usr/bin/env python3
"""boss-db - read-only SQL against BOSS's database."""
import json, sys

sys.path.insert(0, "/app/engine")
sys.path.insert(0, "/app")
from boss_engine.services.debug_session import sql_is_read_only  # noqa: E402

DB_URL = {db_url!r}

statement = " ".join(sys.argv[1:]) or sys.stdin.read()
ok, why = sql_is_read_only(statement)
if not ok:
    print("refused: " + why, file=sys.stderr)
    raise SystemExit(2)
if not DB_URL:
    print("no database url was provisioned for this investigation", file=sys.stderr)
    raise SystemExit(3)

from sqlalchemy import create_engine, text  # noqa: E402

with create_engine(DB_URL, pool_pre_ping=True).connect() as c:
    rows = c.execute(text(statement)).mappings().all()
print(json.dumps([dict(r) for r in rows[:200]], indent=1, default=str))
if len(rows) > 200:
    print("... " + str(len(rows) - 200) + " more rows (add LIMIT)", file=sys.stderr)
'''

_HOST_SCRIPT = '''#!/usr/bin/env python3
"""boss-host - run a command on a registered fleet host over SSH."""
import sys

sys.path.insert(0, "/app/engine")
sys.path.insert(0, "/app")
from boss_engine.services.debug_session import run_on_investigation_host  # noqa: E402

PROJECT_ID = {project_id!r}
ALLOW_WRITE = {allow_fix!r}

if len(sys.argv) < 3:
    print("usage: boss-host <host> <command...>", file=sys.stderr)
    raise SystemExit(64)
result = run_on_investigation_host(host=sys.argv[1], command=" ".join(sys.argv[2:]),
                                   project_id=PROJECT_ID, allow_write=ALLOW_WRITE)
if not result.get("ok"):
    print(result.get("error") or "failed", file=sys.stderr)
    raise SystemExit(1)
sys.stdout.write(result.get("stdout") or "")
sys.stderr.write(result.get("stderr") or "")
raise SystemExit(int(result.get("exit_code") or 0))
'''

_PROBE_SCRIPT = '''#!/usr/bin/env python3
"""boss-probe - is it answering? HTTP if given a url, TCP if given host:port."""
import socket, sys, time, urllib.request

target = sys.argv[1] if len(sys.argv) > 1 else ""
if not target:
    print("usage: boss-probe <url|host:port>", file=sys.stderr)
    raise SystemExit(64)
started = time.monotonic()
if target.startswith("http"):
    try:
        with urllib.request.urlopen(target, timeout=10) as response:
            body = response.read(2000).decode("utf-8", "replace")
        print("HTTP %s in %dms" % (response.status, (time.monotonic() - started) * 1000))
        print(body)
    except Exception as exc:
        print("HTTP failed after %dms: %s" % ((time.monotonic() - started) * 1000, exc))
        raise SystemExit(1)
else:
    host, _, port = target.rpartition(":")
    sock = socket.socket()
    sock.settimeout(10)
    try:
        sock.connect((host, int(port)))
        print("TCP open in %dms" % ((time.monotonic() - started) * 1000))
    except OSError as exc:
        print("TCP closed after %dms: %s" % ((time.monotonic() - started) * 1000, exc))
        raise SystemExit(1)
    finally:
        sock.close()
'''


def run_on_investigation_host(*, host: str, command: str, project_id: str,
                              allow_write: bool) -> dict[str, Any]:
    """SSH for an investigation, reusing the fleet's own credential resolution.

    Deliberately NOT a new key path: the host record, the bound secret and the connection are
    the ones every deployment already uses, so an investigation cannot reach a machine BOSS is
    not already trusted on, and rotating a key rotates it here too.
    """
    allowed, why = host_command_allowed(command, allow_write=allow_write)
    if not allowed:
        return {"ok": False, "error": why}

    from ..config import get_settings
    from ..db import begin, get_engine
    from .deploy_service import _discard_key_file, _materialise_key, _ssh, _ssh_key_file
    from .secret_backends import resolve_target_secret

    settings = get_settings()
    ephemeral = None
    with begin(get_engine(settings)) as c:
        row = c.execute(
            sql("SELECT * FROM deployment_hosts WHERE project_id = :p AND (id = :h OR name = :h)"),
            {"p": project_id, "h": host.strip()}).mappings().first()
        if row is None:
            names = [r["name"] for r in c.execute(
                sql("SELECT name FROM deployment_hosts WHERE project_id = :p"),
                {"p": project_id}).mappings().all()]
            return {"ok": False,
                    "error": f"no registered host {host!r}. This project has: {names}"}
        record = dict(row)
        probe = {"secret_refs": ([{"name": "ssh_key",
                                   "secret_ref_id": record["key_secret_ref"]}]
                                 if record.get("key_secret_ref") else []),
                 "host_id": record["id"]}
        try:
            material = resolve_target_secret(c, probe, "ssh_key", settings)
            key = ephemeral = (_materialise_key(material) if material
                               else _ssh_key_file(settings))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"no usable credential for this host: {exc}"[:200]}
    try:
        res = _ssh(key, record["address"], (record.get("ssh_user") or "root"),
                   int(record.get("ssh_port") or 22), command, timeout=90)
    finally:
        if ephemeral:
            _discard_key_file(ephemeral)
    return {"ok": True, "host": record["name"], "command": command,
            "exit_code": res.returncode, "stdout": res.stdout or "", "stderr": res.stderr or ""}


def _install_toolbelt(root: Path, *, project_id: str, allow_fix: bool, db_url: str) -> Path:
    """Write the helper commands into the sandbox.

    HONEST LIMIT: an agent with a shell and a readable script can extract the connection string
    from it. `sql_is_read_only` is the sanctioned path, not a sandbox — the real containment is
    a Postgres role that cannot write, supplied as BOSS_INVESTIGATION_DB_URL. Until that exists
    this stops the ACCIDENTAL route (a stray `psql $DATABASE_URL`, which is precisely how the
    2026-09-02 wipe happened) and nothing more. Said plainly rather than implied.
    """
    binaries = root / ".boss-bin"
    binaries.mkdir(parents=True, exist_ok=True)
    for name, body in (
        ("boss-db", _DB_SCRIPT.format(db_url=db_url)),
        ("boss-host", _HOST_SCRIPT.format(project_id=project_id, allow_fix=bool(allow_fix))),
        ("boss-probe", _PROBE_SCRIPT),
    ):
        path = binaries / name
        path.write_text(body)
        path.chmod(0o700)          # owner only: the credential lives in one of these
    return binaries


# --------------------------------------------------------------------------- #
#  The briefing                                                                #
# --------------------------------------------------------------------------- #
def _briefing(connection, *, project_id: str, question: str, run_id: Optional[str]) -> str:
    """What BOSS already knows, so the investigation starts where the answers ran out.

    Handing over a bare question would spend the first ten turns rediscovering rows BOSS can
    produce in three queries — and worse, it would let the agent build its picture from a
    partial re-read rather than from the record.
    """
    parts: list[str] = [f"# Briefing\n\n## The question\n\n{question}\n"]

    def _rows(label: str, statement: str, params: dict) -> None:
        try:
            rows = connection.execute(sql(statement), params).mappings().all()
        except Exception as exc:  # noqa: BLE001 — a briefing gap is not a failed investigation
            parts.append(f"\n## {label}\n\n(could not read: {exc})\n")
            return
        if not rows:
            parts.append(f"\n## {label}\n\n(none)\n")
            return
        parts.append(f"\n## {label}\n\n```json\n"
                     + json.dumps([dict(r) for r in rows], indent=1, default=str)
                     + "\n```\n")

    _rows("Deployment hosts",
          "SELECT id, name, address, status, arch FROM deployment_hosts WHERE project_id = :p",
          {"p": project_id})
    _rows("Deployment targets",
          "SELECT id, app_name, environment, status, domains, host_id, health_checks "
          "FROM deployment_targets WHERE project_id = :p ORDER BY app_name", {"p": project_id})
    if run_id:
        _rows("This run",
              "SELECT id, title, status, created_at, updated_at, pull_request_url "
              "FROM code_task_runs WHERE id = :r", {"r": run_id})
        _rows("Its workflow steps",
              "SELECT step_id, status, error, output FROM workflow_step_runs "
              "WHERE project_id = :p AND (output->>'code_task_run_id' = :r "
              "   OR output->>'run_id' = :r) ORDER BY created_at DESC LIMIT 12",
              {"p": project_id, "r": run_id})
    _rows("Recent deployments",
          "SELECT id, app_name, environment, status, created_at FROM deployment_runs "
          "WHERE project_id = :p ORDER BY created_at DESC LIMIT 8", {"p": project_id})
    return "".join(parts)


_PROMPT = """You are BOSS's investigator. Something is broken and previous answers have not
fixed it. You are not writing a report for a meeting — you are finding out what is actually
true and, where you are permitted to, making it work.

THE QUESTION

{question}

WHAT YOU HAVE

Your working directory is {where}. Read BRIEFING.md FIRST: it holds the rows BOSS has already
read, so you do not have to rediscover them.{repo_note}

Three commands, in addition to your own tools:

  ./.boss-bin/boss-db "SELECT ..."          read-only SQL over BOSS's database. 244 tables;
                                            the briefing names the ones that matter. Use it
                                            to check any claim, including your own.
  ./.boss-bin/boss-host <host> <command>    a shell on a registered fleet host over SSH.
                                            {host_mode}
  ./.boss-bin/boss-probe <url|host:port>    is it answering? HTTP or a raw socket.

HOW TO WORK

- Form a hypothesis, then TEST it. An untested hypothesis is a guess, and guesses are what the
  previous attempts already produced.
- Prefer the machine over the database. The database records what BOSS INTENDED; the host
  shows what is true. Where they disagree, that disagreement is usually the answer.
- Read the app's code when the failure could be in it. You have Grep and Read; use them.
- If your first theory is wrong, say so and try a different one. Repeating a diagnosis is the
  specific failure this investigation exists to end.
- Cheap checks first, and do not stop at the first plausible story.

{fix_mode}

HOW TO FINISH

End your final message with a JSON object, alone, in a ```json fence:

```json
{{
  "cause": "one sentence: what is actually wrong",
  "evidence": ["each thing you RAN that proves it, with the output that mattered"],
  "fix": "the specific change that resolves it",
  "applied": false,
  "verified": "what you ran afterwards to prove it worked, or why you could not",
  "confidence": "high | medium | low",
  "next": "what a person should do now"
}}
```

`evidence` must be things you ran in this session. If you could not establish the cause, say so
in `cause`, put what you ruled OUT in `evidence`, and make `next` the most informative thing to
try — never a yes/no question, and never a request for an identifier you could look up."""

_FIX_ALLOWED = """YOU MAY FIX IT. An operator opened this investigation with fix permission, so
`boss-host` will run commands that change the machine. Use it carefully: make the smallest
change that resolves the problem, verify it with boss-probe or by re-running the failing check,
and record exactly what you changed under `evidence`. Set "applied": true only if you made the
change AND verified the result. Do not delete anything, do not restart services that are not
part of the problem, and never remove data to make a symptom go away."""

_FIX_REFUSED = """YOU ARE READ-ONLY. `boss-db` refuses anything but SELECT and `boss-host`
refuses anything that could change the machine. This is not an obstacle to route around: work
out exactly what needs to change, prove it with evidence, and put it in `fix` so an operator
can apply it in one step. Do not spend turns on commands that will be refused."""


# --------------------------------------------------------------------------- #
#  Lifecycle                                                                   #
# --------------------------------------------------------------------------- #
def open_investigation(connection, *, project_id: str, question: str, actor: str,
                       thread_id: Optional[str] = None, run_id: Optional[str] = None,
                       allow_fix: bool = False) -> dict[str, Any]:
    """Record the intent and queue the work. Returns immediately — this takes minutes."""
    from .jobs import enqueue_job

    live = connection.execute(
        sql("SELECT id FROM investigations WHERE project_id = :p "
            "AND status IN ('queued', 'running') ORDER BY created_at DESC LIMIT 1"),
        {"p": project_id}).scalar()
    if live:
        # One at a time per project. An agent that can open investigations would otherwise open
        # one per turn while waiting for the first, which is a loop with a bill attached.
        return {"ok": False, "already_running": str(live),
                "error": f"investigation {live} is already running for this project. Read it "
                         "with get_investigation before starting another."}

    investigation_id = _new_id()
    connection.execute(
        sql("INSERT INTO investigations (id, project_id, thread_id, run_id, question, actor, "
            "allow_fix, status, created_at, updated_at) "
            "VALUES (:i, :p, :t, :r, :q, :a, :f, 'queued', now(), now())"),
        {"i": investigation_id, "p": project_id, "t": thread_id, "r": run_id,
         "q": question[:4000], "a": actor, "f": bool(allow_fix)})
    enqueue_job(connection, project_id, "investigation.run",
                {"investigation_id": investigation_id}, priority=40)
    emit_event(connection, project_id, "investigation.opened", actor,
               "investigation", investigation_id,
               {"question": question[:500], "allow_fix": bool(allow_fix), "run_id": run_id})
    return {"ok": True, "investigation_id": investigation_id, "status": "queued",
            "allow_fix": bool(allow_fix),
            "note": ("Running now — it reads the code, the database and the host, and takes a "
                     "few minutes. Tell the operator it has started and answer with what you "
                     "already know. The result is posted into this conversation when it lands.")}


def _verdict_from(summary: str) -> dict[str, Any]:
    """Pull the JSON verdict out of the final message; the prose is kept either way."""
    for match in re.finditer(r"```json\s*(\{.*?\})\s*```", summary or "", re.S):
        try:
            parsed = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("cause"):
            return parsed
    return {}


def _repo_for(engine, project_id: str, run_id: str, settings: Settings) -> tuple[str, str]:
    """The repository this investigation should be able to read."""
    from ..db import begin

    owner = str(getattr(settings, "target_github_owner", "") or "")
    repo = ""
    with begin(engine) as c:
        if run_id:
            spec = c.execute(sql("SELECT spec FROM code_task_runs WHERE id = :r"),
                             {"r": run_id}).scalar()
            if isinstance(spec, dict):
                repo = str(spec.get("repo") or "")
                owner = str(spec.get("owner") or owner)
        if not repo:
            repo = str(c.execute(
                sql("SELECT repo FROM deployment_targets WHERE project_id = :p AND repo <> '' "
                    "ORDER BY created_at DESC LIMIT 1"), {"p": project_id}).scalar() or "")
    repo = repo.rsplit("/", 1)[-1]
    return owner, (repo or str(getattr(settings, "target_github_repo", "") or ""))


def _post_to_thread(connection, thread_id: str, investigation_id: str,
                    summary: str, verdict: dict) -> None:
    """Put the result where the question was asked.

    An investigation nobody sees is a report filed in a drawer. It lands in the conversation
    that started it, as its own message, so the answer arrives rather than waiting to be asked
    for a second time.
    """
    from .core import make_id

    cause = str(verdict.get("cause") or "").strip()
    fix = str(verdict.get("fix") or "").strip()
    evidence = verdict.get("evidence") or []
    body = [f"**Investigation {investigation_id} finished.**", ""]
    if cause:
        body.append(cause)
    if isinstance(evidence, list) and evidence:
        body.append("")
        body.append("What proves it:")
        body.extend(f"- {str(item)[:400]}" for item in evidence[:6])
    if fix:
        body.append("")
        body.append(f"**Fix:** {fix}"
                    + ("  ·  *applied and verified*" if verdict.get("applied") else ""))
    if verdict.get("next"):
        body.append("")
        body.append(f"Next: {verdict['next']}")
    if not cause:
        body.append(summary[:3000] or "(the investigation returned nothing)")

    try:
        connection.execute(
            sql("INSERT INTO agent_messages (id, thread_id, role, content, created_at) "
                "VALUES (:i, :t, 'assistant', :c, now())"),
            {"i": make_id("amsg"), "t": thread_id, "c": "\n".join(body)[:12000]})
    except Exception as exc:  # noqa: BLE001 — the record is stored either way
        _log.warning("could not post investigation %s to thread %s: %s",
                     investigation_id, thread_id, exc)


def run_investigation(engine, settings: Settings, *, investigation_id: str) -> dict[str, Any]:
    """Do the work. Runs on the API container, which is where the Claude CLI lives."""
    from ..db import begin
    from .code_agent import ClaudeCodeAgent
    from .sandbox import EphemeralSandbox

    with begin(engine) as c:
        row = c.execute(sql("SELECT * FROM investigations WHERE id = :i"),
                        {"i": investigation_id}).mappings().first()
        if not row:
            return {"ok": False, "error": f"no such investigation: {investigation_id}"}
        record = dict(row)
        if record["status"] not in ("queued", "running"):
            return {"ok": True, "status": record["status"], "already_done": True}
        c.execute(sql("UPDATE investigations SET status = 'running', started_at = now(), "
                      "updated_at = now() WHERE id = :i"), {"i": investigation_id})

    project_id = str(record["project_id"])
    allow_fix = bool(record["allow_fix"])
    started = time.monotonic()

    try:
        with EphemeralSandbox(settings, task_id=investigation_id) as sandbox:
            repo_note = ""
            try:
                owner, repo = _repo_for(engine, project_id, str(record["run_id"] or ""), settings)
                if owner and repo:
                    sandbox.provision(owner, repo, base_branch="main",
                                      token=(getattr(settings, "github_token", "") or None))
                    repo_note = (f" The app's repository ({owner}/{repo}) is checked out there, "
                                 "so you can read the code that actually shipped.")
            except Exception as exc:  # noqa: BLE001 — a missing checkout is not a failed run
                _log.warning("investigation %s: no repo checkout (%s)", investigation_id, exc)
            if sandbox.path is None:
                # Still worth running: the database, the host and the probes are the larger part
                # of a deployment investigation, and refusing over a missing clone would turn a
                # recoverable gap into no answer at all.
                sandbox.path = Path(sandbox._root()) / investigation_id  # noqa: SLF001
                sandbox.path.mkdir(parents=True, exist_ok=True)

            with begin(engine) as c:
                brief = _briefing(c, project_id=project_id, question=str(record["question"]),
                                  run_id=(str(record["run_id"]) if record["run_id"] else None))
            (sandbox.path / "BRIEFING.md").write_text(brief)
            _install_toolbelt(
                sandbox.path, project_id=project_id, allow_fix=allow_fix,
                # A genuinely read-only role when one has been provisioned; the ordinary URL
                # otherwise, with the statement guard as the only barrier. Setting
                # BOSS_INVESTIGATION_DB_URL to a SELECT-only role is the real fix, and this
                # picks it up the moment it exists.
                db_url=str(getattr(settings, "investigation_db_url", "")
                           or os.environ.get("DATABASE_URL") or ""))

            prompt = _PROMPT.format(
                question=str(record["question"]),
                where=str(sandbox.path),
                repo_note=repo_note,
                host_mode=("It runs ANY command — this investigation may change the host."
                           if allow_fix else
                           "Read-only: logs, processes, sockets, disk and service status, "
                           "piped into grep/tail/head/awk/jq."),
                fix_mode=(_FIX_ALLOWED if allow_fix else _FIX_REFUSED),
            )
            data = ClaudeCodeAgent(settings)._invoke(  # noqa: SLF001 — the pipeline's own entry
                prompt, sandbox.path,
                read_only=not allow_fix,
                allowed_tools=("Read,Grep,Glob,Bash,Write,Edit" if allow_fix
                               else "Read,Grep,Glob,Bash"),
                model=(str(getattr(settings, "investigation_model", "") or "") or None),
                max_turns=int(getattr(settings, "investigation_max_turns", 60) or 60),
                seat="investigation",
            )
            summary = str(data.get("result") or "").strip()
            turns = int(data.get("num_turns") or 0)
    except Exception as exc:  # noqa: BLE001
        _log.exception("investigation %s failed", investigation_id)
        with begin(engine) as c:
            c.execute(sql("UPDATE investigations SET status = 'failed', error = :e, "
                          "finished_at = now(), updated_at = now() WHERE id = :i"),
                      {"i": investigation_id, "e": f"{type(exc).__name__}: {exc}"[:1000]})
        return {"ok": False, "investigation_id": investigation_id,
                "error": f"{type(exc).__name__}: {exc}"[:500]}

    verdict = _verdict_from(summary)
    with begin(engine) as c:
        c.execute(
            sql("UPDATE investigations SET status = 'completed', summary = :s, "
                "verdict = CAST(:v AS JSONB), turns = :t, duration_ms = :d, "
                "finished_at = now(), updated_at = now() WHERE id = :i"),
            {"i": investigation_id, "s": summary[:20000], "v": json.dumps(verdict),
             "t": turns, "d": int((time.monotonic() - started) * 1000)})
        emit_event(c, project_id, "investigation.completed", str(record["actor"]),
                   "investigation", investigation_id,
                   {"cause": str(verdict.get("cause") or "")[:500],
                    "applied": bool(verdict.get("applied")), "turns": turns})
        if record.get("thread_id"):
            _post_to_thread(c, str(record["thread_id"]), investigation_id, summary, verdict)

    return {"ok": True, "investigation_id": investigation_id, "status": "completed",
            "verdict": verdict, "turns": turns, "summary": summary[:4000]}


def get_investigation(connection, *, project_id: str,
                      investigation_id: str = "") -> dict[str, Any]:
    """The latest investigation, or a named one."""
    if investigation_id:
        row = connection.execute(
            sql("SELECT * FROM investigations WHERE id = :i AND project_id = :p"),
            {"i": investigation_id, "p": project_id}).mappings().first()
    else:
        row = connection.execute(
            sql("SELECT * FROM investigations WHERE project_id = :p "
                "ORDER BY created_at DESC LIMIT 1"), {"p": project_id}).mappings().first()
    if not row:
        return {"found": False, "note": "no investigation has been opened for this project yet"}
    record = dict(row)
    return {
        "found": True, "investigation_id": record["id"], "status": record["status"],
        "question": record["question"], "allow_fix": record["allow_fix"],
        "verdict": record.get("verdict") or {},
        "summary": (record.get("summary") or "")[:8000],
        "turns": record.get("turns"), "duration_ms": record.get("duration_ms"),
        "error": record.get("error"),
        "note": ("Still running — a real investigation reads the host and the code and takes a "
                 "few minutes. Answer with what you know now and say it is running."
                 if record["status"] in ("queued", "running") else ""),
    }


__all__ = ["open_investigation", "run_investigation", "get_investigation",
           "host_command_allowed", "sql_is_read_only", "run_on_investigation_host"]
