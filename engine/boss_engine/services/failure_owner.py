"""EVERY FAILURE HAS AN OWNER — nothing in BOSS is ever left as a bare "failed".

THE OPERATOR'S WORDS (2026-11-19, said many times). "Any problems in the system the Agents should
be actively involved, assume there are no humans to take a look. Agents should care about what's
going on, attempt to fix, have a proper reason why it cannot be fixed, tag Human for Action
Items, so they can fix and retry. Just leaving it as Failed is not Agentic Software factory."

THE CASE (laptop, 2026-11-19). 66 failed and 133 blocked builds, 27 failed and 11 dead-lettered
workflow runs, 686 dead-lettered jobs (355 model-connection proofs, 202 docs updates, 50
repository indexes, 7 merges, 3 panel steps), 10 failed deployments, 4 failed previews and 2
failed backups. Some of them had an agent on them — a build's bounded automatic retry (#222,
#227), a workflow step's recovery agent (step_recovery), the Shepherd's diagnosis (stuck_work),
a deploy's repair build (deploy_repair) — but most had nothing: 739 dead letters sat `open`
with no remediation ever tried, and nothing anywhere said who owned them.

THE INVARIANT, FOR THE CLASS. Every failed item a person could see (within REACH_DAYS, the
reach every surface uses for "the project's failure", build_failure.REACH_DAYS) is, after each
monitor tick, in exactly one of three states, recorded in `failure_owners`:

  * fixing        an agent is acting on it: BOSS's own bounded attempt ("BOSS is fixing this:
                  attempt 1 of 2 — …"), or another BOSS agent that already owns it (a build's
                  automatic retry, a step's recovery agent, a repair build, the proof schedule);
  * needs_person  ONE plain-words action item for a person (a decision, the Needs-you card and
                  the bell), naming exactly what to do and WHY BOSS could not do it, with a
                  "Done — retry" button that runs it again;
  * resolved      it recovered, a newer attempt replaced it, or a person dismissed it.

`unowned()` is the invariant as a query, and engine/tests/test_every_failure_has_an_owner.py
enforces it — over every surface below, and over every table the code writes a failure state
to (a table that fails rows and is neither a surface here nor in EXEMPT, with its reason, fails
the guard).

ONE OWNER LOOP (`sweep`, on the monitor tick), no second engine. For each failed item:
  1. SETTLED?   it recovered or a newer attempt replaced it → resolved (and its card withdrawn).
  2. MOVING?    a newer attempt of the same lineage is running → fixing (ours or another's).
  3. HELD?      another BOSS agent or card already owns it → fixing / needs_person, by that one.
  4. CLASSIFY   app (the work's own fault) / platform (BOSS's side) / person (only a person can:
                a login, a key, a repository, a budget, a grant, a host) — the record's own
                class first (failure_class, failure_kind, the deploy's cause), then the words.
  5. ACT        app → a fix on the item's own lineage (a repair build told what failed), then
                it runs again; platform → a bounded retry with backoff (MAX_ATTEMPTS, BACKOFF);
                spent → a BOSS self-fix item (boss_recommendations) and the person's action
                item; person → the action item at once. Work that failed more than ACT_HOURS ago
                is not re-run by itself (what it was for may have changed): the item says so.
Every attempt is recorded on the owner row (`history`) and as an event.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import text

log = logging.getLogger("boss.failure_owner")

#: How far back a failure still speaks for the project (the reach every surface uses).
REACH_DAYS = 14
#: BOSS re-runs work by itself only when it failed this recently.
ACT_HOURS = 72
#: BOSS's own attempts per item before it hands it to a person, with the reason.
MAX_ATTEMPTS = 2
#: Seconds before attempt n (1-based) may start, after the last failure.
BACKOFF_SECONDS = (60, 600)
#: Attempts started per tick (each may be a real build): bounded spend.
MAX_ACTS_PER_TICK = 3
#: Items read per surface per tick (newest first).
MAX_ITEMS_PER_SURFACE = 300
#: After a person's "Done — retry", how long BOSS waits for it to take before asking again.
PERSON_GRACE_MINUTES = 30
#: A model connection's re-checks that may die on BOSS's side before it is a person's item.
PROOF_REPEATS = 3
#: Who owns failures: the Shepherd, BOSS's agent for work that stopped moving (stuck_work).
AGENT = "Shepherd"
ACTOR = "agent:shepherd"
#: The action item a person answers.
CARD_ACTION = "failure_owner.retry"
CARD_REASON = "failure_needs_person"

FIXING, NEEDS_PERSON, RESOLVED = "fixing", "needs_person", "resolved"
STATES = (FIXING, NEEDS_PERSON, RESOLVED)
APP, PLATFORM, PERSON = "app", "platform", "person"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def _json(value: Any, default: Any = None) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def _words(value: Any, n: int = 200) -> str:
    """A failure as a person reads it: one redacted sentence, never a stack or a secret."""
    from .build_failure import failure_words

    said = failure_words(value)
    return said if len(said) <= n else said[: n - 1].rstrip() + "…"


# ─────────────────────────────── the item ───────────────────────────────


@dataclass
class Item:
    """One failed thing, as the loop sees it. `key` names its LINEAGE (a build and its retries,
    a workflow run, a job and its re-queues): attempts are counted per key, so a retry that
    fails again is the same item's next attempt, never a fresh item with a fresh budget."""

    surface: str
    key: str
    id: str
    project_id: Optional[str]
    title: str
    error: str
    failed_at: Optional[datetime]
    hints: dict[str, Any] = field(default_factory=dict)


@dataclass
class Said:
    """What another owner says, or what an outcome is."""

    state: str
    words: str
    by: str = ""
    decision_id: Optional[str] = None
    reason: str = ""


class NeedsPerson(Exception):
    """BOSS cannot do this attempt itself: `reason` says why, `action` what a person does."""

    def __init__(self, reason: str, action: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.action = action


# ─────────────────────────────── classification ───────────────────────────────

#: BOSS's own side, checked FIRST: a permission error inside BOSS's sandbox reads "Permission
#: denied" next to "login rejected" (the codex arg0 temp-dir bug, 301 proofs on the laptop) and
#: is still BOSS's to fix, not the person's login.
_PLATFORM_FIRST = re.compile(
    r"os error 13|\[Errno 13\]|/var/lib/boss/|lease_lost|lost its holder|"
    r"symlink, special file|Budget admission is busy|control plane crashed|"
    r"Internal Server Error|\b50[0-4]\b|\b409 Conflict\b|timed? ?out|Connection (?:refused|reset)|"
    r"EAI_AGAIN|stalled \(\d+s", re.I)

#: (cause_code, pattern, what a person does) — ordered; the first match wins.
_PERSON_RULES: tuple[tuple[str, str, str], ...] = (
    ("budget", r"spending cap|max[_ ]budget|budget[_ ]cap|error_max_budget_usd|over the cap|"
               r"budget_denied|raise the cap",
     "Raise the spending cap in Govern › Budget, then press Done — retry."),
    ("no_repository", r"No GitHub credentials|has no repository connected|no installation for it|"
                      r"BOSS_GITHUB_TOKEN is not set",
     "Connect this project's repository in Setup › Repositories (or install the GitHub App on "
     "it), then press Done — retry."),
    ("repo_access", r"repository '[^']*' not found|Permission to [\w./-]+ denied|Bad credentials|"
                    r"repo_access_denied|Repository not found|returned error: 40[34]",
     "Give BOSS access to the repository in Setup › Repositories (the account BOSS uses can't "
     "reach it), then press Done — retry."),
    ("toolchain", r"boss toolchains enable (\w+)",
     "Turn on the toolchain it names on the BOSS host (`boss toolchains enable …`), then press "
     "Done — retry."),
    ("model_login", r"not logged in|re-?authenticate|failed to authenticate|token (?:has )?expired|"
                    r"login rejected|HTTP 401|\b401\b|invalid api key|model_auth_rejected|"
                    r"plain http|credential_misdirected|harness_unavailable",
     "Sign in to the model again (or fix its key or address) in Setup › Models, then press "
     "Done — retry."),
    ("grant", r"no person's grant|no one's authority|consent_required|authority carries over|"
              r"no grant you gave|no_authority|needs your go-ahead|needs your OK",
     "Give the agent a grant for this work: press Done — retry and accept the grant it asks for."),
    ("merge_conflict", r"does not merge cleanly|conflicts with its base branch|merge conflict",
     "Resolve the conflict on the pull request (or merge its base into it), then press "
     "Done — retry."),
    ("host", r"Host key verification failed|Permission denied \(publickey|No route to host|"
             r"sudo: a password is required|no space left on device",
     "Fix the host it names (access, sudo or disk) in Setup › Hosts, then press Done — retry."),
)

#: The work's own fault: BOSS fixes it with a repair on the item's own lineage.
_APP = re.compile(
    r"tests? failed|failed its checks|quality gate|ERR_MODULE_NOT_FOUND|Cannot find (?:module|package)|"
    r"SyntaxError|TypeError|exited \(code|exit(?:ed)? (?:code|status) [1-9]|returned 404|"
    r"no page at the site root|command not found|npm ERR|build failed|compile|"
    r"health check|did not answer as expected", re.I)

#: Why BOSS cannot fix a person-side cause itself, in one line.
_WHY_PERSON = {
    "budget": "Raising a budget is a person's call.",
    "no_repository": "Only a person can connect a repository or install the GitHub App.",
    "repo_access": "Only the repository's owner can give BOSS's account access to it.",
    "toolchain": "Turning on a toolchain changes the BOSS host; a person does that.",
    "model_login": "Only a person can sign in to their model account or set its key.",
    "grant": "Only a person can let an agent act for them.",
    "merge_conflict": "Merging needs a choice about whose change wins; BOSS does not pick for you.",
    "host": "The host is yours: BOSS does not change its access, sudo or disk by itself.",
    "needs_human": "Only a person can do what it needs.",
}


def classify(error: Any, *, failure_class: Any = None, reason_code: Any = None
             ) -> tuple[str, str, str]:
    """(class, cause_code, what a person does when it is theirs). The record's own class wins:
    `needs_human` is the person's, `infra` is BOSS's; otherwise the words decide, BOSS's own side
    first (a sandbox permission error is never the person's login)."""
    raw = f"{error or ''} {reason_code or ''}"
    fc = str(failure_class or "")
    if fc == "infra":
        return PLATFORM, f"platform:{str(reason_code or 'infra')}", ""
    if fc != "needs_human" and _PLATFORM_FIRST.search(raw):
        return PLATFORM, "platform", ""
    for code, pattern, action in _PERSON_RULES:
        if re.search(pattern, raw, re.I):
            return PERSON, code, action
    if fc == "needs_human":
        return PERSON, "needs_human", "Do what its page says it needs, then press Done — retry."
    if _APP.search(raw):
        return APP, "app", ""
    return PLATFORM, "platform", ""


# ─────────────────────────────── the surfaces ───────────────────────────────


class Surface:
    """One kind of thing that can fail. Subclasses read their own tables and reuse the machinery
    that already acts on them; nothing here is a second engine."""

    key = ""
    #: The tables whose failure states this surface owns (the invariant guard reads it).
    tables: tuple[str, ...] = ()
    noun = "Work"

    def failed(self, c: Any, since: datetime, pid: Optional[str] = None) -> list[Item]:
        """The failed items within reach, newest first; `pid` scopes them to one project."""
        raise NotImplementedError

    def settled(self, c: Any, item: Item) -> Optional[Said]:
        """Resolved (recovered, replaced, ended by a person), or None."""
        return None

    def moving(self, c: Any, item: Item) -> Optional[Said]:
        """A newer attempt of the lineage is running, or None."""
        return None

    def held(self, c: Any, item: Item) -> Optional[Said]:
        """Another owner (agent or card) already has it, or None."""
        return None

    def classify(self, c: Any, item: Item) -> tuple[str, str, str]:
        return classify(item.error, failure_class=item.hints.get("failure_class"),
                        reason_code=item.hints.get("reason_code"))

    def attempts_made(self, c: Any, item: Item, recorded: int) -> int:
        return recorded

    def retry(self, engine: Any, settings: Any, jobs: Any, item: Item, *, cls: str,
              attempt: int, note: str) -> str:
        """BOSS's own attempt. Returns what it started, in words; raises NeedsPerson."""
        raise NeedsPerson("BOSS has no safe way to run this again by itself.")

    def person_retry(self, engine: Any, settings: Any, jobs: Any, ref: Mapping[str, Any],
                     actor: str) -> str:
        """The person's "Done — retry", run as that person."""
        raise NeedsPerson("This cannot be run again from here; start it again from its page.")

    def still_failed(self, c: Any, ref: Mapping[str, Any]) -> Optional[bool]:
        """For an open owner row not seen failing this tick: True still failed, False it is
        fine now (resolve), None moving (keep)."""
        return False


def _lineage_newest(c: Any, project_id: str, root: str) -> Optional[dict[str, Any]]:
    row = c.execute(text(
        "SELECT id, status, finalize_ctx->'merge'->>'sha' AS merged FROM code_task_runs "
        "WHERE project_id = :p AND (id = :r OR finalize_ctx->>'root_run_id' = :r) "
        "ORDER BY created_at DESC LIMIT 1"), {"p": project_id, "r": root}).mappings().first()
    return dict(row) if row else None


_BUILD_LIVE = ("running", "queued", "repairing", "waiting_for_github", "provisioning",
               "waiting_on_person")


def _open_list() -> list[str]:
    from .decision_actions import OPEN_STATUSES

    return sorted(OPEN_STATUSES)


def _open_decision(c: Any, decision_id: Any) -> Optional[str]:
    if not decision_id:
        return None
    from .decision_actions import OPEN_STATUSES

    found = c.execute(text("SELECT id FROM decisions WHERE id = :d AND status = ANY(:s)"),
                      {"d": str(decision_id), "s": sorted(OPEN_STATUSES)}).scalar()
    return str(found) if found else None


class CodeTasks(Surface):
    """A build that failed or was blocked, on its own (a workflow's build is its run's)."""

    key = "code_task"
    tables = ("code_task_runs",)
    noun = "Build"

    def failed(self, c, since, pid=None):
        from .build_failure import _internal_clauses  # noqa: SLF001 — the one definition
        from . import projects_archive

        params: dict[str, Any] = {"since": since, "n": MAX_ITEMS_PER_SURFACE, "pid": pid}
        rows = c.execute(text(
            "SELECT c.id, c.project_id, c.title, c.status, c.error, c.updated_at, c.decision_id, "
            "c.finalize_ctx, COALESCE(c.finalize_ctx->>'root_run_id', c.id) AS root "
            "FROM code_task_runs c WHERE c.status IN ('failed', 'blocked') "
            "AND c.updated_at >= :since AND c.finalize_ctx->>'workflow_run_id' IS NULL "
            "AND (CAST(:pid AS text) IS NULL OR c.project_id = :pid) "
            "AND COALESCE(c.finalize_ctx->>'simulated', 'false') <> 'true' "
            "AND NOT EXISTS (SELECT 1 FROM code_task_runs n WHERE n.project_id = c.project_id "
            "  AND COALESCE(n.finalize_ctx->>'root_run_id', n.id) = "
            "      COALESCE(c.finalize_ctx->>'root_run_id', c.id) AND n.created_at > c.created_at) "
            "AND " + projects_archive.live_sql("c.project_id") + " "
            + _internal_clauses(params, "c") + "ORDER BY c.updated_at DESC LIMIT :n"),
            params).mappings().all()
        out = []
        for r in rows:
            fctx = _json(r["finalize_ctx"], {}) or {}
            out.append(Item(self.key, str(r["root"]), str(r["id"]), r["project_id"],
                            str(r["title"] or "Build"), str(r["error"] or ""),
                            _aware(r["updated_at"]),
                            {"status": r["status"], "decision_id": r["decision_id"],
                             "failure_class": fctx.get("failure_class"),
                             "reason_code": fctx.get("reason_code"), "fctx": fctx}))
        return out

    def settled(self, c, item):
        merged = c.execute(text(
            "SELECT id FROM code_task_runs WHERE project_id = :p AND (id = :r OR "
            "finalize_ctx->>'root_run_id' = :r) AND finalize_ctx->'merge'->>'sha' IS NOT NULL "
            "LIMIT 1"), {"p": item.project_id, "r": item.key}).scalar()
        if merged:
            return Said(RESOLVED, "Its change was merged.")
        from .build_failure import waits_on

        waits = waits_on(c, str(item.project_id), {"error": item.error})
        if waits is not None and waits.get("resolved"):
            return Said(RESOLVED, str(waits.get("detail") or "Its cause is gone."))
        return None

    def held(self, c, item):
        fctx = item.hints.get("fctx") or {}
        card = _open_decision(c, item.hints.get("decision_id"))
        if card:
            return Said(NEEDS_PERSON, "Its own card asks you what to do.", by="its card",
                        decision_id=card, reason="It is waiting on your answer on its card.")
        diag = fctx.get("diagnosis") if isinstance(fctx.get("diagnosis"), dict) else {}
        card = _open_decision(c, diag.get("card_id"))
        if card:
            return Said(NEEDS_PERSON, str(diag.get("next") or "The Shepherd asks you."),
                        by="stuck_work", decision_id=card,
                        reason=str(diag.get("why_not") or diag.get("cause") or ""))
        from .build_failure import waits_on

        waits = waits_on(c, str(item.project_id), {"error": item.error})
        if waits is not None and _open_decision(c, waits.get("decision_id")):
            return Said(NEEDS_PERSON, str(waits.get("detail") or ""), by="its merge",
                        decision_id=str(waits["decision_id"]),
                        reason="It builds on a change that is not merged yet.")
        return None

    def classify(self, c, item):
        cls, code, action = super().classify(c, item)
        if cls == PERSON and item.hints.get("failure_class") == "needs_human":
            from .build_failure import action_for

            fix = (action_for(str(item.project_id), source="code_task", run_id=item.id,
                              failure_class="needs_human",
                              reason_code=item.hints.get("reason_code"),
                              error=item.error).get("fix") or {})
            if fix.get("detail"):
                action = f"{str(fix['detail']).rstrip('.')}. Then press Done — retry."
        if item.hints.get("status") == "blocked" and cls == PLATFORM and \
                not item.hints.get("failure_class"):
            cls, code = APP, "app"            # blocked at its own checks: the work's to fix
        return cls, code, action

    def attempts_made(self, c, item, recorded):
        from .governed_code_task_service import auto_retries_made

        fctx = item.hints.get("fctx") or {}
        try:
            lineage = int(c.execute(text(
                "SELECT count(*) FROM code_task_runs WHERE project_id = :p AND "
                "finalize_ctx->>'root_run_id' = :r AND (finalize_ctx ? 'auto_infra_retry' "
                "OR finalize_ctx ? 'failure_owner_attempt')"),
                {"p": item.project_id, "r": item.key}).scalar() or 0)
        except Exception:  # noqa: BLE001 — the recorded count still bounds it
            lineage = 0
        return max(recorded, lineage, auto_retries_made(fctx))

    def retry(self, engine, settings, jobs, item, *, cls, attempt, note):
        from .code_task_service import RetryNeedsPerson, retry_code_task

        try:
            if cls == APP:
                got = retry_code_task(engine, settings, jobs, item.id, automatic=True,
                                      fix_note=note)
            else:
                got = retry_code_task(engine, settings, jobs, item.id, automatic=True,
                                      platform_retry=True, auto_retry_attempt=attempt)
        except RetryNeedsPerson as withheld:
            raise NeedsPerson(str(withheld), "Press Done — retry to let it run again.") from None
        except ValueError as refused:
            raise NeedsPerson(_words(refused, 280)) from None
        new_id = str(got.get("run_id") or item.id)
        if new_id != item.id:
            with engine.begin() as c2:
                c2.execute(text(
                    "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) "
                    "|| jsonb_build_object('failure_owner_attempt', CAST(:a AS int)) "
                    "WHERE id = :i"), {"a": attempt, "i": new_id})
        return (f"building a fix on the same branch (build {new_id})" if cls == APP
                else f"running it again (build {new_id})")

    def person_retry(self, engine, settings, jobs, ref, actor):
        from .code_task_service import retry_code_task

        got = retry_code_task(engine, settings, jobs, str(ref["id"]), by_person=True)
        return f"build {got.get('run_id') or ref['id']}"

    def still_failed(self, c, ref):
        newest = _lineage_newest(c, str(ref.get("project_id") or ""), str(ref["key"]))
        if newest is None or newest["merged"] or newest["status"] in ("completed", "cancelled"):
            return False
        if newest["status"] in _BUILD_LIVE:
            return None
        return True


class WorkflowRuns(Surface):
    """A workflow run that failed or was dead-lettered (its builds and deploys are its own)."""

    key = "workflow_run"
    tables = ("workflow_runs", "workflow_step_runs")
    noun = "Workflow run"

    def failed(self, c, since, pid=None):
        from . import projects_archive

        rows = c.execute(text(
            "SELECT r.id, r.project_id, r.status, COALESCE(r.error, r.blocked_reason) AS error, "
            "COALESCE(r.input->>'title', d.name, 'Workflow') AS title, r.updated_at, "
            "s.step_id, s.status AS step_status, s.last_error, s.output "
            "FROM workflow_runs r LEFT JOIN workflow_definitions d ON d.id = r.workflow_definition_id "
            "LEFT JOIN LATERAL (SELECT x.step_id, x.status, x.last_error, x.output "
            "  FROM workflow_step_runs x WHERE x.workflow_run_id = r.id "
            "  AND x.status IN ('failed', 'deadlettered') ORDER BY x.updated_at DESC LIMIT 1) s "
            "  ON TRUE "
            "WHERE r.status IN ('failed', 'deadlettered') AND r.updated_at >= :since "
            "AND (CAST(:pid AS text) IS NULL OR r.project_id = :pid) "
            "AND COALESCE(r.mode, 'normal') <> 'test' AND r.parent_run_id IS NULL "
            "AND " + projects_archive.live_sql("r.project_id") + " "
            "ORDER BY r.updated_at DESC LIMIT :n"),
            {"since": since, "n": MAX_ITEMS_PER_SURFACE, "pid": pid}).mappings().all()
        out = []
        for r in rows:
            output = _json(r["output"], {}) or {}
            from .step_recovery import STATE_KEY

            rec = output.get(STATE_KEY) if isinstance(output.get(STATE_KEY), dict) else {}
            last = (rec.get("decisions") or [{}])[-1] if rec.get("decisions") else {}
            out.append(Item(self.key, str(r["id"]), str(r["id"]), r["project_id"],
                            str(r["title"]), str(r["last_error"] or r["error"] or ""),
                            _aware(r["updated_at"]),
                            {"step_id": r["step_id"], "step_status": r["step_status"],
                             "recovery": last if isinstance(last, dict) else {}}))
        return out

    def moving(self, c, item):
        live = c.execute(text(
            "SELECT id FROM code_task_runs WHERE project_id = :p AND "
            "finalize_ctx->>'workflow_run_id' = :r AND status IN ('running', 'queued', "
            "'repairing', 'provisioning') LIMIT 1"),
            {"p": item.project_id, "r": item.id}).scalar()
        if live:
            return Said(FIXING, f"its repair build {live} is running", by="step_recovery")
        return None

    def held(self, c, item):
        from .decision_actions import OPEN_STATUSES

        card = c.execute(text(
            "SELECT id FROM decisions WHERE project_id = :p AND status = ANY(:s) AND "
            "recommendation->>'workflow_run_id' = :r AND COALESCE(recommendation->>'action', '') "
            "<> :mine ORDER BY created_at DESC LIMIT 1"),
            {"p": item.project_id, "s": sorted(OPEN_STATUSES), "r": item.id,
             "mine": CARD_ACTION}).scalar()
        if card:
            return Said(NEEDS_PERSON, "Its own card asks you what to do.", by="its card",
                        decision_id=str(card), reason="It is waiting on your answer on its card.")
        return None

    def classify(self, c, item):
        last = item.hints.get("recovery") or {}
        if str(last.get("action") or "") == "ask_person" and last.get("question"):
            # THE STEP'S OWN RECOVERY AGENT ALREADY DECIDED (step_recovery): only a person can.
            return PERSON, "asked_by_" + str(last.get("agent") or "agent").lower(), \
                f"{str(last['question']).rstrip('?.')}? Then press Done — retry."
        return super().classify(c, item)

    def retry(self, engine, settings, jobs, item, *, cls, attempt, note):
        return self._retry(engine, item.project_id, item.id, item.hints.get("step_id"),
                           actor=ACTOR, note=note)

    @staticmethod
    def _retry(engine, project_id, run_id, step_id, *, actor, note):
        from . import workflows

        if not step_id:
            raise NeedsPerson("BOSS could not tell which step failed, so it cannot retry one.",
                              "Open the run and retry its failed step.")
        try:
            with engine.begin() as c:
                workflows.retry_failed_step(c, project_id=str(project_id), run_id=str(run_id),
                                            step_id=str(step_id), actor=actor, note=note[:900])
        except workflows.WorkflowError as refused:
            raise NeedsPerson(_words(refused, 280)) from None
        except Exception as exc:  # noqa: BLE001 — a consent refusal and the like
            if type(exc).__name__ in ("ConsentRequired", "ConsentNeeded"):
                raise NeedsPerson("Running the step again needs your grant.",
                                  "Press Done — retry and accept the grant it asks for.") from None
            raise
        return f"running its '{step_id}' step again"

    def person_retry(self, engine, settings, jobs, ref, actor):
        return self._retry(engine, ref.get("project_id"), ref["id"],
                           (ref.get("hints") or {}).get("step_id"), actor=actor,
                           note="A person did what the action item asked; run the step again.")

    def still_failed(self, c, ref):
        status = c.execute(text("SELECT status FROM workflow_runs WHERE id = :r"),
                           {"r": ref["id"]}).scalar()
        if status in ("failed", "deadlettered"):
            return True
        return None if status in ("running", "waiting", "paused", "queued") else False


class Deployments(Surface):
    """A stack deployment or governed host deployment that failed."""

    key = "deployment"
    tables = ("stack_deployments", "deployment_runs", "changesets", "changeset_operations")
    noun = "Deploy"

    def failed(self, c, since, pid=None):
        rows = c.execute(text(
            "SELECT 'stack' AS kind, d.id, d.project_id, d.target_id, d.workflow_run_id, "
            "COALESCE(d.failure_reason, d.error::text, '') AS error, d.updated_at, "
            "d.repository, d.ref, d.commit_sha, d.code_task_run_id, "
            "COALESCE(t.app_name, 'Deploy') AS title "
            "FROM stack_deployments d LEFT JOIN deployment_targets t ON t.id = d.target_id "
            "WHERE d.status = 'failed' AND d.updated_at >= :since AND (CAST(:pid AS text) IS NULL OR d.project_id = :pid) "
            "UNION ALL SELECT 'host', r.id, r.project_id, r.deployment_target_id, NULL, "
            "COALESCE(r.failure_reason, ''), r.updated_at, r.repo, r.commit_ref, r.commit_ref, "
            "NULL, COALESCE(r.app_name, 'Deploy') FROM deployment_runs r "
            "WHERE r.status = 'failed' AND r.updated_at >= :since AND (CAST(:pid AS text) IS NULL OR r.project_id = :pid) "
            "ORDER BY 7 DESC LIMIT :n"), {"since": since, "n": MAX_ITEMS_PER_SURFACE, "pid": pid}
        ).mappings().all()
        return [Item(self.key, str(r["id"]), str(r["id"]), r["project_id"], str(r["title"]),
                     str(r["error"] or ""), _aware(r["updated_at"]), dict(r)) for r in rows]

    def _newer(self, c, item):
        h = item.hints
        if h["kind"] == "stack":
            return c.execute(text(
                "SELECT id, status, decision_id FROM stack_deployments WHERE target_id = :t "
                "AND created_at > (SELECT created_at FROM stack_deployments WHERE id = :i) "
                "ORDER BY created_at DESC LIMIT 1"), {"t": h["target_id"], "i": item.id}
            ).mappings().first()
        return c.execute(text(
            "SELECT id, status, decision_id FROM deployment_runs WHERE deployment_target_id = :t "
            "AND created_at > (SELECT created_at FROM deployment_runs WHERE id = :i) "
            "ORDER BY created_at DESC LIMIT 1"), {"t": h["target_id"], "i": item.id}
        ).mappings().first()

    def settled(self, c, item):
        newer = self._newer(c, item)
        if newer is not None and newer["status"] in ("healthy", "succeeded", "live", "active"):
            return Said(RESOLVED, f"A later deploy ({newer['id']}) went live.")
        wfr = item.hints.get("workflow_run_id")
        if wfr:
            status = c.execute(text("SELECT status FROM workflow_runs WHERE id = :r"),
                               {"r": wfr}).scalar()
            if status in ("completed", "cancelled"):
                return Said(RESOLVED, f"Its workflow run {status.replace('_', ' ')} since.")
        return None

    def moving(self, c, item):
        newer = self._newer(c, item)
        if newer is not None and newer["status"] in ("planned", "applying", "running", "queued",
                                                      "pending"):
            return Said(FIXING, f"a new deploy ({newer['id']}) is under way", by="deploy")
        return None

    def held(self, c, item):
        # THE DEPLOY'S OWN AGENT FIRST (deploy_repair.underway, fix/agents-fix-failed-deploys-
        # themselves): what it says BOSS is doing about this deployment is the one answer.
        from . import deploy_repair

        underway = getattr(deploy_repair, "underway", None)
        if callable(underway) and item.hints.get("kind") == "stack":
            try:
                with c.begin_nested():
                    said = underway(c, [{**item.hints, "status": "failed", "error": None,
                                         "failure_reason": item.error, "log_tail": "",
                                         "finished_at": item.failed_at}]).get(item.id)
            except Exception:  # noqa: BLE001 — its reading never costs this one
                said = None
            if said:
                return Said(FIXING, str(said.get("reason") or said.get("word") or ""),
                            by="deploy_repair")
        newer = self._newer(c, item)
        if newer is not None and newer["status"] == "awaiting_approval":
            card = _open_decision(c, newer["decision_id"])
            if card:
                return Said(NEEDS_PERSON, f"A new deploy ({newer['id']}) waits for your approval.",
                            by="deploy", decision_id=card,
                            reason="A deploy changes what runs on your host, so it waits for you.")
        wfr = item.hints.get("workflow_run_id")
        if wfr:
            # A WORKFLOW'S DEPLOY IS ITS RUN'S: the run's owner (step_recovery, or this loop's
            # workflow item, processed first) owns it.
            row = c.execute(text("SELECT state, words, decision_id, reason FROM failure_owners "
                                 "WHERE surface = 'workflow_run' AND item_key = :k"),
                            {"k": wfr}).mappings().first()
            if row is not None and row["state"] in (FIXING, NEEDS_PERSON):
                return Said(row["state"], f"Its workflow run's owner has it: {row['words']}",
                            by=f"workflow_run:{wfr}", decision_id=row["decision_id"],
                            reason=str(row["reason"] or ""))
        return None

    def classify(self, c, item):
        if item.hints.get("kind") == "stack":
            from .deploy_repair import failure_of_deployment, is_app_side

            try:
                with c.begin_nested():
                    failure = failure_of_deployment(c, item.id)
            except Exception:  # noqa: BLE001
                failure = None
            if failure and is_app_side(failure):
                return APP, "app", ""
            if failure and str(failure.get("cause") or "") == "host":
                return PERSON, "host", _PERSON_RULES[-1][2]
        return super().classify(c, item)

    def retry(self, engine, settings, jobs, item, *, cls, attempt, note):
        if item.hints.get("workflow_run_id"):
            raise NeedsPerson("Its workflow run's deploy step retries it; that step is not "
                              "retryable now.", "Open the run and retry its deploy step.")
        if item.hints.get("kind") != "stack":
            raise NeedsPerson("A deploy to your host changes what runs there; BOSS re-plans it "
                              "only when you ask.")
        return self._plan(engine, settings, item.project_id, item.hints, actor=ACTOR)

    @staticmethod
    def _plan(engine, settings, project_id, hints, *, actor):
        from .stack_deploy import plan_stack_deploy

        got = plan_stack_deploy(engine, settings, project_id=str(project_id),
                                target_id=str(hints["target_id"]),
                                repository=hints.get("repository"),
                                ref=str(hints.get("commit_sha") or hints.get("ref") or ""),
                                actor=actor, run_id=hints.get("code_task_run_id"))
        return (f"planning it again (deploy {got.get('id') or got.get('deployment_id') or ''}, "
                "held for your approval)")

    def person_retry(self, engine, settings, jobs, ref, actor):
        hints = ref.get("hints") or {}
        if hints.get("kind") == "host":
            from .delivery_pipeline import redeploy

            got = redeploy(engine, settings, str(ref["id"]), actor=actor)
            return f"deploy {got.get('id') or ''}"
        return self._plan(engine, settings, ref.get("project_id"), hints, actor=actor)

    def still_failed(self, c, ref):
        table = "stack_deployments" if (ref.get("hints") or {}).get("kind") != "host" \
            else "deployment_runs"
        status = c.execute(text(f"SELECT status FROM {table} WHERE id = :i"),
                           {"i": ref["id"]}).scalar()
        return True if status == "failed" else False


class Previews(Surface):
    """A preview of a build that failed to start or serve."""

    key = "preview"
    tables = ("app_previews", "preview_sandboxes")
    noun = "Preview"

    _PLATFORM_KINDS = ("port_timeout", "start_failed", "sandbox_error", "substrate_error",
                       "workdir_missing")

    def failed(self, c, since, pid=None):
        rows = c.execute(text(
            "SELECT p.id, p.project_id, p.run_id, p.failure_kind, p.error, p.created_at, "
            "COALESCE(r.finalize_ctx->>'root_run_id', r.id, p.run_id) AS root, "
            "COALESCE(r.title, 'Preview') AS title "
            "FROM app_previews p LEFT JOIN code_task_runs r ON r.id = p.run_id "
            "WHERE p.status = 'failed' AND p.created_at >= :since AND (CAST(:pid AS text) IS NULL OR p.project_id = :pid) "
            "AND NOT EXISTS (SELECT 1 FROM app_previews q LEFT JOIN code_task_runs qr "
            "  ON qr.id = q.run_id WHERE q.project_id = p.project_id AND q.created_at > p.created_at "
            "  AND COALESCE(qr.finalize_ctx->>'root_run_id', qr.id, q.run_id) = "
            "      COALESCE(r.finalize_ctx->>'root_run_id', r.id, p.run_id)) "
            "ORDER BY p.created_at DESC LIMIT :n"), {"since": since, "n": MAX_ITEMS_PER_SURFACE,
                                                     "pid": pid}
        ).mappings().all()
        return [Item(self.key, f"preview:{r['root']}", str(r["id"]), r["project_id"],
                     str(r["title"]), str(r["error"] or r["failure_kind"] or ""),
                     _aware(r["created_at"]),
                     {"run_id": r["run_id"], "root": r["root"], "failure_kind": r["failure_kind"]})
                for r in rows]

    def settled(self, c, item):
        newest = _lineage_newest(c, str(item.project_id), str(item.hints.get("root") or ""))
        if newest is not None and newest["id"] != item.hints.get("run_id") and \
                newest["status"] not in _BUILD_LIVE:
            return Said(RESOLVED, f"A newer attempt ({newest['id']}) replaced the build it "
                                  "previewed.")
        return None

    def moving(self, c, item):
        newest = _lineage_newest(c, str(item.project_id), str(item.hints.get("root") or ""))
        if newest is not None and newest["id"] != item.hints.get("run_id") and \
                newest["status"] in _BUILD_LIVE:
            return Said(FIXING, f"a fix build ({newest['id']}) is running", by="build")
        return None

    def classify(self, c, item):
        kind = str(item.hints.get("failure_kind") or "")
        if kind in self._PLATFORM_KINDS or "health check rejected" in item.error:
            return PLATFORM, f"platform:{kind or 'preview'}", ""
        if kind:
            return APP, f"app:{kind}", ""
        return super().classify(c, item)

    def retry(self, engine, settings, jobs, item, *, cls, attempt, note):
        run_id = str(item.hints.get("run_id") or "")
        if cls == APP:
            from .code_task_service import RetryNeedsPerson, retry_code_task

            try:
                got = retry_code_task(engine, settings, jobs, run_id, automatic=True,
                                      fix_note=note)
            except RetryNeedsPerson as withheld:
                raise NeedsPerson(str(withheld), "Press Done — retry to let it run again.") \
                    from None
            except ValueError as refused:
                raise NeedsPerson(_words(refused, 280)) from None
            return f"building a fix so the app starts (build {got.get('run_id') or ''})"
        from .preview_service import start_preview_async

        got = start_preview_async(engine, settings, run_id, actor=ACTOR)
        return f"starting the preview again ({got.get('id') or got.get('preview_id') or ''})"

    def person_retry(self, engine, settings, jobs, ref, actor):
        from .preview_service import start_preview_async

        got = start_preview_async(engine, settings, str((ref.get("hints") or {}).get("run_id")),
                                  actor=actor)
        return f"preview {got.get('id') or got.get('preview_id') or ''}"

    def still_failed(self, c, ref):
        status = c.execute(text(
            "SELECT p.status FROM app_previews p WHERE p.project_id = :pr AND p.run_id IN "
            "(SELECT id FROM code_task_runs WHERE id = :r OR finalize_ctx->>'root_run_id' = :r) "
            "ORDER BY p.created_at DESC LIMIT 1"),
            {"pr": ref.get("project_id"), "r": str(ref["key"]).split(":", 1)[-1]}).scalar()
        if status == "failed":
            return True
        return None if status == "starting" else False


#: Job kinds whose failure another surface owns (the deadletter fails that record in words).
_JOB_KINDS_OWNED_ELSEWHERE = ("code.task", "deployment.stack_apply", "deployment.host_apply")
#: Kinds whose dedupe key is per attempt (a model check, a docs update per event): their lineage
#: is what they keep doing — the role's model, the docs subscription, the repository binding.
#: 334 proofs of one project's model, each its own dedupe key, are ONE thing failing.
_JOB_LINEAGE = {"connection.proof": "role", "docs.update": "subscription_id",
                "knowledge.repository.index": "binding_id"}
#: Payload fields that name what a job is about, for the lineage of a job with no dedupe key.
_JOB_SUBJECT = ("pull_request_url", "run_id", "conversation_id", "binding_id", "repo",
                "entity_id", "workflow_run_id", "target_id", "connection_id")


class Jobs(Surface):
    """A background job that ran out of attempts (an open dead letter): a model-connection
    proof, a docs update, a repository index, a panel step, a merge, an agent turn."""

    key = "job"
    tables = ("jobs", "deadletters", "receipts", "resource_receipts", "meeting_runs",
              "discovery_runs", "agent_conversation_turns", "agent_runs", "agent_wakeups",
              "webhook_deliveries", "inbox_messages", "a2a_deliveries", "a2a_envelopes",
              "a2a_transport_attempts")
    noun = "Background job"

    def failed(self, c, since, pid=None):
        rows = c.execute(text(
            "SELECT d.id AS dl_id, d.created_at, d.reason, j.id, j.project_id, j.kind, "
            "j.dedupe_key, j.payload, j.last_error, j.on_behalf_system "
            "FROM deadletters d JOIN jobs j ON j.id = d.source_id "
            "WHERE d.status = 'open' AND d.source_kind = 'job' AND d.created_at >= :since "
            "AND j.kind <> ALL(:owned) AND (CAST(:pid AS text) IS NULL OR j.project_id = :pid) "
            "ORDER BY d.created_at DESC LIMIT :n"),
            {"since": since, "owned": list(_JOB_KINDS_OWNED_ELSEWHERE), "pid": pid,
             "n": MAX_ITEMS_PER_SURFACE * 3}).mappings().all()
        out = []
        for r in rows:
            payload = _json(r["payload"], {}) or {}
            subject = next((str(payload[k]) for k in _JOB_SUBJECT if payload.get(k)), "")
            by_field = _lineage_field(str(r["kind"]), payload)
            lineage = (f"{by_field[0]}:{by_field[1]}" if by_field else
                       _job_lineage(c, str(r["id"]), r["dedupe_key"], payload))
            err = _json(r["last_error"], {}) if r["last_error"] is not None else {}
            words = (err.get("message") if isinstance(err, dict) else None) or r["reason"] or ""
            out.append(Item(self.key, f"{r['kind']}:{lineage}"[:400], str(r["dl_id"]),
                            r["project_id"], _job_title(str(r["kind"])), str(words),
                            _aware(r["created_at"]),
                            {"job_id": r["id"], "kind": r["kind"], "dedupe_key": r["dedupe_key"],
                             "subject": subject, "payload": payload,
                             "system": bool(r["on_behalf_system"])}))
        # ONE ITEM PER LINEAGE: the newest dead letter of it.
        seen: dict[str, Item] = {}
        for it in out:
            seen.setdefault(it.key, it)
        return list(seen.values())[:MAX_ITEMS_PER_SURFACE]

    def _later_success(self, c, item) -> Optional[str]:
        h = item.hints
        by_field = _lineage_field(h["kind"], h["payload"] or {})
        if by_field:
            clause, params = f"payload->>'{by_field[0]}' = :lf", {"lf": by_field[1]}
        elif h.get("dedupe_key"):
            clause, params = "dedupe_key = :k", {"k": h["dedupe_key"]}
        elif h.get("subject"):
            clause = " OR ".join(f"payload->>'{k}' = :s" for k in _JOB_SUBJECT)
            clause, params = f"({clause})", {"s": h["subject"]}
        else:
            return None
        return c.execute(text(
            "SELECT id FROM jobs WHERE project_id = :p AND kind = :kind AND status = 'succeeded' "
            "AND updated_at > :at AND " + clause + " ORDER BY updated_at DESC LIMIT 1"),
            {"p": item.project_id, "kind": h["kind"], "at": item.failed_at, **params}).scalar()

    def settled(self, c, item):
        later = self._later_success(c, item)
        if later:
            self._close(c, item, f"superseded: job {later} did the same work since")
            return Said(RESOLVED, f"The same job ran fine since ({later}).")
        if item.hints["kind"] == "github.pull_request.merge":
            decision = (item.hints["payload"] or {}).get("decision_id")
            status = c.execute(text("SELECT status FROM decisions WHERE id = :d"),
                               {"d": decision}).scalar() if decision else None
            if status in ("executed", "withdrawn", "rejected"):
                self._close(c, item, f"its merge question was {status}")
                return Said(RESOLVED, f"Its merge question was {status} since.")
        return None

    def moving(self, c, item):
        live = c.execute(text(
            "SELECT j.id FROM jobs j JOIN deadletters d ON d.requeued_job_id = j.id "
            "WHERE d.id = :d AND j.status IN ('queued', 'leased', 'running')"),
            {"d": item.id}).scalar()
        if live:
            return Said(FIXING, f"its re-run (job {live}) is in the queue", by="failure_owner")
        return None

    def held(self, c, item):
        kind = item.hints["kind"]
        if kind == "connection.proof":
            # THE PROOF SCHEDULE OWNS A MODEL CONNECTION'S RE-CHECKS (proof_schedule: 1 h, 2 h,
            # 4 h … 24 h). A cause only a person can fix is theirs at once (classify); BOSS's own
            # failing again and again is handed over, with a fix for itself, below.
            cls, _, _ = self.classify(c, item)
            if cls != PERSON and self._dead_count(c, item) < PROOF_REPEATS:
                return Said(FIXING, "BOSS re-checks this model connection on its own schedule "
                                    "(1 h, 2 h, 4 h … up to a day apart)", by="proof_schedule")
        if kind == "github.pull_request.merge":
            card = _open_decision(c, (item.hints["payload"] or {}).get("decision_id"))
            if card:
                return Said(NEEDS_PERSON, "Its merge card says why the merge failed; Merge "
                            "there runs it again.", by="merge_failures", decision_id=card,
                            reason="A merge runs only on your approval.")
        if kind == "meeting.step":
            card = c.execute(text(
                "SELECT d.id FROM decisions d WHERE d.project_id = :p AND d.status = ANY(:open) "
                "AND d.recommendation->>'action' = "
                "'review_stalled_meeting' AND d.recommendation->>'run_id' = :r LIMIT 1"),
                {"p": item.project_id, "r": item.hints["subject"], "open": _open_list()}).scalar()
            if card:
                return Said(NEEDS_PERSON, "Its stalled-meeting card asks you.", by="meeting",
                            decision_id=str(card), reason="A stalled panel asks a person.")
        return None

    def classify(self, c, item):
        cls, code, action = super().classify(c, item)
        if item.hints["kind"] == "connection.proof" and cls != PERSON:
            code = "platform:connection_proof"
        return (PLATFORM if cls == APP else cls), code, action   # a job has no app to fix

    def _dead_count(self, c, item) -> int:
        """Dead letters of this lineage within reach (each one an attempt that failed)."""
        by_field = _lineage_field(item.hints["kind"], item.hints["payload"] or {})
        clause, params = ((f"j.payload->>'{by_field[0]}' = :lf", {"lf": by_field[1]}) if by_field
                          else ("COALESCE(j.dedupe_key, '') = :dk",
                                {"dk": item.hints.get("dedupe_key") or ""}))
        return int(c.execute(text(
            "SELECT count(*) FROM deadletters d JOIN jobs j ON j.id = d.source_id WHERE "
            "d.source_kind = 'job' AND j.project_id = :p AND j.kind = :k AND " + clause +
            " AND d.created_at > :since"),
            {"p": item.project_id, "k": item.hints["kind"], **params,
             "since": _now() - timedelta(days=REACH_DAYS)}).scalar() or 0)

    def attempts_made(self, c, item, recorded):
        if item.hints["kind"] == "connection.proof":
            # Its re-checks ARE the attempts: past PROOF_REPEATS it is not retried again here.
            return max(recorded, MAX_ATTEMPTS if self._dead_count(c, item) >= PROOF_REPEATS
                       else recorded)
        return recorded

    def retry(self, engine, settings, jobs, item, *, cls, attempt, note):
        from ..identity.lineage import DELEGATED_JOB_KINDS

        if item.hints["kind"] in DELEGATED_JOB_KINDS and not item.hints.get("system"):
            raise NeedsPerson("This job acts for a person; BOSS runs it again only under their "
                              "OK.", "Press Done — retry to run it again as you.")
        delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS)) - 1]
        return self._requeue(engine, settings, item.id, actor=ACTOR, as_boss=True,
                             run_after_seconds=delay)

    @staticmethod
    def _requeue(engine, settings, deadletter_id, *, actor, as_boss, run_after_seconds=0):
        from .deadletters import DeadletterRemediationBlocked, DeadletterRemediationService

        try:
            with engine.begin() as c:
                got = DeadletterRemediationService(settings).requeue_job_deadletter(
                    c, deadletter_id=deadletter_id, actor=actor,
                    reason="failure_owner_retry", run_after_seconds=run_after_seconds,
                    as_boss=as_boss)
        except (DeadletterRemediationBlocked, ValueError) as refused:
            raise NeedsPerson(_words(refused, 280)) from None
        return f"running it again (job {got.get('requeued_job_id') or ''})"

    def person_retry(self, engine, settings, jobs, ref, actor):
        return self._requeue(engine, settings, str(ref["id"]), actor=actor, as_boss=False)

    @staticmethod
    def _close(c, item, why):
        c.execute(text(
            "UPDATE deadletters SET status = 'resolved', resolved_by = :a, resolved_at = now(), "
            "updated_at = now() WHERE id = :d AND status = 'open'"), {"a": ACTOR, "d": item.id})

    def still_failed(self, c, ref):
        row = c.execute(text(
            "SELECT d.status, j.status AS requeued FROM deadletters d LEFT JOIN jobs j "
            "ON j.id = d.requeued_job_id WHERE d.id = :d"), {"d": ref["id"]}).mappings().first()
        if row is None or row["status"] == "resolved":
            return False
        if row["status"] == "open":
            return True
        if row["requeued"] in ("queued", "leased", "running"):
            return None
        return row["requeued"] in ("deadlettered", "failed_final")


def _job_lineage(c: Any, job_id: str, dedupe_key: Any, payload: Mapping[str, Any]) -> str:
    """What a job is FOR, across its re-runs: a re-queued job (deadletters.requeue_job_deadletter)
    has no dedupe key of its own, so its lineage is the job it re-ran, followed back to the first;
    the first's is its dedupe key, else the subject its payload names, else its id."""
    for _ in range(8):
        requeue = payload.get("requeue") if isinstance(payload.get("requeue"), dict) else {}
        source = requeue.get("source_job_id")
        if not source:
            break
        row = c.execute(text("SELECT id, dedupe_key, payload FROM jobs WHERE id = :i"),
                        {"i": source}).mappings().first()
        if row is None:
            return str(source)
        job_id, dedupe_key, payload = str(row["id"]), row["dedupe_key"], \
            (_json(row["payload"], {}) or {})
    subject = next((str(payload[k]) for k in _JOB_SUBJECT if payload.get(k)), "")
    return str(dedupe_key or subject or job_id)


def _lineage_field(kind: str, payload: Mapping[str, Any]) -> Optional[tuple[str, str]]:
    field_name = _JOB_LINEAGE.get(kind)
    value = payload.get(field_name) if field_name else None
    return (field_name, str(value)) if value else None


def _job_title(kind: str) -> str:
    return {"connection.proof": "Model connection check", "docs.update": "Docs update",
            "knowledge.repository.index": "Repository index", "meeting.step": "Panel step",
            "github.pull_request.merge": "Merge", "agent.conversation": "Agent reply",
            "architecture.draft": "Architecture draft", "discovery.run": "Research run",
            }.get(kind, kind.replace(".", " ").replace("_", " ").capitalize())


class Meetings(Surface):
    """A panel (meeting) that failed or stalled."""

    key = "meeting"
    tables = ("meeting_runs", "panel_runs")
    noun = "Panel"

    def failed(self, c, since, pid=None):
        rows = c.execute(text(
            "SELECT id, project_id, task_id, COALESCE(title, directive, 'Panel') AS title, error, "
            "phase, updated_at FROM meeting_runs WHERE status = 'failed' AND updated_at >= :since "
            "AND (CAST(:pid AS text) IS NULL OR project_id = :pid) "
            "ORDER BY updated_at DESC LIMIT :n"), {"since": since, "n": MAX_ITEMS_PER_SURFACE,
                                                   "pid": pid}
        ).mappings().all()
        return [Item(self.key, str(r["id"]), str(r["id"]), r["project_id"],
                     str(r["title"])[:120], str(r["error"] or ""), _aware(r["updated_at"]),
                     {"task_id": r["task_id"], "phase": r["phase"]}) for r in rows]

    def held(self, c, item):
        card = c.execute(text(
            "SELECT id FROM decisions WHERE project_id = :p AND status = ANY(:open) "
            "AND recommendation->>'action' = 'review_stalled_meeting' AND "
            "recommendation->>'run_id' = :r LIMIT 1"),
            {"p": item.project_id, "r": item.id, "open": _open_list()}).scalar()
        if card:
            return Said(NEEDS_PERSON, "Its stalled-meeting card asks you.", by="meeting",
                        decision_id=str(card), reason="A stalled panel asks a person.")
        return None

    def classify(self, c, item):
        cls, code, action = super().classify(c, item)
        return (PLATFORM if cls == APP else cls), code, action

    def retry(self, engine, settings, jobs, item, *, cls, attempt, note):
        return self._resume(engine, settings, jobs, item.project_id, item.id,
                            item.hints.get("task_id"))

    @staticmethod
    def _resume(engine, settings, jobs, project_id, run_id, task_id):
        """Resume the panel from the phase it stopped in: its earlier phases are saved."""
        if jobs is None:
            from .jobs import JobLeaseService

            jobs = JobLeaseService(settings)
        with engine.begin() as c:
            moved = c.execute(text(
                "UPDATE meeting_runs SET status = 'running', error = NULL, updated_at = now() "
                "WHERE id = :r AND status = 'failed'"), {"r": run_id}).rowcount
            if not moved:
                raise NeedsPerson("The panel is no longer failed, so there is nothing to resume.")
            job = jobs.create_job(c, project_id=str(project_id), task_id=task_id,
                                  kind="meeting.step", payload={"run_id": run_id})
        return f"resuming the panel where it stopped (job {job.get('id') or ''})"

    def person_retry(self, engine, settings, jobs, ref, actor):
        return self._resume(engine, settings, jobs, ref.get("project_id"), str(ref["id"]),
                            (ref.get("hints") or {}).get("task_id"))

    def still_failed(self, c, ref):
        status = c.execute(text("SELECT status FROM meeting_runs WHERE id = :r"),
                           {"r": ref["id"]}).scalar()
        if status == "failed":
            return True
        return None if status == "running" else False


class Backups(Surface):
    """BOSS's own database backup that failed."""

    key = "backup"
    tables = ("backups",)
    noun = "Backup"

    def failed(self, c, since, pid=None):
        from .storage_incidents import default_project

        if pid is not None and pid != default_project(c):
            return []                  # BOSS's own backups belong to its first project only
        rows = c.execute(text(
            "SELECT id, kind, error, COALESCE(finished_at, started_at) AS at FROM backups "
            "WHERE status = 'failed' AND COALESCE(finished_at, started_at) >= :since "
            "ORDER BY 4 DESC LIMIT 20"), {"since": since}).mappings().all()
        owner_pid = default_project(c) if rows else None
        return [Item(self.key, "backups", str(r["id"]), owner_pid, "BOSS backup",
                     str(r["error"] or ""), _aware(r["at"]), {"kind": r["kind"]})
                for r in rows][:1]

    def settled(self, c, item):
        later = c.execute(text(
            "SELECT id FROM backups WHERE status IN ('verified', 'pruned', 'succeeded', 'ok') "
            "AND started_at > (SELECT started_at FROM backups WHERE id = :i) LIMIT 1"),
            {"i": item.id}).scalar()
        return Said(RESOLVED, f"A later backup ({later}) succeeded.") if later else None

    def classify(self, c, item):
        cls, code, action = super().classify(c, item)
        return (PLATFORM if cls == APP else cls), code, action

    def retry(self, engine, settings, jobs, item, *, cls, attempt, note):
        from .backup_scheduler import run_backup

        got = run_backup(engine, settings) or {}
        return f"taking a new backup ({got.get('id') or got.get('status') or ''})"

    def person_retry(self, engine, settings, jobs, ref, actor):
        return self.retry(engine, settings, jobs, None, cls=PLATFORM, attempt=1, note="")  # type: ignore[arg-type]

    def still_failed(self, c, ref):
        newest = c.execute(text("SELECT status FROM backups ORDER BY started_at DESC LIMIT 1")
                           ).scalar()
        return newest == "failed"


class GitHubConnections(Surface):
    """A GitHub connection BOSS tested and found broken."""

    key = "github_connection"
    tables = ("github_connections",)
    noun = "GitHub connection"

    def failed(self, c, since, pid=None):
        rows = c.execute(text(
            "SELECT id, project_id, COALESCE(connection_label, account_login, 'GitHub') AS title, "
            "last_error, updated_at FROM github_connections WHERE status = 'error' "
            "AND revoked_at IS NULL AND disabled_at IS NULL AND updated_at >= :since "
            "AND (CAST(:pid AS text) IS NULL OR project_id = :pid)"), {"since": since, "pid": pid}).mappings().all()
        return [Item(self.key, str(r["id"]), str(r["id"]), r["project_id"], str(r["title"]),
                     str(r["last_error"] or "GitHub refused BOSS's key."), _aware(r["updated_at"]),
                     {}) for r in rows]

    def classify(self, c, item):
        return PERSON, "repo_access", ("Reconnect GitHub in Setup › Repositories (its key or "
                                       "app installation was refused), then press Done — retry.")

    def person_retry(self, engine, settings, jobs, ref, actor):
        return "checking the connection again"

    def still_failed(self, c, ref):
        return c.execute(text("SELECT status FROM github_connections WHERE id = :i"),
                         {"i": ref["id"]}).scalar() == "error"


#: THE REGISTRY, in processing order (a workflow run before the deployments it owns).
SURFACES: dict[str, Surface] = {s.key: s for s in (
    CodeTasks(), WorkflowRuns(), Deployments(), Previews(), Jobs(), Meetings(), Backups(),
    GitHubConnections())}

#: Tables the code writes a failure state to that are NOT a surface of their own, and why their
#: failures still have an owner. The invariant guard fails on a table in neither place.
EXEMPT: dict[str, str] = {
    "receipts": "a receipt records one attempt of an effect; its job (Jobs) is the item",
    "model_gateway_invocations": "one model call; the build, step or job that made it owns it",
    "changesets": "a deployment's change set; its deployment (Deployments) is the item",
    "changeset_operations": "one step of a deployment's change set; its deployment owns it",
    "repo_change_sets": "a multi-repo merge set; its merge card (approve_change_set) owns it",
    "repo_push_pending": "a push waiting on GitHub; github_outage retries it and says so",
    "github_resolution_plans": "a plan step of a merge; its merge card owns it",
    "architecture_reviews": "a workflow's architecture step; its workflow run owns it",
    "investigations": "an Ask BOSS investigation; it answers in its own thread",
    "infra_plans": "a provisioning plan; its workflow step or deploy owns it",
    "project_onboarding_runs": "onboarding ends needs_you with its own setup card (needs.setup_cards)",
    "task_copies": "a copy to an external tracker; ticket_sync retries it with backoff",
    "agent_conversation_turns": "one chat turn; the person sees the error in their thread and asks again",
    "agent_runs": "a kernel agent run; its job (Jobs) or work item owns it",
    "agent_wakeups": "a timer for an agent; the agent's work item owns it",
    "webhook_deliveries": "an outbound webhook; delivered with backoff and dead-lettered as a job",
    "inbox_messages": "an inbound message; its job (Jobs) owns it",
    "a2a_deliveries": "an agent-to-agent delivery; retried by its sender with backoff",
    "a2a_envelopes": "an agent-to-agent envelope; its delivery owns it",
    "a2a_transport_attempts": "one transport attempt of a delivery; its delivery owns it",
    "resource_receipts": "a provisioned resource's receipt; its deploy step owns it",
    "app_alerts": "a firing alert; the triage agent and the incident squad own it",
    "incidents": "an incident; the incident squad owns it",
    "storage_incidents": "disk pressure; the Infra Agent owns it and asks with its own card",
    "discovery_runs": "a research run fails with its job; that dead letter (Jobs) is the item",
}


# ─────────────────────────────── the owner record ───────────────────────────────


def _row(c: Any, surface: str, key: str) -> Optional[dict[str, Any]]:
    r = c.execute(text("SELECT * FROM failure_owners WHERE surface = :s AND item_key = :k"),
                  {"s": surface, "k": key}).mappings().first()
    return dict(r) if r else None


def _write(c: Any, item: Item, *, state: str, words: str, by: str = "failure_owner",
           cls: Optional[str] = None, cause_code: Optional[str] = None, cause: str = "",
           reason: str = "", action: str = "", decision_id: Optional[str] = None,
           attempts: Optional[int] = None, next_attempt_at: Optional[datetime] = None,
           note: Optional[dict[str, Any]] = None, now: Optional[datetime] = None) -> None:
    """Upsert the item's owner row; `note` (an attempt, a hand-off) is appended to its history
    and said as an event when the state or the words changed."""
    from .core import emit_event, make_id

    now = now or _now()
    prev = _row(c, item.surface, item.key)
    changed = prev is None or prev["state"] != state or prev["words"] != words \
        or prev.get("decision_id") != decision_id
    entry = {"at": now.isoformat(), "state": state, "words": words, "by": by,
             **({"item_id": item.id} if item.id else {}), **(note or {})}
    hints = {k: v for k, v in item.hints.items() if k in (
        "step_id", "run_id", "task_id", "kind", "target_id", "repository", "ref", "commit_sha",
        "code_task_run_id", "workflow_run_id", "root")}
    c.execute(text(
        "INSERT INTO failure_owners (id, surface, item_key, item_id, project_id, title, state, "
        " class, cause_code, cause, owner, owned_by, attempts, max_attempts, next_attempt_at, words, "
        " reason, action, decision_id, hints, history, failed_at, opened_at, updated_at, "
        " resolved_at) VALUES (:id, :s, :k, :i, :p, :t, :st, :cl, :cc, :ca, :ow, :by, "
        " COALESCE(CAST(:at AS int), 0), :mx, CAST(:na AS timestamptz), :w, :r, :ac, :d, CAST(:h AS JSONB), CAST(:hist AS JSONB), "
        " CAST(:fa AS timestamptz), :now, :now, CASE WHEN :st = 'resolved' THEN CAST(:now AS timestamptz) END) "
        "ON CONFLICT (surface, item_key) DO UPDATE SET item_id = EXCLUDED.item_id, "
        " project_id = COALESCE(EXCLUDED.project_id, failure_owners.project_id), "
        " title = EXCLUDED.title, state = EXCLUDED.state, "
        " class = COALESCE(EXCLUDED.class, failure_owners.class), "
        " cause_code = COALESCE(EXCLUDED.cause_code, failure_owners.cause_code), "
        " cause = CASE WHEN EXCLUDED.cause <> '' THEN EXCLUDED.cause ELSE failure_owners.cause END, "
        " owned_by = EXCLUDED.owned_by, attempts = COALESCE(CAST(:at AS int), failure_owners.attempts), "
        " next_attempt_at = EXCLUDED.next_attempt_at, words = EXCLUDED.words, "
        " reason = EXCLUDED.reason, action = EXCLUDED.action, decision_id = EXCLUDED.decision_id, "
        " hints = EXCLUDED.hints, failed_at = COALESCE(EXCLUDED.failed_at, failure_owners.failed_at), "
        " history = CASE WHEN CAST(:changed AS boolean) THEN (failure_owners.history || EXCLUDED.history) "
        "   ELSE failure_owners.history END, updated_at = :now, "
        " resolved_at = CASE WHEN EXCLUDED.state = 'resolved' THEN CAST(:now AS timestamptz) END"),
        {"id": make_id("fown"), "s": item.surface, "k": item.key, "i": item.id,
         "p": item.project_id, "t": item.title[:200], "st": state, "cl": cls, "cc": cause_code,
         "ca": cause[:500], "ow": AGENT, "by": by, "at": attempts, "mx": MAX_ATTEMPTS,
         "na": next_attempt_at, "w": words[:600], "r": reason[:800], "ac": action[:600],
         "d": decision_id, "h": json.dumps(hints, default=str), "hist": json.dumps([entry],
                                                                                  default=str),
         "fa": item.failed_at, "now": now, "changed": changed})
    if changed and item.project_id:
        emit_event(c, str(item.project_id), f"failure_owner.{state}", ACTOR, item.surface,
                   item.id or item.key, {"key": item.key, "words": words[:300], "by": by,
                                         "reason": reason[:300], "decision_id": decision_id,
                                         **(note or {})})


def fixing_words(attempt: int, doing: str) -> str:
    return f"BOSS is fixing this: attempt {attempt} of {MAX_ATTEMPTS} — {doing}."


def _card(c: Any, item: Item, *, cls: str, cause_code: str, cause: str, action: str,
          reason: str) -> Optional[str]:
    """ONE action item per cause per surface per project: a second item failing the same way
    joins the first card (its `items`), so 28 docs updates that need a repository are one card."""
    from .core import create_blocking_decision
    from .decision_actions import OPEN_STATUSES

    pid = item.project_id
    if not pid:
        return None
    ref = {"surface": item.surface, "key": item.key, "id": item.id, "title": item.title}
    found = c.execute(text(
        "SELECT id FROM decisions WHERE project_id = :p AND status = ANY(:s) AND "
        "recommendation->>'action' = :a AND recommendation->>'surface' = :sf AND "
        "recommendation->>'cause_code' = :c ORDER BY created_at LIMIT 1"),
        {"p": pid, "s": sorted(OPEN_STATUSES), "a": CARD_ACTION, "sf": item.surface,
         "c": cause_code}).scalar()
    if found:
        c.execute(text(
            "UPDATE decisions SET recommendation = jsonb_set(recommendation, '{items}', "
            "COALESCE(recommendation->'items', '[]'::jsonb) || CAST(:r AS jsonb)), "
            "updated_at = now() WHERE id = :id AND NOT EXISTS (SELECT 1 FROM "
            "jsonb_array_elements(COALESCE(recommendation->'items', '[]'::jsonb)) e "
            "WHERE e->>'key' = :k)"), {"r": json.dumps([ref]), "id": found, "k": item.key})
        return str(found)
    noun = SURFACES[item.surface].noun
    what = item.title if item.title.lower() != noun.lower() else noun
    question = _clip(f"{noun} · {what}: {cause.rstrip('.')}. {action}", 280)
    return create_blocking_decision(
        c, str(pid), None, question,
        {"action": CARD_ACTION, "surface": item.surface, "items": [ref],
         "cause_code": cause_code, "class": cls, "cause": cause, "what_to_do": action,
         "recommended": action, "why": reason, "why_a_person": reason, "why_not": reason,
         "title": item.title, "asked_by": None},
        {}, CARD_REASON, default_action="hold")


def _clip(s: str, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ─────────────────────────────── the loop ───────────────────────────────


@dataclass
class _Budget:
    acts: int


def _own(engine: Any, settings: Any, jobs: Any, surface: Surface, item: Item, now: datetime,
         budget: _Budget) -> str:
    """Give ONE failed item its owner. Returns the state it ends in."""
    from ..db import begin

    with begin(engine) as c:
        row = _row(c, surface.key, item.key)
        if row and row["state"] == RESOLVED and row.get("item_id") == item.id:
            return RESOLVED                     # a person dismissed this very failure
        said = surface.settled(c, item)
        if said is not None:
            _write(c, item, state=RESOLVED, words=said.words, by=said.by or "failure_owner",
                   reason=said.reason, attempts=(row or {}).get("attempts"), now=now)
            return RESOLVED
        said = surface.moving(c, item) or surface.held(c, item)
        if said is not None:
            words = said.words
            if said.state == FIXING and row and row.get("owned_by") == "failure_owner" and \
                    int(row.get("attempts") or 0) > 0:
                words = fixing_words(int(row["attempts"]), said.words)
            elif said.state == FIXING:
                words = f"BOSS is fixing this: {said.words}."
            _write(c, item, state=said.state, words=words, by=said.by,
                   decision_id=said.decision_id, reason=said.reason or "",
                   attempts=(row or {}).get("attempts"), now=now)
            return said.state
        cls, cause_code, action = surface.classify(c, item)
        cause = _words(item.error, 200)
        made = surface.attempts_made(c, item, int((row or {}).get("attempts") or 0))
        if row and row["state"] == NEEDS_PERSON and _open_decision(c, row.get("decision_id")) \
                and row.get("item_id") == item.id:
            return NEEDS_PERSON                 # its card stands until it is answered
        if row and row.get("owned_by") == "person" and row["state"] == FIXING and \
                (_aware(row.get("updated_at")) or now) > now - timedelta(minutes=PERSON_GRACE_MINUTES):
            return FIXING                       # a person just said it is done: give it time
        old = item.failed_at is not None and now - item.failed_at > timedelta(hours=ACT_HOURS)
        if cls == PERSON:
            reason = _WHY_PERSON.get(cause_code, "Only a person can do what it needs.")
            if cause_code.startswith("asked_by_"):
                rec = item.hints.get("recovery") or {}
                reason = _clip(f"{rec.get('agent') or 'Its agent'} looked into it: "
                               f"{rec.get('reasoning') or cause}", 600)
            return _hand_over(c, item, cls=cls, cause_code=cause_code, cause=cause,
                              action=action, reason=reason, attempts=made, now=now)
        if old and made == 0:
            return _hand_over(
                c, item, cls=cls, cause_code=f"{cause_code}:old", cause=cause,
                action="Press Done — retry to run it again now.",
                reason=(f"It failed {_ago(item.failed_at, now)} ago. BOSS re-runs work by itself "
                        f"only within {ACT_HOURS} hours of a failure, because what it was for "
                        "may have changed since."), attempts=made, now=now)
        if made >= MAX_ATTEMPTS:
            if cls == PLATFORM:
                _self_fix(c, settings, item, cause_code, cause)
            return _hand_over(
                c, item, cls=cls, cause_code=f"{cause_code}:spent", cause=cause,
                action=("Check what it names, then press Done — retry." if cls == APP else
                        "Check the BOSS host is healthy (Infra › Doctor), then press Done — "
                        "retry."),
                reason=(f"BOSS tried {made} time{'s' if made != 1 else ''} and it failed the same "
                        "way each time" + (" on BOSS's own side; BOSS filed a fix for itself."
                                           if cls == PLATFORM else
                                           ": the fix needs a person's judgement.")),
                attempts=made, now=now)
        due = _aware((row or {}).get("next_attempt_at"))
        attempt = made + 1
        if (due is not None and due > now) or budget.acts <= 0:
            # WAITING ITS TURN (the backoff, or this tick's attempts are spent): still owned,
            # and said so — with the time when it is known.
            later = (f"{_plan(cls)} at {due:%H:%M} UTC" if due is not None and due > now
                     else f"{_plan(cls)} on its next pass")
            _write(c, item, state=FIXING, cls=cls, cause_code=cause_code, cause=cause,
                   words=fixing_words(attempt, later), attempts=made,
                   next_attempt_at=due if due is not None and due > now else None, now=now)
            return FIXING
    # ACT, outside the read transaction: an attempt opens its own.
    budget.acts -= 1
    note = _fix_note(surface, item, cause)
    try:
        doing = surface.retry(engine, settings, jobs, item, cls=cls, attempt=attempt, note=note)
    except NeedsPerson as np:
        with begin(engine) as c:
            return _hand_over(c, item, cls=cls, cause_code=f"{cause_code}:withheld", cause=cause,
                              action=np.action or "Press Done — retry to run it again.",
                              reason=np.reason, attempts=made, now=now)
    except Exception as exc:  # noqa: BLE001 — an attempt that could not start is still an attempt
        log.warning("failure owner: attempt %s on %s %s did not start", attempt, surface.key,
                    item.key, exc_info=True)
        wait = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS)) - 1]
        with begin(engine) as c:
            _write(c, item, state=FIXING, cls=cls, cause_code=cause_code, cause=cause,
                   words=fixing_words(attempt, f"the attempt could not start ({_words(exc, 120)}); "
                                               f"BOSS tries again at "
                                               f"{now + timedelta(seconds=wait):%H:%M} UTC"),
                   attempts=attempt, next_attempt_at=now + timedelta(seconds=wait),
                   note={"attempt": attempt, "started": False, "error": _words(exc, 200)}, now=now)
        return FIXING
    wait = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS)) - 1]
    with begin(engine) as c:
        _write(c, item, state=FIXING, cls=cls, cause_code=cause_code, cause=cause,
               words=fixing_words(attempt, doing), attempts=attempt,
               next_attempt_at=now + timedelta(seconds=wait),
               note={"attempt": attempt, "started": True, "doing": doing, "class": cls}, now=now)
    return FIXING


def _plan(cls: str) -> str:
    return "building a fix" if cls == APP else "running it again"


def _ago(at: Optional[datetime], now: datetime) -> str:
    if at is None:
        return "a while"
    hours = int((now - at).total_seconds() // 3600)
    return f"{hours // 24} days" if hours >= 48 else f"{hours} hours"


def _fix_note(surface: Surface, item: Item, cause: str) -> str:
    return _clip(
        f"BOSS's failure owner retried this after it failed: {cause} "
        "Find why it failed and fix that cause; do not work around it or weaken a check.", 900)


def _hand_over(c: Any, item: Item, *, cls: str, cause_code: str, cause: str, action: str,
               reason: str, attempts: int, now: datetime) -> str:
    card = _card(c, item, cls=cls, cause_code=cause_code, cause=cause, action=action,
                 reason=reason)
    _write(c, item, state=NEEDS_PERSON, cls=cls, cause_code=cause_code, cause=cause,
           words=f"Needs you: {action}", reason=reason, action=action, decision_id=card,
           attempts=attempts, note={"handed_over": True, "why": reason[:300]}, now=now)
    return NEEDS_PERSON


def _self_fix(c: Any, settings: Any, item: Item, cause_code: str, cause: str) -> None:
    """A platform failure that repeated: written down for BOSS to fix itself
    (boss_recommendations, the same record step_recovery's wait_for_boss writes)."""
    if not item.project_id:
        return
    from . import boss_recommendations

    boss_recommendations.record(
        c, settings, project_id=str(item.project_id),
        signature=f"failure_owner:{item.surface}:{cause_code}",
        title=f"{SURFACES[item.surface].noun} keeps failing on BOSS's side: {cause}"[:200],
        symptom=item.error[:1000], evidence=[f"{item.surface} {item.id}", cause],
        ref={"surface": item.surface, "item_id": item.id, "key": item.key}, actor=ACTOR)


def reconcile(c: Any, seen: set[tuple[str, str]], now: datetime, *,
              project_id: Optional[str] = None, only: Optional[list[str]] = None
              ) -> dict[str, int]:
    """Owner rows still open whose item no longer shows as failed: resolved when it is fine
    now (it recovered, a person's own retry, a newer attempt finished), kept while it moves.
    Then every action item whose items are all resolved is withdrawn."""
    out = {"resolved": 0, "withdrawn": 0}
    rows = c.execute(text(
        "SELECT * FROM failure_owners WHERE state <> 'resolved' AND "
        "(CAST(:pid AS text) IS NULL OR project_id = :pid) AND "
        "(CAST(:only AS text[]) IS NULL OR surface = ANY(CAST(:only AS text[]))) "
        "ORDER BY updated_at LIMIT 1000"), {"pid": project_id, "only": only}).mappings().all()
    for r in rows:
        if (r["surface"], r["item_key"]) in seen:
            continue
        surface = SURFACES.get(r["surface"])
        ref = {"id": r["item_id"], "key": r["item_key"], "project_id": r["project_id"],
               "hints": _json(r["hints"], {}) or {}}
        try:
            with c.begin_nested():
                failed = surface.still_failed(c, ref) if surface else False
        except Exception:  # noqa: BLE001 — unreadable: leave it as it is
            continue
        if failed is False:
            item = Item(r["surface"], r["item_key"], r["item_id"], r["project_id"],
                        str(r["title"] or ""), "", _aware(r["failed_at"]),
                        _json(r["hints"], {}) or {})
            _write(c, item, state=RESOLVED, by=str(r["owned_by"] or "failure_owner"),
                   words=("It works now" + (f", after {r['attempts']} attempt"
                                            f"{'s' if r['attempts'] != 1 else ''} by BOSS."
                                            if r["attempts"] else ".")),
                   attempts=r["attempts"], now=now)
            out["resolved"] += 1
    out["withdrawn"] = withdraw_settled_cards(c, project_id=project_id)
    return out


def withdraw_settled_cards(c: Any, *, project_id: Optional[str] = None) -> int:
    """An action item none of whose items still needs a person is withdrawn, with words."""
    from .decision_actions import OPEN_STATUSES
    from .run_decisions import _withdraw_with_note

    n = 0
    cards = c.execute(text(
        "SELECT id, project_id, recommendation FROM decisions WHERE status = ANY(:s) AND "
        "recommendation->>'action' = :a AND (CAST(:pid AS text) IS NULL OR project_id = :pid)"),
        {"s": sorted(OPEN_STATUSES), "a": CARD_ACTION, "pid": project_id}
    ).mappings().all()
    for card in cards:
        rec = _json(card["recommendation"], {}) or {}
        keys = [str(i.get("key")) for i in rec.get("items") or [] if isinstance(i, dict)]
        waiting = c.execute(text(
            "SELECT count(*) FROM failure_owners WHERE surface = :s AND item_key = ANY(:k) "
            "AND state = 'needs_person' AND decision_id = :d"),
            {"s": rec.get("surface"), "k": keys, "d": card["id"]}).scalar() or 0
        if not waiting and _withdraw_with_note(
                c, str(card["id"]), str(card["project_id"]),
                "Withdrawn: BOSS sees it moving or fixed now.", actor=ACTOR,
                why={"why": "every failure on this card has recovered or is moving"}):
            n += 1
    return n


def sweep(engine: Any, settings: Any, *, now: Optional[datetime] = None, jobs: Any = None,
          max_acts: int = MAX_ACTS_PER_TICK, only: Optional[list[str]] = None,
          project_id: Optional[str] = None) -> dict[str, Any]:
    """The always-on pass (monitor tick): every failed item gets its owner. Never raises; one
    item's trouble never costs another."""
    from ..db import begin

    now = now or _now()
    since = now - timedelta(days=REACH_DAYS)
    budget = _Budget(acts=max_acts)
    report: dict[str, Any] = {"items": 0, FIXING: 0, NEEDS_PERSON: 0, RESOLVED: 0, "errors": 0}
    seen: set[tuple[str, str]] = set()
    for key, surface in SURFACES.items():
        if only and key not in only:
            continue
        try:
            with begin(engine) as c:
                items = surface.failed(c, since, project_id)
        except Exception:  # noqa: BLE001 — a surface an install lacks
            log.warning("failure owner: could not read %s", key, exc_info=True)
            report["errors"] += 1
            continue
        for item in items:
            seen.add((item.surface, item.key))
            try:
                state = _own(engine, settings, jobs, surface, item, now, budget)
                report[state] = report.get(state, 0) + 1
                report["items"] += 1
            except Exception:  # noqa: BLE001
                log.exception("failure owner: %s %s", key, item.key)
                report["errors"] += 1
    try:
        with begin(engine) as c:
            report.update(reconcile(c, seen, now, project_id=project_id, only=only))
    except Exception:  # noqa: BLE001
        log.exception("failure owner: reconcile")
        report["errors"] += 1
    return report


# ─────────────────────────────── the person's answer ───────────────────────────────


def on_answered(connection: Any, settings: Any, jobs: Any, decision: Mapping[str, Any], *,
                verdict: str, actor: str, spawn: Optional[Callable[[Callable[[], None]], Any]] = None
                ) -> list[dict[str, Any]]:
    """"Done — retry" (approve): every item on the card runs again as the person, its owner row
    says so; "Leave it" (reject): BOSS stops owning those failures, saying who decided."""
    rec = _json(decision.get("recommendation"), {}) or {}
    surface = SURFACES.get(str(rec.get("surface") or ""))
    now = _now()
    refs = []
    for ref in rec.get("items") or []:
        if not isinstance(ref, dict):
            continue
        row = _row(connection, str(ref.get("surface") or rec.get("surface")), str(ref.get("key")))
        if row is None:
            continue
        item = Item(row["surface"], row["item_key"], row["item_id"], row["project_id"],
                    str(row["title"] or ""), "", _aware(row["failed_at"]),
                    _json(row["hints"], {}) or {})
        if verdict == "approve":
            _write(connection, item, state=FIXING, by="person",
                   words=f"BOSS is running it again: {actor_words(actor)} said it is done.",
                   attempts=0, now=now, note={"retry_by": actor, "decision_id": decision["id"]})
            refs.append({"id": row["item_id"], "key": row["item_key"],
                         "project_id": row["project_id"], "hints": item.hints,
                         "surface": row["surface"]})
        else:
            _write(connection, item, state=RESOLVED, by="person",
                   words=f"{actor_words(actor)} chose not to retry it.",
                   attempts=row["attempts"], now=now,
                   note={"dismissed_by": actor, "decision_id": decision["id"]})
    if verdict == "approve" and refs and surface is not None:
        def run() -> None:
            from ..db import get_engine

            eng = get_engine()
            for ref in refs:
                try:
                    did = SURFACES[ref["surface"]].person_retry(eng, settings, jobs, ref, actor)
                    words = f"BOSS is running it again ({did}) because you said it is done."
                except NeedsPerson as np:
                    words = f"Could not run it again: {np.reason}"
                except Exception as exc:  # noqa: BLE001 — said on the item, never lost
                    log.warning("failure owner: person retry of %s failed", ref["key"],
                                exc_info=True)
                    words = f"Could not run it again: {_words(exc, 200)}"
                try:
                    with eng.begin() as c:
                        c.execute(text("UPDATE failure_owners SET words = :w, updated_at = now() "
                                       "WHERE surface = :s AND item_key = :k"),
                                  {"w": words[:600], "s": ref["surface"], "k": ref["key"]})
                except Exception:  # noqa: BLE001
                    log.warning("failure owner: could not record person retry", exc_info=True)

        (spawn or (lambda fn: fn()))(run)
    return refs


def actor_words(actor: str) -> str:
    return "You" if not actor or actor.startswith(("user:", "usr_", "ceo", "operator")) else actor


# ─────────────────────────────── reading it ───────────────────────────────


def public(row: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    """What a surface shows of an owner row: never a bare failure."""
    if not row:
        return None
    return {"state": row["state"], "words": row["words"], "reason": row.get("reason") or "",
            "action": row.get("action") or "", "decision_id": row.get("decision_id"),
            "attempts": int(row.get("attempts") or 0), "max_attempts": MAX_ATTEMPTS,
            "owner": row.get("owner") or AGENT, "by": row.get("owned_by") or "",
            "class": row.get("class"), "at": (row.get("updated_at").isoformat()
                                              if isinstance(row.get("updated_at"), datetime)
                                              else row.get("updated_at"))}


def owners(connection: Any, surface: str, keys: list[str]) -> dict[str, dict[str, Any]]:
    """{item key: public owner} for one surface; {} where the table is missing."""
    keys = [k for k in dict.fromkeys(str(k) for k in keys if k)]
    if not keys:
        return {}
    try:
        with connection.begin_nested():
            rows = connection.execute(text(
                "SELECT * FROM failure_owners WHERE surface = :s AND item_key = ANY(:k)"),
                {"s": surface, "k": keys}).mappings().all()
    except Exception:  # noqa: BLE001 — an install before the table
        return {}
    return {str(r["item_key"]): public(dict(r)) for r in rows}


def board(connection: Any, project_id: Optional[str] = None, *, limit: int = 50
          ) -> list[dict[str, Any]]:
    """What BOSS is fixing and what needs a person, newest first (the Command Center's list)."""
    try:
        with connection.begin_nested():
            rows = connection.execute(text(
                "SELECT * FROM failure_owners WHERE state <> 'resolved' AND "
                "(CAST(:p AS text) IS NULL OR project_id = :p) ORDER BY updated_at DESC LIMIT :n"),
                {"p": project_id, "n": limit}).mappings().all()
    except Exception:  # noqa: BLE001
        return []
    return [{"surface": r["surface"], "item_id": r["item_id"], "project_id": r["project_id"],
             "title": r["title"], **(public(dict(r)) or {})} for r in rows]


def unowned(engine_or_connection: Any, *, now: Optional[datetime] = None,
            only: Optional[list[str]] = None, project_id: Optional[str] = None
            ) -> list[dict[str, Any]]:
    """THE INVARIANT AS A QUERY: every failed item within reach with no valid owner — no row,
    `fixing` with no words, `needs_person` with no reason or no open card, or `resolved` before
    it failed again. Empty on a healthy install."""
    now = now or _now()
    since = now - timedelta(days=REACH_DAYS)
    bad: list[dict[str, Any]] = []

    def check(c: Any) -> None:
        for key, surface in SURFACES.items():
            if only and key not in only:
                continue
            for item in surface.failed(c, since, project_id):
                row = _row(c, key, item.key)
                why = _invalid(c, row, item)
                if why:
                    bad.append({"surface": key, "key": item.key, "id": item.id,
                                "project_id": item.project_id, "why": why})

    if hasattr(engine_or_connection, "begin") and not hasattr(engine_or_connection, "execute"):
        with engine_or_connection.begin() as c:
            check(c)
    else:
        check(engine_or_connection)
    return bad


def _invalid(c: Any, row: Optional[Mapping[str, Any]], item: Item) -> Optional[str]:
    if row is None:
        return "no owner"
    if row["state"] == FIXING:
        return None if str(row.get("words") or "").strip() else "fixing, but says nothing"
    if row["state"] == NEEDS_PERSON:
        if not str(row.get("reason") or "").strip():
            return "handed to a person with no reason"
        if not _open_decision(c, row.get("decision_id")):
            return "handed to a person with no open action item"
        return None
    if row["state"] == RESOLVED:
        # Settled or dismissed: valid for THIS failure only; a newer failure of the same
        # lineage is a new one to own.
        return None if str(row.get("item_id") or "") == item.id else \
            "resolved an earlier failure; this one has no owner"
    return f"unknown state {row['state']!r}"


__all__ = ["ACT_HOURS", "AGENT", "APP", "CARD_ACTION", "CARD_REASON", "EXEMPT", "FIXING",
           "Item", "MAX_ATTEMPTS", "NEEDS_PERSON", "NeedsPerson", "PERSON", "PLATFORM",
           "REACH_DAYS", "RESOLVED", "SURFACES", "Surface", "board", "classify", "on_answered",
           "owners", "public", "reconcile", "sweep", "unowned", "withdraw_settled_cards"]
