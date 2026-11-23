"""Say why model work is not moving, in words an operator can act on.

Discovery sat at "queued" through three different failures on boss-1 (2026-10-01): the API
refused the worker (409), the budget's per-minute cap refused every stage, and the Claude
subscription's stored credential was rejected (401). Each was recorded somewhere — a job's
last_error, an event, a run's error column — and none was shown where the operator was looking,
so the run looked like it was waiting for nothing. This turns a raw failure into one sentence and
the next step, and every surface that shows model work uses it.
"""
from __future__ import annotations

import logging
import re
import threading
import traceback
from typing import Any, Mapping, Optional

log = logging.getLogger(__name__)

#: Where an operator fixes a model connection.
SETUP_HREF = "/?area=setup&focus=models"


def setup_href(source_id: Optional[str] = None) -> str:
    """Setup's Models row — and that source's menu, when the source is known."""
    from urllib.parse import quote

    return f"{SETUP_HREF}&source={quote(source_id)}" if source_id else SETUP_HREF

_LOGIN_REJECTED = (
    "invalid bearer token", "failed to authenticate", "authentication_error",
    "invalid api key", "invalid x-api-key", "not logged in", "please run /login",
    "oauth token has expired", "token has expired", "login was rejected",
    "claude sign-in needed",          # claude_login.ClaudeSignInNeeded
)
_HTTP_401 = re.compile(r"(?<![\w.])401(?![\w.])")
_PROVIDER_NAMES = {
    "anthropic_claude_code": "Claude Code subscription",
    "claude_code": "Claude Code subscription",
    "openai_codex_chatgpt": "Codex subscription",
    "codex": "Codex subscription",
    "typesafe_jev": "TypeSafe Jev",
    "typesafe": "TypeSafe Jev",
}


#: What a model failure says when a call that was meant to WRITE an answer reached for a tool
#: instead (claude_cli_adapter.ClaudeCliAdapter._judge_generation). The CLI's own result says
#: so — a permission denial, a second turn, `stop_reason: tool_use`, `max_turns` — and the text
#: it returns is a preamble ("I'll check memory for context…") or a tool name ("Read"), not the
#: answer. A retry can succeed: it is the model's turn that failed, not the setup.
NO_DIRECT_ANSWER = "reached for a tool instead of answering"


#: Where an operator raises the cap.
BUDGET_HREF = "/?area=budget"

#: The job gate's reasons for holding work by budget state (jobs._budget_state_policy_for_job).
BUDGET_HOLD_REASONS = (
    "red_halts_non_p0_work", "orange_defers_non_critical_work",
    "red_freezes_infra_provisioning", "red_freezes_tier2_effects",
)
_HELD_PREFIX = "held — the budget is"
_HELD_DETAIL = re.compile(r"held — the budget is (\w+) \(([^)]*)\)", re.I)
#: "It resets at 00:00 UTC, or raise the daily cap in Govern › Limits." — the way out of a period cap.
_RESETS = re.compile(r"It resets at [^.]*? in Govern › Limits\.")
_REFUSED_BY_CAP = re.compile(r"budget cap exceeded: (.+?)\. It resets at", re.I)


def _usd(micros: int) -> str:
    dollars = int(micros or 0) / 1_000_000
    if dollars == int(dollars):
        return f"${dollars:,.0f}"
    if dollars < 0.01:  # a test-sized cap of a few micros is not "$0.00"
        return f"${dollars:.6f}".rstrip("0")
    return f"${dollars:,.2f}"


def cap_used_sentence(period: Mapping[str, Any]) -> str:
    """How much of a period's cap is used, for a period from BudgetGovernor.status_for_project
    (the snapshot itself, or one of its `periods`):
    "today's budget is used ($10.20 of $10)", "this month's budget is 95% used ($190 of $200)"."""
    whose = period.get("whose") or "the"
    cap = int(period.get("cap_usd_micros") or 0)
    committed = int(period.get("committed_usd_micros") or 0)
    ratio = float(period.get("utilization_ratio") or 0)
    used = "used" if ratio >= 1 else f"{round(ratio * 100):.0f}% used"
    return f"{whose} budget is {used} ({_usd(committed)} of {_usd(cap)})"


def cap_refusal_sentence(period: Mapping[str, Any], requested_usd_micros: int) -> str:
    """Why a reservation did not fit, naming WHAT WAS ASKED (brec_bd2a474ed4eb4537a373: a $10
    self-fix was refused "today's budget is 0% used ($0.000012 of $10)" — true, and no reason
    at all). A used-up period says so; otherwise the sentence is the reservation against what is
    left, or against the whole cap when one reservation is bigger than the cap itself."""
    cap = int(period.get("cap_usd_micros") or 0)
    committed = int(period.get("committed_usd_micros") or 0)
    if float(period.get("utilization_ratio") or 0) >= 1 or not requested_usd_micros:
        return cap_used_sentence(period)
    whose = period.get("whose") or "the"
    if requested_usd_micros > cap:
        return (f"this needs {_usd(requested_usd_micros)} reserved, more than {whose} whole "
                f"budget of {_usd(cap)}")
    # What is left, rounded DOWN to the cent: "$10.00 left" beside "needs $10" reads as a
    # contradiction when $9.999988 is left.
    left_cents = max(cap - committed, 0) // 10_000
    return (f"this needs {_usd(requested_usd_micros)} reserved and {whose} budget has "
            f"${left_cents / 100:,.2f} left ({_usd(committed)} of {_usd(cap)} used)")


def resets_sentence(period: Mapping[str, Any]) -> str:
    """"It resets at 00:00 UTC, or raise the daily cap in Govern › Limits." — every cap measures its own
    period, so the first way out is waiting for it to turn over."""
    return (f"It resets at {period.get('resets_at_label') or '00:00 UTC'}, "
            f"or raise the {period.get('cap_name') or 'cap'} in Govern › Limits.")


def budget_hold_sentence(budget_state: str, utilization_ratio: Optional[float],
                         cap_usd_micros: Optional[int],
                         period: Optional[Mapping[str, Any]] = None) -> str:
    """THE ONE WORDING for work the budget state holds — the job's last_error, the run's and the
    discovery's attention, and readiness all say this, never "no worker has picked it up".

    With the budget's `period` (the status snapshot, BudgetGovernor.status_for_project):
    "Held — the budget is red (95% of today's $10 used), so only P0 work runs. It resets at
    00:00 UTC, or raise the daily cap in Govern › Limits."

    The money it counts is money: a subscription's calls are not in it (included_spend.py), and
    only the money of the cap's own period (budget.period_spend)."""
    state = str(budget_state or "unknown")
    whose = (period or {}).get("whose")
    used = ""
    if cap_usd_micros:
        percent = round(float(utilization_ratio or 0) * 100)
        cap = f"{whose} {_usd(cap_usd_micros)}" if whose else _usd(cap_usd_micros)
        used = f" ({percent:.0f}% of {cap} used)"
    only = {"orange": ", so only P0 and P1 work runs",
            "red": ", so only P0 work runs"}.get(state, "")
    way_out = (resets_sentence(period) if period and period.get("resets_at_label")
               else "Raise the cap in Govern › Limits to let it run.")
    return f"Held — the budget is {state}{used}{only}. {way_out}"


def held_headline(sentence: object) -> str:
    """A hold sentence without its way out: "Held — the budget is red (…), so only P0 work
    runs" — for a readiness detail or a headline that shows the way out beside it."""
    return re.split(r"\. (?:It resets at|Raise the cap)", str(sentence or "").strip(),
                    maxsplit=1)[0].rstrip(".")


def is_budget_hold(error: object) -> bool:
    lowered = str(error or "").lower()
    return (_HELD_PREFIX in lowered
            or any(reason in lowered for reason in BUDGET_HOLD_REASONS)
            or bool(re.search(r"budget state \w+ deferred job", lowered)))


def provider_name(source_or_engine: Optional[str]) -> str:
    return _PROVIDER_NAMES.get(str(source_or_engine or ""), "the model")


def is_login_rejected(error: object) -> bool:
    text = str(error or "").lower()
    return any(term in text for term in _LOGIN_REJECTED) or bool(_HTTP_401.search(text))


#: Kinds that are NOT failures: the work could not start yet, for a reason that clears by
#: itself. Every surface that decides "fail or wait" asks `waiting_for`, so a new kind of wait
#: added here waits everywhere — a workflow step (workflow_capability_runner._as_wait), a
#: discovery (discovery_service.execute_discovery_job), a typed decision.
WAIT_KINDS = frozenset({"budget_wait", "capacity_wait", "credential_store_wait"})

#: The gateway's refusal when every concurrent lease on the source is held (model_gateway).
CAPACITY_BUSY = "no concurrent lease capacity"


def waiting_for(error: object, *, source_id: Optional[str] = None) -> Optional[dict]:
    """The explained wait this failure really is, or None when it is a failure.

    THE ONE CLASSIFICATION of transient-vs-terminal for model work. On boss-1 (2026-10-04)
    three example workflows started together; the third discovery's stage found the Claude
    subscription's one lease held by the other two and was FAILED with "Selected model source
    has no concurrent lease capacity" — a queue, read as a verdict."""
    said = explain(error, source_id=source_id)
    return said if said["kind"] in WAIT_KINDS else None


#: How long to wait before asking once more whether a refused login really is refused.
LOGIN_RECHECK_SECONDS = 3.0


def confirm_login_rejection(call, *, sleep=None):
    """Run `call`; when it fails as a REJECTED LOGIN, run it once more before believing that.

    A rejected login is the one failure every surface stops at for good — the run fails, Setup
    shows the subscription broken, nothing retries — so a single report of it must not be
    enough. On boss-1 one research call out of many concurrent ones said "Not logged in" while
    the calls a few seconds either side of it succeeded on the same login. A second refusal is
    a login that is really rejected; a success means it never was."""
    try:
        return call()
    except Exception as error:  # noqa: BLE001 — only a login refusal is looked at again
        if not is_login_rejected(error):
            raise
    import time

    (sleep or time.sleep)(LOGIN_RECHECK_SECONDS)
    return call()


#: A database failure, as text (a recorded last_error) — SQLAlchemy/psycopg's own wording, AT
#: THE START of the text (after at most one "IntegrityError: " class name, the shape a step's
#: generic failure takes). Anywhere else it is a QUOTE: northgate-gateway's circuit breaker said
#: "circuit breaker: 2 attempts … have failed (last: '(psycopg.errors.IdleInTransaction…')" — a
#: refusal that quoted an old build's error — and the step read it as BOSS's database failing,
#: retried itself three times in a second and was dead-lettered (2026-11-16).
_STORAGE_TEXT = re.compile(
    r"^\s*(?:[A-Za-z_]\w*: )?"
    r"(?:\((?:psycopg2?|sqlalchemy)\.(?:errors|exc)\.\w+\)"
    r"|\([\w.]+\) [^\n]*\n\[SQL: "        # str(sqlalchemy DBAPIError) with any driver's error
    r"|duplicate key value violates unique constraint"
    r"|(?:insert or update on table \S+ |update or delete on table \S+ |null value in column \S+ )?"
    r"(?:violates (?:foreign key|not-null|check|exclusion) constraint)"
    r"|current transaction is aborted|could not serialize access|deadlock detected)",
    re.I,
)


def is_storage_failure(error: object) -> bool:
    """A failure of BOSS's own database — not the model, not the operator's setup.

    An exception is one when it, or what it was raised from, is SQLAlchemy's or psycopg's; text
    (and an exception's own message) only when it BEGINS with the database's wording — a sentence
    that merely quotes a database error (a refusal naming a build's last error) is not one."""
    seen = 0
    current = error
    while isinstance(current, BaseException) and seen < 5:
        module = type(current).__module__ or ""
        if module.startswith(("sqlalchemy", "psycopg")):
            return True
        current = current.__cause__
        seen += 1
    return bool(_STORAGE_TEXT.search(str(error or "")))


#: How much of a failure's message and stack is kept on a step, a dead letter or a log line.
_RECORD_MESSAGE_LIMIT = 2000
_RECORD_STACK_LIMIT = 6000


def _scrubbed(value: str) -> str:
    """No credential leaves in a record: secrets and URL userinfo masked (redaction), and a
    statement's bound values dropped — a row's data is not a diagnosis."""
    from .redaction import mask_url_credentials, redact_secrets

    try:
        value = re.sub(r"\[parameters: [^\n]*", "[parameters: not recorded]", value)
        return mask_url_credentials(redact_secrets(value))
    except Exception:  # noqa: BLE001 — a record must never fail; keep nothing rather than risk it
        return "[unavailable: the text could not be redacted]"


def failure_record(error: object) -> dict:
    """THE FAILURE ITSELF, for an operator: {type, message, stack?} — secrets redacted.

    A person reads one sentence ("BOSS couldn't save this to its database."); this is what that
    sentence stands for, stored next to it (the step's output, the dead letter) and logged, so
    the cause is never only in a message that was rewritten away. `stack` is the traceback of a
    live exception (its chain included); a recorded string has none."""
    if isinstance(error, BaseException):
        record: dict[str, Any] = {
            "type": f"{type(error).__module__}.{type(error).__qualname__}",
            "message": _scrubbed(str(error))[:_RECORD_MESSAGE_LIMIT],
        }
        if error.__traceback__ is not None or error.__cause__ is not None:
            stack = "".join(traceback.format_exception(type(error), error, error.__traceback__))
            # The END of a long stack is where it broke: keep that part.
            record["stack"] = _scrubbed(stack)[-_RECORD_STACK_LIMIT:]
        return record
    return {"type": "text", "message": _scrubbed(str(error or ""))[:_RECORD_MESSAGE_LIMIT]}


#: Recorded failures (text) already logged by this process — a re-shown last_error is logged once.
_LOGGED_TEXT: dict[str, None] = {}
_LOGGED_LOCK = threading.Lock()
_LOGGED_KEEP = 512


def log_failure(error: object, *, where: str = "", said: str = "") -> dict:
    """Log a failure that is being turned into a person-facing sentence, WITH its type, message
    and stack (secrets redacted), and return its failure_record.

    The sentence a person sees may point at "the log"; this makes that true. A live exception
    is logged every time (each is a new failure); a recorded string once per process, so a
    screen re-explaining a stored last_error does not flood the log."""
    record = failure_record(error)
    if isinstance(error, BaseException):
        if getattr(error, "_boss_failure_logged", False):
            return record          # already logged where it happened, with its context
        try:
            error._boss_failure_logged = True  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — an exception that takes no attributes is logged again
            pass
    else:
        key = record["message"]
        with _LOGGED_LOCK:
            if key in _LOGGED_TEXT:
                return record
            _LOGGED_TEXT[key] = None
            while len(_LOGGED_TEXT) > _LOGGED_KEEP:
                _LOGGED_TEXT.pop(next(iter(_LOGGED_TEXT)))
    log.warning("%s%s: %s%s%s", f"{where}: " if where else "", record["type"],
                record["message"], f" (shown as: {said})" if said else "",
                f"\n{record['stack']}" if record.get("stack") else "")
    return record


def technical_summary(error: object) -> str:
    """One or two lines for a collapsed "Details" and the log: the error class and the
    database's own message, WITHOUT the statement, its parameters or the background link.
    "IntegrityError: duplicate key value violates unique constraint "idx_…" · Key (…) already
    exists." — never the SQL text a raw str(error) carries."""
    text = str(error or "").strip()
    for cut in ("[SQL:", "[parameters:", "(Background on this error"):
        at = text.find(cut)
        if at >= 0:
            text = text[:at]
    text = re.sub(r"^\([\w.]+\)\s*", "", text)   # "(psycopg.errors.UniqueViolation) "
    text = " · ".join(line.strip() for line in text.splitlines() if line.strip())
    name = type(error).__name__ if isinstance(error, BaseException) else ""
    return (f"{name}: {text}" if name and text else name or text)[:300]


def cause_line(error: object, n: int = 140) -> str:
    """The failure's own one-line cause, safe to show a person: technical_summary, secrets
    redacted, cut, in backticks (it is the error's words, not BOSS's)."""
    from .failure_words import short

    line = short(technical_summary(error), n).replace("`", "'")
    return f"`{line}`" if line else ""


# ── BOSS's own database, by cause ────────────────────────────────────────────────────────────
# "BOSS couldn't save this to its database. Try again." was said for a full disk, a lost
# connection, a duplicate and a deadlock alike (enterprise RCA, BOSSAPP-1428): the person could
# not tell a wait from a fix. Each cause now has its own sentence and its own next step. The kind
# stays "storage" for every caller; `storage_cause` says which.
_DISK_FULL = re.compile(r"\bDiskFull\b|no space left on device|could not extend file|disk is full|"
                        r"disk full", re.I)
_CONNECTION_LOST = re.compile(
    r"server closed the connection|connection refused|terminating connection|could not connect|"
    r"connection (?:is |was )?closed|connection timed? ?out|connection reset|"
    r"\b(?:AdminShutdown|CannotConnectNow|ConnectionFailure|ConnectionTimeout)\b|"
    r"the database system is (?:starting up|shutting down|in recovery mode)", re.I)
_DUPLICATE = re.compile(r"\bUniqueViolation\b|duplicate key value violates unique constraint", re.I)
_BUSY = re.compile(r"\b(?:DeadlockDetected|SerializationFailure|LockNotAvailable|"
                   r"InFailedSqlTransaction)\b|deadlock detected|could not serialize access|"
                   r"current transaction is aborted|lock timeout|could not obtain lock", re.I)
_CONSTRAINT = (
    (re.compile(r"update or delete on table \S+ violates foreign key", re.I),
     "other saved records still refer to it"),
    (re.compile(r"\bForeignKeyViolation\b|violates foreign key constraint", re.I),
     "it refers to something that is no longer there (it may have just been deleted)"),
    (re.compile(r"\bNotNullViolation\b|violates not-null constraint", re.I),
     "a value it needs was missing"),
    (re.compile(r"\bCheckViolation\b|violates check constraint", re.I),
     "a value was outside what BOSS allows there"),
    (re.compile(r"\bExclusionViolation\b|violates exclusion constraint", re.I),
     "it overlaps something already saved"),
)


def _chain_text(error: object) -> str:
    """The failure's words and every exception class in its chain, for classifying it — never
    the statement (a table or value named like a cause is not that cause)."""
    def words(value: object) -> str:
        text = str(value or "")
        return text[:text.find("[SQL:")] if "[SQL:" in text else text

    parts, current, seen = [words(error)], error, 0
    while isinstance(current, BaseException) and seen < 5:
        parts.append(type(current).__name__)
        parts.append(words(getattr(current, "orig", "") or ""))
        current = current.__cause__ or getattr(current, "orig", None)
        seen += 1
    return " ".join(parts)


def storage_said(error: object) -> dict:
    """{storage_cause, headline, action, turn_action, retry} for a failure of BOSS's database."""
    said_text = _chain_text(error)
    if _DISK_FULL.search(said_text):
        return {"storage_cause": "disk_full", "retry": True,
                "headline": "BOSS's database is out of disk space, so this was not saved.",
                "action": ("Infra Agent frees space and the work resumes by itself. To help, "
                           "free disk on the host (Govern › Infrastructure) or run `boss "
                           "doctor` on it."),
                "turn_action": ("Its database is out of disk space. Infra Agent frees space; "
                                "to help, free disk in Govern › Infrastructure or run `boss "
                                "doctor`.")}
    if _CONNECTION_LOST.search(said_text):
        return {"storage_cause": "connection_lost", "retry": True,
                "headline": "BOSS lost its connection to its database, so this was not saved.",
                "action": ("BOSS reconnects by itself: retry in a minute. If it keeps "
                           "happening, run `boss doctor` on the host; the error is in the "
                           "Activity Log."),
                "turn_action": ("Try again in a minute: BOSS lost its database connection and "
                                "reconnects by itself. If it persists, run `boss doctor`.")}
    if _DUPLICATE.search(said_text):
        # A unique violation is a COLLISION: a name already taken, or another save of the same
        # thing landing at the same moment (a chat thread's insert race). Say both; retry stays
        # allowed because the race clears on a second try.
        return {"storage_cause": "duplicate", "retry": True,
                "headline": ("BOSS did not save this: something with the same name or key is "
                             "already saved."),
                "action": ("Reload to see what is there. If it is not what you meant, choose a "
                           "different name and try again; the error is in the Activity Log."),
                "turn_action": ("Try again: another save landed at the same moment. If it "
                                "repeats, Details shows the error.")}
    if _BUSY.search(said_text):
        return {"storage_cause": "busy", "retry": True,
                "headline": ("BOSS's database was busy with a change that conflicted with this "
                             "one, so this was not saved."),
                "action": ("Retry it: this clears by itself. If it repeats, the error is in "
                           "the Activity Log."),
                "turn_action": ("Try again: the database was busy with a conflicting change. "
                                "If it repeats, Details shows the error.")}
    for pattern, words in _CONSTRAINT:
        if pattern.search(said_text):
            return {"storage_cause": "constraint", "retry": False,
                    "headline": f"BOSS did not save this: {words}. Nothing was changed.",
                    "action": ("Reload this page to see what is there now, then make the "
                               "change again. If it repeats, the error is in the Activity "
                               "Log."),
                    "turn_action": (f"Try again; it conflicted ({words}). If it repeats, "
                                    "Details shows the error.")}
    cause = cause_line(error)
    return {"storage_cause": "other", "retry": True,
            "headline": (f"BOSS couldn't save this to its database: {cause}." if cause else
                         "BOSS couldn't save this to its database."),
            "action": ("Retry it. If it keeps happening, run `boss doctor` on the host; the "
                       "full error is in the Activity Log."),
            "turn_action": "Try again; if it repeats, Details shows the error."}


def explain_turn_failure(error: object, *, source_id: Optional[str] = None) -> dict:
    """THE ONE SENTENCE for an agent turn that failed — the dock, AgentChat, a room, a step.

    An existing install showed the operator "BOSS could not complete that turn: IntegrityError:
    (psycopg.errors.UniqueViolation) duplicate key value … [SQL: INSERT INTO agent_threads …"
    — a database's diagnostics in a chat bubble. The visible message is `headline` + `action`
    (`sentence`); `detail` is technical_summary, for a collapsed Details and the server log.
    A turn that failed for no better-named reason says its one-line cause (never the SQL).
    """
    said = dict(_redacted(_explain(error, source_id=source_id)))
    if said["kind"] == "storage":
        said["headline"] = "BOSS couldn't save this conversation."
        said["action"] = said.get("turn_action") or said["action"]
    elif said["kind"] == "failed":
        cause = cause_line(error)
        said.update(kind="turn_failed",
                    headline=(f"BOSS couldn't finish this turn: {cause}." if cause
                              else "BOSS couldn't finish this turn."),
                    action="Try again; if it repeats, Details shows the error.")
    said.pop("turn_action", None)
    said["detail"] = _scrubbed(technical_summary(error))
    said["sentence"] = f"{said['headline']} {said['action']}".strip()
    return said


def explain(error: object, *, source_id: Optional[str] = None) -> dict:
    """{kind, headline, action, href, retry} for a failure. Never repeats a credential: every
    word of it (a headline that quotes the error, the `detail` for Details) is redacted here,
    once, whichever branch said it."""
    said = _redacted(_explain(error, source_id=source_id))
    # The chat wording of a storage failure is explain_turn_failure's, never a caller's payload.
    said.pop("turn_action", None)
    return said


def _redacted(said: dict) -> dict:
    from .redaction import redact_secrets

    for key, value in list(said.items()):
        if isinstance(value, str) and value:
            try:
                said[key] = redact_secrets(value)
            except Exception:  # noqa: BLE001 — never show what could not be redacted
                said[key] = ("" if key in ("detail", "turn_action") else
                             "BOSS could not say this safely; the error is in the Activity Log.")
    return said


def _explain(error: object, *, source_id: Optional[str] = None) -> dict:
    text = str(error or "")
    lowered = text.lower()
    # BOSS'S OWN DATABASE, before every text check below: a statement naming a table such as
    # `reconciliations` or `budget_caps` would otherwise read as that kind of failure.
    if is_storage_failure(error):
        said = storage_said(error)
        # THE LOG HOLDS IT BECAUSE IT IS WRITTEN HERE: the sentence below points at the
        # Activity Log, so the failure is logged (type, message, stack; redacted) before it is.
        log_failure(error, where="storage failure", said=said["headline"])
        return {
            "kind": "storage", **said,
            "href": "/?area=budget&tab=infrastructure" if said["storage_cause"] == "disk_full"
            else None,
            "detail": technical_summary(error),
        }
    # THE ROUTER REFUSED, IN WORDS (model_router: Local means local, no metered spend while the
    # budget is red): its sentence IS the explanation — never "couldn't finish this turn".
    from .model_router.failover import RouteRefused

    if isinstance(error, RouteRefused):
        local = "local" in lowered
        return {
            "kind": "route_refused", "headline": text.strip(),
            "action": ("Its model policy is in Agent Studio; local models are in Setup."
                       if local else "Pins and the budget are in Setup › Model routing."),
            "href": "/?area=setup", "retry": False, "detail": text.strip(),
        }
    # A HOST CREDENTIAL ABOUT TO GO TO ANOTHER PROVIDER'S ADDRESS (rc6 N-1, env_refs): refused
    # before anything was sent. Its own words name the credential and the host, never the value;
    # retrying cannot change the answer.
    from .env_refs import misdirection_in

    misdirected = misdirection_in(error) if isinstance(error, BaseException) else None
    if misdirected is not None or ("is sent only to" in lowered and "refusing to send it" in lowered) \
            or "boss only sends keys over https" in lowered:
        said = str(misdirected or text).strip()
        return {
            "kind": "credential_misdirected", "headline": said,
            "action": ("Store a key for this endpoint on the project (Setup), or have "
                       "the operator list the host in BOSS_CREDENTIAL_HOSTS."),
            "href": setup_href(source_id), "retry": False, "detail": said,
        }
    # THE ENGINE ITSELF COULD NOT RUN (harness_failures): its sandbox, a runtime binary, its
    # login or account, a crash. Before the login check below: the sentence carries the
    # engine's own words, which may name a login, and its fix is already the right one.
    from .harness_failures import parse_sentence

    harness = parse_sentence(text)
    if harness is not None:
        return {
            "kind": "harness_failure",
            "headline": f"BOSS could not run {harness['engine']}: {harness['message']}.",
            "action": f"Fix: {harness['fix'][0].upper()}{harness['fix'][1:]}.",
            "href": setup_href(source_id), "retry": True,
            "detail": text.strip()[:300],
        }
    who = provider_name(source_id)
    #: "the Claude Code subscription", or "the model" when the source is not known.
    the_source = who if who.startswith("the ") else f"the {who}"
    # THE STORE HOLDING THE LOGIN DID NOT ANSWER (cli_harness.CredentialStoreUnavailable).
    # Before the login check: the login was never tried, so nothing about it was rejected.
    if "could not reach its secret store to read" in lowered:
        return {
            "kind": "credential_store_wait",
            "headline": "Waiting for BOSS's secret store to answer, so the model's login can "
                        "be read.",
            "action": "Nothing to do — it resumes by itself when the store answers. If this "
                      "keeps happening, check the secret store (Infisical) is running.",
            "href": None, "retry": True,
        }
    if "boss could not read the stored" in lowered:
        return {
            "kind": "credential_unreadable",
            "headline": f"BOSS could not read the login it stored for {the_source}.",
            "action": f"Reconnect {the_source} in Setup to store it again.",
            "href": setup_href(source_id), "retry": False,
        }
    # EVERY LEASE ON THE SOURCE IS HELD BY OTHER WORK. A queue, not a verdict: it frees when
    # that work's call settles (model_gateway.acquire).
    if CAPACITY_BUSY in lowered:
        return {
            "kind": "capacity_wait",
            "headline": f"Waiting for {the_source} — other work is using it right now.",
            "action": "Nothing to do — it starts by itself when the work using it finishes.",
            "href": None, "retry": True,
        }
    if is_login_rejected(lowered):
        return {
            "kind": "login_rejected",
            "headline": f"The {who} login was rejected, so no model call can run.",
            "action": (f"Reconnect the {who} in Setup. A subscription (Codex, "
                       "Claude Code, Grok) signs in again inside BOSS: press Sign in there, "
                       "or run `boss connect <provider>`."),
            "href": setup_href(source_id), "retry": False,
        }
    # HELD BY THE BUDGET STATE (the job gate). Not a failure and not a missing worker: the job
    # is queued with a later run_after, and it runs once the money spent is back under the line.
    if is_budget_hold(text):
        found = _HELD_DETAIL.search(text)
        state = (found.group(1).lower() if found else
                 "red" if "red" in lowered else "orange" if "orange" in lowered else "over")
        headline = (held_headline(text) + "." if found
                    else f"Held — the budget is {state}.")
        resets = _RESETS.search(text)
        return {
            "kind": "budget_hold",
            "headline": headline,
            "action": ((resets.group(0) + " " if resets else "Raise the cap in Govern › Limits to let it "
                        "run. ") + "Work on a subscription costs no money and does not count "
                       "against it."),
            "href": BUDGET_HREF, "retry": False,
        }
    if "budget admission is busy" in lowered:
        return {"kind": "budget_wait", "headline": "Waiting for another budget admission to finish.",
                "action": "Retry shortly; no budget increase is needed for this temporary wait.",
                "href": "/?area=budget", "retry": True}
    if "budget is held for an unresolved provider outcome" in lowered:
        return {"kind": "budget_hold", "headline": "Budget is held until an unresolved attempt is reconciled.",
                "action": "Review the reconciliation decision and provider outcome before retrying.",
                "href": "/?area=budget", "retry": False}
    # Before the cap below: the governor's headroom refusal mentions the budget too, and it
    # clears by itself when the work holding the reservations finishes (BudgetGovernor.reserve).
    if ("held by other work" in lowered
            or "waiting for other work to release the budget" in lowered):
        return {
            "kind": "budget_wait",
            "headline": "Waiting for other work to release the budget it is holding.",
            "action": "Nothing to do — it resumes by itself when the work holding the budget "
                      "finishes. Raise the daily or monthly cap in Govern › Limits if this happens often.",
            "href": "/?area=budget", "retry": True,
        }
    if "burst cap" in lowered or "waiting for the per-minute budget" in lowered:
        return {
            "kind": "budget_wait",
            "headline": "Waiting for the per-minute budget to free up.",
            "action": "Nothing to do — it resumes by itself within about a minute. Raise the "
                      "per-minute cap in Govern › Limits if this happens often.",
            "href": "/?area=budget", "retry": True,
        }
    if any(term in lowered for term in ("budget cap", "daily cap", "monthly cap",
                                        "budget reservation denied", "insufficient",
                                        "budget allocation", "frozen at this budget")):
        refused = _REFUSED_BY_CAP.search(text)
        resets = _RESETS.search(text)
        if refused and resets:
            # A period cap: "today's budget is used ($10.20 of $10)" — and when it resets.
            return {
                "kind": "budget_refused",
                "headline": ("The budget refused this work. "
                             f"{refused.group(1)[0].upper()}{refused.group(1)[1:]}."),
                "action": f"{resets.group(0)} Then retry.",
                "href": "/?area=budget", "retry": False,
            }
        return {
            "kind": "budget_refused",
            "headline": "The budget refused this work.",
            "action": "Raise the daily or monthly cap in Govern › Limits, or wait for the period to reset, "
                      "then retry.",
            "href": "/?area=budget", "retry": False,
        }
    if "cannot be pooled" in lowered or "owner scoped" in lowered:
        return {
            "kind": "capacity_owner",
            "headline": f"The {who} belongs to someone else on this install and cannot be used "
                        "for this work.",
            "action": "Connect your own subscription in Setup.",
            "href": setup_href(source_id), "retry": False,
        }
    # A SKILL THAT NEEDS ONE PROVIDER'S API (image generation, speech-to-text) and the project
    # has not connected it. The exception carries its own fix (model_routing.ProviderRequirement).
    from .model_routing import PROVIDER_REQUIREMENTS, ProviderRequirementMissing

    if ProviderRequirementMissing.MARKER in lowered:
        named = next((r for r in PROVIDER_REQUIREMENTS.values()
                      if r.label.lower() in lowered), None)
        return {
            "kind": "provider_requirement",
            "headline": (f"This step needs {named.label[0].lower() + named.label[1:]}, and this "
                         "project has not connected one." if named else
                         "This step needs a provider capability this project has not "
                         "connected."),
            "action": named.fix if named else "Connect the provider it names in Setup.",
            "href": setup_href(source_id), "retry": False,
        }
    if "no_eligible_source" in lowered or "no eligible model" in lowered \
            or "openai_api_key is not configured" in lowered:
        # "OPENAI_API_KEY is not configured" reaches here only when the project's model routing
        # (model_routing.project_chat_adapter) found nothing connected and fell through to the
        # OpenAI adapter — i.e. no model at all, not "you need OpenAI".
        return {
            "kind": "no_model",
            "headline": "No connected model can run this work.",
            "action": "Connect a model in Setup.",
            "href": setup_href(source_id), "retry": False,
        }
    if NO_DIRECT_ANSWER in lowered:
        return {
            "kind": "no_direct_answer",
            "headline": "The model reached for a tool instead of writing the answer, so what "
                        "came back was not an answer.",
            "action": "Retry the step. A writing step has no tools, so this is the model's "
                      "turn failing, not your setup; if it repeats, open the run's activity "
                      "for what the model tried.",
            "href": None, "retry": True,
            "detail": text[:300],
        }
    if re.search(r"(?<![\w.])409(?![\w.])", lowered) and "conflict" in lowered:
        return {
            "kind": "refused_by_api",
            "headline": "BOSS's API refused the worker that runs this work (409).",
            "action": "This is a BOSS defect, not something you configured: run `boss "
                      "upgrade` on the BOSS host, then press Retry on the run.",
            "href": None, "retry": False,
        }
    if "reconcil" in lowered:
        return {
            "kind": "reconcile",
            "headline": "A model call may have been charged without its answer being kept.",
            "action": "Review the reconciliation decision before retrying, so nothing is paid "
                      "for twice.",
            "href": "/?area=decisions", "retry": False,
        }
    cause = cause_line(error, 120)
    return {
        "kind": "failed",
        "headline": f"The model call failed: {cause}." if cause else "The model call failed.",
        "action": "Open the run's activity for the full error, then retry.",
        "href": None, "retry": True,
        "detail": text[:300],
    }


#: Failures that are FACTS about the project's setup — nothing connected that can answer, a
#: provider capability not connected, the budget refusing a model turn. A retry re-reads the
#: same setup and reaches the same sentence, so a step failing on one is settled at once.
#: Deliberately narrow: "no eligible source" can be a window that resets, so it still retries.
_SETTLED_MARKERS = ("openai_api_key is not configured",)


def settled_failure(error: object) -> Optional[str]:
    """The explained sentence for a failure retrying cannot fix, or None.

    Called where EVERY workflow step failure is recorded (`workflows.fail_step`), so the
    classification does not depend on which kind of step raised it: an agent step, a skill, a
    typed decision ("typed decision failed: OPENAI_API_KEY is not configured.") or the runner's
    catch-all. It was done only for agent steps, and every other path retried on a schedule and
    deadlettered on the raw sentence.
    """
    text = str(error or "")
    lowered = text.lower()
    from .model_routing import ModelSpendRefused

    said = explain(text)
    # An engine that could not run fails the same way on every retry until its fix is made.
    settled = (said["kind"] in ("provider_requirement", "harness_failure")
               or (said["kind"] == "no_model" and any(m in lowered for m in _SETTLED_MARKERS))
               or ModelSpendRefused.MARKER in lowered)
    if not settled:
        return None
    if text.startswith(said["headline"]) or said["kind"] == "harness_failure":
        return text            # already explained once; do not wrap it twice
    return f"{said['headline']} {said['action']} ({text})"


def mark_login(connection, project_id: str, source_id: Optional[str], *, rejected: bool) -> None:
    """Record on the model source whether its login was just rejected or just worked, so Setup
    shows a rejected subscription as broken instead of green "Connected"."""
    if not source_id:
        return
    from sqlalchemy import text

    if rejected:
        # A REAL FAILURE SIGNAL resets the proof schedule (proof_schedule): a cached green
        # must not sit out its TTL on a login live traffic just saw refused — the next sweep
        # re-proves it now. Only on the TRANSITION to rejected: a login that keeps failing
        # keeps its backoff instead of being re-proved every sweep it is seen failing.
        connection.execute(
            text("UPDATE entitlement_sources SET connection_metadata = "
                 "COALESCE(connection_metadata, '{}'::jsonb) - 'reproof' "
                 "WHERE project_id = :p AND source_id = :s "
                 "AND COALESCE(observed_capacity->>'credential_state', '') <> 'rejected'"),
            {"p": project_id, "s": source_id},
        )
    connection.execute(
        text("UPDATE entitlement_sources SET observed_capacity = "
             "COALESCE(observed_capacity, '{}'::jsonb) || jsonb_build_object("
             "'credential_state', CAST(:state AS text), 'credential_checked_at', now()::text), "
             "updated_at = now() WHERE project_id = :p AND source_id = :s"),
        {"state": "rejected" if rejected else "ok", "p": project_id, "s": source_id},
    )


def clear_login_state(connection, project_id: str, source_id: Optional[str]) -> None:
    """A new credential has not been tried yet: neither rejected nor proven."""
    if not source_id or connection is None:
        return
    from sqlalchemy import text

    # A fresh credential also starts a fresh proof schedule: the next sweep proves it now
    # instead of waiting out the old failure's backoff (proof_schedule).
    connection.execute(
        text("UPDATE entitlement_sources SET observed_capacity = "
             "COALESCE(observed_capacity, '{}'::jsonb) - 'credential_state', "
             "connection_metadata = COALESCE(connection_metadata, '{}'::jsonb) - 'reproof', "
             "updated_at = now() "
             "WHERE project_id = :p AND source_id = :s"),
        {"p": project_id, "s": source_id},
    )
