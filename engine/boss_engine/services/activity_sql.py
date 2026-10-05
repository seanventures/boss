"""The activity log as SQL: one projection per source, so filters, paging and counts are queries.

The log used to be a Python union. Every source loaded up to 5,000 rows, Python classified each
one (the LEVEL and STATUS of a row are decided from its names), and only then were the filters
applied. A filtered page classified up to 50,000 rows to show 50, the facet chips classified
another 20,000 on every scope change, and the step panel did it again every five seconds.

Here each source is described once — its table, its time column, and the SQL expressions for
every field an operator filters on — and the level and status are SQL `CASE`s that mirror the
Python classifier exactly (`activity_classify`). `test_level_sql_matches_the_classifier` runs
both over every source/kind/status combination; the Python classifier stays the oracle and
still builds each returned row, so the two cannot drift silently.

Nothing here is a second event store. Every expression reads the source table as it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .activity_classify import (
    _CLOSED_MARKERS,
    _FAILED_MARKERS,
    _OK_MARKERS,
    _STUCK_MARKERS,
    _WAITING_MARKERS,
)


def _alternation(markers: tuple[str, ...]) -> str:
    return "(" + "|".join(markers) + ")"


FAILED_RE = _alternation(_FAILED_MARKERS)
STUCK_RE = _alternation(_STUCK_MARKERS)
WAITING_RE = _alternation(_WAITING_MARKERS)
OK_RE = _alternation(_OK_MARKERS)
CLOSED_RE = _alternation(_CLOSED_MARKERS)


def status_case(parts: tuple[str, ...], status_value: str) -> str:
    """`classify_activity`'s status, as SQL.

    failed → stuck (only while the status is still open) → waiting → ok → info, by substring of
    the lower-cased names. Joining the parts with spaces cannot create a marker the Python
    version would not see: no marker contains a space except `handoff succeeded`, which
    `succeeded` already covers.
    """
    haystack = "lower(" + " || ' ' || ".join(f"coalesce({p}, '')" for p in parts) + ")"
    value = f"lower(coalesce({status_value}, ''))"
    return (
        f"(CASE WHEN {haystack} ~ '{FAILED_RE}' THEN 'failed' "
        f"WHEN {haystack} ~ '{STUCK_RE}' AND {value} !~ '{CLOSED_RE}' THEN 'stuck' "
        f"WHEN {haystack} ~ '{WAITING_RE}' THEN 'waiting' "
        f"WHEN {haystack} ~ '{OK_RE}' THEN 'ok' ELSE 'info' END)"
    )


def level_from_status(status_sql: str) -> str:
    """`_level_for(severity, status)`: the classifier's severity is a function of its status."""
    return f"(CASE {status_sql} WHEN 'failed' THEN 'error' WHEN 'stuck' THEN 'warn' ELSE 'info' END)"


def _trim(expr: str) -> str:
    return f"btrim(coalesce({expr}, ''))"


@dataclass(frozen=True)
class Source:
    """One stream of the log, as SQL over its own table (alias `t`)."""

    name: str
    table: str
    #: One WHERE per index-backed part; app_logs has two (this project, and no project).
    scopes: tuple[str, ...]
    ts: str
    raw: str
    level: str
    status: str
    kind: str
    actor: str
    entity_type: str
    entity_id: str
    title: str
    text: str
    #: The run a row belongs to, for the RUN facet (a workflow run, else a work task).
    run_key: str = "''"
    #: Raw columns, so a filter stays index-backed. None: this source cannot carry it, and a
    #: filter on it skips the source rather than scanning it for nothing.
    agent: Optional[str] = None
    service: Optional[str] = None
    #: Scope predicates (raw columns, index-backed). None: no row of this source can match.
    run_match: Optional[str] = None
    wfr_match: Optional[str] = None
    step_match: Optional[str] = None
    work_match: Optional[str] = None
    act_match: Optional[str] = None
    #: `subject=<id>`: the one thing a sibling screen links here about (a deploy target, a
    #: model, a service). Raw columns; None means the source never names that kind of thing.
    subject_match: Optional[str] = None
    fts: Optional[str] = None
    started: str = "NULL::timestamptz"
    finished: str = "NULL::timestamptz"
    always: str = ""
    #: The column the newest-row probe reads for this source's ETag (index-backed).
    newest: str = "t.created_at"


_P = "t.project_id = :project_id"

_EVENT_STATUS = status_case(("t.event_type", "t.event_type"), "''")
_PS_KIND = f"coalesce(nullif({_trim('t.action')}, ''), {_trim('t.step_key')})"
_PS_STATUS = status_case(("t.action", "t.status", _PS_KIND), "t.status")
_JOB_STATUS = status_case(("t.kind", "t.status", "t.kind"), "t.status")
_RECEIPT_STATUS = status_case(("t.effect_kind", "t.status", "t.effect_kind"), "t.status")
_LLM_KIND = f"('llm.' || coalesce(nullif({_trim('t.stage')}, ''), 'call'))"
_LLM_OK = "(CASE WHEN coalesce(t.ok, false) THEN 'ok' ELSE 'failed' END)"
_LLM_STATUS = status_case((_LLM_KIND, _LLM_OK), _LLM_OK)
_TURN_KIND = (f"('turn.' || {_trim('t.direction')} || CASE WHEN {_trim('t.tool_name')} <> '' "
              f"THEN '.' || {_trim('t.tool_name')} ELSE '' END)")
_TURN_OK = _LLM_OK
_TURN_STATUS = status_case((_TURN_KIND, _TURN_OK), _TURN_OK)
_APP_RAW_LEVEL = f"lower(coalesce(nullif({_trim('t.level')}, ''), 'info'))"
_APP_LEVEL = (f"(CASE WHEN {_APP_RAW_LEVEL} IN ('warning', 'warn') THEN 'warn' "
              f"WHEN {_APP_RAW_LEVEL} IN ('critical', 'fatal', 'error') THEN 'error' "
              f"WHEN {_APP_RAW_LEVEL} = 'verbose' THEN 'verbose' ELSE 'info' END)")
_APP_KIND = (f"('log.' || regexp_replace(coalesce(nullif({_trim('t.logger')}, ''), 'boss'), "
             r"'^.*\.', ''))")
_APP_STATUS_VALUE = f"(CASE WHEN {_APP_LEVEL} = 'error' THEN 'failed' ELSE 'info' END)"
_APP_STATUS = status_case((_APP_KIND, _APP_STATUS_VALUE), _APP_STATUS_VALUE)
_STEP_TS = "COALESCE(t.completed_at, t.started_at, t.created_at)"
_STEP_KIND = (f"(CASE WHEN {_trim('t.step_kind')} <> '' THEN 'step.' || {_trim('t.step_kind')} "
              "ELSE 'step' END)")
_STEP_ACTION = (f"(CASE WHEN {_trim('t.step_id')} <> '' THEN 'step ' || {_trim('t.step_id')} "
                f"|| ' ' || {_trim('t.status')} ELSE 'step ' || {_trim('t.status')} END)")
_STEP_STATUS = status_case((_STEP_ACTION, "t.status", "t.step_kind"), "t.status")
_STEP_LEVEL = (
    f"(CASE WHEN {_trim('t.status')} IN ('failed', 'deadlettered') THEN 'error' "
    f"WHEN coalesce(t.attempt, 0) > 1 OR ({_trim('t.last_error')} <> '' "
    f"AND {_trim('t.status')} <> 'succeeded') THEN 'warn' "
    f"WHEN {_trim('t.status')} IN ('succeeded', 'skipped', 'cancelled') THEN 'info' "
    "ELSE 'verbose' END)"
)
_DEPLOY_KIND = (f"(CASE WHEN {_trim('t.environment')} <> '' THEN 'deploy.' || "
                f"{_trim('t.environment')} ELSE 'deploy' END)")
_DEPLOY_ACTION = (f"btrim('deploy ' || {_trim('t.app_name')} || ' to ' || "
                  f"{_trim('t.environment')} || ' ' || {_trim('t.status')})")
_DEPLOY_STATUS = status_case((_DEPLOY_ACTION, "t.status", "'deployment'"), "t.status")


def _text_of(*parts: str) -> str:
    """The haystack plain-text search reads where no full-text index exists (lower-cased).
    A superset of `_matches_q`'s haystack: rows are confirmed against it after the query."""
    return "lower(concat_ws(' ', " + ", ".join(parts) + "))"


def _words(expr: str) -> str:
    """A title as `_humanize` spells it (separators to spaces), for search."""
    return rf"regexp_replace(coalesce({expr}, ''), '[._\-]+', ' ', 'g')"


SOURCES: dict[str, Source] = {
    "event": Source(
        name="event", table="events", scopes=(_P,), ts="t.created_at",
        raw=("jsonb_build_object('event_type', t.event_type, 'actor', t.actor, "
             "'entity_type', t.entity_type, 'entity_id', t.entity_id, 'data', t.data)"),
        status=_EVENT_STATUS, level=level_from_status(_EVENT_STATUS),
        kind=_trim("t.event_type"), actor=_trim("t.actor"), entity_type=_trim("t.entity_type"),
        entity_id=_trim("t.entity_id"), title="t.event_type",
        text=_text_of("t.event_type", "t.actor", "t.entity_type", "t.entity_id",
                      _words("t.event_type")),
        run_key=("(CASE WHEN t.entity_type = 'workflow_run' OR left(t.entity_id, 5) = 'crun_' "
                 "THEN t.entity_id ELSE '' END)"),
        run_match="t.entity_id = :run_id AND left(:run_id, 5) = 'crun_'",
        wfr_match="t.entity_type = 'workflow_run' AND t.entity_id = :wfr",
        step_match=("t.entity_type = 'workflow_run' AND t.entity_id = :wfr "
                    "AND t.data->>'step_id' = :step"),
        work_match="t.entity_id = ANY(:work)", act_match="t.entity_id = :act",
        subject_match="t.entity_id = :subject",
        fts=("to_tsvector('english', translate(coalesce(t.event_type, '') || ' ' || "
             "coalesce(t.actor, '') || ' ' || coalesce(t.entity_type, '') || ' ' || "
             "coalesce(t.entity_id, ''), '._-', '   ')) @@ "
             "plainto_tsquery('english', translate(:q, '._-', '   '))"),
    ),
    "process_step": Source(
        name="process_step", table="agent_process_steps", scopes=(_P,), ts="t.created_at",
        raw=("jsonb_build_object('step_key', t.step_key, 'role_key', t.role_key, "
             "'action', t.action, 'status', t.status, 'handoff_from', t.handoff_from)"),
        status=_PS_STATUS, level=level_from_status(_PS_STATUS), kind=_PS_KIND,
        actor=(f"coalesce(nullif({_trim('t.role_key')}, ''), "
               f"nullif({_trim('t.handoff_from')}, ''), 'system')"),
        entity_type="'agent_process_step'", entity_id="t.id",
        title=f"coalesce(nullif({_trim('t.action')}, ''), {_PS_KIND})",
        text=_text_of(_PS_KIND, "t.role_key", "t.handoff_from", "'agent_process_step'", "t.id",
                      _words(f"coalesce(nullif({_trim('t.action')}, ''), {_PS_KIND})")),
    ),
    "job": Source(
        name="job", table="jobs", scopes=(_P,), ts="t.created_at",
        raw=("jsonb_build_object('kind', t.kind, 'status', t.status, "
             "'lease_owner', t.lease_owner)"),
        status=_JOB_STATUS, level=level_from_status(_JOB_STATUS), kind=_trim("t.kind"),
        actor=f"coalesce(nullif({_trim('t.lease_owner')}, ''), 'system')",
        entity_type="'job'", entity_id="t.id", title="t.kind",
        text=_text_of("t.kind", "t.lease_owner", "'system'", "'job'", "t.id", _words("t.kind")),
        always=("t.status IN ('queued', 'running', 'succeeded', 'failed', 'deadlettered')"),
    ),
    "deadletter": Source(
        name="deadletter", table="deadletters", scopes=(_P,), ts="t.created_at",
        raw="jsonb_build_object('source_kind', t.source_kind, 'status', t.status)",
        # Its classify row names "deadletter", which is a failure marker: always failed.
        status="'failed'", level="'error'",
        kind=f"coalesce(nullif({_trim('t.source_kind')}, ''), 'deadletter')",
        actor="'system'", entity_type="'deadletter'", entity_id="t.id", title="'deadletter'",
        text=_text_of("t.source_kind", "'system deadletter'", "t.id"),
    ),
    "receipt": Source(
        name="receipt", table="receipts", scopes=(_P,), ts="t.created_at",
        raw="jsonb_build_object('effect_kind', t.effect_kind, 'status', t.status)",
        status=_RECEIPT_STATUS, level=level_from_status(_RECEIPT_STATUS),
        kind=_trim("t.effect_kind"), actor="'system'", entity_type="'receipt'",
        entity_id="t.id", title="t.effect_kind",
        text=_text_of("t.effect_kind", "'system receipt'", "t.id", _words("t.effect_kind")),
    ),
    "llm_call": Source(
        name="llm_call", table="llm_calls", scopes=(_P,), ts="t.created_at",
        raw=("jsonb_build_object('code_task_run_id', t.code_task_run_id, 'stage', t.stage, "
             "'role', t.role, 'engine', t.engine, 'model', t.model, "
             "'total_tokens', t.total_tokens, 'cost_usd_micros', t.cost_usd_micros, "
             "'latency_ms', t.latency_ms, 'retries', t.retries, 'turns', t.turns, "
             "'ok', t.ok, 'error', t.error, 'act_id', t.act_id, "
             "'included_in_subscription', t.included_in_subscription)"),
        status=_LLM_STATUS,
        level=("(CASE WHEN NOT coalesce(t.ok, false) THEN 'error' "
               "WHEN coalesce(t.retries, 0) <> 0 THEN 'warn' ELSE 'info' END)"),
        kind=_LLM_KIND, actor=f"coalesce(nullif({_trim('t.role')}, ''), 'agent')",
        entity_type="'code_task_run'", entity_id=_trim("t.code_task_run_id"), title=_LLM_KIND,
        text=_text_of(_LLM_KIND, "t.role", "'code_task_run'", "t.code_task_run_id", "t.model",
                      "t.error", _words(_LLM_KIND)),
        run_key="coalesce(t.code_task_run_id, '')", agent="t.role",
        run_match="t.code_task_run_id = :run_id", work_match="t.code_task_run_id = ANY(:work)",
        act_match="t.act_id = :act",
        subject_match=("(t.model = :subject OR t.role = :subject OR t.engine = :subject "
                       "OR t.code_task_run_id = :subject)"),
    ),
    "agent_turn": Source(
        name="agent_turn", table="agent_turns", scopes=(_P,), ts="t.created_at",
        raw=("jsonb_build_object('code_task_run_id', t.code_task_run_id, 'stage', t.stage, "
             "'role_key', t.role_key, 'model', t.model, 'turn_index', t.turn_index, "
             "'direction', t.direction, 'tool_name', t.tool_name, 'content', t.content, "
             "'content_bytes', t.content_bytes, 'truncated', t.truncated, 'tokens', t.tokens, "
             "'level', t.level, 'ok', t.ok, 'act_id', t.act_id)"),
        status=_TURN_STATUS, level=f"coalesce(nullif({_trim('t.level')}, ''), 'verbose')",
        kind=_TURN_KIND, actor=f"coalesce(nullif({_trim('t.role_key')}, ''), 'agent')",
        entity_type="'code_task_run'", entity_id=_trim("t.code_task_run_id"), title=_TURN_KIND,
        text=_text_of(_TURN_KIND, "t.role_key", "t.code_task_run_id", "t.content"),
        run_key="coalesce(t.code_task_run_id, '')", agent="t.role_key",
        run_match="t.code_task_run_id = :run_id", work_match="t.code_task_run_id = ANY(:work)",
        act_match="t.act_id = :act",
        fts=("to_tsvector('english', translate(coalesce(t.content, ''), '._-', '   ')) "
             "@@ plainto_tsquery('english', translate(:q, '._-', '   '))"),
    ),
    "app_log": Source(
        name="app_log", table="app_logs",
        # Records with no project belong in every operator's log: an error raised before any
        # run was resolved has no other home. Two index-backed parts, not one OR.
        scopes=(_P, "t.project_id IS NULL"), ts="t.created_at",
        raw=("jsonb_build_object('run_id', t.run_id, 'service', t.service, 'level', t.level, "
             "'logger', t.logger, 'message', t.message)"),
        status=_APP_STATUS, level=_APP_LEVEL, kind=_APP_KIND,
        actor=f"coalesce(nullif({_trim('t.service')}, ''), 'boss')",
        entity_type=("(CASE WHEN coalesce(t.run_id, '') <> '' THEN 'code_task_run' "
                     "ELSE 'service' END)"),
        entity_id=(f"coalesce(nullif({_trim('t.run_id')}, ''), "
                   f"nullif({_trim('t.service')}, ''), 'boss')"),
        title=_APP_KIND,
        text=_text_of(_APP_KIND, "t.service", "t.run_id", "t.message", _words(_APP_KIND)),
        run_key="coalesce(t.run_id, '')", service="t.service",
        run_match="t.run_id = :run_id", work_match="t.run_id = ANY(:work)",
        subject_match="(t.run_id = :subject OR t.service = :subject)",
    ),
    "workflow_step": Source(
        name="workflow_step", table="workflow_step_runs", scopes=(_P,), ts=_STEP_TS,
        raw=("jsonb_build_object('workflow_run_id', t.workflow_run_id, 'step_id', t.step_id, "
             "'step_kind', t.step_kind, 'status', t.status, 'iteration', t.iteration, "
             "'attempt', t.attempt, 'max_attempts', t.max_attempts, "
             "'last_error', t.last_error)"),
        status=_STEP_STATUS, level=_STEP_LEVEL, kind=_STEP_KIND, actor="'workflow'",
        entity_type="'workflow_step_run'", entity_id="t.id", title=_STEP_ACTION,
        text=_text_of(_STEP_KIND, "'workflow'", "'workflow_step_run'", "t.id", "t.last_error",
                      _words(_STEP_ACTION)),
        run_key="t.workflow_run_id", wfr_match="t.workflow_run_id = :wfr",
        step_match="t.workflow_run_id = :wfr AND t.step_id = :step",
        started="t.started_at", finished="t.completed_at", newest=_STEP_TS,
    ),
    "deployment": Source(
        name="deployment", table="deployment_runs", scopes=(_P,), ts="t.created_at",
        raw=("jsonb_build_object('app_name', t.app_name, 'environment', t.environment, "
             "'status', t.status, 'target_kind', t.target_kind, 'target_ref', t.target_ref, "
             "'commit_ref', t.commit_ref, 'repo', t.repo, 'failure_reason', t.failure_reason, "
             "'created_by', t.created_by)"),
        status=_DEPLOY_STATUS,
        level=(f"(CASE WHEN {_trim('t.failure_reason')} <> '' OR {_trim('t.status')} = 'failed' "
               "THEN 'error' ELSE 'info' END)"),
        kind=_DEPLOY_KIND, actor=f"coalesce(nullif({_trim('t.created_by')}, ''), 'deployer')",
        entity_type="'deployment_run'", entity_id="t.id", title=_DEPLOY_ACTION,
        text=_text_of(_DEPLOY_KIND, "t.created_by", "'deployment_run'", "t.id",
                      "t.failure_reason", _words(_DEPLOY_ACTION)),
        service="t.app_name",
        subject_match=("(t.deployment_target_id = :subject OR t.id = :subject "
                       "OR t.app_name = :subject)"),
        started="t.started_at", finished="t.finished_at",
    ),
}

ORDER = tuple(SOURCES)
