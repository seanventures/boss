"""WHAT A RELEASE WOULD SHIP, AND WHERE A RELEASE'S FIX IS BUILT.

THE D2 CASE (incident run wfr_d43ec9d0). The fix for the Order API's incident was built as the
app's next build, so it landed on the app's lineage branch — the branch of the shared, still-open
PR #231. That branch already carried 14 commits from two runs whose release approvals had been
REJECTED (crun_1f5f2883, crun_6722492b). The release would have shipped the branch head: 15
commits beyond the serving commit. The run's reviewers saw only its own one-line change, and the
release card said nothing of the other fourteen.

Two rules, one module:

1. A FIX THAT TARGETS A RELEASE GETS ITS OWN BRANCH (`release_branch`, `prepare_branch`): cut
   from the commit that is serving on the target, never from a lineage branch that carries other
   runs' unreleased commits. Its pull request is opened against a base branch at that same
   serving commit, so it shows only its own diff.

2. EVERY RELEASE / DEPLOY CARD LISTS WHAT IT WOULD SHIP beyond the serving commit (`ship_list`):
   every commit, the run or build it came from, that run's approval status and whether its
   reviewers saw it. The card says plainly when it carries commits from rejected or unreviewed
   runs, and when the release exceeds what this run's reviewers saw. An approval of such a card
   needs a stated reason (the same forced-with-a-note path as "merge with checks failing"), and
   a plain approval is refused unless it carries the hash of the list it was shown
   (`refuse_approval`). The deploy then ships exactly the approved head (`approved_head`).
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

from ..ports import sql

#: On a code task's finalize_ctx: the release branch this build works on.
FCTX_KEY = "release_branch"
#: On a decision's recommendation: what the release would ship.
CARD_KEY = "release_commits"
#: Emitted when a person approves a release that carries rejected or unreviewed commits.
FORCED_EVENT = "decision.release_with_unreviewed_commits"

_LINEAGE = re.compile(r"Boss-Lineage:\s*\S*?/(crun_[A-Za-z0-9]+)")
_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_APPROVED = {"approved", "executed", "resolved"}


class ReleaseBranchError(ValueError):
    """The fix's own branch could not be cut from the serving commit; nothing is built on a
    shared branch instead."""


class ReleaseApprovalRefused(ValueError):
    """An approval of a release card that the card's own rules do not allow."""


# --------------------------------------------------------------------------------------------
# 1. The fix's own branch.
# --------------------------------------------------------------------------------------------

def release_branch(key: str, serving_sha: str) -> dict[str, str]:
    """The branch a release-targeting fix is built on, and the base its PR is opened against —
    both at the serving commit."""
    key = re.sub(r"[^A-Za-z0-9_.-]", "-", str(key or "").strip())[:60] or "release"
    sha = str(serving_sha or "").strip()
    if not _SHA.match(sha):
        raise ReleaseBranchError(f"not a commit: {sha[:20]!r}")
    return {"branch": f"boss/fix/{key}", "base_sha": sha, "pr_base": f"boss/serving/{key}"}


def of_run(fctx: Any) -> Optional[dict[str, str]]:
    """The release branch a run's finalize_ctx names, or None."""
    rb = (fctx or {}).get(FCTX_KEY) if isinstance(fctx, dict) else None
    return rb if isinstance(rb, dict) and rb.get("branch") and rb.get("base_sha") else None


def prepare_branch(github: Any, rb: dict[str, str]) -> str:
    """Make sure the fix branch and its PR base exist, both cut from the serving commit.
    Returns the fix branch. A branch that already exists is left alone (a retry continues it).
    Raises ReleaseBranchError when the host cannot cut a branch at a commit."""
    make = getattr(github, "create_branch_ref", None)
    exists = getattr(github, "branch_exists", None)
    if not callable(make) or not callable(exists):
        raise ReleaseBranchError(
            "This repository host cannot cut a branch at a commit, so the fix cannot be built "
            "from the serving release on a branch of its own.")
    for name in (rb["pr_base"], rb["branch"]):
        if not exists(name):
            try:
                make(name, rb["base_sha"])
            except Exception as exc:  # noqa: BLE001 — said, never a fall back to a shared branch
                raise ReleaseBranchError(
                    f"Could not cut {name} at {rb['base_sha'][:12]}: {str(exc)[:160]}") from exc
    return rb["branch"]


# --------------------------------------------------------------------------------------------
# 2. What a release would ship.
# --------------------------------------------------------------------------------------------

def _json(v: Any) -> Any:
    if isinstance(v, (dict, list)) or v is None:
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


def attribute(connection, commits: list[dict[str, Any]],
              project_id: Optional[str] = None) -> list[Optional[str]]:
    """Which build made each commit (oldest first), or None when BOSS cannot tell.

    A commit's own `Boss-Lineage: …/crun_…` trailer names its run. Otherwise the run is read from
    the heads BOSS recorded when each build opened or refreshed its PR (change_set_prs): every
    commit up to and including a run's recorded head, after the previous one, is that run's."""
    shas = [str(c.get("sha") or "") for c in commits]
    heads: dict[str, str] = {}
    if shas:
        for r in connection.execute(sql(
                "SELECT p.code_task_run_id, p.head_sha FROM change_set_prs p "
                "JOIN code_task_runs c ON c.id = p.code_task_run_id "
                "WHERE p.head_sha = ANY(:s) AND (CAST(:p AS TEXT) IS NULL OR c.project_id = :p) "
                "ORDER BY c.created_at"),
                {"s": shas, "p": project_id}).mappings().all():
            heads[str(r["head_sha"])] = str(r["code_task_run_id"])
    out: list[Optional[str]] = [None] * len(commits)
    pending: list[int] = []
    for i, c in enumerate(commits):
        named = _LINEAGE.search(str(c.get("full_message") or c.get("message") or ""))
        if named:
            out[i] = named.group(1)
            continue
        pending.append(i)
        run = heads.get(shas[i])
        if run:
            for j in pending:
                out[j] = run
            pending = []
    return out


def _approval(connection, run_id: str, workflow_run_id: Optional[str]) -> tuple[str, str]:
    """(status, what was asked) of a build's release: its workflow's release or deploy gate,
    its stack deployment's decision, else its PR's merge decision."""
    rows: list[Any] = []
    if workflow_run_id:
        rows = connection.execute(sql(
            "SELECT d.status FROM workflow_step_runs s JOIN decisions d ON d.id = s.decision_id "
            "WHERE s.workflow_run_id = :w AND COALESCE(s.binding_snapshot->>'capability', "
            "      s.binding_snapshot->'binding'->>'capability', s.step_id) "
            "      IN ('release_approval', 'deploy') "
            "ORDER BY d.created_at DESC LIMIT 1"), {"w": workflow_run_id}).all()
        if rows:
            return str(rows[0][0]), "release"
    rows = connection.execute(sql(
        "SELECT d.status FROM stack_deployments sd JOIN decisions d ON d.id = sd.decision_id "
        "WHERE sd.code_task_run_id = :r ORDER BY sd.created_at DESC LIMIT 1"),
        {"r": run_id}).all()
    if rows:
        return str(rows[0][0]), "deploy"
    rows = connection.execute(sql(
        "SELECT status FROM decisions WHERE recommendation->>'run_id' = :r "
        "AND recommendation->>'action' = 'review_pull_request' "
        "ORDER BY created_at DESC LIMIT 1"), {"r": run_id}).all()
    if rows:
        return str(rows[0][0]), "merge"
    return "none", ""


def _word(status: str) -> str:
    s = str(status or "").lower()
    if s in _APPROVED:
        return "approved"
    if s == "rejected":
        return "rejected"
    if s in ("none", ""):
        return "none"
    return "pending"


def run_facts(connection, run_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Per build: its title, workflow run, release approval and whether it was reviewed."""
    ids = sorted({r for r in run_ids if r})
    if not ids:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for r in connection.execute(sql(
            "SELECT id, title, state, finalize_ctx FROM code_task_runs WHERE id = ANY(:i)"),
            {"i": ids}).mappings().all():
        state = _json(r["state"]) or {}
        fctx = _json(r["finalize_ctx"]) or {}
        wfr = str(fctx.get("workflow_run_id") or "") or None
        status, asked = _approval(connection, str(r["id"]), wfr)
        out[str(r["id"])] = {
            "title": str(r["title"] or "")[:120], "workflow_run_id": wfr,
            "approval": _word(status), "approval_of": asked,
            # Reviewed: the build's code-review seat read it (governed pipeline state).
            # Reviewed: the build's code-review seat read it, or a person approved its release
            # or its merge (governed pipeline state; the decisions above).
            "reviewed": bool(state.get("review_summary") or state.get("adversarial_verdict")
                             or _word(status) == "approved"),
        }
    return out


def card_hash(card: dict[str, Any]) -> str:
    """The hash an approval of this card must carry: of exactly what it lists."""
    material = {"serving": card.get("serving_sha"), "head": card.get("head_sha"),
                "known": card.get("known"),
                "commits": [[c.get("sha"), c.get("run_id"), c.get("approval"), c.get("reviewed")]
                            for c in card.get("commits") or []]}
    return "sha256:" + hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _short(sha: Any) -> str:
    return str(sha or "")[:7]


def sentence(card: dict[str, Any]) -> str:
    """The card in words: what ships, from whom, and what nobody reviewed or someone rejected."""
    if not card.get("known"):
        return (f"BOSS could not list what this release would ship beyond the serving commit "
                f"{_short(card.get('serving_sha'))} ({card.get('why') or 'unknown'}). Approving "
                "it ships code you have not been shown, so it needs your reason.")
    commits = card.get("commits") or []
    n = len(commits)
    if not n:
        return f"Ships nothing beyond the serving commit {_short(card.get('serving_sha'))}."
    own = card.get("own_count") or 0
    parts = [f"Ships {n} commit{'s' if n != 1 else ''} beyond the serving commit "
             f"{_short(card.get('serving_sha'))}: {own} from this run"]
    others = n - own
    if others:
        runs = card.get("other_runs") or []
        parts[0] += (f", {others} from {len(runs)} other run{'s' if len(runs) != 1 else ''}"
                     if runs else f", {others} from other work")
    parts[0] += "."
    rejected = card.get("rejected_runs") or []
    if rejected:
        k = sum(1 for c in commits if c.get("approval") == "rejected")
        parts.append(f"{k} come from runs whose release was REJECTED ({', '.join(rejected)}).")
    unreviewed = sum(1 for c in commits if not c.get("reviewed") and not c.get("own"))
    if unreviewed:
        parts.append(f"{unreviewed} were never reviewed"
                     + (f" ({card['unattributed']} BOSS cannot trace to any run)"
                        if card.get("unattributed") else "") + ".")
    if card.get("beyond_reviewers"):
        parts.append(f"This run's reviewers saw only its own {own} commit"
                     f"{'s' if own != 1 else ''}; the release ships {others} they did not see.")
    if card.get("needs_reason"):
        parts.append("Approving it needs your reason.")
    return " ".join(parts)


def ship_list(connection, compare: Any, *, serving_sha: Optional[str], head_sha: Optional[str],
              own_workflow_run_id: Optional[str] = None,
              own_run_ids: tuple[str, ...] = (), repository: str = "",
              project_id: Optional[str] = None) -> Optional[dict]:
    """The card: every commit `head_sha` has beyond `serving_sha`, attributed and judged.
    `compare(base, head)` answers GitHub's compare (commits oldest first).

    None when nothing is serving yet (a first release has no serving commit to compare with).
    A list that cannot be read is a card all the same (`known: False`, needs a reason): a
    release must never ship code the person was not shown."""
    serving, head = str(serving_sha or "").strip(), str(head_sha or "").strip()
    if not serving:
        return None
    card: dict[str, Any] = {"serving_sha": serving, "head_sha": head or None,
                            "repository": repository or None, "known": False, "commits": []}
    if not head:
        card["why"] = "the release's head commit is not known"
    elif compare is None:
        card["why"] = "the repository could not be read"
    elif head == serving:
        card["known"] = True
    else:
        try:
            got = compare(serving, head)
        except Exception as exc:  # noqa: BLE001 — said on the card, never a silent empty list
            card["why"] = f"could not compare: {str(exc)[:120]}"
        else:
            if int(got.get("behind_by") or 0):
                # The head does not contain the serving commit: shipping it also REMOVES what
                # is serving and not on this branch. Said on the card.
                card["behind_by"] = int(got["behind_by"])
            card["known"] = True
            commits = list(got.get("commits") or [])
            runs = attribute(connection, commits, project_id)
            facts = run_facts(connection, [r for r in runs if r])
            for c, run in zip(commits, runs):
                f = facts.get(run or "") or {}
                own = bool(run) and (run in own_run_ids or (
                    own_workflow_run_id and f.get("workflow_run_id") == own_workflow_run_id))
                card["commits"].append({
                    "sha": str(c.get("sha") or ""), "short": _short(c.get("sha")),
                    "message": str(c.get("message") or "")[:200],
                    "run_id": run, "run_title": f.get("title"),
                    "workflow_run_id": f.get("workflow_run_id"),
                    "approval": f.get("approval") or ("none" if run else "unknown"),
                    "approval_of": f.get("approval_of") or "",
                    "reviewed": bool(f.get("reviewed")), "own": bool(own)})
    commits = card["commits"]
    card["own_count"] = sum(1 for c in commits if c["own"])
    card["other_runs"] = sorted({c["run_id"] for c in commits if c["run_id"] and not c["own"]})
    card["rejected_runs"] = sorted({c["run_id"] for c in commits
                                    if c["run_id"] and c["approval"] == "rejected"})
    card["unreviewed_runs"] = sorted({c["run_id"] for c in commits
                                      if c["run_id"] and not c["reviewed"]})
    card["unattributed"] = sum(1 for c in commits if not c["run_id"])
    card["beyond_reviewers"] = any(not c["own"] for c in commits)
    # WHAT NEEDS A REASON: a list BOSS could not read, a commit from a run whose release was
    # rejected, a commit no BOSS run made, or another run's commit nobody reviewed. This run's
    # own commits are what the card is about: they are listed, reviewed or not, and approving
    # the card is a person reading them.
    card["needs_reason"] = (not card["known"]) or any(
        c["approval"] == "rejected" or not c["run_id"] or (not c["own"] and not c["reviewed"])
        for c in commits)
    card["sentence"] = sentence(card)
    card["content_hash"] = card_hash(card)
    return card


def preview(card: dict[str, Any], *, step_label: str = "Release") -> dict[str, Any]:
    """The card as the console's `fields` preview: one row per commit, bound by its hash (the
    console sends the hash back with Approve; decisions.transition refuses any other)."""
    fields = [{"label": "What ships", "value": card["sentence"]}]
    for c in card.get("commits") or []:
        who = (f"{'this run' if c['own'] else c['run_id']}" if c.get("run_id")
               else "no BOSS run")
        flags = [c.get("approval") or "unknown",
                 "reviewed" if c.get("reviewed") else "NOT reviewed"]
        fields.append({"label": f"{c['short']} · {who} · {', '.join(flags)}",
                       "value": c.get("message") or ""})
    return {"kind": "fields", "skill_id": None, "step_label": step_label,
            "content_hash": card["content_hash"], "redactions": 0, "fields": fields,
            "release": True}


def attach(recommendation: dict[str, Any], card: Optional[dict[str, Any]], *,
           step_label: str = "Release") -> dict[str, Any]:
    """Put the card on a decision's recommendation. Where the card already carries another
    preview (an infrastructure plan), the commits are added to that preview's rows and its own
    hash still binds the approval — the commit list is fixed by the planned commit."""
    if not card:
        return recommendation
    out = {**recommendation, CARD_KEY: card}
    existing = out.get("action_preview")
    if isinstance(existing, dict) and existing.get("content_hash"):
        rows = preview(card)["fields"]
        out["action_preview"] = {**existing,
                                 "fields": list(existing.get("fields") or []) + rows}
        out[CARD_KEY] = {**card, "bound_by": "action_preview"}
    else:
        out["action_preview"] = preview(card, step_label=step_label)
    return out


def of_decision(recommendation: Any) -> Optional[dict[str, Any]]:
    rec = _json(recommendation) if not isinstance(recommendation, dict) else recommendation
    card = (rec or {}).get(CARD_KEY)
    return card if isinstance(card, dict) else None


def needs_reason(recommendation: Any) -> bool:
    card = of_decision(recommendation)
    return bool(card and card.get("needs_reason"))


def refuse_approval(recommendation: Any, *, content_hash: Optional[str], force: bool,
                    note: Optional[str]) -> None:
    """Raise ReleaseApprovalRefused unless this approval is of the list the card showed and,
    where the list carries rejected or unreviewed commits, says why."""
    rec = _json(recommendation) if not isinstance(recommendation, dict) else recommendation
    card = of_decision(rec)
    if card is None:
        return
    bound = ((rec or {}).get("action_preview") or {}).get("content_hash") or card.get(
        "content_hash")
    if not content_hash or content_hash != bound:
        raise ReleaseApprovalRefused(
            "This release card lists the commits it would ship, and this approval was not of "
            "that list (it was not shown, or it changed since). Reload the card, read what it "
            "ships, and approve again.")
    if card.get("needs_reason") and not (force and str(note or "").strip()):
        raise ReleaseApprovalRefused(
            "This release ships commits from rejected or unreviewed runs (or commits BOSS "
            "could not list). Say why it should ship anyway: approve it with your reason.")


def approved_head(connection, workflow_run_id: str) -> Optional[str]:
    """The head commit an approved release card of this run listed, or None. The deploy ships
    exactly this commit — never a head that moved after the person approved."""
    if not workflow_run_id:
        return None
    row = connection.execute(sql(
        "SELECT d.recommendation->:k->>'head_sha' FROM workflow_step_runs s "
        "JOIN decisions d ON d.id = s.decision_id WHERE s.workflow_run_id = :w "
        "AND d.status = ANY(:ok) AND d.recommendation ? :k "
        "ORDER BY d.approved_at DESC NULLS LAST LIMIT 1"),
        {"w": workflow_run_id, "k": CARD_KEY, "ok": sorted(_APPROVED)}).scalar()
    return str(row) if row and _SHA.match(str(row)) else None


# --------------------------------------------------------------------------------------------
# 3. Reading the serving commit and the release head for a run.
# --------------------------------------------------------------------------------------------

def github_for(connection, settings: Any, project_id: str, repository: str):
    """The repository's GitHub client (compare, branches). None when it cannot be made."""
    try:
        from .incident_response import _github_for

        return _github_for(connection, settings, project_id, repository)
    except Exception:  # noqa: BLE001 — the card says the list could not be read
        return None


def serving_of_target(connection, target_id: str) -> Optional[str]:
    """The commit serving on a target: its newest healthy stack deployment's."""
    if not target_id:
        return None
    return connection.execute(sql(
        "SELECT commit_sha FROM stack_deployments WHERE target_id = :t AND status = 'healthy' "
        "AND commit_sha IS NOT NULL ORDER BY created_at DESC LIMIT 1"),
        {"t": target_id}).scalar()


def serving_for_run(connection, run_input: Any, build_run_id: Optional[str]) -> Optional[str]:
    """The commit serving where this run's release goes: an incident's target, else the newest
    healthy stack deployment of the build's app."""
    run_input = run_input if isinstance(run_input, dict) else {}
    incident_id = str(run_input.get("incident_id") or "").strip()
    if incident_id:
        ctx = _json(connection.execute(sql(
            "SELECT finalize_ctx FROM incidents WHERE id = :i"), {"i": incident_id}).scalar()) or {}
        target = str((ctx.get("app") or {}).get("target_id") or "")
        return serving_of_target(connection, target) or (ctx.get("deploy") or {}).get(
            "commit_sha")
    if not build_run_id:
        return None
    return connection.execute(sql(
        "WITH me AS (SELECT COALESCE(finalize_ctx->>'app_id', finalize_ctx->>'root_run_id', id) "
        "            AS app FROM code_task_runs WHERE id = :r) "
        "SELECT sd.commit_sha FROM stack_deployments sd JOIN code_task_runs c "
        "  ON c.id = sd.code_task_run_id, me "
        "WHERE sd.status = 'healthy' AND sd.commit_sha IS NOT NULL "
        "  AND COALESCE(c.finalize_ctx->>'app_id', c.finalize_ctx->>'root_run_id', c.id) = me.app "
        "ORDER BY sd.created_at DESC LIMIT 1"), {"r": build_run_id}).scalar()


def card_for_build(connection, settings: Any, project_id: str, *, build_run_id: str,
                   serving_sha: Optional[str], workflow_run_id: Optional[str],
                   github: Any = None) -> Optional[dict[str, Any]]:
    """The release card for a workflow run's build: its PR head beyond `serving_sha`."""
    row = connection.execute(sql(
        "SELECT spec, pull_request_number FROM code_task_runs WHERE id = :i AND project_id = :p"),
        {"i": build_run_id, "p": project_id}).mappings().first()
    if row is None or not serving_sha:
        return None
    spec = _json(row["spec"]) or {}
    repository = (f"{spec.get('owner')}/{spec.get('repo')}"
                  if spec.get("owner") and spec.get("repo") else "")
    if github is None and repository:
        github = github_for(connection, settings, project_id, repository)
    head = None
    if github is not None and row["pull_request_number"]:
        try:
            head = github.pull_request_snapshot(int(row["pull_request_number"])).get("head_sha")
        except Exception:  # noqa: BLE001 — the card says the head is not known
            head = None
    compare = getattr(github, "compare_summary", None) if github is not None else None
    return ship_list(connection, compare, serving_sha=serving_sha, head_sha=head,
                     own_workflow_run_id=workflow_run_id, own_run_ids=(build_run_id,),
                     repository=repository, project_id=project_id)
