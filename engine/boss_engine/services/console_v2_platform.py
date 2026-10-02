"""Workflows, Infra & Models, Settings — the last three v2 screens.

Grouped in one module because they are all "what this company is CONFIGURED with", as opposed
to "what it is doing right now", and they share the same discipline: show what is really there,
say plainly what is not, and never render a control that cannot act.

WHAT THE ARTIFACT SHOWS vs WHAT BOSS HAS
-----------------------------------------
The artifact's data is a demo company — five named clouds, ten integrations, four budget lines,
five repos, three roles, two API keys. BOSS's equivalents exist but are fewer and differently
named, and several have no equivalent at all.

    artifact                 BOSS
    ---------------------    ------------------------------------------------
    workflow + stages        agent_process_templates + spec.steps
    deploy version           n/a — templates are not versioned
    fleet (clouds, load)     deployment_hosts + deployment_targets
    budgets (spend/cap)      llm_calls against the budget policy
    integrations             mcp_servers
    repos                    the repos code tasks actually target
    CI rows                  the pipeline's own gates
    SSO fields               n/a on this deployment
    members / roles          n/a — BOSS has agent roles, not human seats
    API keys                 n/a as a listable resource

Everything with an "n/a" is reported as absent WITH the reason. A settings page that renders
an empty SSO form implies SSO is configurable here and merely unconfigured, which is a
different and false claim.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text as sql


#: What a stage DOES, derived from the spec rather than labelled by hand.
#:
#: The spec records `kind`, `next` and `wait_for` — which is a DAG, not a list. An earlier
#: version read only the array order and rendered eleven identical lines, losing the fact that
#: three reviews run at once and one step waits for all of them. The shape was in the data the
#: whole time.
PANEL, PARALLEL, JOIN, SEQUENTIAL = "PANEL", "PARALLEL", "JOIN", "SEQUENTIAL"


def _stage_kind(step: dict) -> str:
    """PANEL, PARALLEL, JOIN or SEQUENTIAL — in that order of precedence.

    A panel is a panel whatever its edges. Otherwise a fan-out is PARALLEL, a fan-in is JOIN,
    and anything else runs on its own.
    """
    if str(step.get("kind") or "") == "panel":
        return PANEL
    if len(step.get("next") or []) > 1:
        return PARALLEL
    if len(step.get("wait_for") or []) > 1:
        return JOIN
    return SEQUENTIAL


def _order_steps(steps: list[dict], entry: Optional[str]) -> list[dict]:
    """Execution order, walking the graph from its entry point.

    The array order is authoring order and can differ from what actually runs. Falls back to the
    array when the graph is incomplete rather than dropping steps: a spec written by hand or by
    an older version must still render.
    """
    by_id = {str(s.get("id")): s for s in steps if isinstance(s, dict) and s.get("id")}
    if not by_id:
        return [s for s in steps if isinstance(s, dict)]

    ordered: list[dict] = []
    seen: set[str] = set()
    queue = [entry] if entry and entry in by_id else [next(iter(by_id))]
    while queue:
        key = queue.pop(0)
        if key in seen or key not in by_id:
            continue
        seen.add(key)
        ordered.append(by_id[key])
        queue.extend(str(n) for n in (by_id[key].get("next") or []))

    # Anything the walk never reached is unreachable from the entry — shown, not hidden, because
    # a step that cannot run is worth seeing.
    for key, step in by_id.items():
        if key not in seen:
            step = {**step, "_unreachable": True}
            ordered.append(step)
    return ordered


def workflows(connection, project_id: str) -> dict[str, Any]:
    """The processes this company runs, their stages, and how those stages fit together.

    The reference offers 'Deploy version' and a version number. BOSS's templates are not
    versioned -- there is one active spec per process_key -- so that control is absent rather
    than rendered as a button that does nothing.
    """
    rows = [dict(r) for r in connection.execute(
        sql(
            """
            SELECT t.id, t.process_key, t.name, t.description, t.spec, t.status,
                   t.created_by, t.updated_at,
                   count(r.id) FILTER (WHERE r.created_at > now() - interval '30 days') AS runs_30d,
                   count(r.id) FILTER (WHERE r.status = 'running') AS running_now,
                   max(r.created_at) AS last_run_at
            FROM agent_process_templates t
            LEFT JOIN agent_process_runs r ON r.template_id = t.id
            WHERE t.project_id = :p
            GROUP BY t.id
            ORDER BY t.name
            """
        ),
        {"p": project_id},
    ).mappings().all()]

    gates_recorded = False
    for row in rows:
        for step in ((row.get("spec") or {}).get("steps") or []):
            if isinstance(step, dict) and ("gated" in step or "requires_approval" in step):
                gates_recorded = True
                break

    items = []
    for row in rows:
        spec = row.get("spec") or {}
        raw_steps = spec.get("steps") or spec.get("stages") or []
        ordered = _order_steps([s for s in raw_steps if isinstance(s, dict)], spec.get("entry"))

        stages = []
        for step in ordered:
            kind = _stage_kind(step)
            stages.append({
                "key": str(step.get("id") or step.get("key") or ""),
                "action": str(step.get("action") or ""),
                "role": str(step.get("role") or ", ".join(step.get("roles") or [])),
                "kind": kind,
                # Where control goes next, and what this stage waits on. This is the structure
                # the reference conveys with its PARALLEL / SEQUENTIAL badges, and BOSS has it.
                "next": [str(n) for n in (step.get("next") or [])],
                "waits_for": [str(w) for w in (step.get("wait_for") or [])],
                "unreachable": bool(step.get("_unreachable")),
                "gated": (
                    bool(step.get("gated") or step.get("requires_approval"))
                    if gates_recorded else None
                ),
            })

        items.append({
            "id": row["id"],
            "key": row["process_key"],
            "name": row["name"],
            "description": row.get("description") or "",
            "status": row.get("status") or "active",
            "created_by": row.get("created_by"),
            "runs_30d": int(row.get("runs_30d") or 0),
            "running_now": int(row.get("running_now") or 0),
            "last_run_at": row["last_run_at"].isoformat() if row.get("last_run_at") else None,
            "stages": stages,
            # The reference's summary line. Counted, never estimated.
            "panels": sum(1 for s in stages if s["kind"] == PANEL),
            "parallel_points": sum(1 for s in stages if s["kind"] == PARALLEL),
            "roles": sorted({s["role"] for s in stages if s["role"]}),
        })

    return {
        "project_id": project_id,
        "workflows": items,
        "gates_recorded": gates_recorded,
        "gates_note": (
            ""
            if gates_recorded
            else "These specs do not record which stages stop for a human. BOSS's approval "
                 "gates live under Decisions."
        ),
        "versioned": False,
        "version_note": (
            "BOSS keeps one active spec per process. There are no versions to deploy or roll "
            "back — editing a template changes it for the next run."
        ),
        # No editing API exists, so the reference's + Add stage / Deploy version / remove-stage
        # controls are absent rather than drawn as buttons that cannot act.
        "editable": False,
    }


def _host_for(target_ref: str, hosts: list[dict]) -> Optional[str]:
    """Which registered host a target deploys to, matched on the address inside its ref.

    A target_ref is "root@192.0.2.202:22" and a host records "192.0.2.202". Matched on the address
    rather than parsed as a URL, because the ref is an ssh destination and target_kind is not
    always ssh. Returns None rather than guessing: an app shown under the wrong machine is worse
    than an app shown under none.
    """
    for h in hosts:
        addr = str(h.get("address") or "").strip()
        if addr and addr in target_ref:
            return str(h.get("name") or addr)
    return None


def infra(connection, project_id: str) -> dict[str, Any]:
    """Hosts, deployment targets, and what the models cost.

    The artifact shows a fleet with load percentages. BOSS records hosts and their verification
    state but not utilisation, so load is absent rather than invented — a fabricated 62% would
    be indistinguishable from a real one, and this console has already shipped one number that
    was quietly made up.
    """
    hosts = [dict(r) for r in connection.execute(
        sql(
            """
            SELECT id, name, address, arch, status, labels, created_at
            FROM deployment_hosts
            WHERE project_id = :p
            ORDER BY name
            """
        ),
        {"p": project_id},
    ).mappings().all()]

    targets = [dict(r) for r in connection.execute(
        sql(
            """
            SELECT id, app_name, repo, environment, target_kind, target_ref, status, domains
            FROM deployment_targets
            WHERE project_id = :p
            ORDER BY app_name, environment
            """
        ),
        {"p": project_id},
    ).mappings().all()]

    # The latest deploy per target, in ONE query. The v1 CI/CD screen shows change -> merged ->
    # deploy -> live for every app; without this the v2 screen could say a target existed and
    # nothing about whether anything had ever landed on it.
    latest: dict[str, dict] = {}
    for row in connection.execute(
        sql(
            """
            SELECT DISTINCT ON (deployment_target_id)
                   deployment_target_id, status, commit_ref, finished_at, failure_reason
            FROM deployment_runs
            WHERE project_id = :p
            ORDER BY deployment_target_id, created_at DESC
            """
        ),
        {"p": project_id},
    ).mappings().all():
        latest[str(row["deployment_target_id"])] = dict(row)

    # Model spend for the month, by model. This IS attributable — an llm_call records its own
    # model — unlike per-agent spend, which is not recorded and is therefore not shown.
    models = [dict(r) for r in connection.execute(
        sql(
            """
            SELECT model,
                   count(*) AS calls,
                   COALESCE(sum(cost_usd_micros), 0) AS micros,
                   COALESCE(sum(total_tokens), 0) AS tokens,
                   bool_or(price_estimated) AS any_estimated
            FROM llm_calls
            WHERE project_id = :p
              AND created_at > date_trunc('month', now())
            GROUP BY model
            ORDER BY sum(cost_usd_micros) DESC
            LIMIT 20
            """
        ),
        {"p": project_id},
    ).mappings().all()]

    # MEETING PANELS, said beside the model table. boss-1, 2026-10-02: v2 read "$0.00 on models
    # this month / No model calls recorded this month" while v1's meeting cards showed $0.87-$1.02
    # each. Both were true — the month had just turned, and a panel's cost is recorded on the
    # meeting run — but side by side they read as one console lying. So the month's start is
    # stated, and the panels' own recorded spend is shown as its own line, never added to the
    # per-model sum (it may overlap it).
    month = connection.execute(
        sql(
            """
            SELECT date_trunc('month', now()) AS month_start,
                   count(*) AS runs,
                   COALESCE(sum((usage->>'cost_usd_micros')::bigint), 0) AS micros
            FROM meeting_runs
            WHERE project_id = :p AND created_at > date_trunc('month', now())
            """
        ),
        {"p": project_id},
    ).mappings().all()
    month = dict(month[0]) if month else {}

    # Through the server model (mcp_registry), like every other MCP screen, so this view says
    # what the others say about a server — including whether it answered at the last check.
    from .mcp_registry import list_mcp_servers

    servers = sorted(list_mcp_servers(connection, project_id), key=lambda s: s["name"])

    return {
        "project_id": project_id,
        "hosts": [
            {
                "id": h["id"],
                "name": h["name"],
                # The address is where deployments land, so it is shown; it is infrastructure
                # the operator owns, not a secret.
                "address": h.get("address"),
                "arch": h.get("arch"),
                "status": h.get("status") or "unknown",
                # Only a verified host can take a deployment. Showing them alike would offer a
                # destination that fails at deploy time, far from the choice that caused it.
                "verified": str(h.get("status")) == "verified",
            }
            for h in hosts
        ],
        "targets": [
            {
                "id": t["id"],
                "app_name": t["app_name"],
                "repo": t.get("repo"),
                "environment": t.get("environment"),
                "kind": t.get("target_kind"),
                "ref": t.get("target_ref"),
                "status": t.get("status") or "unknown",
                "retired": str(t.get("status")) == "retired",
                # Where it actually answers. v1 links to the running app from here and v2 could
                # not, because this column was never selected.
                "domains": [str(d) for d in (t.get("domains") or []) if d],
                "url": (f"http://{[str(d) for d in (t.get('domains') or []) if d][0]}"
                        if [d for d in (t.get("domains") or []) if d] else None),
                # Which host this lands on, so the screen can group apps under their machine
                # instead of listing two flat lists that the reader has to join by eye.
                "host": _host_for(str(t.get("target_ref") or ""), hosts),
                "last_deploy": (
                    {
                        "status": str(latest[t["id"]].get("status") or ""),
                        "commit": (str(latest[t["id"]]["commit_ref"])[:10]
                                   if latest[t["id"]].get("commit_ref") else None),
                        "finished_at": (latest[t["id"]]["finished_at"].isoformat()
                                        if latest[t["id"]].get("finished_at") else None),
                        "failure_reason": (str(latest[t["id"]].get("failure_reason"))[:200]
                                           if latest[t["id"]].get("failure_reason") else None),
                    }
                    if t["id"] in latest else None
                ),
            }
            for t in targets
        ],
        "models": [
            {
                "model": m.get("model") or "(unrecorded)",
                "calls": int(m.get("calls") or 0),
                "tokens": int(m.get("tokens") or 0),
                "spend_usd": round(int(m.get("micros") or 0) / 1_000_000, 2),
                # A price the price-map did not know is a guess, and the screen says so rather
                # than presenting it beside measured figures as if it were one.
                "estimated": bool(m.get("any_estimated")),
            }
            for m in models
        ],
        "integrations": [
            {
                "id": s["id"],
                "name": s["name"],
                "url": s.get("url"),
                "transport": s.get("transport"),
                "enabled": bool(s.get("enabled")),
                # Not `int(x or 1)`: tier 0 is falsy, and a read-only server was shown as tier 1.
                "risk_tier": int(s["risk_tier"]),
                "data_boundary": s.get("data_boundary") or "internal",
                "trusted": bool(s.get("trusted")),
                "reachable": s.get("reachable"),
                "status_note": s.get("status_note"),
            }
            for s in servers
        ],
        "month_start": (month["month_start"].date().isoformat()
                        if month.get("month_start") is not None else None),
        "meeting_panels": {
            "runs": int(month.get("runs") or 0),
            "spend_usd": round(int(month.get("micros") or 0) / 1_000_000, 2),
        },
        # Absent, with the reason — not invented. See the docstring.
        "load_recorded": False,
        "load_note": "BOSS records host verification, not utilisation. There is no load to show.",
    }


#: WHAT SIGN-IN USES, word for word what the v1 Identity area says (apps/web/lib/sign-in-truth.ts;
#: a web test fails if they drift). This used to say the deployment had only an API key and
#: nothing identity-related to configure, while v1 offered SAML and SCIM forms — two consoles,
#: two answers (console walk, 2026-10-02).
SIGN_IN_TRUTH = (
    "Console sign-in is the one password the installer generated. Identity providers recorded "
    "here are kept as configuration, and SCIM can sync members into BOSS, but the console's "
    "sign-in page does not use them yet."
)

#: Settings the artifact offers that this deployment genuinely does not have. Each is reported
#: as absent WITH a reason: an empty SSO form implies SSO is configurable here and merely
#: unconfigured, which is a different and false claim.
UNAVAILABLE_SETTINGS = (
    {
        "id": "sso",
        "label": "SSO & identity",
        "reason": SIGN_IN_TRUTH + " Record providers in the v1 console under Identity.",
    },
    {
        "id": "members",
        "label": "Members & seats",
        "reason": "BOSS models AGENT roles, not human seats. Access is the API key, held by whoever holds it.",
    },
    {
        "id": "api_keys",
        "label": "API keys",
        "reason": "Keys are held in the platform secret store and are not listable through the console.",
    },
)


def settings(connection, project_id: str) -> dict[str, Any]:
    """What is configured, and what this deployment simply does not have.

    The honesty here is the feature. A settings screen that renders empty forms for things it
    cannot configure teaches the operator that the console is decorative.
    """
    roles = [dict(r) for r in connection.execute(
        sql(
            """
            SELECT role_key, display_name, autonomy_level, trust_score
            FROM roles
            WHERE project_id = :p
            ORDER BY display_name
            """
        ),
        {"p": project_id},
    ).mappings().all()]

    repos = [dict(r) for r in connection.execute(
        sql(
            """
            SELECT spec->>'owner' AS owner, spec->>'repo' AS repo,
                   count(*) AS runs,
                   count(*) FILTER (WHERE pull_request_url IS NOT NULL) AS prs,
                   max(created_at) AS last_run_at
            FROM code_task_runs
            WHERE project_id = :p
              AND spec->>'repo' IS NOT NULL
            GROUP BY 1, 2
            ORDER BY count(*) DESC
            LIMIT 20
            """
        ),
        {"p": project_id},
    ).mappings().all()]

    return {
        "project_id": project_id,
        "roles": [
            {
                "key": r["role_key"],
                "name": r.get("display_name") or r["role_key"],
                "autonomy_level": int(r.get("autonomy_level") or 0),
                "trust_score": int(r.get("trust_score") or 0),
            }
            for r in roles
        ],
        "repos": [
            {
                "name": f"{r.get('owner') or '?'}/{r.get('repo') or '?'}",
                "runs": int(r.get("runs") or 0),
                "prs": int(r.get("prs") or 0),
                "last_run_at": r["last_run_at"].isoformat() if r.get("last_run_at") else None,
            }
            for r in repos
        ],
        "unavailable": list(UNAVAILABLE_SETTINGS),
    }
