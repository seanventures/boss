"""THE PRD'S RECORD (BOSSAPP-929): where an approved PRD is read, where it was argued, where it lives.

The operator, approving a PRD: "The PRD is never uploaded to the git? in the approve PRD there's no
link or any doc / pdf to look into the PRD before approval, we need to also give the link to the
place where the discussion happened and PRD generated especially the meeting panel".

A meeting's PRD is ONE decision (meeting_service._finalize_meeting; a workflow's spec step holds on
the same decision, workflow_capability_runner). So the links are made here, once, from that
decision, and every surface that shows the decision — Needs you, Approvals, a build's PRD step, the
project's Work — reads them from the decision payload:

* ``markdown`` — the PRD itself, readable in place; ``full_page`` — the meeting page that shows it
  whole.
* ``meeting`` — the panel that wrote it: agenda (the brief and who sat), each agent's position, the
  debate, the dissent, the facilitator's synthesis; ``href`` opens the meeting.
* ``repo`` — the file in the repository: ``docs/prd/<slug>.md`` and the minutes beside it. Before a
  build commits it the path is what WILL be written (``committed: false``); once the build's branch
  holds it, the branch, the commit and a link to the file.

THE PRD LANDS WITH THE PR. The build commits the approved PRD and its meeting minutes to its own
work branch through the same repo host its stages commit through (GitHub, or the local repository of
a local-first project), so the pull request carries the spec it was built from
(:func:`commit_to_branch`, called by the governed build).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

PRD_DIR = "docs/prd"
_POINTS = 4


def _j(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value if value is not None else default


def slug(title: Any) -> str:
    """`PRD: Hello World REST API` → `hello-world-rest-api` (the file's name in docs/prd)."""
    raw = re.sub(r"^\s*PRD\s*[:·-]\s*", "", str(title or ""), flags=re.I)
    out = re.sub(r"[^a-zA-Z0-9]+", "-", raw).strip("-").lower()[:60].strip("-")
    return out or "prd"


def paths(title: Any) -> dict[str, str]:
    name = slug(title)
    return {"prd": f"{PRD_DIR}/{name}.md", "minutes": f"{PRD_DIR}/{name}-meeting.md"}


def _meeting_row(connection, decision_id: str) -> Optional[dict]:
    row = connection.execute(text(
        "SELECT id, project_id, task_id, meeting_id, title, directive, role_keys, constraints, "
        "state, finalize_ctx, status, updated_at FROM meeting_runs WHERE decision_id = :d "
        "ORDER BY updated_at DESC LIMIT 1"), {"d": str(decision_id)}).mappings().first()
    return dict(row) if row else None


def _is_prd_decision(recommendation: Any) -> bool:
    rec = recommendation if isinstance(recommendation, dict) else {}
    args = rec.get("args") if isinstance(rec.get("args"), dict) else {}
    return rec.get("tool") == "publish_document" and (
        bool(rec.get("start_code_task")) or args.get("repo_subdir") == PRD_DIR)


def _seat_names(connection, project_id: str, keys: list[str]) -> dict[str, str]:
    """Each seat's name as the panel shows it: the role's title (Product Manager…)."""
    try:
        from .meeting_service import load_role_map, resolve_roles

        return {p["key"]: str(p.get("title") or p["key"])
                for p in resolve_roles(keys, load_role_map(connection, project_id))}
    except Exception:  # noqa: BLE001 — a missing role map names the seat by its key
        return {k: k.replace("_", " ").title() for k in keys}


def _gists(turns: Any, stored: Any) -> dict[str, dict]:
    from .meeting_gist import gists_for

    return gists_for(turns if isinstance(turns, dict) else {}, stored if isinstance(stored, dict) else {})


def _dissent(state: dict, synthesis: str) -> list[dict]:
    """What was disagreed and not settled: a typed meeting's dissent records, else the
    facilitator's own dissent section of the synthesis (the synthesis prompt asks for "any
    recorded dissent", meeting_service)."""
    out = [{"role": d.get("role"), "position": d.get("position"), "reason": d.get("reason")}
           for d in state.get("dissent_records") or [] if isinstance(d, dict)]
    if out:
        return out
    m = re.search(r"^#{1,4}[^\n]*\bdissent[^\n]*$", synthesis or "", re.I | re.M)
    if not m:
        return []
    body = synthesis[m.end():]
    nxt = re.search(r"^#{1,4}\s", body, re.M)
    lines = [re.sub(r"^[\s>*-]+|\*\*", "", ln).strip() for ln in (body[:nxt.start()] if nxt else body).splitlines()]
    return [{"role": None, "position": ln, "reason": None} for ln in lines if len(ln) > 3][:6]


def meeting_summary(connection, run: dict) -> dict:
    """The panel as a person reads it before approving: agenda, positions, debate, dissent."""
    from .meeting_brief import split_brief
    from .meeting_service import FACILITATOR_TITLE, meeting_title
    from .project_experience.common import href

    state = _j(run.get("state"), {}) or {}
    constraints = _j(run.get("constraints"), {}) or {}
    keys = [str(k) for k in (_j(run.get("role_keys"), []) or [])]
    names = _seat_names(connection, str(run["project_id"]), keys)
    r1 = _gists(state.get("round1"), state.get("round1_gists"))
    r2 = _gists(state.get("round2"), state.get("round2_gists"))
    positions = []
    for key in keys or list(r1):
        one, two = r1.get(key) or {}, r2.get(key) or {}
        if not (one.get("headline") or two.get("headline")):
            continue
        positions.append({"seat": key, "agent": names.get(key, key),
                          "position": one.get("headline") or "",
                          "points": list(one.get("points") or [])[:_POINTS],
                          "debate": two.get("headline") or "",
                          "debate_points": list(two.get("points") or [])[:_POINTS]})
    synthesis = str(state.get("synthesis") or "")
    brief = split_brief(run.get("directive"), constraints.get("brief"))["brief"]
    return {
        "run_id": str(run["id"]), "meeting_id": run.get("meeting_id"),
        "title": meeting_title(run.get("title"), run.get("directive")),
        "href": href(str(run["project_id"]), area="meetings", meeting=str(run["id"])),
        "agenda": {"brief": str(brief or "").strip()[:1200],
                   "participants": [names.get(k, k) for k in keys] + [FACILITATOR_TITLE]},
        "positions": positions,
        "dissent": _dissent(state, synthesis),
        "synthesis": synthesis,
    }


def minutes_markdown(summary: dict, *, prd_path: str = "") -> str:
    """The meeting minutes committed beside the PRD (docs/prd/<slug>-meeting.md)."""
    out = [f"# Meeting minutes · {summary.get('title') or 'PRD panel'}", ""]
    if prd_path:
        out += [f"The panel that wrote [{prd_path.rsplit('/', 1)[-1]}]({prd_path.rsplit('/', 1)[-1]}).", ""]
    agenda = summary.get("agenda") or {}
    out += ["## Agenda", "", str(agenda.get("brief") or "(no brief recorded)"), ""]
    if agenda.get("participants"):
        out += ["**Panel:** " + ", ".join(agenda["participants"]), ""]
    out += ["## Each agent's position", ""]
    for p in summary.get("positions") or []:
        out.append(f"### {p['agent']}")
        out.append("")
        if p.get("position"):
            out.append(f"**Position:** {p['position']}")
        out += [f"- {x}" for x in p.get("points") or []]
        if p.get("debate"):
            out += ["", f"**In debate:** {p['debate']}"]
            out += [f"- {x}" for x in p.get("debate_points") or []]
        out.append("")
    out += ["## Dissent", ""]
    dissent = summary.get("dissent") or []
    out += ([f"- {(d.get('role') + ': ') if d.get('role') else ''}{d.get('position') or ''}"
             f"{(' — ' + d['reason']) if d.get('reason') else ''}" for d in dissent]
            or ["None recorded."])
    out += ["", "## Synthesis", "", str(summary.get("synthesis") or "(none)").strip(), ""]
    return "\n".join(out)


def _repo_of(connection, project_id: str) -> Optional[str]:
    try:
        return connection.execute(text(
            "SELECT full_name FROM github_repository_bindings WHERE project_id = :p AND "
            "status = 'active' ORDER BY (role = 'primary') DESC, created_at LIMIT 1"),
            {"p": project_id}).scalar()
    except Exception:  # noqa: BLE001
        return None


def file_link(project_id: str, full_name: Optional[str], path: str, ref: str = "HEAD") -> Optional[str]:
    """GitHub's page for the file, or BOSS's own document drawer for a local repository."""
    from .project_experience.common import project_href
    from .repo_links import blob_url, is_local_repo

    if full_name and not is_local_repo(full_name):
        return blob_url(full_name, path, ref)
    return project_href(project_id, tab="knowledge", drawer=f"doc:{path}")


def links(connection, decision: dict) -> Optional[dict]:
    """The PRD's record for a decision payload, or None when the decision is not a meeting's PRD.

    Never raises: a decision card renders without its links rather than not at all."""
    try:
        rec = _j(decision.get("recommendation"), {}) or {}
        if not _is_prd_decision(rec):
            return None
        args = rec.get("args") or {}
        project_id = str(decision.get("project_id") or "")
        title = str(args.get("title") or "PRD")
        where = paths(title)
        run = _meeting_row(connection, str(decision.get("id")))
        meeting = meeting_summary(connection, run) if run else None
        committed = (_j(run.get("finalize_ctx"), {}) or {}).get("prd_commit") if run else None
        full = (committed or {}).get("repository") or _repo_of(connection, project_id)
        repo: dict[str, Any] = {"path": where["prd"], "minutes_path": where["minutes"],
                                "repository": full, "committed": bool(committed)}
        if committed:
            branch = str(committed.get("branch") or "")
            repo.update({"branch": branch, "commit": committed.get("commit"),
                         "url": file_link(project_id, full, committed.get("path") or where["prd"], branch),
                         "minutes_url": file_link(project_id, full,
                                                  committed.get("minutes_path") or where["minutes"], branch)})
        return {
            "title": re.sub(r"^\s*PRD\s*[:·-]\s*", "", title, flags=re.I) or title,
            "markdown": str(args.get("markdown") or ""),
            "full_page": (meeting or {}).get("href") or None,
            "meeting": meeting,
            "repo": repo,
        }
    except Exception:  # noqa: BLE001 — never block a card on its links
        logger.warning("PRD links failed for %s", decision.get("id"), exc_info=True)
        return None


def commit_to_branch(connection, host: Any, *, project_id: str, decision_id: str,
                     branch: str) -> Optional[dict]:
    """Commit the approved PRD and its meeting minutes to the build's branch, through `host` —
    the repo host the build's stages commit through (GitHubAdapter or LocalGitAdapter). Records
    where it landed on the meeting run, so every PRD link names the file. Idempotent: a branch
    that already holds the same PRD is not committed to again. None when there is nothing to
    commit (not a meeting PRD)."""
    row = connection.execute(text(
        "SELECT id, project_id, recommendation, status FROM decisions WHERE id = :d AND project_id = :p"),
        {"d": str(decision_id), "p": project_id}).mappings().first()
    if not row:
        return None
    rec = _j(row["recommendation"], {}) or {}
    if not _is_prd_decision(rec):
        return None
    args = rec.get("args") or {}
    markdown = str(args.get("markdown") or "").strip()
    if not markdown:
        return None
    where = paths(args.get("title"))
    run = _meeting_row(connection, str(decision_id))
    files: dict[str, str] = {where["prd"]: markdown + "\n"}
    if run:
        files[where["minutes"]] = minutes_markdown(meeting_summary(connection, run),
                                                   prd_path=where["prd"])
    reader = getattr(host, "read_text_file", None)
    commit = None
    for path, content in files.items():
        try:
            if reader is not None and reader(branch, path) == content:
                continue
        except Exception:  # noqa: BLE001 — unreadable means write it
            pass
        commit = host.upsert_file(branch, path, content, f"docs(prd): add {path}")
    record = {"path": where["prd"], "minutes_path": where["minutes"] if run else None,
              "branch": branch, "commit": commit,
              "repository": getattr(host, "repository_full_name", None), "decision_id": str(decision_id)}
    if run:
        connection.execute(text(
            "UPDATE meeting_runs SET finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) || "
            "jsonb_build_object('prd_commit', CAST(:r AS jsonb)) WHERE id = :id"),
            {"r": json.dumps(record), "id": run["id"]})
    return record


def for_decision_id(connection, decision_id: Any, *, with_meeting: bool = True) -> Optional[dict]:
    """links() for a decision named by id (a meeting room, a build's PRD step, a work story)."""
    if not decision_id:
        return None
    try:
        row = connection.execute(text(
            "SELECT id, project_id, recommendation FROM decisions WHERE id = :d"),
            {"d": str(decision_id)}).mappings().first()
    except Exception:  # noqa: BLE001
        return None
    out = links(connection, dict(row)) if row else None
    if out and not with_meeting:
        out = {**out, "meeting": None, "markdown": ""}
    return out
