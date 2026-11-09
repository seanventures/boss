"""WHAT AN ANSWER CARRIES BESIDES ITS WORDS (screen 09, BOSSAPP-913): a typed `proposal` (the
"New work" card: title, text, cost and time, the plan it would run, Start / Edit / Not now) and
`citations` (PR / run / file / doc / issue / place chips that open what the answer named).

Made on THE ONE AGENT TURN (agent_service.chat), after the router answered, for every surface
— the Ask BOSS dock, the project's captain drawer and Metatron read the same two fields. There
is no second decision here and no model call: both are derived from what the turn already
holds, so they behave the same whichever engine answered.

  * `proposal` comes from the turn's own offer to start work (a `create_code_task` /
    `start_workflow` button the agent proposed), else from BOSS's decision that the message
    asked for new work (`new_code_change`) — the person's own words then. Its estimate is the
    start door's (start_project.how_to_start: the workflow's minutes to the PR, and the cost
    range of this project's past runs of it); its plan strip is that workflow's stations. Start
    sends THE start door (POST /v1/projects/{id}/work) with `source_ref` naming the message.
  * `citations` are the references the reply names, resolved against the project's records:
    "PR #3" → that work's drawer (or the PR on GitHub), "Run #44" → the run's drawer,
    `sla.py` → the file in the repository (only a path the code graph knows or the turn read),
    a PRD / repo doc by its name → its drawer, "issue #12" → the issue, "Staging" → the
    environment, "demo logins" → Settings › Demo logins. Ordered as the reply names them, at
    most MAX_CITATIONS, never a guess: an unresolvable reference is left as text.
"""
from __future__ import annotations

import logging
import re
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

log = logging.getLogger(__name__)

MAX_CITATIONS = 6
CITATION_KINDS = ("pr", "run", "file", "doc", "issue", "env", "place")
#: The offers that START work: their arguments are the work's words.
_WORK_TOOLS = ("create_code_task", "start_workflow")
_FILE_EXT = ("py|ts|tsx|js|jsx|mjs|cjs|go|rb|java|kt|rs|sql|ya?ml|json|md|css|scss|html|sh|toml"
             "|cfg|ini|php|cs|swift|vue|svelte")
_RE_PR = re.compile(r"\b(?:PR|pull request)\s*#\s?(\d{1,6})\b", re.I)
_RE_PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_RE_RUN = re.compile(r"\bRun\s*#\s?(\d{1,6})\b")
_RE_RUN_ID = re.compile(r"\b(wfr_[a-z0-9]{6,})\b")
_RE_ISSUE = re.compile(r"\b(?:issue|bug|ticket)\s*#\s?(\d{1,6})\b", re.I)
_RE_FILE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w-][\w.-]*\.(?:" + _FILE_EXT + r"))(?![\w/-])")
_RE_ENV = re.compile(r"\b(staging|production|preview)\b", re.I)
_RE_LOGINS = re.compile(r"\bdemo log-?ins?\b", re.I)


# ── citations ────────────────────────────────────────────────────────────────────────────────


def _cite(kind: str, ref: str, label: str, href: Optional[str], *, at: int,
          external: bool = False, title: str = "") -> dict[str, Any]:
    return {"kind": kind, "ref": ref, "label": label, "href": href, "external": external,
            "title": title or label, "_at": at}


def _repo(connection: Connection, project_id: str) -> Optional[dict[str, Any]]:
    from .project_experience.common import repos

    found = repos(connection, project_id)
    return found[0] if found else None


def _known_files(connection: Connection, project_id: str, names: list[str]) -> dict[str, str]:
    """name-or-path → the path the code graph knows (the shortest match for a bare name)."""
    if not names:
        return {}
    out: dict[str, str] = {}
    try:
        with connection.begin_nested():
            rows = connection.execute(text(
                "SELECT DISTINCT path FROM kg_nodes WHERE project_id = :p AND kind = 'file' "
                "AND valid_to IS NULL AND (path = ANY(:n) OR path LIKE ANY(:like)) LIMIT 200"),
                {"p": project_id, "n": names, "like": [f"%/{n}" for n in names]}).scalars().all()
    except Exception:  # noqa: BLE001 — no graph yet: only the files the turn read are cited
        rows = []
    for name in names:
        hits = sorted((str(p) for p in rows if str(p) == name or str(p).endswith("/" + name)),
                      key=len)
        if hits:
            out[name] = hits[0]
    return out


def _read_paths(tool_calls: Iterable[Mapping[str, Any]]) -> set[str]:
    """Paths the turn itself opened (read_code / search_code results' `file` / `path` args)."""
    out: set[str] = set()
    for call in tool_calls or []:
        args = call.get("args") or call.get("arguments") or {}
        if not isinstance(args, Mapping):
            continue
        for key in ("file", "path"):
            value = str(args.get(key) or "").strip().lstrip("/")
            if value and "." in PurePosixPath(value).name:
                out.add(value)
    return out


def citations(connection: Connection, project_id: str, reply: str,
              tool_calls: Iterable[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    """The records the reply names, resolved; [] when it names none (cheap: no reads then)."""
    from .project_experience.common import project_href

    said = str(reply or "")
    if not said.strip():
        return []
    prs = [(m.start(), int(m.group(1))) for m in _RE_PR.finditer(said)]
    pr_urls = [(m.start(), m.group(1), int(m.group(2))) for m in _RE_PR_URL.finditer(said)]
    runs = [(m.start(), int(m.group(1))) for m in _RE_RUN.finditer(said)]
    run_ids = [(m.start(), m.group(1)) for m in _RE_RUN_ID.finditer(said)]
    issues = [(m.start(), int(m.group(1))) for m in _RE_ISSUE.finditer(said)]
    read = _read_paths(tool_calls)
    files = [(m.start(), m.group(1)) for m in _RE_FILE.finditer(said)
             if not m.group(1).lower().startswith(("http", "www."))
             and "github.com" not in m.group(1)]
    envs = [(m.start(), m.group(1).lower()) for m in _RE_ENV.finditer(said)]
    logins = [m.start() for m in _RE_LOGINS.finditer(said)]
    out: list[dict[str, Any]] = []

    work: list[dict[str, Any]] = []
    if prs or pr_urls or runs or run_ids:
        try:
            from .project_experience import work as work_service

            with connection.begin_nested():
                work = work_service.everything(connection, project_id)
        except Exception:  # noqa: BLE001 — a citation is a help, never a failed answer
            log.exception("turn_cards: work read failed")
    by_pr = {int((w["links"].get("pr") or {}).get("number") or 0): w for w in work
             if (w.get("links") or {}).get("pr")}
    by_run = {int(w.get("run_number") or 0): w for w in work if w.get("run_number")}
    by_id = {str(w["id"]): w for w in work}
    repo = _repo(connection, project_id) if (prs or issues or files or pr_urls) else None
    full = (repo or {}).get("full_name")
    from .repo_links import is_local_repo

    # A repository BOSS keeps on this machine (BOSSAPP-918) has no GitHub page: a PR is its
    # change set in BOSS, and issues / file links on GitHub do not exist.
    local = bool(full) and is_local_repo(full)

    for at, number in prs:
        w = by_pr.get(number)
        if w:
            out.append(_cite("pr", f"#{number}", f"PR #{number}", w["href"], at=at,
                             title=f"PR #{number} · {w.get('label') or ''}".rstrip(" ·")))
        elif full and local:
            out.append(_cite("pr", f"#{number}", f"Change #{number}",
                             f"/changes/{project_id}/{number}", at=at))
        elif full:
            out.append(_cite("pr", f"#{number}", f"PR #{number}",
                             f"https://github.com/{full}/pull/{number}", at=at, external=True))
    for at, repo_name, number in pr_urls:
        w = by_pr.get(number) if full and repo_name.lower() == full.lower() else None
        out.append(_cite("pr", f"#{number}", f"PR #{number}",
                         w["href"] if w else f"https://github.com/{repo_name}/pull/{number}",
                         at=at, external=not w))
    for at, number in runs:
        w = by_run.get(number)
        if w:
            out.append(_cite("run", f"#{number}", f"Run #{number}", w["href"], at=at,
                             title=f"Run #{number} · {w.get('label') or ''}".rstrip(" ·")))
    for at, rid in run_ids:
        w = by_id.get(rid)
        if w:
            label = f"Run #{w['run_number']}" if w.get("run_number") else (w.get("label") or "Run")
            out.append(_cite("run", rid, label, w["href"], at=at, title=w.get("label") or label))
    pr_numbers = {n for _, n in prs}
    for at, number in issues:
        if number in pr_numbers or not full or local:
            continue
        name = _issue_title(connection, project_id, number)
        out.append(_cite("issue", f"#{number}", f"#{number} {name}".strip() if name else f"#{number}",
                         f"https://github.com/{full}/issues/{number}", at=at, external=True,
                         title=f"Issue #{number}" + (f" · {name}" if name else "")))
    if files and full and not local:
        names = list(dict.fromkeys(f for _, f in files))
        known = _known_files(connection, project_id, names)
        branch = (repo or {}).get("branch") or "HEAD"
        for at, name in files:
            path = known.get(name) or next((p for p in read if p == name or p.endswith("/" + name)),
                                           None)
            if not path:
                continue
            out.append(_cite("file", path, PurePosixPath(path).name,
                             f"https://github.com/{full}/blob/{branch}/{path}", at=at,
                             external=True, title=path))
    if envs:
        targets = _targets(connection, project_id)
        for at, env in envs:
            t = targets.get(env)
            if t:
                out.append(_cite("env", t, env.title(), project_href(project_id, tab="deployments",
                                                                      drawer=f"env:{t}"), at=at,
                                 title=f"{env.title()} · Deployments"))
    for at in logins[:1]:
        out.append(_cite("place", "demo_logins", "Demo logins",
                         project_href(project_id, tab="settings", drawer="section:demo_logins"),
                         at=at, title="Settings › Demo logins"))
    out.extend(_docs(connection, project_id, said))
    seen: set[tuple[str, str]] = set()
    ordered = []
    for c in sorted(out, key=lambda c: c["_at"]):
        key = (c["kind"], c["ref"])
        if key in seen or not c["href"]:
            continue
        seen.add(key)
        ordered.append({k: v for k, v in c.items() if k != "_at"})
    return ordered[:MAX_CITATIONS]


def _targets(connection: Connection, project_id: str) -> dict[str, str]:
    """environment word → its active target id (staging / production / preview)."""
    out: dict[str, str] = {}
    try:
        with connection.begin_nested():
            for r in connection.execute(text(
                    "SELECT id, lower(environment) AS env FROM deployment_targets "
                    "WHERE project_id = :p AND status = 'active' ORDER BY created_at"),
                    {"p": project_id}).mappings():
                env = {"stage": "staging", "prod": "production"}.get(r["env"], r["env"])
                out.setdefault(env, str(r["id"]))
    except Exception:  # noqa: BLE001
        log.exception("turn_cards: targets read failed")
    return out


def _issue_title(connection: Connection, project_id: str, number: int) -> str:
    """An issue's short name from the onboarding backlog (it lists the repo's open issues)."""
    try:
        from .project_experience import onboarding

        with connection.begin_nested():
            ob = onboarding.view(connection, project_id)
        for item in ob.get("backlog") or []:
            url = str(item.get("external_url") or "")
            if url.rstrip("/").endswith(f"/issues/{number}"):
                from .project_experience.titles import shorten

                said = re.sub(r"^\s*(?:issue\s*)?#\d+\s*[:·—-]?\s*", "",
                              str(item.get("label") or item.get("text") or ""), flags=re.I)
                return shorten(said, 4)
    except Exception:  # noqa: BLE001
        log.exception("turn_cards: backlog read failed")
    return ""


def _docs(connection: Connection, project_id: str, said: str) -> list[dict[str, Any]]:
    """PRDs and repo docs the reply names by their own name ("the SLA timers PRD", "ADR-001")."""
    if not re.search(r"\b(PRD|ADR-?\d+|RFC-?\d+|README|runbook)\b", said, re.I):
        return []
    try:
        from .project_experience import knowledge
        from .ambient import dossier as dossier_service
        from .project_experience.common import as_json

        with connection.begin_nested():
            row = dossier_service.get(connection, project_id)[0] or {}
            facts_docs = (as_json(row.get("facts"), {}) or {}).get("docs") or {}
            found = knowledge.docs(connection, project_id, facts_docs, None)
    except Exception:  # noqa: BLE001
        log.exception("turn_cards: docs read failed")
        return []
    low = said.lower()
    out = []
    for d in found:
        label = str(d.get("label") or "")
        name = label.split(" · ", 1)[-1].strip().lower()
        head = label.split(" · ", 1)[0].strip().lower()
        hit = -1
        if d.get("kind") in ("adr", "runbook") and head and head != name:
            hit = low.find(head)                      # "ADR-001"
        if hit < 0 and len(name) >= 4:
            hit = low.find(name)                      # "SLA timers"
        if hit < 0 and d.get("kind") == "readme":
            hit = low.find("readme")
        if hit >= 0:
            out.append(_cite("doc", str(d.get("href")), label, d.get("href"), at=hit,
                             title=label))
    return out


# ── the proposal ─────────────────────────────────────────────────────────────────────────────


def _offer_words(offer: Mapping[str, Any]) -> tuple[str, str]:
    """(title, text) of an offer that starts work."""
    args = offer.get("args") if isinstance(offer.get("args"), Mapping) else {}
    inner = args.get("input") if isinstance(args.get("input"), Mapping) else {}
    said = str(args.get("instructions") or args.get("directive") or inner.get("directive")
               or args.get("brief") or args.get("text") or "").strip()
    title = str(args.get("title") or inner.get("title") or "").strip()
    return title, said


def _kind_of(said: str, offer: Optional[Mapping[str, Any]]) -> str:
    args = (offer or {}).get("args") if isinstance((offer or {}).get("args"), Mapping) else {}
    given = str((args or {}).get("kind") or (args or {}).get("work_kind") or "").lower()
    if given in ("feature", "bug", "ticket"):
        return given
    from .project_experience.work import kind_of

    key = str((args or {}).get("workflow_key") or "")
    return kind_of(key) if key and kind_of(key) in ("bug",) else "feature"


def plan_steps(connection: Connection, project_id: str, kind: str = "feature") -> dict[str, Any]:
    """The start door's estimate and the plan strip of the workflow it would run — Helm's plan
    (project_experience.new_work.plan, the Work tab composer's) when this engine has it, so the
    card and the composer say the same minutes, dollars and steps."""
    try:
        from .project_experience import new_work  # type: ignore[attr-defined]
    except ImportError:
        new_work = None
    if new_work is not None and hasattr(new_work, "plan"):
        try:
            with connection.begin_nested():
                got = new_work.plan(connection, project_id, kind)
            est = got.get("estimate") or None
            estimate = None
            if est and (est.get("usd") or est.get("minutes")):
                runs = int(est.get("usd_runs") or est.get("minutes_runs") or 0)
                name = (got.get("workflow") or {}).get("name") or "workflow"
                estimate = {"usd": est.get("usd"), "minutes": est.get("minutes"),
                            "runs": runs, "metered": bool(est.get("metered")),
                            "basis": (f"from {runs} past {name} run{'s' if runs != 1 else ''} here"
                                      if est.get("usd_basis") == "history"
                                      or est.get("minutes_basis") == "history"
                                      else f"the {name}'s typical steps")}
            steps = [{"key": n.get("key"), "label": n.get("label"), "type": n.get("type"),
                      "role_key": (n.get("agent") or {}).get("role_key"),
                      "agent": n.get("agent")} for n in got.get("nodes") or []]
            if steps:
                return {"estimate": estimate, "steps": steps,
                        "workflow": (got.get("workflow") or {}).get("name")}
        except Exception:  # noqa: BLE001 — the start door's own estimate below
            log.exception("turn_cards: Helm's plan failed")
    from . import start_project

    try:
        with connection.begin_nested():
            page = start_project.view(connection, project_id)
    except Exception:  # noqa: BLE001 — no estimate is better than a failed answer
        log.exception("turn_cards: start view failed")
        return {"estimate": None, "steps": [], "workflow": None}
    start = page.get("start") or {}
    cost = start.get("cost") or {}
    usd = None
    if cost.get("runs"):
        low, high = float(cost.get("low_usd") or 0), float(cost.get("high_usd") or 0)
        usd = round((low + high) / 2, 2) if high else round(low, 2)
    minutes = start.get("minutes_to_pr") or start.get("minutes")
    estimate = None
    if usd or minutes:
        estimate = {"usd": usd, "minutes": int(minutes) if minutes else None,
                    "minutes_low": start.get("minutes_low"),
                    "minutes_high": start.get("minutes_high"),
                    "runs": int(cost.get("runs") or 0),
                    "metered": bool(cost.get("metered")),
                    "basis": (f"from {cost['runs']} past {start.get('workflow_name') or 'run'} "
                              f"run{'s' if cost['runs'] != 1 else ''} here"
                              if cost.get("runs") else
                              f"the {start.get('workflow_name') or 'workflow'}'s typical steps")}
    return {"estimate": estimate, "steps": _steps_of(connection, project_id, start),
            "workflow": start.get("workflow_name")}


def _steps_of(connection: Connection, project_id: str, start: Mapping[str, Any]
              ) -> list[dict[str, Any]]:
    """The plan strip: Plan (the planner's face) → You approve (a person's gate) → Build → PR,
    from the stations the start workflow really has."""
    from . import start_project
    from .project_experience.work import stages

    key = start.get("resolved_key") or start.get("workflow_key")
    chosen = None
    try:
        with connection.begin_nested():
            if start.get("work_class"):
                chosen = start_project._mapped(connection, project_id, str(start["work_class"]))
            if not chosen and key:
                chosen = start_project._installed(connection, project_id, str(key))
    except Exception:  # noqa: BLE001
        chosen = None
    spec = (chosen or {}).get("spec") or {}
    if isinstance(spec, str):
        import json

        spec = json.loads(spec or "{}")
    strip = {s["key"]: s["state"] for s in stages(spec, {}, has_pr=False, finished=False)} \
        if isinstance(spec, Mapping) and spec.get("steps") else {}
    gate = any(isinstance(s, Mapping) and (s.get("kind") == "decision" or (
        isinstance(s.get("binding"), Mapping) and s["binding"].get("capability")
        in ("spec_review", "architecture", "ux_review"))) for s in (spec.get("steps") or [])) \
        if isinstance(spec, Mapping) else False
    out: list[dict[str, Any]] = []
    if strip.get("plan", "skipped") != "skipped" or not strip:
        out.append({"key": "plan", "label": "Plan", "type": "agent",
                    "role_key": "product_manager"})
    if gate or not strip:
        out.append({"key": "approve", "label": "You approve", "type": "you"})
    if strip.get("build", "skipped") != "skipped" or not strip:
        out.append({"key": "build", "label": "Build", "icon": "hammer"})
    out.append({"key": "pr", "label": "PR", "icon": "pr"})
    return out


def proposal(connection: Connection, project_id: str, *, offers: Iterable[Mapping[str, Any]],
             decision: Optional[Mapping[str, Any]], message: str,
             message_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The "New work" card, or None when the turn did not offer to start work."""
    from .project_experience.titles import shorten

    offer = next((o for o in offers or [] if str(o.get("tool") or "") in _WORK_TOOLS), None)
    title, said = _offer_words(offer) if offer else ("", "")
    if not offer:
        if str((decision or {}).get("action") or "") != "new_code_change":
            return None
        said = str(message or "").strip()
    said = said or str((offer or {}).get("label") or "").strip()
    if not said:
        return None
    short = shorten(title or said, 6) or shorten(said, 6) or "New work"
    kind = _kind_of(said, offer)
    plan = plan_steps(connection, project_id, kind)
    body = {"kind": kind, "text": said[:8000], "title": short}
    if message_id:
        body["source_ref"] = f"chat:{message_id}"
    return {"kind": "new_work", "work_kind": kind, "title": short, "text": said[:8000],
            "estimate": plan["estimate"], "steps": plan["steps"], "workflow": plan["workflow"],
            "offer_id": str((offer or {}).get("id") or "") or None,
            "state": "open",
            "start": {"href": f"/v1/projects/{project_id}/work", "method": "POST", "body": body},
            "edit_href": _edit_href(project_id, kind, said)}


def _edit_href(project_id: str, kind: str, said: str) -> str:
    """Work tab's composer, prefilled (`compose=` + `kind=`)."""
    from .project_experience.common import href

    return href(project_id, area="project", tab="work", compose="1", compose_kind=kind,
                compose_text=said[:1500])


def for_turn(connection: Connection, project_id: str, *, reply: str,
             tool_calls: Iterable[Mapping[str, Any]], offers: Iterable[Mapping[str, Any]],
             decision: Optional[Mapping[str, Any]], message: str,
             message_id: Optional[str] = None) -> dict[str, Any]:
    """Both fields for one answer; a failure in either is logged and that field is empty."""
    out: dict[str, Any] = {"proposal": None, "citations": []}
    try:
        out["proposal"] = proposal(connection, project_id, offers=offers, decision=decision,
                                   message=message, message_id=message_id)
    except Exception:  # noqa: BLE001
        log.exception("turn_cards: proposal failed")
    try:
        out["citations"] = citations(connection, project_id, reply, tool_calls)
    except Exception:  # noqa: BLE001
        log.exception("turn_cards: citations failed")
    return out


__all__ = ["CITATION_KINDS", "MAX_CITATIONS", "citations", "for_turn", "plan_steps", "proposal"]
