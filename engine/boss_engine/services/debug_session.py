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
  * a project-scoped briefing of BOSS's recorded state;
  * `boss-host`, a shell on any registered fleet host;
  * `boss-do`, BOSS's own operations — the same path the console's buttons take;
  * `boss-probe`, HTTP and TCP against anything it wants to test;
  * and a requirement to end with a verdict it has EVIDENCE for.

WHAT IT MAY CHANGE

Nothing, by default. A default investigation is read-only everywhere: `boss-host` refuses
anything outside an enumerated set of read commands, and the agent has no Edit or Write tool.
Freedom to look is not freedom to act, and that
distinction is the whole reason this can be turned loose at all.

An investigation opened with `fix=True` can change things: `boss-do` runs BOSS's own
operations exactly as the console's buttons run them, and `boss-host` gets a real shell.
It opens when the OPERATOR authorised action — either by clicking, or by
telling BOSS to fix something, which is the same authority the write gate already recognises.
A question still gets an offer.

What no investigation may do, in either mode, whoever asked: cancel work in flight, waive a
gate, change the database's shape, delete in bulk, or run the irreversible shell verbs. After
2026-09-02, when an agent holding production credentials deleted 204 tables, that list is the
line that matters most — and it did not move when write access arrived.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import text as sql

from ..config import Settings
from .core import create_blocking_decision, emit_event

_log = logging.getLogger(__name__)

COMMUNITY_INVESTIGATION_BLOCK_REASON = (
    "Live investigations are unavailable in Community until model calls use durable "
    "worker leases, budget reservations, and reconciled receipts. Host and database "
    "writes also require governed effect records."
)
#: COMMUNITY RUNS A READ-ONLY INVESTIGATION AS GOVERNED WORK (BOSSAPP-571's doorway). Its one
#: model call is spent under the worker's lease of its `investigation.run` job, a budget
#: reservation and a model lease, with a receipt settled at the actual cost (InvestigationSpend,
#: the same governed stage as Discovery's). What stays closed is what still has no governed
#: effect record: a FIXING investigation (host shell writes, BOSS's operations).
COMMUNITY_FIX_BLOCK_REASON = (
    "A fixing investigation is unavailable in Community: it changes the host or BOSS's data, "
    "and those writes require governed effect records, which investigations do not have yet. "
    "Run it read-only "
    "(leave fix unset): a read-only investigation's model call runs under the worker's lease, "
    "a budget reservation and a reconciled receipt."
)
COMMUNITY_UNGOVERNED_REASON = (
    "This investigation was not opened as governed work (no worker lease of its own job), so "
    "Community will not run it: its model call needs a durable worker lease, a budget "
    "reservation and a reconciled receipt. Open it again."
)
_QUALIFIED_INVESTIGATION_EDITIONS = frozenset({
    "cloud", "enterprise", "enterprise_cloud", "enterprise_self_hosted",
})


class InvestigationExecutionBlocked(ValueError):
    """An investigation would start ungoverned model or host effects in Community."""


def _community_investigations_paused(settings: Settings) -> bool:
    """Community (and any unknown spelling): every model call governed, no host or data write.
    Unknown spellings do not silently acquire an unbudgeted model or host shell."""
    return str(settings.boss_edition).strip().lower() not in _QUALIFIED_INVESTIGATION_EDITIONS


def require_investigation_runtime(settings: Settings, *, allow_fix: bool = False) -> None:
    """Refuse what this edition cannot govern, before a row or a job exists: in Community, a
    fixing investigation (its host and data writes have no governed effect record yet)."""
    if _community_investigations_paused(settings) and allow_fix:
        raise InvestigationExecutionBlocked(COMMUNITY_FIX_BLOCK_REASON)


JOB_KIND = "investigation.run"


def _spend_class():
    """InvestigationSpend: Discovery's governed stage, for an investigation's one model call."""
    from .discovery_spend import DiscoveryStageSpend, DiscoveryStopped

    class InvestigationSpend(DiscoveryStageSpend):
        IDENTITY = "investigation"
        LABEL = "Investigation"
        EVENT_PREFIX = "investigation"
        ENTITY = "investigation"
        AGENT_ID = "investigation"
        SKILL_ID = "investigation.debug"
        CAPABILITIES = ("research",)

        def check_active(self, connection) -> None:
            """The worker still holds THIS investigation's job, and the row is still running
            under it (a cancelled or finished investigation spends nothing more)."""
            job = self.gateway.jobs.require_active_lease(
                connection, self.worker_id, self.job_id, self.job_lease_token)
            payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
            if (job["project_id"] != self.project_id or job["kind"] != JOB_KIND
                    or payload.get("investigation_id") != self.run_id):
                raise DiscoveryStopped("invalid_lineage")
            row = connection.execute(sql(
                "SELECT status, job_id FROM investigations WHERE id = :i AND project_id = :p "
                "FOR UPDATE"), {"i": self.run_id, "p": self.project_id}).mappings().first()
            if not row or row["status"] != "running" or row["job_id"] != self.job_id:
                raise DiscoveryStopped(str(row["status"]) if row else "missing")

    return InvestigationSpend

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


#: Statements refused in EVERY mode. A fix investigation may change rows; it may not change
#: the SHAPE of the database or hand out permissions, and it may not delete in bulk. Those are
#: migrations and administration — different acts, with a different review.
_SQL_NEVER = re.compile(r"\b(drop|truncate|alter|grant|revoke|create\s+(?:table|database|role)|"
                        r"vacuum|reindex|copy)\b", re.I)


def _without_string_literals(statement: str) -> str:
    """The statement with quoted text blanked, so a guard reads SQL rather than data.

    Keeps the same length and shape — only the contents of '...' and "..." become spaces — so
    every offset and every word boundary outside the quotes is unchanged.
    """
    out: list[str] = []
    quote = ""
    source = statement or ""
    offset = 0
    while offset < len(source):
        char = source[offset]
        if quote:
            if char == "\\" and offset + 1 < len(source):
                # PostgreSQL E'...' escapes a quote; treating it as the end of the
                # string would expose a fake WHERE to the guard.
                out.extend("  ")
                offset += 2
                continue
            if char == quote and offset + 1 < len(source) and source[offset + 1] == quote:
                out.extend("  ")
                offset += 2
                continue
            out.append(" " if char != quote else char)
            if char == quote:
                quote = ""
        elif char in ("'", '"'):
            quote = char
            out.append(char)
        elif char == "$":
            # PostgreSQL dollar strings can hold the same "where" payload as quotes.
            delimiter = re.match(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$", source[offset:])
            if delimiter:
                token = delimiter.group(0)
                end = source.find(token, offset + len(token))
                if end >= 0:
                    length = end + len(token) - offset
                    out.extend(" " * length)
                    offset += length
                    continue
            out.append(char)
        else:
            out.append(char)
        offset += 1
    return "".join(out)


def _top_level_sql(statement: str) -> str | None:
    """Keep SQL outside parentheses; a CTE or subquery cannot qualify an outer write."""
    depth = 0
    out: list[str] = []
    for char in statement:
        if char == "(":
            depth += 1
            out.append(" ")
        elif char == ")":
            if depth == 0:
                return None
            depth -= 1
            out.append(" ")
        else:
            out.append(char if depth == 0 else " ")
    return "".join(out) if depth == 0 else None


def sql_is_allowed(statement: str, *, allow_write: bool) -> tuple[bool, str]:
    """Whether an investigation may run this statement.

    Read mode: SELECT only. Fix mode: rows may be written, because an investigation that can
    diagnose a stuck retry and not clear it is half a capability — but never DDL, never a
    grant, and never an unqualified DELETE or UPDATE, which is how one wrong statement becomes
    an outage rather than a fix.
    """
    body = (statement or "").strip().rstrip(";")
    if not body:
        return False, "empty statement"
    # A SEMICOLON INSIDE A STRING IS NOT A SECOND STATEMENT.
    #
    # The naive check rejected every reconciliation whose text happened to contain one — an
    # investigation hit it writing an honest `last_error` and reported it as a defect in my own
    # guard, which it was. What "one statement at a time" is defending against is a second
    # statement, so look outside the quotes.
    if ";" in _without_string_literals(body):
        return False, ("one statement at a time (a semicolon inside a quoted string is fine; "
                       "this one separates statements)")
    visible = _without_string_literals(body)
    # Comments can hide a write or supply a fake WHERE. An investigation needs neither.
    if any(marker in visible for marker in ("--", "/*", "*/")):
        return False, "SQL comments are not permitted in an investigation"
    outer = _top_level_sql(visible)
    if outer is None:
        return False, "unbalanced SQL parentheses"
    head = body.split(None, 1)[0].lower() if body.split() else ""
    if _SQL_NEVER.search(visible):
        return False, ("schema changes, grants and bulk copies are refused in every mode — "
                       "that is a migration, not an investigation")
    if head == "with":
        # WITH is a prefix, not a read. Find the verb after the CTE bodies, and refuse
        # data-changing CTEs whose inner effect would otherwise escape that classification.
        outer_verb = re.search(r"\b(select|update|delete|insert)\b", outer, re.I)
        if outer_verb is None:
            return False, "a WITH statement needs a supported outer operation"
        if re.search(r"\b(update|delete|insert)\b", visible[:outer_verb.start()], re.I):
            return False, "data-changing CTEs are not permitted in an investigation"
        head = outer_verb.group(1).lower()
        outer = outer[outer_verb.start():]
    if head in ("select", "explain", "table", "show"):
        if re.search(r"\b(update|delete|insert)\b", visible, re.I):
            return False, "read-only: the statement contains a write keyword"
        return True, ""
    if not allow_write:
        return False, (f"this investigation is read-only ({head!r} refused). Work out exactly "
                       "what needs changing and put it in `fix`; an operator can open a fixing "
                       "investigation, or apply it from the console in one click.")
    if head not in ("insert", "update", "delete"):
        return False, f"{head!r} is not a statement an investigation may run"
    if head in ("update", "delete") and not re.search(r"\bwhere\b", outer, re.I):
        return False, (f"an unqualified {head.upper()} would hit every row. Add a WHERE, or use "
                       "boss-do to call the operation that does this properly.")
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
#: THE SANDBOX HOLDS ONE CREDENTIAL AND IT IS NOT THE DATABASE.
#:
#: The first version baked the connection string into the working directory. That hands a shell
#: a permanent credential, and it was worse than it looked: the commands ran IN the sandbox
#: subprocess, whose environment `agent_environment` strips — so `BOSS_KMS_MASTER_KEY` was
#: absent, and the first `boss-host` call auto-provisioned a replacement master key row. The
#: investigation found that itself and reported it as "a silent wrong-key fallback if that var
#: were ever unset". It was right, and the cause was the architecture, not the fallback.
#:
#: So the commands are thin HTTP clients now. They call BOSS's own API — the same path the
#: console's buttons take, with the same validation, the same secret resolution and the same
#: audit trail — carrying a token minted for this investigation and cleared when it ends. The
#: database URL, the KMS key and the SSH keys never leave the API process.
_CLIENT_PREAMBLE = '''#!/usr/bin/env python3
import json, sys, urllib.error, urllib.request

BASE = "__BOSS_BASE__"
INVESTIGATION = "__BOSS_INVESTIGATION__"
TOKEN = "__BOSS_TOKEN__"


def call(path, payload):
    request = urllib.request.Request(
        BASE + "/v1/investigations/" + INVESTIGATION + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Investigation-Token": TOKEN},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()[:900]
        print("BOSS refused: " + body, file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        print("could not reach BOSS: %s" % exc, file=sys.stderr)
        raise SystemExit(3)
'''

_DB_SCRIPT = _CLIENT_PREAMBLE + '''
"""boss-db - SQL against BOSS's database, through BOSS."""
statement = " ".join(sys.argv[1:]) or sys.stdin.read()
out = call("/db", {"sql": statement})
if out.get("rows") is not None:
    print(json.dumps(out["rows"], indent=1, default=str))
    if out.get("truncated"):
        print("... more rows (add LIMIT)", file=sys.stderr)
else:
    print(json.dumps(out, indent=1, default=str))
'''

_HOST_SCRIPT = _CLIENT_PREAMBLE + '''
"""boss-host - run a command on a registered fleet host over SSH."""
if len(sys.argv) < 3:
    print("usage: boss-host <host> <command...>", file=sys.stderr)
    raise SystemExit(64)
out = call("/host", {"host": sys.argv[1], "command": " ".join(sys.argv[2:])})
sys.stdout.write(out.get("stdout") or "")
sys.stderr.write(out.get("stderr") or "")
raise SystemExit(int(out.get("exit_code") or 0))
'''

_DO_SCRIPT = _CLIENT_PREAMBLE + '''
"""boss-do - run one of BOSS's own operations, the way the console's buttons do."""
if len(sys.argv) < 2:
    print("usage: boss-do <tool> [json-args]     e.g. boss-do redeploy '{\"run_id\": \"crun_1\"}'",
          file=sys.stderr)
    print("       boss-do --list                 to see what is available", file=sys.stderr)
    raise SystemExit(64)
if sys.argv[1] in ("--list", "-l"):
    print(json.dumps(call("/do", {"list": True}), indent=1))
    raise SystemExit(0)
args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
print(json.dumps(call("/do", {"tool": sys.argv[1], "args": args}), indent=1, default=str))
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
    from ..config import get_settings

    settings = get_settings()
    if _community_investigations_paused(settings) and allow_write:
        # A READ on a host is not an effect; a write is, and has no governed record yet.
        return {"ok": False, "error": COMMUNITY_FIX_BLOCK_REASON}

    allowed, why = host_command_allowed(command, allow_write=allow_write)
    if not allowed:
        return {"ok": False, "error": why}

    from ..db import begin, get_engine
    from .deploy_service import _discard_key_file, _materialise_key, _ssh, _ssh_key_file
    from .secret_backends import resolve_target_secret

    ephemeral = None
    with begin(get_engine(settings)) as c:
        row = c.execute(
            sql(f"SELECT * FROM deployment_hosts WHERE {_usable_hosts()} AND (id = :h OR name = :h)"),
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


def _install_toolbelt(root: Path, *, base_url: str, investigation_id: str,
                      token: str) -> Path:
    """Write the helper commands into the sandbox.

    Substitution rather than str.format: these scripts are full of JSON braces, and formatting
    them was a KeyError waiting for the first command that took a payload.
    """
    binaries = root / ".boss-bin"
    binaries.mkdir(parents=True, exist_ok=True)
    for name, body in (("boss-db", _DB_SCRIPT), ("boss-host", _HOST_SCRIPT),
                       ("boss-do", _DO_SCRIPT), ("boss-probe", _PROBE_SCRIPT)):
        path = binaries / name
        path.write_text(body
                        .replace("__BOSS_BASE__", base_url)
                        .replace("__BOSS_INVESTIGATION__", investigation_id)
                        .replace("__BOSS_TOKEN__", token))
        path.chmod(0o700)          # owner only: the token lives in three of these
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
          f"SELECT id, name, address, status, arch FROM deployment_hosts WHERE {_usable_hosts()}",
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

  ./.boss-bin/boss-host <host> <command>    a shell on a registered fleet host over SSH.
                                            {host_mode}
  ./.boss-bin/boss-do <tool> '<json>'       BOSS's OWN operations — redeploy, retry_code_task,
                                            create_deployment_target, retarget_app and the
                                            rest — run exactly as the console's buttons run
                                            them, with the same validation, secret resolution
                                            and audit trail. `boss-do --list` shows what is
                                            available. {do_mode}
  ./.boss-bin/boss-probe <url|host:port>    is it answering? HTTP or a raw socket.

PREFER boss-do FOR CHANGES. An operation knows things a statement does not: `redeploy` resolves
the target's key, reserves budget, records a deployment run and health-checks the result. A
hand-written UPDATE gets some of that right and leaves no trace of the rest. Raw SQL is held
until the database can enforce the investigation's project boundary. If no operation covers
what you need, say in `fix` which operation is missing.

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

_FIX_ALLOWED = """YOU MAY FIX IT. This investigation has fix permission: `boss-do` runs BOSS's
operations, and `boss-host` runs commands that change the machine. That
is the point — an investigation that can diagnose a stuck retry and not clear it is half a
capability.

Use it the way a careful engineer would. Make the smallest change that resolves the problem.
Prefer `boss-do` to a shell command because it knows what else needs to happen.
Verify with boss-probe or by re-running the failing
check, and record exactly what you changed under `evidence`. Set "applied": true only if you
made the change AND verified the result.

What you may not do, whatever it would achieve: throw away work in flight (cancelling a
running build is refused, deliberately and permanently), delete data to make a symptom go
away, restart services that are not part of the problem, or change the database's shape. If
the fix needs one of those, put it in `fix` and stop — that is a decision for a person."""

_FIX_REFUSED = """YOU ARE READ-ONLY. `boss-host`
refuses anything that could change the machine. This is not an obstacle to route around: work
out exactly what needs to change, prove it with evidence, and put it in `fix` so an operator
can apply it in one step. Do not spend turns on commands that will be refused."""


# --------------------------------------------------------------------------- #
#  Lifecycle                                                                   #
# --------------------------------------------------------------------------- #
def open_investigation(connection, *, project_id: str, question: str, actor: str,
                       thread_id: Optional[str] = None, run_id: Optional[str] = None,
                       allow_fix: bool = False,
                       settings: Optional[Settings] = None) -> dict[str, Any]:
    """Record the intent and queue the work. Returns immediately — this takes minutes."""
    from ..config import get_settings
    from .jobs import enqueue_job

    # This direct service boundary also covers the engine tool and future callers. Refuse
    # before touching a row or queueing a job what this edition cannot govern (Community: a
    # fixing investigation). A read-only one is queued as governed work below.
    require_investigation_runtime(settings or get_settings(), allow_fix=allow_fix)

    # AN INVESTIGATION THE CONTAINER OUTLIVED IS NOT STILL RUNNING.
    #
    # It executes inside the API process, so a deploy — or a crash — leaves the row `running`
    # with nobody working on it. Combined with the one-at-a-time guard below, that meant the
    # first restart during an investigation permanently disabled the feature for that project:
    # every later attempt was refused in favour of a session that had stopped existing.
    #
    # `updated_at` moves on every state change, so a row that has not moved in the window is
    # abandoned. Marked, not silently reused, because "it stopped when the API restarted" is
    # something the operator should be able to read afterwards.
    connection.execute(
        sql("UPDATE investigations SET status = 'abandoned', token = NULL, "
            "error = 'the process running it stopped (deploy or restart) before it finished', "
            "finished_at = now(), updated_at = now() "
            "WHERE project_id = :p AND status IN ('queued', 'running') "
            "  AND updated_at < now() - INTERVAL '25 minutes'"),
        {"p": project_id})
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
            "allow_fix, status, token, created_at, updated_at) "
            "VALUES (:i, :p, :t, :r, :q, :a, :f, 'queued', :k, now(), now())"),
        {"i": investigation_id, "p": project_id, "t": thread_id, "r": run_id,
         "q": question[:4000], "a": actor, "f": bool(allow_fix),
         # The sandbox's only credential, and it authorises exactly this investigation for
         # exactly as long as it runs.
         "k": uuid.uuid4().hex + uuid.uuid4().hex})
    # A DURABLE JOB, and the investigation names it: the worker that leases it is the only one
    # whose call may spend for this investigation (InvestigationSpend.check_active).
    job = enqueue_job(connection, project_id, JOB_KIND,
                      {"investigation_id": investigation_id, "agent_id": "investigation",
                       "actor": actor},
                      priority=40, max_attempts=1, dedupe_key=f"investigation:{investigation_id}",
                      # The workload a model source admits for the install's own work, as
                      # Discovery's job declares (entitlements: workload_classes_allowed).
                      workload_class="owner_sandbox", data_boundary="internal")
    connection.execute(sql("UPDATE investigations SET job_id = :j WHERE id = :i"),
                       {"j": (job or {}).get("id"), "i": investigation_id})
    emit_event(connection, project_id, "investigation.opened", actor,
               "investigation", investigation_id,
               {"question": question[:500], "allow_fix": bool(allow_fix), "run_id": run_id})
    return {"ok": True, "investigation_id": investigation_id, "status": "queued",
            "allow_fix": bool(allow_fix),
            "note": ("Running now — it reads the code, the database and the host, and takes a "
                     "few minutes. Tell the operator it has started and answer with what you "
                     "already know. The result is posted into this conversation when it lands.")}


#: Tool names as an operator would say them.
_DOING = {
    "Bash": "running", "Read": "reading", "Grep": "searching", "Glob": "looking for",
    "Edit": "editing", "Write": "writing", "TodoWrite": "planning",
}


def _what_it_is_doing(turn: dict) -> str:
    """One line describing a single turn of the investigation, for the operator to read.

    "Investigating…" for eight minutes is indistinguishable from "stuck", and an operator who
    believes a system is stuck interrupts it — which is how the last incident started. What
    removes that impression is not a nicer spinner; it is showing the actual work.
    """
    direction = str(turn.get("direction") or "")
    if direction == "completion":
        # Its own reasoning, which is the most informative thing on offer: "the container is
        # bound to 8000 but the target says 3000" tells the operator more than any tool name.
        first = " ".join(str(turn.get("content") or "").split())
        return first[:180] if first else ""
    if direction != "tool_call":
        return ""
    name = str(turn.get("tool_name") or "")
    args = turn.get("content") if isinstance(turn.get("content"), dict) else {}
    detail = ""
    if name == "Bash":
        detail = str(args.get("command") or "")
        # The toolbelt is where the interesting work happens; say what it did, not the path.
        for command, said in (("boss-db", "querying the database"),
                              ("boss-host", "on the host"),
                              ("boss-do", "running a BOSS operation"),
                              ("boss-probe", "probing")):
            if command in detail:
                return f"{said}: {detail.split(command, 1)[1].strip()[:140]}"
    elif name in ("Read", "Edit", "Write"):
        detail = str(args.get("file_path") or "")
    elif name in ("Grep", "Glob"):
        detail = str(args.get("pattern") or args.get("query") or "")
    verb = _DOING.get(name, name.lower())
    return f"{verb} {detail}".strip()[:180]


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


def _post_failure_to_thread(connection, thread_id: Optional[str], investigation_id: str,
                            error: Any) -> None:
    """A FAILED investigation says so where it was asked, in words.

    The agent tells the operator "the result is posted into this conversation when it lands";
    a failure is a result too. On the laptop (2026-10-24) one failed in 0.4 s on a refused model
    source and the conversation never heard: the operator was left waiting on a promise. The
    words are model_failures.explain's — the same sentence every surface gives for that cause —
    never a stack trace or a credential.
    """
    if not thread_id:
        return
    from .core import make_id
    from .model_failures import explain

    try:
        said = explain(error)
        words = f"{said.get('headline') or ''} {said.get('action') or ''}".strip()
    except Exception:  # noqa: BLE001 — the reason in plain text still beats silence
        words = ""
    reason = str(error or "").strip()
    body = [f"**Investigation {investigation_id} did not run to a verdict.**", "",
            words or reason[:600] or "It stopped without saying why."]
    if words and reason and reason not in words:
        body += ["", f"What BOSS saw: {reason[:600]}"]
    body += ["", "Nothing was changed. Ask again once that is fixed, or ask me what I can find "
                 "with my own tools meanwhile."]
    try:
        connection.execute(
            sql("INSERT INTO agent_messages (id, thread_id, role, content, created_at) "
                "VALUES (:i, :t, 'assistant', :c, now())"),
            {"i": make_id("amsg"), "t": thread_id, "c": "\n".join(body)[:6000]})
    except Exception as exc:  # noqa: BLE001 — the record is stored either way
        _log.warning("could not post investigation %s failure to thread %s: %s",
                     investigation_id, thread_id, exc)


def block_community_investigation(
    connection, settings: Settings, investigation_id: str, *,
    lease: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """In Community, retire a run that cannot be governed before it reaches the model or a host:
    a fixing one (no governed effect records for its writes), or one with no worker lease of
    its own job (opened before investigations were governed, or called without the lease). A
    read-only one under the lease of the job it names runs (None).

    A legacy running row might already have spent money before an upgrade. It needs an
    operator decision to reconcile that uncertain spend; replaying it would compound it.
    """
    if not _community_investigations_paused(settings):
        return None
    row = connection.execute(
        sql("SELECT id, project_id, status, allow_fix, job_id, thread_id FROM investigations "
            "WHERE id = :i FOR UPDATE"),
        {"i": investigation_id},
    ).mappings().first()
    if row is None:
        return {"ok": False, "error": f"no such investigation: {investigation_id}"}
    if row["status"] not in ("queued", "running"):
        return {"ok": True, "status": row["status"], "already_done": True}
    governed = bool(lease and row["job_id"] and lease.get("job_id") == row["job_id"])
    if governed and not row["allow_fix"]:
        return None
    reason = (COMMUNITY_FIX_BLOCK_REASON if row["allow_fix"] else
              COMMUNITY_UNGOVERNED_REASON if row["job_id"] else
              COMMUNITY_INVESTIGATION_BLOCK_REASON)

    # A governed row that is running spent under its own receipts: those say whether a call
    # needs reconciling (InvestigationSpend). Only an ungoverned running row's spend is unknown.
    was_running = row["status"] == "running" and not row["job_id"]
    connection.execute(
        sql("UPDATE investigations SET status = 'failed', error = :reason, "
            "token = NULL, finished_at = now(), updated_at = now() WHERE id = :i"),
        {"i": investigation_id, "reason": reason},
    )
    if was_running:
        create_blocking_decision(
            connection, row["project_id"], None,
            "Check the spend of an investigation that was interrupted while it ran",
            {"action": "review_model_spend_and_host_effects_before_retry"},
            {"investigation_id": investigation_id, "recorded_usage": "unknown"},
            "investigation_unverified_effects", default_action="hold",
        )
    emit_event(
        connection, row["project_id"], "investigation.blocked", "boss",
        "investigation", investigation_id,
        {"reason": "community_investigation_governance_gate",
         "effect_reconciliation_required": was_running},
    )
    _post_failure_to_thread(connection, row.get("thread_id"), investigation_id, reason)
    return {"ok": False, "status": "blocked", "investigation_id": investigation_id,
            "error": reason}


def run_investigation(engine, settings: Settings, *, investigation_id: str,
                      lease: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Do the work. Runs on the API container, which is where the Claude CLI lives.

    `lease` is the worker's lease of the investigation's job ({job_id, worker_id,
    lease_token}). With it, the model call is GOVERNED (InvestigationSpend: budget reservation,
    model lease and a receipt settled at the actual cost, replayed rather than paid twice);
    Community requires it."""
    from ..db import begin

    # Defense in depth for direct service callers, including any worker path other than HTTP.
    # This must run before importing or invoking the paid CLI or provisioning a sandbox.
    with begin(engine) as c:
        blocked = block_community_investigation(c, settings, investigation_id, lease=lease)
    if blocked is not None:
        return blocked

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
        if lease:
            # The lease first: a worker that lost it starts nothing, not even the row.
            from .jobs import JobLeaseService, LeaseNotFoundError

            try:
                JobLeaseService(settings).require_active_lease(
                    c, str(lease["worker_id"]), str(lease["job_id"]), str(lease["lease_token"]))
            except LeaseNotFoundError as error:
                return {"ok": False, "status": "stopped", "error": f"lease: {error}"}
        c.execute(sql("UPDATE investigations SET status = 'running', started_at = now(), "
                      "updated_at = now() WHERE id = :i"), {"i": investigation_id})

    project_id = str(record["project_id"])
    spend = None
    if lease:
        spend = _spend_class().for_engine(
            settings, engine, project_id=project_id, run_id=investigation_id,
            job_id=str(lease["job_id"]), worker_id=str(lease["worker_id"]),
            job_lease_token=str(lease["lease_token"]), actor=str(record["actor"]))
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
                sandbox.path,
                base_url=str(getattr(settings, "investigation_api_url", "")
                             or "http://127.0.0.1:8000"),
                investigation_id=investigation_id,
                token=str(record.get("token") or ""))

            prompt = _PROMPT.format(
                question=str(record["question"]),
                where=str(sandbox.path),
                repo_note=repo_note,
                host_mode=("It runs ANY command — this investigation may change the host."
                           if allow_fix else
                           "Read-only: logs, processes, sockets, disk and service status, "
                           "piped into grep/tail/head/awk/jq."),
                do_mode=("Available." if allow_fix else
                         "Read-only investigations may not run operations — name the one you "
                         "would have run, with its arguments, in `fix`."),
                fix_mode=(_FIX_ALLOWED if allow_fix else _FIX_REFUSED),
            )
            from .intent_router import _progress_reporter

            report = _progress_reporter(project_id, str(record.get("thread_id") or ""))

            beats = {"n": 0}

            def _tell_the_operator(turn: dict) -> None:
                line = _what_it_is_doing(turn)
                if line and report:
                    report(line)
                # A HEARTBEAT, so "stale" means stalled rather than merely long. Without it the
                # abandonment sweep would have to guess a window longer than the slowest
                # investigation, and would still reclaim a live one eventually. Every tenth
                # turn: often enough to be unambiguous, rare enough to be free.
                beats["n"] += 1
                if beats["n"] % 10 == 0:
                    try:
                        with begin(engine) as hb:
                            hb.execute(sql("UPDATE investigations SET updated_at = now() "
                                           "WHERE id = :i"), {"i": investigation_id})
                    except Exception:  # noqa: BLE001 — a heartbeat must not fail the work
                        pass

            # THE CREDENTIAL IT RUNS ON, AND THE SOURCE IT BOOKS, FROM ONE RESOLUTION
            # (claude_credential): the subscription login or the stored key, in the operator's
            # order — bound as Discovery's and the builds' CLI is, never the bare environment.
            from .claude_credential import bind_claude_credential

            with engine.begin() as _c:
                bound = bind_claude_credential(settings, connection=_c, project_id=project_id)
            agent = ClaudeCodeAgent(bound)

            def _call() -> dict:
                data = agent._invoke(  # noqa: SLF001 — the pipeline's own entry
                    prompt, sandbox.path,
                    on_turn=_tell_the_operator,
                    read_only=not allow_fix,
                    allowed_tools=("Read,Grep,Glob,Bash,Write,Edit" if allow_fix
                                   else "Read,Grep,Glob,Bash"),
                    model=(str(getattr(settings, "investigation_model", "") or "") or None),
                    max_turns=int(getattr(settings, "investigation_max_turns", 60) or 60),
                    seat="investigation",
                )
                usage, cost = agent._usage(data, settings)  # noqa: SLF001
                return {"result": str(data.get("result") or ""),
                        "num_turns": int(data.get("num_turns") or 0),
                        "usage": usage, "cost_usd_micros": int(cost or 0),
                        "model": data.get("model")}

            if spend is not None:
                # GOVERNED: reserve, lease the model, receipt in progress (committed), the call,
                # settle at the actual cost. A receipt that already succeeded replays its answer;
                # one still in progress blocks for reconciliation, never a second call.
                from .discovery_spend import research_source_id

                with engine.begin() as _c:
                    source = research_source_id(_c, settings, project_id)
                data = spend.run("investigate", _call, source_id=source)
            else:
                data = _call()
            summary = str(data.get("result") or "").strip()
            turns = int(data.get("num_turns") or 0)
    except Exception as exc:  # noqa: BLE001
        _log.exception("investigation %s failed", investigation_id)
        with begin(engine) as c:
            c.execute(sql("UPDATE investigations SET status = 'failed', error = :e, "
                          "token = NULL, finished_at = now(), updated_at = now() "
                          "WHERE id = :i"),
                      {"i": investigation_id, "e": f"{type(exc).__name__}: {exc}"[:1000]})
            emit_event(c, project_id, "investigation.failed", str(record["actor"]),
                       "investigation", investigation_id,
                       {"error": f"{type(exc).__name__}: {exc}"[:500]})
            _post_failure_to_thread(c, record.get("thread_id"), investigation_id, exc)
        return {"ok": False, "investigation_id": investigation_id,
                "error": f"{type(exc).__name__}: {exc}"[:500]}

    verdict = _verdict_from(summary)
    # RAN OUT OF TURNS IS NOT FINISHED.
    #
    # Measured: a session spent most of its budget working around a broken toolbelt, stopped at
    # the ceiling, and was stored `completed` with an empty verdict — so the console showed a
    # finished investigation with nothing in it, and the operator had to query the summary
    # column to discover it had not finished. That is the same lie as a deploy step going green
    # over a failure, in the one place built to end that class of lie.
    _finished = bool(verdict.get("cause"))
    _ran_out = (not _finished
                and ("ran out of turns" in summary.lower()
                     or turns >= int(getattr(settings, "investigation_max_turns", 60) or 60)))
    status = "completed" if _finished else ("incomplete" if _ran_out else "completed")
    with begin(engine) as c:
        c.execute(
            # THE TOKEN DIES WITH THE INVESTIGATION. The sandbox is torn down, but a token
            # recovered from one afterwards must open nothing.
            sql("UPDATE investigations SET status = :st, summary = :s, "
                "verdict = CAST(:v AS JSONB), turns = :t, duration_ms = :d, token = NULL, "
                "error = :e, finished_at = now(), updated_at = now() WHERE id = :i"),
            {"i": investigation_id, "st": status, "s": summary[:20000],
             "v": json.dumps(verdict), "t": turns,
             "e": (f"stopped at the {turns}-turn ceiling without reaching a verdict"
                   if status == "incomplete" else None),
             "d": int((time.monotonic() - started) * 1000)})
        emit_event(c, project_id, f"investigation.{status}", str(record["actor"]),
                   "investigation", investigation_id,
                   {"cause": str(verdict.get("cause") or "")[:500],
                    "applied": bool(verdict.get("applied")), "turns": turns})
        if record.get("thread_id"):
            _post_to_thread(c, str(record["thread_id"]), investigation_id, summary, verdict)

    return {"ok": True, "investigation_id": investigation_id, "status": status,
            "verdict": verdict, "turns": turns, "summary": summary[:4000]}


# --------------------------------------------------------------------------- #
#  What the sandbox's commands reach — server side, where the credentials are   #
# --------------------------------------------------------------------------- #
def authorise(connection, investigation_id: str, token: str) -> dict[str, Any]:
    """The investigation this token belongs to, if it is still running.

    Minted per investigation and cleared when it ends, so a token recovered from a finished
    sandbox opens nothing. The mode travels with it: a read-only investigation cannot acquire
    write access by calling a different endpoint.
    """
    row = connection.execute(
        sql("SELECT id, project_id, actor, allow_fix, status, thread_id FROM investigations "
            "WHERE id = :i AND token = :t"),
        {"i": investigation_id, "t": token or ""}).mappings().first()
    if not row:
        raise PermissionError("unknown or expired investigation token")
    if row["status"] not in ("queued", "running"):
        raise PermissionError(f"investigation {investigation_id} is {row['status']}")
    return dict(row)


def run_db(connection, record: dict, statement: str) -> dict[str, Any]:
    """Fail closed until sandbox SQL can enforce the investigation's project boundary."""
    return {"ok": False, "error": "raw SQL is unavailable in project-scoped investigations; "
            "use governed project-scoped operations"}


#: Operations an investigation may perform. The registry's own writes, minus the ones that
#: throw work away — an investigation exists to make things work, and nothing in that job needs
#: to cancel a running build. The operator's rule, in the operator's words: cancelling "should
#: be prevented at all cost".
_NOT_FOR_INVESTIGATIONS = frozenset({
    "cancel_job", "cancel_code_task", "close_orphaned_runs", "waive_gates",
    "write_db", "run_on_host", "generate_image", "create_discovery", "request_meeting",
    "decide", "delete_workflow",
})


def run_operation(ctx, record: dict, tool: str, args: dict) -> dict[str, Any]:
    """One of BOSS's own operations, run exactly as the console's buttons run it.

    THE SAME PATH, DELIBERATELY. `redeploy` resolves the target's SSH key from the secret
    store, reserves budget, records a deployment_run and health-checks the result. A shell
    doing that by hand would get some of it right and no audit trail at all — which is what
    hand-written SQL against `deployment_targets` already demonstrated.
    """
    from ..tools import build_default_registry
    from ..tools.registry import ToolError

    registry = build_default_registry()
    available = sorted(
        s.name for s in registry.specs(kind="write")
        if s.name not in _NOT_FOR_INVESTIGATIONS and s.self_executable)
    if not record["allow_fix"]:
        return {"ok": False,
                "error": "this investigation is read-only, so it may not run operations. Put "
                         "what should be done in `fix` and an operator applies it in one click.",
                "available_in_a_fixing_investigation": available}
    if tool in _NOT_FOR_INVESTIGATIONS:
        return {"ok": False,
                "error": f"{tool!r} is not something an investigation may do — it throws work "
                         "away or starts new work rather than repairing what is broken. Say "
                         "what needs it in `fix` and let the operator decide.",
                "available": available}
    try:
        spec = registry.get(tool)
    except ToolError as exc:
        return {"ok": False, "error": str(exc), "available": available}
    if spec.kind != "write":
        return {"ok": False, "error": f"{tool!r} changes nothing — use project-scoped reads instead"}
    if not spec.self_executable:
        return {"ok": False, "requires_confirmation": True,
                "error": f"{tool!r} requires its own operator click; describe the action "
                         "and let the operator run it from the console.",
                "available": available}
    from ..config import get_settings

    if _community_investigations_paused(getattr(ctx, "settings", None) or get_settings()):
        return {"ok": False, "error": COMMUNITY_INVESTIGATION_BLOCK_REASON}
    return registry.invoke(
        spec.name, ctx, project_id=str(record["project_id"]), actor=str(record["actor"]),
        args=args or {})


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
           "host_command_allowed", "sql_is_read_only", "sql_is_allowed",
           "run_on_investigation_host", "authorise", "run_db", "run_operation"]


def _usable_hosts(alias: str = "") -> str:
    """deployment_fleet.usable_hosts_sql: a project's hosts include the ones its organisation shares."""
    from .deployment_fleet import usable_hosts_sql

    return usable_hosts_sql(alias)
