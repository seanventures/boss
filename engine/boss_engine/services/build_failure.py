"""A PROJECT'S LATEST FAILED BUILD — THE ONE RULE (recipe-box, 2026-11-18).

The Easy Mode build of Recipe box failed ("git add failed: … Permission denied") and every
surface that summarises a project's builds still said "Working". This module is the single
answer the project page (header, journey, Running tile, Needs you), the projects list and the
Command Center (its card, the bell, Live) all read, so they can never disagree:

* WHICH failure: the project's newest piece of work — a product workflow run (with the builds
  it owns) or a build started on its own (with its retries and follow-ups) — when it failed,
  within the last REACH_DAYS. A newer piece of work moving, done or cancelled makes an older
  failure history. A build blocked at its checks or budget is not here: it has its own cards.
* WHAT IT IS CALLED: "Build failed", "Deploy failed" (a workflow whose failed step ships) or
  "Follow-up failed" (a later attempt of work that already landed).
* WHY, in words (`failure_words`): one sentence, redacted for people
  (identity.redaction.redact_text(public=True)), never a stack, a token or a host path.
* WHAT TO DO (`action_for`): a failure only a person can fix (needs_human: a login, a
  toolchain, a service, a brief with nothing to test; a spending cap) names that fix first,
  with Retry beside it for after; a merged change or a stack BOSS cannot build offers no Retry. A workflow run's
  failure is retried from its failed step on the run's page — never a bare code-task retry,
  which would start an attempt the workflow does not know about (factory_view leaves workflow
  builds to the run's failed-step card for the same reason).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text

#: What BOSS says once its own platform retries of a build are spent (the pipeline's
#: _retry_after_infra_failure and the failed-build card): what it did, never a question.
AUTO_RETRY_SPENT_WORDS = ("BOSS tried this build {tries} times; each stopped the same way, on "
                          "BOSS's side, so BOSS stopped retrying. Press Retry to try again.")


def spent_words(n: int) -> str:
    """`n` automatic retries: n + 1 tries in all, counted from the lineage (never a field on the
    last run — stranger test r6's card said "retried twice" after 14)."""
    return AUTO_RETRY_SPENT_WORDS.format(tries=int(n) + 1)

#: How far back a failure still speaks for the project, on every surface.
REACH_DAYS = 14

_FAILED_RUN = ("failed", "deadlettered")
_LIVE_BUILD = ("running", "queued", "repairing", "waiting_for_github", "provisioning")

# ── words ───────────────────────────────────────────────────────────────────────────────────

_SUBSTRATE = re.compile(r"\s*\[substrate=[^\]]*\]")
_EXC_PREFIX = re.compile(r"^(?:[A-Za-z_][\w.]*(?:Error|Exception)|fatal|error):\s*", re.I)
_GIT_LINE = re.compile(r"^(remote|fatal|error):\s*(.+)$", re.I)
_BARE_KEY = re.compile(r"^'([\w.-]+)'$")
#: A log line's own prefix ("2026-11-17T02:00:27Z ERROR codex_api::endpoint::x: …"): not words.
_LOG_PREFIX = re.compile(r"\b\d{4}-\d\d-\d\dT[\d:.]+Z?\s+(?:ERROR|WARN|INFO|DEBUG)\s+[\w:]+:\s*")
_EXIT_STOPPED = re.compile(r"\bexit(?:ed)?(?: with)?(?: code| status)?\s*(?:143|137|-15|-9)\b", re.I)


def _sentence(line: str) -> str:
    line = line.strip().rstrip(":;,").strip()
    if len(line) > 160:
        line = line[:157].rstrip(" ,;:") + "…"
    if not line:
        return "BOSS didn't record why it stopped."
    line = line[0].upper() + line[1:]
    return line if line[-1] in ".!?…)" else line + "."


#: A worker's HTTP client failing on BOSS's own API ("Server error '500 Internal Server Error' for
#: url 'http://api:8000/v1/meetings/step'\nFor more information check: https://…"): an internal
#: address and a status line, never words for a person (stranger test r8, 2026-11-19).
_CLIENT_SERVER_ERROR = re.compile(r"^(?:\w+:\s*)?Server error '5\d\d[^']*'", re.I)
_CLIENT_NOISE = re.compile(r"\s*for url '[^']*'|\s*For more information check: \S+", re.I)
SERVER_ERROR_WORDS = ("BOSS hit an error of its own while running it; what broke is in BOSS's "
                      "Activity Log.")


def failure_words(error: Any) -> str:
    """A failure as a person reads it: one sentence, never a stack, a sandbox tag, a token or a
    host path. The raw error stays on the run's own page for whoever needs it."""
    from ..identity.redaction import redact_text
    from .agent_health import plain_cause
    from .harness_failures import parse_sentence

    raw = _LOG_PREFIX.sub("", redact_text(str(error or "").strip(), public=True))
    if _CLIENT_SERVER_ERROR.search(raw.strip()):
        return SERVER_ERROR_WORDS
    raw = _CLIENT_NOISE.sub("", raw).strip()
    if not raw:
        return "BOSS didn't record why it stopped."
    said = parse_sentence(raw)
    if said:
        return _sentence(_SUBSTRATE.sub("", f"BOSS couldn't run {said['engine']}: {said['message']}"))
    lines = [ln.strip() for ln in _SUBSTRATE.sub("", raw).splitlines() if ln.strip()]
    if not lines:
        return "BOSS didn't record why it stopped."
    first = lines[0]
    if first.startswith("Traceback"):
        line = lines[-1]
    elif len(lines) > 1 and re.match(r"^git\b", first, re.I):
        # A git error says its cause on a later `remote:`/`fatal:` line ("Cloning into '.'..."
        # first, "remote: Repository not found." after): the remote's own words win.
        found = [(m.group(1).lower(), m.group(2)) for m in map(_GIT_LINE.match, lines[1:]) if m]
        remote = [t for k, t in found if k == "remote"]
        line = "Git failed: " + (remote[-1] if remote else found[-1][1] if found else first)
    else:
        line = first
    while _EXC_PREFIX.match(line):
        line = _EXC_PREFIX.sub("", line, count=1).strip()
    low = line.lower()
    bare = _BARE_KEY.match(line)
    if bare:
        return f"It stopped unexpectedly at the {bare.group(1)} step."
    if _EXIT_STOPPED.search(line):
        return "It was stopped (timed out or cancelled)."
    if "permission denied" in low and ("git" in low or ".git/" in raw.lower()):
        return ("It couldn't save its changes: git was not allowed to write to the build's "
                "copy of the repo (permission denied).")
    if "permission denied" in low:
        return "It couldn't write a file it needed (permission denied)."
    cause = plain_cause(line) or ""
    if cause and not cause.startswith("it failed:"):
        return _sentence(cause)
    return _sentence(re.sub(r":\s*(?:fatal|error):\s*", ": ", line))


# ── what to do ──────────────────────────────────────────────────────────────────────────────

_LOGIN = re.compile(r"not logged in|/login|oauth|re-?authenticate|failed to authenticate|"
                    r"token (?:has )?expired|401\b|sign in", re.I)
_TOOLCHAIN = re.compile(r"boss toolchains enable (\w+)", re.I)
_CANNOT_BUILD = re.compile(r"asks for ([\w#+. ]+?), which BOSS cannot build", re.I)
_BUDGET = re.compile(r"spending cap|max[_ ]budget|budget[_ ]cap|error_max_budget_usd", re.I)


def action_for(project_id: str, *, source: str, run_id: Optional[str],
               failure_class: Any = None, reason_code: Any = None, error: Any = None,
               merged: bool = False, see_href: str = "") -> dict[str, Any]:
    """{retry, fix, retry_reason}. A failure a person can fix names the fix (`fix.label`,
    `fix.detail`, `fix.href` when the console has a place for it) FIRST, with Retry beside it
    for after the fix. No Retry at all when retrying cannot help — a merged change, a stack
    BOSS cannot build, a workflow's build (its step Retry) — and `retry_reason` says why."""
    from .project_experience.common import href
    from .harness_failures import parse_sentence

    raw = str(error or "")
    rc, fc = str(reason_code or ""), str(failure_class or "")

    def out(retry: bool, fix: Optional[dict[str, Any]] = None, why: Optional[str] = None):
        return {"retry": ({"run_id": run_id, "href": f"/v1/code-tasks/{run_id}/retry",
                           "method": "POST"} if retry and run_id else None),
                "fix": fix, "retry_reason": None if retry and run_id else why}

    if source == "workflow_run":
        return out(False, {"label": "Retry its failed step", "href": see_href,
                           "detail": "A workflow's failure is retried from its failed step, on "
                                     "the run's page."},
                   "Retried from the workflow's failed step, on its page")
    if merged:
        return out(False, None, "Its change is already merged: there is nothing to retry")
    if _BUDGET.search(raw) or rc in ("budget_cap", "budget_denied"):
        # The fix first, then Retry once it is done (the fix's own words say "then retry").
        return out(True, {"label": "Raise the cap in Govern",
                           "href": href(project_id, area="budget", tab="limits"),
                           "detail": "It hit its spending cap. Raise the cap, then retry."},
                   "It hit its spending cap: raise the cap first")
    if fc == "needs_human" or rc in ("harness_unavailable", "stack_unsupported",
                                     "build_services_unavailable", "no_test_files",
                                     "credential_plain_http", "credential_misdirected",
                                     "model_auth_rejected", "model_not_configured",
                                     "repo_access_denied") or re.search(
            r"\b[a-z0-9_]+_(?:base_url|api_key)\s+is\s+not\s+configured\b", raw, re.I):
        tool = _TOOLCHAIN.search(raw)
        said = parse_sentence(raw)
        cannot = _CANNOT_BUILD.search(raw)
        if cannot:
            # A STACK BOSS CANNOT BUILD AT ALL (Ruby, PHP): no toolchain turns it on — never
            # "Turn on Java" because the same error also names a toolchain that is off.
            return out(False, {"label": "See what it needs", "href": see_href,
                               "detail": f"BOSS can't build {cannot.group(1)}. Change the brief to a "
                                         "stack BOSS builds, then start again."},
                       f"BOSS can't build {cannot.group(1)}: retrying fails the same way")
        if tool:
            lang = tool.group(1).lower()
            fix = {"label": f"Turn on {lang.capitalize()}", "href": "",
                   "command": f"boss toolchains enable {lang}",
                   "detail": f"Run `boss toolchains enable {lang}` on the BOSS host, then start "
                             "it again."}
        elif rc == "harness_unavailable" or said or _LOGIN.search(raw):
            login = bool(_LOGIN.search(raw))
            fix = {"label": "Sign in again in Setup › Models" if login else "Fix it in Setup › Models",
                   "href": href(project_id, area="setup", focus="models"),
                   "detail": (_sentence(said["fix"]) if said else
                              "Reconnect the model's account in Setup, then retry.")}
        elif rc == "model_not_configured" or re.search(
                r"\b[a-z0-9_]+_(?:base_url|api_key)\s+is\s+not\s+configured\b", raw, re.I):
            return out(False, {"label": "Fix the model in Setup › Models",
                               "href": href(project_id, area="setup", focus="models"),
                               "detail": "This model's endpoint or key is not configured for "
                                         "builds. Connect or update it in Setup › Models."},
                       "This model's endpoint or key is not configured: retrying fails the same way")
        elif rc in ("credential_plain_http", "credential_misdirected", "model_auth_rejected"):
            # THE PERSON'S OWN MODEL SETUP (stranger test r6): a plain-http address, a key bound
            # to another host, a key the provider rejected. Said in the refusal's own words.
            plain = "plain http" in raw.lower()
            fix = {"label": "Use an https address in Setup › Models" if plain
                   else "Fix the model in Setup › Models",
                   "href": href(project_id, area="setup", focus="models"),
                   "detail": (failure_words(raw) if plain else
                              "The model you connected refused BOSS's request with its key. "
                              "Fix the key or address in Setup › Models, then retry.")}
        elif rc == "repo_access_denied":
            fix = {"label": "Check repository access", "href": href(project_id, area="setup"),
                   "detail": "The repository refused BOSS (not found, or no access for this "
                             "account). Connect it with access in Setup, then retry."}
        elif rc == "build_services_unavailable":
            fix = {"label": "Start build services", "href": href(project_id, area="infra"),
                   "detail": "BOSS can't reach its build services. Start them on the BOSS host, "
                             "then retry."}
        elif rc == "no_test_files":
            fix = {"label": "Make the brief testable", "href": see_href,
                   "detail": "Give the brief acceptance criteria a test can check, then retry."}
        else:
            fix = {"label": "See what it needs", "href": see_href,
                   "detail": "Only a person can fix this; the build's page says what."}
        # The fix first, and Retry beside it for after the fix (every fix ends "then retry").
        return out(True, fix)
    return out(True)


# ── the rule ────────────────────────────────────────────────────────────────────────────────

def _internal_clauses(params: dict[str, Any], alias: str) -> str:
    from .code_task_service import _internal_run_prefixes  # noqa: SLF001 — the one definition

    clauses = []
    for i, prefix in enumerate(_internal_run_prefixes()):
        clauses.append(f"lower(coalesce({alias}.title, '')) NOT LIKE :pre{i}")
        params[f"pre{i}"] = f"{prefix}%"
    return ("AND " + " AND ".join(clauses) + " ") if clauses else ""


def _aware(v: Any) -> Optional[datetime]:
    if v is None:
        return None
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def latest_failures(connection: Any, project_ids: Iterable[str], *,
                    now: Optional[datetime] = None) -> dict[str, dict[str, Any]]:
    """{project: its latest piece of work, when that failed} — batched (three queries for any
    number of projects), the one answer every surface reads."""
    from .project_experience.common import href, project_href
    from .project_experience.titles import short_title

    ids = list(dict.fromkeys(str(p) for p in project_ids))
    if not ids:
        return {}
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=REACH_DAYS)
    params: dict[str, Any] = {"ids": ids, "since": since}
    builds = [dict(r) for r in connection.execute(text(
        "SELECT c.id, c.project_id, c.status, c.error, c.title, c.created_at, c.updated_at, "
        "c.pull_request_url, c.finalize_ctx->>'workflow_run_id' AS wfr, "
        "COALESCE(c.finalize_ctx->>'root_run_id', c.id) AS root, "
        "c.finalize_ctx->>'failure_class' AS failure_class, "
        "c.finalize_ctx->>'reason_code' AS reason_code, "
        "c.finalize_ctx->'merge'->>'sha' AS merged_sha, "
        "c.finalize_ctx->>'deferred_from_run' AS deferred_from, "
        "(SELECT d.status FROM decisions d WHERE d.id = c.decision_id) AS decision_status, "
        "c.finalize_ctx->'merge'->>'at' AS merged_at, "
        "(c.finalize_ctx ? 'change_request') AS asks_change, "
        "c.finalize_ctx->>'auto_infra_retry' AS auto_retries, "
        "c.finalize_ctx->'restart_recovery' AS restart_recovery, "
        "c.finalize_ctx->>'auto_retry_needs_ok' AS needs_ok, "
        "c.finalize_ctx->>'is_followup' AS is_followup "
        "FROM code_task_runs c WHERE c.project_id = ANY(:ids) AND c.created_at >= :since "
        + _internal_clauses(params, "c")), params).mappings().all()]
    runs = [dict(r) for r in connection.execute(text(
        "SELECT r.id, r.project_id, r.status, COALESCE(r.error, r.blocked_reason) AS error, "
        "COALESCE(r.input->>'title', d.name) AS title, r.created_at, r.updated_at, "
        "r.workflow_version_id FROM workflow_runs r "
        "JOIN workflow_definitions d ON d.id = r.workflow_definition_id "
        "WHERE r.project_id = ANY(:ids) AND d.namespace = 'product' "
        "AND COALESCE(r.mode, 'normal') <> 'test' AND r.parent_run_id IS NULL "
        "AND (r.created_at >= :since OR r.updated_at >= :since)"),
        {"ids": ids, "since": since}).mappings().all()]
    # Pieces of work: a workflow run with its own builds; a standalone lineage by its root.
    work: dict[tuple[str, str], dict[str, Any]] = {}
    for r in runs:
        work[(str(r["project_id"]), f"run:{r['id']}")] = {"run": r, "members": []}
    for b in builds:
        pid = str(b["project_id"])
        key = (pid, f"run:{b['wfr']}") if b["wfr"] else (pid, f"build:{b['root']}")
        if b["wfr"] and key not in work:
            continue                         # its run is outside the window or not product
        work.setdefault(key, {"run": None, "members": []})["members"].append(b)
    newest: dict[str, tuple[datetime, str, dict[str, Any]]] = {}
    for (pid, key), w in work.items():
        times = [_aware(m["created_at"]) for m in w["members"]]
        if w["run"] is not None:
            times.append(_aware(w["run"]["created_at"]))
        at = max(t for t in times if t is not None)
        if pid not in newest or at > newest[pid][0]:
            newest[pid] = (at, key, w)
    failed_runs = [str(w["run"]["id"]) for _, _, w in newest.values()
                   if w["run"] is not None and str(w["run"]["status"]) in _FAILED_RUN]
    shipping = _failed_at_ship(connection, failed_runs)
    out: dict[str, dict[str, Any]] = {}
    for pid, (_at, key, w) in newest.items():
        members = sorted(w["members"], key=lambda m: _aware(m["created_at"]))
        latest = members[-1] if members else None
        run = w["run"]
        if run is not None:
            if str(run["status"]) not in _FAILED_RUN or (
                    latest is not None and str(latest["status"]) in _LIVE_BUILD):
                continue
            rid = str(run["id"])
            own = latest if latest is not None and str(latest["status"]) == "failed" else None
            error = (own or {}).get("error") or run["error"]
            word = "Deploy failed" if rid in shipping else "Build failed"
            see = href(pid, area="delivery_workflows", wfr=rid)
            failure = {"id": rid, "source": "workflow_run", "build_id": str(own["id"]) if own else None,
                       "label": short_title(run["title"] or "Workflow"), "word": word,
                       "follow_up": False, "at": _iso(run["updated_at"]),
                       "work_href": project_href(pid, tab="work", drawer=f"run:{rid}"),
                       "see_href": see, "reason": failure_words(error)}
            failure.update(action_for(pid, source="workflow_run", run_id=None, error=error,
                                      see_href=see))
        else:
            if latest is None or str(latest["status"]) != "failed":
                continue
            # Not the project's failure: a follow-up of a build still ready on its merge card
            # (the light lane), or an attempt the lineage moved past (a later merge).
            if _followups_only(latest, members[:-1]) or _moved_on(members, latest):
                continue
            root = key.split(":", 1)[1]
            waits = waits_on(connection, pid, latest)
            if waits is not None and waits.get("resolved"):
                continue                     # its stated cause is gone: nothing to say
            if waits is not None:
                # A REFUSAL THAT WAITS ON A PERSON'S MERGE is that merge, not a red failure.
                out[pid] = {"id": root, "source": "code_task", "build_id": str(latest["id"]),
                            "label": short_title(_root_title(members, root) or "Build"),
                            "word": waits["label"], "waits_on": waits, "follow_up": False,
                            "at": _iso(latest["updated_at"]), "reason": waits["detail"],
                            "work_href": project_href(pid, tab="work", drawer=f"build:{root}"),
                            "see_href": href(pid, area="code_tasks", build=latest["id"]),
                            "retry": None, "fix": {"label": waits["label"], "href": waits["href"],
                                                   "detail": waits["detail"]},
                            "retry_reason": "Merge it first; then this build isn't needed"}
                continue
            follow_up = len(members) > 1 and any(
                m["pull_request_url"] or m["merged_sha"] for m in members[:-1])
            see = href(pid, area="code_tasks", build=latest["id"])
            failure = {"id": root, "source": "code_task", "build_id": str(latest["id"]),
                       "label": short_title(_root_title(members, root) or "Build"),
                       "word": "Follow-up failed" if follow_up else "Build failed",
                       "follow_up": follow_up, "at": _iso(latest["updated_at"]),
                       "work_href": project_href(pid, tab="work", drawer=f"build:{root}"),
                       "see_href": see, "reason": failure_words(latest["error"])}
            # HOW MANY AUTOMATIC RETRIES, FROM THE LINEAGE: every attempt BOSS started itself.
            auto_n = sum(1 for m in members if str(m.get("auto_retries") or "").strip()
                         not in ("", "0", "false"))
            doing = boss_did(latest["failure_class"], auto_n)
            if doing:
                failure["doing"] = doing
            ok = needs_ok_words(latest["failure_class"], latest.get("needs_ok"),
                                follow_up=follow_up or bool(latest.get("is_followup")))
            if ok:
                # BOSS would retry it but no grant the person gave covers another attempt: one
                # OK (Retry, which asks consent) and it runs. Never a red failure to fix.
                failure.update({"word": "Needs your OK", "reason": ok, "tone": "info"})
                failure.pop("doing", None)
            stopped = _stopped_by_boss(latest.get("restart_recovery"))
            if stopped:
                # A BOSS bug, fixed since, stopped it: said as that, never a red "Build failed".
                failure.update({"word": "Stopped by a BOSS bug", "reason": stopped,
                                "tone": "info", "boss_bug": True})
                failure.pop("doing", None)
            failure.update(action_for(
                pid, source="code_task", run_id=str(latest["id"]),
                failure_class=latest["failure_class"], reason_code=latest["reason_code"],
                error=latest["error"], merged=bool(latest["merged_sha"]), see_href=see))
        out[pid] = failure
    return with_owners(connection, out)


def with_owners(connection: Any, failures: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """NEVER A BARE "FAILED" (failure_owner): each failure says who owns it now. BOSS fixing it
    reads "BOSS is fixing it" with the attempt ("attempt 1 of 2 — …"), in the calm tone; a
    person's action item reads "Needs you" with what to do and why BOSS could not, linked to
    that card. The owner rides on the entry as `owner` for every surface that reads it."""
    from . import failure_owner
    from .project_experience.common import href

    by_surface: dict[str, list[str]] = {}
    for f in failures.values():
        if is_failure(f):
            surface = "workflow_run" if f.get("source") == "workflow_run" else "code_task"
            by_surface.setdefault(surface, []).append(str(f["id"]))
    owned = {(s, k): o for s, keys in by_surface.items()
             for k, o in failure_owner.owners(connection, s, keys).items()}
    for pid, f in failures.items():
        if not is_failure(f):
            continue
        surface = "workflow_run" if f.get("source") == "workflow_run" else "code_task"
        owner = owned.get((surface, str(f["id"])))
        if owner is None or owner["state"] == failure_owner.RESOLVED:
            continue
        f["owner"] = owner
        if owner["state"] == failure_owner.FIXING:
            f.update({"word": "BOSS is fixing it", "doing": owner["words"], "tone": "info"})
        else:
            f.update({"word": "Needs you", "reason": " ".join(
                p for p in (owner["action"], f"Why BOSS could not: {owner['reason']}"
                            if owner["reason"] else "") if p).strip() or f.get("reason")})
            f.pop("doing", None)
            if owner.get("decision_id"):
                f["fix"] = {"label": "Open the action item",
                            "href": href(pid, area="decisions", decision=owner["decision_id"]),
                            "detail": owner["action"], "decision_id": owner["decision_id"]}
    return failures


def _followups_only(latest: Mapping[str, Any], earlier: list[Mapping[str, Any]]) -> bool:
    """A FOLLOW-UP-ONLY OUTCOME IS NOT A FAILURE (quality_bar.merge_followups, operator
    2026-11-18). The failed build is the follow-up BOSS started on its own for a ready build's
    follow-ups (`deferred_from_run`), and the build it follows still waits, ready, on its merge
    card: the project reads "ready to merge", never "Follow-up failed" with a Retry for a
    person. The follow-up's own page keeps its failure; agents pick it up from there."""
    from .decision_actions import OPEN_STATUSES

    parent = str(latest.get("deferred_from") or "")
    if not parent:
        return False
    return any(str(m["id"]) == parent and m.get("pull_request_url") and not m.get("merged_sha")
               and str(m.get("decision_status") or "") in OPEN_STATUSES for m in earlier)


def latest_failure(connection: Any, project_id: str, *, now: Optional[datetime] = None
                   ) -> Optional[dict[str, Any]]:
    return latest_failures(connection, [project_id], now=now).get(str(project_id))


def _root_title(members: list[Mapping[str, Any]], root: str) -> str:
    from .project_experience.work import clean_title

    first = next((m for m in members if str(m["id"]) == root), members[0])
    return clean_title(first.get("title") or "")


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else None)


def _failed_at_ship(connection: Any, run_ids: list[str]) -> set[str]:
    """The failed workflow runs whose failed step ships (deploy, smoke test…): "Deploy failed"."""
    from .project_experience.common import as_json
    from .project_experience.work import _station_of  # noqa: SLF001 — the one station map

    if not run_ids:
        return set()
    rows = connection.execute(text(
        "SELECT s.workflow_run_id, s.step_id, s.step_kind, v.spec FROM workflow_step_runs s "
        "JOIN workflow_runs r ON r.id = s.workflow_run_id "
        "JOIN workflow_versions v ON v.id = r.workflow_version_id "
        "WHERE s.workflow_run_id = ANY(:ids) AND s.status = ANY(:failed)"),
        {"ids": run_ids, "failed": list(_FAILED_RUN)}).mappings().all()
    out: set[str] = set()
    for r in rows:
        spec = as_json(r["spec"], {}) or {}
        step = next((s for s in spec.get("steps") or [] if isinstance(s, dict)
                     and str(s.get("id")) == str(r["step_id"])), {})
        binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
        cap = str(binding.get("capability") or step.get("capability") or "")
        if _station_of(cap, str(r["step_kind"] or "")) == "ship":
            out.add(str(r["workflow_run_id"]))
    return out


def needs_ok_words(failure_class: Any, needs_ok: Any, *, follow_up: bool = False
                   ) -> Optional[str]:
    """A platform failure BOSS would retry but may not by itself (no grant the person gave
    covers another attempt), in words — an OK to give, not a problem to fix."""
    if str(failure_class or "") != "infra" or str(needs_ok or "").lower() not in ("true", "1"):
        return None
    what = "this follow-up" if follow_up else "this build"
    return (f"BOSS needs your OK to retry {what}: it stopped on a platform error on BOSS's side, "
            "and no grant you gave covers another attempt. Press Retry to allow it, or give "
            "its agent a standing grant so BOSS retries by itself next time.")


def _stopped_by_boss(value: Any) -> Optional[str]:
    from .build_restart_recovery import words_for
    from .project_experience.common import as_json

    said = words_for({"restart_recovery": as_json(value, None)}) if value else None
    return said["words"] if said and said["state"] == "cannot_resume" else None


def boss_did(failure_class: Any, auto_retries: Any) -> Optional[str]:
    """What BOSS already did about a build that died of BOSS's own platform error, in one
    sentence — said in place of a question: "BOSS retried it twice; …". None when BOSS made no
    automatic retry (a needs_human failure, or one whose retries are off)."""
    if str(failure_class or "") != "infra":
        return None
    raw = str(auto_retries or "").strip().lower()
    n = 1 if raw == "true" else int(raw) if raw.isdigit() else 0
    if n <= 0:
        return None
    return spent_words(n)


def needs_card(failure: Mapping[str, Any]) -> dict[str, Any]:
    """The failure as a Needs-you card (kind `failed`). It carries no generic `action`: its
    buttons are its own (Retry is a POST, never a link), so a generic renderer — the needs
    drawer — draws the same Retry / fix / See what happened, never an API URL as a link."""
    return {"id": f"failed:{failure['id']}", "kind": "failed",
            "label": f"{failure['word']}: {failure['label']}", "word": failure["word"],
            "detail": (f"{failure['doing']} {failure['reason']}".strip() if failure.get("doing")
                       else failure["reason"]),
            "doing": failure.get("doing"), "mini": [], "work_id": failure["id"],
            "created_at": failure.get("at"), "retry": failure.get("retry"),
            "fix": failure.get("fix"), "retry_reason": failure.get("retry_reason"),
            "see_href": failure.get("see_href"), "action": None,
            **({"tone": failure["tone"]} if failure.get("tone") else {})}


def is_failure(entry: Optional[Mapping[str, Any]]) -> bool:
    """A real failure (red), as opposed to work waiting on a person's merge (`waits_on`)."""
    return bool(entry) and not entry.get("waits_on")


def _when(v: Any) -> Optional[datetime]:
    if isinstance(v, str):
        try:
            return _aware(datetime.fromisoformat(v.replace("Z", "+00:00")))
        except ValueError:
            return None
    return _aware(v)


def _moved_on(members: list[Mapping[str, Any]], latest: Mapping[str, Any]) -> bool:
    """THE LINEAGE MOVED ON: another attempt's change was merged (its `merge.at`) after this
    attempt failed — a retry or automatic follow-up that failed before change #1 was merged is
    an earlier attempt. A follow-up that carried its OWN change request ("add X") is not: X
    never landed, so its failure stays visible (review #221)."""
    if latest.get("asks_change"):
        return False
    failed_at = _when(latest.get("updated_at"))
    for m in members:
        if m is latest or not m.get("merged_sha"):
            continue
        merged_at = _when(m.get("merged_at")) or _when(m.get("updated_at"))
        if failed_at is None or (merged_at is not None and merged_at > failed_at):
            return True
    return False


#: A build refused because the app's code waits, unmerged, on another build's branch.
_WAITS_BRANCH = re.compile(r"boss/code/(crun_[0-9a-f]+)\b[^.]*\bnot merged", re.I)


def waits_on(connection: Any, project_id: str, run: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """What a refused build waits for, when its error names it — "its code is on
    boss/code/crun_d8b…, not merged into main … Merge that change" (app_missing): that build's
    change. `resolved` once that change is merged (the refusal's cause is gone); otherwise the
    merge to do, linked to its merge decision. None for any other failure."""
    from .project_experience.common import href
    from .repo_links import change_ref

    found = _WAITS_BRANCH.search(str(run.get("error") or ""))
    if not found:
        return None
    other = connection.execute(text(
        "SELECT id, pull_request_url, pull_request_number, decision_id, "
        "finalize_ctx->'merge'->>'sha' AS merged_sha FROM code_task_runs "
        "WHERE project_id = :p AND id = :i"), {"p": project_id, "i": found.group(1)}
    ).mappings().first()
    if other is None:
        return None
    ref = change_ref(other["pull_request_url"], other["pull_request_number"], lower=True)
    if other["merged_sha"]:
        return {"resolved": True, "run_id": other["id"], "detail": f"Not needed: {ref} was merged."}
    link = (href(project_id, area="decisions", decision=other["decision_id"])
            if other["decision_id"] else href(project_id, area="code_tasks", build=other["id"]))
    return {"resolved": False, "run_id": other["id"], "decision_id": other["decision_id"],
            "label": f"Merge {ref} first", "href": link,
            "detail": f"This build waits on {ref}: the app's code is there, not merged yet. "
                      "Merge it, then build onto it."}
