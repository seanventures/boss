"""The set gate: do the PRs of a change fit together?

Run when every lane is ready, and again after any push to any PR of the set. Two kinds of check:

  DETERMINISTIC (BOSS reads GitHub itself)
    * Every PR links the others — each description lists the whole set.
    * Builds in dependency order — every lane that depends on another was built against it
      (started after it was ready) and nothing it needs is missing from the set.
  REVIEWED (a reviewer model reads every PR's diff together, read-only)
    * Contract vs implementation vs callers, version pins, migration and deploy order — whatever
      the reviewer finds, as named checks with pass/fail and a sentence each.

FAIL CLOSED: a reviewer that cannot run, or answers with no checks, is a failed check ("could not
review"), never a pass — the set waits rather than ship something nobody compared.
"""
from __future__ import annotations

from typing import Any

from ..services.core import make_id
from .common import MultiRepoError, jdump, sql

_MAX_PATCH = 6000


def _diffs(db: Any, settings: Any, cs: dict[str, Any], touched: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, str]]:
    from . import github

    diffs: dict[str, str] = {}
    bodies: dict[str, str] = {}
    for lane in touched:
        try:
            repo = github.Repo(db, settings, cs["project_id"], cs["id"], lane["full_name"], access="read")
            bodies[lane["full_name"]] = str(repo.pull(lane["pr_number"]).get("body") or "")
            parts = []
            for f in repo.files(lane["pr_number"]):
                parts.append(f"--- {f.get('filename')} (+{f.get('additions', 0)} -{f.get('deletions', 0)})\n"
                             f"{str(f.get('patch') or '')[:1500]}")
            diffs[lane["full_name"]] = "\n".join(parts)[:_MAX_PATCH]
        except MultiRepoError as error:
            diffs[lane["full_name"]] = f"(could not read the diff: {error.message})"
            bodies.setdefault(lane["full_name"], "")
    return diffs, bodies


def _links(touched: list[dict[str, Any]], bodies: dict[str, str]) -> dict[str, Any]:
    missing = []
    for lane in touched:
        body = bodies.get(lane["full_name"], "")
        others = [x for x in touched if x["full_name"] != lane["full_name"]]
        if "<!-- boss:change-set -->" not in body or any(f"{x['full_name']}#{x['pr_number']}" not in body for x in others):
            missing.append(lane["full_name"])
    n = len(touched)
    return {"name": "Every PR links the others", "kind": "links", "pass": not missing,
            "detail": (f"{n} of {n} descriptions list the set" if not missing
                       else f"{', '.join(missing)} does not list the others")}


def _order(db: Any, cs: dict[str, Any], touched: list[dict[str, Any]]) -> dict[str, Any]:
    from . import change_sets

    deps = change_sets._repo_deps(db, cs)  # noqa: SLF001
    by = {x["full_name"]: x for x in change_sets._lanes(db, cs["id"])}  # noqa: SLF001
    problems = []
    for lane in touched:
        for dep in deps.get(lane["full_name"], []):
            d = by.get(dep)
            if d is None or d["lane_state"] not in ("ready", "unchanged", "merged"):
                problems.append(f"{lane['full_name']} needs {dep}, which is not ready")
    chain = " → ".join(x["full_name"].split("/")[-1] for x in sorted(touched, key=lambda x: x["wave"]))
    return {"name": "Builds and deploys in dependency order", "kind": "order", "pass": not problems,
            "detail": chain if not problems else "; ".join(problems)}


def _review_prompt(cs: dict[str, Any], touched: list[dict[str, Any]], diffs: dict[str, str]) -> str:
    from . import change_sets  # noqa: F401

    plan = cs.get("plan") or {}
    if isinstance(plan, str):
        import json

        plan = json.loads(plan or "{}")
    planned = "\n".join(f"- {c.get('name')}: {c.get('change')} (interface: {c.get('interface') or '—'})"
                        for c in (plan.get("components") or []))
    prs = "\n\n".join(f"=== {x['full_name']} PR #{x['pr_number']} (components: {', '.join(x['components'] or [])})\n"
                      f"{diffs.get(x['full_name'], '')}" for x in sorted(touched, key=lambda x: x["wave"]))
    return ("BOSS CROSS-REPO CHECK\n"
            f"Change: {cs['title']}\nPlan:\n{planned}\n\nThe pull requests, in dependency order:\n{prs}\n\n"
            "Do these changes fit together? Check at least: every interface the contracts/provider side adds is "
            "implemented; callers use only what the contract defines; dependents pin to the same build of what "
            "they depend on; migrations and deploy order are safe (old callers keep working while waves roll out).\n"
            'Answer JSON: {"checks": [{"name": "<short check>", "detail": "<one sentence with the evidence>", '
            '"pass": true}]}')


def run(db: Any, settings: Any, cs: dict[str, Any], touched: list[dict[str, Any]]) -> dict[str, Any]:
    from . import change_sets

    diffs, bodies = _diffs(db, settings, cs, touched)
    results = [_links(touched, bodies), _order(db, cs, touched)]
    reviewed: list[dict[str, Any]] = []
    try:
        raw = change_sets.MODEL(db, settings, cs["project_id"], f"cross_check:{cs['id']}",
                                _review_prompt(cs, touched, diffs))
        parsed = change_sets._json_in(raw)  # noqa: SLF001
        for item in (parsed or {}).get("checks") or [] if isinstance(parsed, dict) else []:
            if isinstance(item, dict) and str(item.get("name") or "").strip():
                reviewed.append({"name": str(item["name"])[:120], "kind": "review",
                                 "detail": str(item.get("detail") or "")[:400], "pass": item.get("pass") is True})
        if not reviewed:
            reviewed.append({"name": "Contracts, implementation and callers agree", "kind": "review", "pass": False,
                             "detail": "The reviewer returned no checks, so nothing was compared. Re-run the check."})
    except Exception as error:  # noqa: BLE001 — fail closed, in words
        reviewed.append({"name": "Contracts, implementation and callers agree", "kind": "review", "pass": False,
                         "detail": f"The cross-repo reviewer could not run ({type(error).__name__}); the set waits."})
    results.extend(reviewed[:12])
    verdict = "pass" if all(r["pass"] for r in results) else "fail"
    heads = {x["full_name"]: x["head_sha"] for x in touched}
    db.execute(sql("INSERT INTO cross_repo_checks (id, change_set_id, head_shas, results, verdict) VALUES "
                   "(:i, :c, CAST(:h AS JSONB), CAST(:r AS JSONB), :v)"),
               {"i": make_id("crc"), "c": cs["id"], "h": jdump(heads), "r": jdump(results), "v": verdict})
    return change_sets.latest_check(db, cs["id"]) or {"verdict": verdict, "results": results}
