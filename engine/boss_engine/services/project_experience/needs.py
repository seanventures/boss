"""NEEDS YOU, in words a person reads at a glance (contract §2 `needs_you[]`).

Every card is built from the decision's STRUCTURED data, never from its question text:
"Deploy paramg/helpdesk-ai-demo@cfe343f1c6cd to 2.29.2.202" was the stack deploy's question
copied onto the card. Now:

  * a stack deploy (`deploy_stack`) → "Deploy PR #3 to Staging": the pull request whose merge
    commit is being shipped (code_task_runs' recorded merge), the target's environment; mini
    flow PR #3 merged → Staging · you → Smoke test; button "Approve deploy";
  * a pull request's question → "Merge PR #3": Build → Review → Merge; button "Review PR";
  * a plan/spec gate → "Approve the plan"; a refusal → the setup it needs;
  * the onboarding agent's "only you" items (Pick a prod host, Add a secret) as they are;
  * anything else → its question cut to a short title by rule (titles.shorten), never raw.

The primary button is named for the action. `detail` keeps the full question for the drawer.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .common import href, iso, pr_number, project_href
from .health import env_label
from .titles import shorten, short_title

#: the onboarding agent's "only you" items, as the thing to do (screen 04: "Pick a prod host").
ONLY_YOU_WORDS = {"deploy_target": "Pick a staging host", "secrets": "Set the app's secrets",
                  "models": "Add a model", "repo_access": "Fix repo access",
                  "tests": "Fix the tests", "baseline_build": "Fix the build"}

ACTION_WORDS = {"merge": "Review PR", "deploy": "Approve deploy", "plan": "Review plan",
                "setup": "Fix setup", "question": "Answer"}


def decision_kind(decision: Mapping[str, Any]) -> str:
    """merge | deploy | plan | setup | question, from the decision's own fields."""
    from ..decision_actions import kind_of

    rec = decision.get("recommendation") if isinstance(decision.get("recommendation"), dict) else {}
    action = str(rec.get("action") or "")
    cap = str(rec.get("capability") or "")
    if action in ("deploy_stack", "deploy_app", "redeploy") or rec.get("deployment_id"):
        return "deploy"
    kind = kind_of(decision)
    if kind == "pull_request" or cap in ("release_approval", "wait_for_ci", "merge"):
        return "merge"
    if kind in ("deploy_gate", "change_set") or cap in ("deploy", "redeploy"):
        return "deploy"
    if kind == "architecture_review" or cap in ("spec_review", "meeting", "research",
                                                "architecture"):
        return "plan"
    if kind == "refusal":
        return "setup"
    return "question"


def _pr_of_commit(connection: Connection, project_id: str, commit: str) -> Optional[int]:
    """The pull request whose recorded merge is this commit (pr_merge.record)."""
    if not commit:
        return None
    row = connection.execute(text(
        "SELECT pull_request_number, pull_request_url FROM code_task_runs WHERE project_id = :p "
        "AND finalize_ctx->'merge'->>'sha' = :c AND (pull_request_number IS NOT NULL "
        "OR pull_request_url IS NOT NULL) ORDER BY created_at LIMIT 1"),
        {"p": project_id, "c": commit}).mappings().first()
    if not row:
        return None
    return row["pull_request_number"] or pr_number(row["pull_request_url"])


_PASSED = (re.compile(r"#\s*pass\s+(\d+)"), re.compile(r"Tests:\s+(\d+)\s+passed"),
           re.compile(r"(\d+)\s+passed"))


def tests_of(connection: Connection, project_id: str, pr_url: Optional[str]) -> Optional[dict]:
    """The PR's newest build's test result: {passed: bool, count: int | None} from the build's
    own verify run (code_task_runs.state), or None when no build ran tests."""
    if not pr_url:
        return None
    row = connection.execute(text(
        "SELECT state->'tests_passed' AS ok, state->>'last_verify_output' AS out FROM code_task_runs "
        "WHERE project_id = :p AND pull_request_url = :u AND state ? 'tests_passed' "
        "ORDER BY created_at DESC LIMIT 1"), {"p": project_id, "u": pr_url}).mappings().first()
    if not row:
        return None
    count = None
    for pattern in _PASSED:
        found = pattern.findall(str(row["out"] or ""))
        if found:
            count = int(found[-1])
            break
    return {"passed": row["ok"] is True, "count": count}


def _target_env(connection: Connection, target_id: str) -> Optional[str]:
    if not target_id:
        return None
    return connection.execute(text("SELECT environment FROM deployment_targets WHERE id = :t"),
                              {"t": target_id}).scalar()


_PULL_REF = re.compile(r"^(?:refs/)?pull/(\d+)(?:/head|/merge)?$")


def shipped_words(connection: Connection, project_id: str, rec: Mapping[str, Any],
                  number: Optional[int]) -> tuple[str, dict[str, str]]:
    """(what the deploy ships, its first mini-flow node) FROM WHAT IT SHIPS: the commit it
    plans. A recorded merge names its PR ("PR #1", merged); a pull request's head names that
    PR, not merged ("PR #2 (not merged)"); any other commit is its short sha. Never the PR of
    the work the deploy hangs on: 99b569f, PR #2's open head, read "Deploy PR #1 to Staging"
    because the workflow's first PR was #1 (helpdesk-ai-v3, dec_8eaeba2f, 2026-11-07)."""
    commit = str(rec.get("commit") or "")
    merged = _pr_of_commit(connection, project_id, commit)
    if merged:
        return f"PR #{merged}", {"label": f"PR #{merged} merged", "state": "done"}
    pulled = _PULL_REF.match(str(rec.get("ref") or "").strip())
    if pulled:
        n = int(pulled.group(1))
        return (f"PR #{n} (not merged)",
                {"label": f"PR #{n} not merged", "state": "failed"})
    if commit:
        return commit[:7], {"label": f"{commit[:7]} · not a merge", "state": "waiting"}
    if number:
        return f"PR #{number}", {"label": f"PR #{number}", "state": "done"}
    return "", {"label": "Merged", "state": "done"}


def release_of(rec: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The deploy's release card for the drawer: the preview Approvals draws (its rows are the
    commits), the hash an approval must carry, and whether approving needs a reason."""
    from ..release_scope import of_decision

    card_ = of_decision(dict(rec))
    preview = rec.get("action_preview") if isinstance(rec.get("action_preview"), dict) else None
    if card_ is None or not preview or not preview.get("content_hash"):
        return None
    return {"preview": preview, "content_hash": preview["content_hash"],
            "needs_reason": bool(card_.get("needs_reason")),
            "sentence": str(card_.get("sentence") or "")}


def subject_of(c: Mapping[str, Any]) -> str:
    """What a Needs-you card asks about, so one subject is one card: a pull request (by its
    URL), else the card's own words."""
    url = str(c.get("external_url") or "")
    m = re.search(r"/pull/(\d+)", url)
    if m and c.get("kind") == "merge":
        return f"pr:{url.split('/pull/')[0]}#{m.group(1)}"
    return f"{c.get('kind')}:{' '.join(str(c.get('label') or '').lower().split())}"


def one_per_subject(cards_: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """ONE CARD PER PULL REQUEST AND PER SUBJECT, the newest asked kept, in the first one's
    place. Two runs that both claimed PR #2 raised two merge questions, and Needs you showed
    "Merge PR #2" twice (helpdesk-ai-v3, dec_df7edb70 + dec_924dfab2, 2026-11-07)."""
    keep: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for c in cards_:
        key = subject_of(c)
        if key in keep:
            i = keep[key]
            if str(c.get("created_at") or "") > str(out[i].get("created_at") or ""):
                out[i] = c
            continue
        keep[key] = len(out)
        out.append(c)
    return out


def card(connection: Connection, project_id: str, decision: Mapping[str, Any],
         run: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """One Needs-you card from one waiting decision."""
    rec = decision.get("recommendation") if isinstance(decision.get("recommendation"), dict) else {}
    kind = decision_kind(decision)
    links = (run or {}).get("links") or {}
    pr_url = rec.get("pull_request_url") or (links.get("pr") or {}).get("external_url")
    number = pr_number(pr_url) if pr_url else None
    stages = [s for s in (run or {}).get("stages") or [] if s.get("state") != "skipped"]
    state_of = {s.get("key"): s.get("state") for s in stages}
    release: Optional[dict[str, Any]] = None
    if kind == "deploy":
        env = env_label(rec.get("environment") or _target_env(connection, str(rec.get("target_id")
                                                                               or "")) or "")
        what, first = shipped_words(connection, project_id, rec, number)
        label = f"Deploy {what} to {env}".replace("  ", " ") if what else f"Deploy to {env}"
        mini = [first, {"label": f"{env} · you", "state": "waiting"},
                {"label": "Smoke test", "state": "todo"}]
        release = release_of(rec)
    elif kind == "merge":
        label = f"Merge PR #{number}" if number else "Merge the pull request"
        tests = tests_of(connection, project_id, pr_url)
        review = state_of.get("review", "done")
        mini = [{"label": (f"Tests {tests['count']}" if tests and tests["count"] else "Tests"),
                 "state": ("done" if (tests is None or tests["passed"]) else "failed")},
                {"label": "Review", "state": "done" if review in ("done", "skipped") else review},
                {"label": "Merge", "state": "waiting"}]
    elif kind == "plan":
        name = (run or {}).get("label")
        label = f"Approve plan: {name}" if name and len(str(name).split()) <= 3 else "Approve the plan"
        mini = [{"label": "Plan", "state": "done"}, {"label": "You approve", "state": "waiting"},
                {"label": "Build", "state": "todo"}]
    else:
        label = shorten(decision.get("question") or "Answer a question", 5) or "Answer a question"
        mini = [{"label": s.get("name") or s.get("label"), "state": s.get("state")} for s in stages]
    # WHAT THE AGENT FOUND (stuck_work): a card about stuck or refused work carries the
    # Shepherd's diagnosis, and its detail is the reason -- never only "X is stuck on Y".
    from .. import stuck_work

    diagnosis = (run or {}).get("diagnosis") or stuck_work.card_diagnosis(connection, decision)
    said = next((str(e.get("detail")) for e in rec.get("batch") or []
                 if isinstance(e, dict) and e.get("detail")), "") or str(rec.get("why") or "")
    detail = str(decision.get("question") or "")
    if diagnosis and (diagnosis.get("cause") or diagnosis.get("headline")):
        # Concise: what to do, then the cause in a few words; the diagram and Details carry more.
        cause = str(diagnosis.get("cause") or diagnosis.get("headline"))
        detail = f"{detail} — {cause}" if detail else cause
    elif said:
        detail = f"{detail} — {said}" if detail else said
    return {"id": decision["id"], "kind": kind, "label": label,
            "detail": detail,
            **({"diagnosis": diagnosis} if diagnosis else {}),
            "work_id": (run or {}).get("id"),
            "work_label": short_title((run or {}).get("label"), (run or {}).get("title"))
            if run else None,
            "mini": mini, "risk_tier": decision.get("risk_tier"),
            "created_at": iso(decision.get("created_at")),
            "external_url": pr_url or None,
            # WHAT THIS DEPLOY SHIPS, as its release card lists it (release_scope): the drawer
            # shows these commits and approves with their hash, as Approvals does.
            **({"release": release} if release else {}),
            # The button acts IN PLACE (04-19/21): Review PR opens the work's drawer (its PR,
            # checks and Merge); everything else opens this card's drawer, which approves or
            # rejects there with step-up. Approvals stays one link away inside it.
            "action": {"label": ACTION_WORDS[kind], "decision_id": decision["id"],
                       "href": (project_href(project_id, drawer=f"run:{run['id']}")
                                if kind == "merge" and run and run.get("source") == "workflow_run"
                                else project_href(project_id, drawer=f"needs:{decision['id']}")),
                       "approvals_href": href(project_id, area="decisions",
                                              decision=decision["id"])}}


def prod_host_card(project_id: str) -> dict[str, Any]:
    """Production waits on a person (staging is live, no production host): screen 04's
    "Pick a prod host · Beacon can't choose this" with its Pick host button (Infra's
    add-target sheet for this project)."""
    return {"id": "prod:host", "kind": "setup", "label": "Pick a prod host",
            "detail": "Beacon can't choose this", "icon": "cloud", "mini": [], "work_id": None,
            "action": {"label": "Pick host", "decision_id": None,
                       "href": href(project_id, area="infra", add="target", env="production")}}


def prod_waits(connection: Connection, project_id: str) -> bool:
    """Production waits on a person: a non-production environment is live (a healthy deploy on
    an active target) and the project has no active production target."""
    row = connection.execute(text(
        "SELECT EXISTS (SELECT 1 FROM stack_deployments s JOIN deployment_targets t ON "
        " t.id = s.target_id WHERE t.project_id = :p AND t.status = 'active' AND s.status = "
        " 'healthy' AND lower(COALESCE(t.environment, '')) NOT IN ('production', 'prod')) AS staged, "
        "EXISTS (SELECT 1 FROM deployment_targets WHERE project_id = :p AND status = 'active' "
        " AND lower(COALESCE(environment, '')) IN ('production', 'prod')) AS prod"),
        {"p": project_id}).mappings().first()
    return bool(row and row["staged"] and not row["prod"])


def setup_cards(connection: Connection, project_id: str,
                onboarding: Optional[Mapping[str, Any]] = None,
                production_waits: Optional[bool] = None,
                onboarding_row: Optional[str] = None) -> list[dict[str, Any]]:
    """THE ONE LIST of what only a person can set up on this project (not decisions): the
    onboarding agent's "only you" items and "Pick a prod host". The Project page's Needs you
    and the top bar's bell both count it, so they never disagree. `onboarding_row` is the Work
    tab's Onboarding row: the "only you" items hang on it (`work_id`)."""
    if onboarding is None:
        from . import onboarding as onboarding_service

        has_run = connection.execute(text(
            "SELECT 1 FROM project_onboarding_runs WHERE project_id = :p LIMIT 1"),
            {"p": project_id}).first()
        onboarding = onboarding_service.view(connection, project_id) if has_run else {}
    out = []
    for item in (onboarding or {}).get("only_you") or []:
        action = item.get("action") or {}
        out.append({"id": f"onb:{item['key']}", "kind": "setup",
                    "label": ONLY_YOU_WORDS.get(str(item["key"])) or item["label"],
                    "detail": item.get("detail") or "", "mini": [], "work_id": onboarding_row,
                    "action": {"label": action.get("label") or "Open",
                               "href": action.get("href") or project_href(project_id,
                                                                          tab="onboarding"),
                               "decision_id": None}})
    waits = prod_waits(connection, project_id) if production_waits is None else production_waits
    if waits:
        out.append(prod_host_card(project_id))
    return out


def cards(connection: Connection, project_id: str, work_items: list[dict[str, Any]],
          onboarding: Optional[Mapping[str, Any]] = None) -> list[dict[str, Any]]:
    """Every card: the decisions waiting on a person (decision_actions.waiting_on_operator —
    the one reader every surface uses), then what only a person can set up (setup_cards)."""
    from ..decision_actions import waiting_on_operator

    runs = {w["id"]: w for w in work_items if w.get("source") == "workflow_run"}
    by_decision = {d: w for w in work_items for d in w.get("decision_ids") or []}
    out = []
    for d in waiting_on_operator(connection, project_id):
        rec = d.get("recommendation") if isinstance(d.get("recommendation"), dict) else {}
        run = runs.get(str(rec.get("workflow_run_id") or "")) or by_decision.get(str(d["id"]))
        out.append(card(connection, project_id, d, run))
    out = one_per_subject(out)
    # ONE SOURCE FOR NEEDS YOU: every piece of work the Work tab groups under Needs you has its
    # card here (a gate or merge whose decision the reader above did not return still waits).
    carded = {c.get("work_id") for c in out if c.get("work_id")}
    asked = {subject_of(c) for c in out}
    for w in work_items:
        if w.get("state") != "needs_you" or w.get("source") == "onboarding" or w["id"] in carded:
            continue
        pr = ((w.get("links") or {}).get("pr") or {}).get("external_url")
        if pr and subject_of({"kind": "merge", "external_url": pr}) in asked:
            continue        # its pull request already has its card
        kind = ("merge" if w.get("action_label") == "Merge" else
                "deploy" if w.get("action_label") == "Approve deploy" else
                "plan" if w.get("action_label") == "Approve plan" else "question")
        decision = (w.get("decision_ids") or [None])[0]
        out.append({"id": f"work:{w['id']}", "kind": kind,
                    "label": shorten(f"{w.get('action_label') or 'Decide'}: {w['label']}", 5),
                    "detail": str(w.get("detail") or ""), "work_id": w["id"],
                    "work_label": w["label"], "mini": [], "created_at": w.get("updated_at"),
                    "external_url": ((w.get("links") or {}).get("pr") or {}).get("external_url"),
                    "action": {"label": ACTION_WORDS[kind], "decision_id": decision,
                               "href": (w.get("links") or {}).get("decision") or w.get("href")}})
    # The onboarding agent's "only you" items belong to the Onboarding row of the Work tab.
    onboarding_row = next((w["id"] for w in work_items if w.get("source") == "onboarding"), None)
    return out + setup_cards(connection, project_id, onboarding or {},
                             onboarding_row=onboarding_row)


__all__ = ["ACTION_WORDS", "card", "cards", "decision_kind", "one_per_subject", "prod_host_card",
           "prod_waits", "release_of", "setup_cards", "shipped_words", "subject_of"]
