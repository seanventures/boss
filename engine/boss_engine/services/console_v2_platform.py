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

from typing import Any

from sqlalchemy import text as sql


def workflows(connection, project_id: str) -> dict[str, Any]:
    """The processes this company runs, and the stages inside them.

    The artifact offers 'Deploy version' and a version number. BOSS's templates are not
    versioned — there is one active spec per process_key — so that control is absent rather
    than rendered as a button that does nothing. Saying "not versioned" is more useful than a
    fake 1.3.
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

    items = []
    # WHETHER GATING IS RECORDED AT ALL, ACROSS EVERY TEMPLATE.
    #
    # The live template has eleven steps and not one carries a gating field. Reporting "0
    # stages stop for you" from that asserts that nothing does -- when the spec simply does not
    # say. BOSS's human gate lives in `decisions`, not on a step, so the absence is about the
    # spec's vocabulary rather than about the process.
    #
    # Same absence-vs-zero mistake as per-agent spend and "Verified today: 100": a field that
    # was never populated must not be rendered as a measured zero.
    gates_recorded = False
    for row in rows:
        for step in ((row.get("spec") or {}).get("steps") or []):
            if isinstance(step, dict) and ("gated" in step or "requires_approval" in step):
                gates_recorded = True
                break

    for row in rows:
        spec = row.get("spec") or {}
        steps = spec.get("steps") or spec.get("stages") or []
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
            "stages": [
                {
                    "key": str(s.get("id") or s.get("key") or f"step-{i}"),
                    "action": str(s.get("action") or ""),
                    "role": str(s.get("role") or ", ".join(s.get("roles") or [])),
                    # None, not False, when the spec has no gating vocabulary at all: the
                    # screen must be able to tell "does not stop" from "does not say".
                    "gated": (
                        bool(s.get("gated") or s.get("requires_approval"))
                        if gates_recorded else None
                    ),
                }
                for i, s in enumerate(steps)
                if isinstance(s, dict)
            ],
        })

    return {
        "project_id": project_id,
        "workflows": items,
        # Stated so the screen does not have to guess whether to draw a version control.
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
    }


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
            SELECT id, app_name, repo, environment, target_kind, target_ref, status
            FROM deployment_targets
            WHERE project_id = :p
            ORDER BY app_name, environment
            """
        ),
        {"p": project_id},
    ).mappings().all()]

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

    servers = [dict(r) for r in connection.execute(
        sql(
            """
            SELECT id, name, url, transport, enabled, risk_tier, data_boundary
            FROM mcp_servers
            WHERE project_id = :p
            ORDER BY name
            """
        ),
        {"p": project_id},
    ).mappings().all()]

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
                "risk_tier": int(s.get("risk_tier") or 1),
                "data_boundary": s.get("data_boundary") or "internal",
            }
            for s in servers
        ],
        # Absent, with the reason — not invented. See the docstring.
        "load_recorded": False,
        "load_note": "BOSS records host verification, not utilisation. There is no load to show.",
    }


#: Settings the artifact offers that this deployment genuinely does not have. Each is reported
#: as absent WITH a reason: an empty SSO form implies SSO is configurable here and merely
#: unconfigured, which is a different and false claim.
UNAVAILABLE_SETTINGS = (
    {
        "id": "sso",
        "label": "SSO & identity",
        "reason": "This deployment authenticates with an API key. There is no SAML or SCIM to configure.",
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
