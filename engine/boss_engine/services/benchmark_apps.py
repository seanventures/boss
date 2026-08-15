"""Pinned full-stack application benchmarks for the governed coding pipeline.

The benchmarks are deliberately small enough to run in an isolated coding sandbox,
but each crosses planning, persistence, server behavior, browser behavior, and
verification.  They are task inputs, not pre-certified demos: only durable run
evidence may mark a benchmark as passed.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


COMPLETION_CONTRACT = [
    "Written implementation plan is attached to the governed run.",
    "Working frontend and backend with database schema, migrations, and seed data.",
    "Client and server validation, API tests, and at least one browser-level end-to-end test.",
    "One-command local startup, health-check endpoint, lint, type-check, and concise README.",
    "Automated final verification report records actual command outcomes and no required TODOs remain.",
]

# Scores are a tuning instrument, not a replacement for acceptance evidence. The
# numeric baselines are deliberately explicit and may be revised only alongside
# a benchmark version; missing evidence remains unscored.
SCORECARD_VERSION = "1.1"

SCORING_DIMENSIONS = (
    {
        "id": "functionality",
        "label": "Functionality",
        "weight": 18,
        "evidence": "Authoritative benchmark verification and acceptance-path tests.",
    },
    {
        "id": "capability",
        "label": "Capability coverage",
        "weight": 10,
        "evidence": "Scenario-to-test mapping for each original-spec capability.",
    },
    {
        "id": "interface",
        "label": "Interface coverage",
        "weight": 6,
        "evidence": "Browser evidence covering responsive, loading, empty, error, and keyboard states.",
    },
    {
        "id": "ui",
        "label": "UI craft",
        "weight": 8,
        "evidence": "Visual hierarchy, contrast, empty/error/loading treatment, and automated a11y violations.",
    },
    {
        "id": "ux",
        "label": "UX / task success",
        "weight": 10,
        "evidence": "JTBD walkthroughs plus rubric scores for task-success, clarity, trust, delight, and accessibility.",
    },
    {
        "id": "quality",
        "label": "App quality",
        "weight": 12,
        "evidence": "Tests, independent review, security, and consistency evidence.",
    },
    {
        "id": "speed",
        "label": "Speed",
        "weight": 6,
        "evidence": "Recorded end-to-end governed-run duration against the 15-minute baseline.",
    },
    {
        "id": "efficiency",
        "label": "Efficiency",
        "weight": 6,
        "evidence": "Acceptance scenarios delivered per agent turn and per verification attempt.",
    },
    {
        "id": "cost",
        "label": "Cost discipline",
        "weight": 6,
        "evidence": "Recorded model cost against the $5 benchmark baseline.",
    },
    {
        "id": "tokens",
        "label": "Token consumption",
        "weight": 6,
        "evidence": "Recorded stage token usage against the 200k-token baseline.",
    },
    {
        "id": "effort",
        "label": "Implementation effort",
        "weight": 4,
        "evidence": "Recorded turns, changed files, and repair attempts.",
    },
    {
        "id": "innovation",
        "label": "Innovation",
        "weight": 4,
        "evidence": "Independent, cited design or product-review assessment; never model self-score.",
    },
    {
        "id": "reliability",
        "label": "Reliability",
        "weight": 4,
        "evidence": "Retry/repair behavior, regressions, and durable gate outcomes.",
    },
)

SCORE_BASELINES = {
    "duration_ms": 15 * 60 * 1000,
    "cost_usd_micros": 5_000_000,
    "tokens": 200_000,
    "turns": 80,
    "files_changed": 30,
}


_APPS: tuple[dict[str, Any], ...] = (
    {
        "id": "bugbox",
        "name": "BugBox",
        "tagline": "Mini issue tracker",
        "summary": "CRUD, search/filtering, persistence, dashboard statistics, and responsive issue workflows.",
        "focus": [
            "full-stack CRUD",
            "database migrations",
            "search and filtering",
            "browser persistence",
        ],
        "acceptance_scenarios": [
            "Start the app with one command, create an issue, and confirm it appears on the dashboard.",
            "Change an issue to completed and confirm the dashboard totals update after refresh.",
            "Search for the issue, delete it, and confirm it disappears.",
        ],
        "prompt": """Build a full-stack application called BugBox, a lightweight issue tracker for one software project. Include a dashboard, issue list, issue creation/editing, issue detail pages, status and priority filters, search, assignees, tags, and a basic immutable activity history. Persist data in a local database with migrations and seed data. Add client and server validation, loading, empty, and useful error states. The issue model must include title, description, priority, status, assignee, and tags. Include API tests and a browser end-to-end test covering creation, editing/status transition, filtering, refresh persistence, and deletion.""",
    },
    {
        "id": "hookwatch",
        "name": "HookWatch",
        "tagline": "Webhook inspector and replay tool",
        "summary": "Dynamic request ingestion, arbitrary payload handling, durable request history, polling, and guarded outbound replay.",
        "focus": [
            "dynamic server routes",
            "request parsing",
            "outbound integration",
            "durable event log",
        ],
        "acceptance_scenarios": [
            "Create an inbox and POST JSON with a custom header to its generated endpoint.",
            "Confirm method, headers, query, body, timestamp, and content type appear and persist after refresh.",
            "Replay to the built-in mock destination and record the response; clear-history removes captured events.",
        ],
        "prompt": """Build a full-stack application called HookWatch. Users create webhook inboxes with unique HTTP endpoints. Any POST to an inbox endpoint is persisted and displayed with method, headers, query parameters, body, timestamp, and content type. Support JSON and plain-text payloads. Add a request detail view, copy-payload action, clear-history action, and a replay action to a built-in mock destination that records response status. Poll for new events unless real-time support already exists. Include seed data, API tests, and a browser end-to-end test that creates an inbox, sends a webhook, views it, refreshes it, and replays it. Treat replay as an auditable, allowlisted test-destination operation only; do not expose arbitrary production egress.""",
    },
    {
        "id": "approveit",
        "name": "ApproveIt",
        "tagline": "Expense approval workflow",
        "summary": "Role-scoped workflow rules, transactional state transitions, immutable audit records, and aggregate reporting.",
        "focus": [
            "authorization",
            "state machine",
            "audit trail",
            "negative-path tests",
        ],
        "acceptance_scenarios": [
            "As requester, submit a positive $125 software expense and see it pending.",
            "As manager, approve with a comment; requester sees status, comment, and immutable audit history.",
            "Server rejects editing or re-approving a terminal request, self-approval, and rejection without a comment.",
        ],
        "prompt": """Build a full-stack application called ApproveIt for submitting and approving expense requests. Seed one requester and one manager and provide a visible role switcher instead of OAuth. A requester can create, view, and withdraw pending requests. A manager can approve or reject pending requests with a decision comment; rejections require a comment. Requests contain amount, vendor, category, date, and explanation. Amounts must be positive. Only pending requests can transition; a requester cannot approve their own request; terminal requests are immutable. Every creation, withdrawal, approval, and rejection writes an immutable audit-log entry. Include dashboards for pending counts and monetary totals, server-side authorization and validation, migrations, seed data, API tests, and a browser end-to-end requester-to-manager workflow including negative paths.""",
    },
)


def list_benchmark_apps() -> list[dict[str, Any]]:
    """Return catalog data without leaking mutable module-level state."""
    return [
        {
            **deepcopy(app),
            "completion_contract": list(COMPLETION_CONTRACT),
            "scoring_dimensions": [deepcopy(dimension) for dimension in SCORING_DIMENSIONS],
            "score_baselines": dict(SCORE_BASELINES),
            "verification_commands": [
                "npm run build",
                "npm run typecheck",
                "npm run lint",
                "npm run test:api",
                "npm run test:e2e",
                "npm run healthcheck",
            ],
        }
        for app in _APPS
    ]


def get_benchmark_app(benchmark_id: str) -> dict[str, Any] | None:
    target = (benchmark_id or "").strip().lower()
    return next((item for item in list_benchmark_apps() if item["id"] == target), None)


def benchmark_task_input(benchmark_id: str) -> dict[str, Any]:
    """Materialize the pinned task contract passed to the governed code-task runtime."""
    benchmark = get_benchmark_app(benchmark_id)
    if benchmark is None:
        raise ValueError(f"unknown QA benchmark app: {benchmark_id}")
    verification = benchmark["verification_commands"]
    instructions = (
        "This is a BOSS QA benchmark. Produce a real, repairable application, not a static mockup. "
        "Start by writing a concrete implementation plan before implementation. Use a local database "
        "and include schema migrations plus deterministic seed data. Do not leave required functionality "
        "as TODOs or pretend a check passed.\n\n"
        + benchmark["prompt"]
        + "\n\nSTANDARD COMPLETION CONTRACT:\n- "
        + "\n- ".join(COMPLETION_CONTRACT)
        + "\n\nREQUIRED PACKAGE SCRIPTS (each must be runnable and report its real result):\n- "
        + "\n- ".join(verification)
        + "\n\nThe README must state the one-command startup, migration/seed behavior, and the complete verification command. "
        "The health check must exercise a real running service rather than returning a hard-coded success response.\n"
        "UI/UX is scored, not optional: ship loading, empty, and error states; keep contrast and "
        "hierarchy readable; cover the acceptance scenarios as JTBD walkthroughs. Attach "
        "`ui_evidence` (a11y_violations, states_covered, contrast_ok) and `ux_evidence` "
        "(jtbd pass/fail plus rubric 1–5 for task_success, clarity, trust, delight, "
        "accessibility). Missing UI/UX evidence stays not_scored — do not invent scores."
    )
    return {
        "benchmark": benchmark,
        "title": f"QA benchmark: {benchmark['name']} — {benchmark['tagline']}",
        "instructions": instructions,
        "acceptance_criteria": [
            *benchmark["acceptance_scenarios"],
            *COMPLETION_CONTRACT,
        ],
        # The existing code-task executor runs one authoritative command. `verify` must invoke
        # every required script and makes a single durable, replayable evidence record.
        "test_commands": ["npm run verify"],
        "finalize_ctx": {
            "qa_benchmark": {
                "id": benchmark["id"],
                "name": benchmark["name"],
                "verification_commands": verification,
                "completion_contract": list(COMPLETION_CONTRACT),
                "scoring_dimensions": [
                    deepcopy(dimension) for dimension in SCORING_DIMENSIONS
                ],
                "score_baselines": dict(SCORE_BASELINES),
            }
        },
    }


def _measured_int(value: Any) -> int | None:
    """Accept a real integer count. Never coerce unknown/falsy to 0."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text or text in {"+", "-"}:
            return None
        digits = text[1:] if text[0] in {"+", "-"} else text
        if not digits.isdigit():
            return None
        return int(text)
    return None


def _measured_bool(value: Any) -> bool | None:
    """Accept an explicit bool only. Strings/ints stay unknown."""
    return value if isinstance(value, bool) else None


def _measured_rubric_score(value: Any) -> float | None:
    """Rubric 1–5. Reject bools (True is an int in Python)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if 1.0 <= number <= 5.0:
            return number
    return None


def honest_ui_evidence(blob: dict[str, Any] | None) -> dict[str, Any]:
    """Copy measurable UI fields. Never mint a11y_violations or contrast_ok."""
    src = blob if isinstance(blob, dict) else {}
    out: dict[str, Any] = {}
    states = src.get("states_covered") or src.get("browser_states")
    if isinstance(states, list):
        cleaned = [str(item).strip() for item in states if str(item).strip()]
        if cleaned:
            out["states_covered"] = cleaned
    markers = _measured_int(src.get("a11y_markers"))
    if markers is not None:
        out["a11y_markers"] = markers
    if isinstance(src.get("has_a11y_script"), bool):
        out["has_a11y_script"] = src["has_a11y_script"]
    violations = _measured_int(src.get("a11y_violations")) if "a11y_violations" in src else None
    if violations is not None and violations >= 0:
        out["a11y_violations"] = violations
    contrast = _measured_bool(src.get("contrast_ok")) if "contrast_ok" in src else None
    if contrast is not None:
        out["contrast_ok"] = contrast
    return out


def honest_ux_evidence(blob: dict[str, Any] | None) -> dict[str, Any]:
    """Copy JTBD ids. Unknown passed stays unknown. Never invent rubric scores."""
    src = blob if isinstance(blob, dict) else {}
    out: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    for item in src.get("jtbd") or src.get("walkthroughs") or []:
        if not isinstance(item, dict):
            continue
        row: dict[str, Any] = {}
        identity = item.get("id") or item.get("name")
        if identity:
            row["id"] = str(identity)[:80]
        if item.get("source"):
            row["source"] = str(item["source"])
        if item.get("passed") in {True, False}:
            row["passed"] = item["passed"]
        elif "passed" in item:
            row["passed"] = None
        if row:
            rows.append(row)
    if rows:
        out["jtbd"] = rows
    if isinstance(src.get("has_e2e"), bool):
        out["has_e2e"] = src["has_e2e"]
    rubric_src = src.get("rubric") if isinstance(src.get("rubric"), dict) else {}
    rubric = {
        key: score
        for key in ("task_success", "clarity", "trust", "delight", "accessibility")
        if (score := _measured_rubric_score(rubric_src.get(key))) is not None
    }
    out["rubric"] = rubric
    return out


def attach_report_evidence(run: dict[str, Any], evidence: dict[str, Any] | None) -> dict[str, Any]:
    """Attach UI/UX blobs for official /report. Missing fields stay missing."""
    src = evidence if isinstance(evidence, dict) else {}
    state = dict(run.get("state") or {})
    ui = honest_ui_evidence(src.get("ui_evidence") if isinstance(src.get("ui_evidence"), dict) else src)
    ux = honest_ux_evidence(src.get("ux_evidence") if isinstance(src.get("ux_evidence"), dict) else src)
    if ui:
        state["ui_evidence"] = ui
    if ux:
        state["ux_evidence"] = ux
    capability = src.get("capability_evidence")
    if isinstance(capability, dict) and capability:
        state["capability_evidence"] = capability
    interface = src.get("interface_evidence")
    if isinstance(interface, dict) and interface:
        state["interface_evidence"] = interface
    out = dict(run)
    out["state"] = state
    return out


def _evidence_blob(run: dict[str, Any], *names: str) -> dict[str, Any]:
    """First attached evidence dict from run state or finalize_ctx. Never invents."""
    state = run.get("state") or {}
    context = run.get("finalize_ctx") or {}
    for name in names:
        for source in (state, context):
            value = source.get(name)
            if isinstance(value, dict) and value:
                if name == "ui_evidence":
                    return honest_ui_evidence(value)
                if name == "ux_evidence":
                    return honest_ux_evidence(value)
                return value
    return {}


def _score_ui(evidence: dict[str, Any]) -> tuple[int, list[str]] | None:
    evidence = honest_ui_evidence(evidence)
    parts: list[int] = []
    notes: list[str] = []
    if evidence.get("a11y_violations") is not None:
        violations = max(0, int(evidence.get("a11y_violations") or 0))
        parts.append(max(0, 100 - min(100, violations * 20)))
        notes.append(f"{violations} a11y violation(s)")
    states = evidence.get("states_covered") or evidence.get("browser_states") or []
    required = {"loading", "empty", "error", "ok"}
    if isinstance(states, list) and states:
        covered = {str(item).strip().lower() for item in states if item}
        parts.append(round(100 * len(covered & required) / len(required)))
        notes.append(f"states={sorted(covered & required)}")
    if evidence.get("contrast_ok") is not None:
        parts.append(100 if evidence.get("contrast_ok") else 40)
        notes.append(f"contrast_ok={bool(evidence.get('contrast_ok'))}")
    if not parts:
        return None
    return round(sum(parts) / len(parts)), notes


def _score_ux(evidence: dict[str, Any]) -> tuple[int, list[str]] | None:
    evidence = honest_ux_evidence(evidence)
    parts: list[int] = []
    notes: list[str] = []
    jtbd = evidence.get("jtbd") or evidence.get("walkthroughs") or []
    if isinstance(jtbd, list) and jtbd:
        known = [
            item for item in jtbd
            if isinstance(item, dict) and item.get("passed") in {True, False}
        ]
        if known:
            passed = sum(1 for item in known if item.get("passed") is True)
            parts.append(round(100 * passed / len(known)))
            notes.append(f"jtbd {passed}/{len(known)} passed")
    rubric = evidence.get("rubric") or {}
    if isinstance(rubric, dict) and rubric:
        keys = ("task_success", "clarity", "trust", "delight", "accessibility")
        values = [
            float(rubric[key])
            for key in keys
            if isinstance(rubric.get(key), (int, float)) and not isinstance(rubric.get(key), bool)
        ]
        if values:
            parts.append(round(100 * (sum(values) / len(values)) / 5))
            notes.append(f"rubric mean {sum(values)/len(values):.1f}/5")
    if not parts:
        return None
    return round(sum(parts) / len(parts)), notes


def _score_interface(evidence: dict[str, Any]) -> tuple[int, list[str]] | None:
    states = evidence.get("states_covered") or evidence.get("browser_states") or []
    if not isinstance(states, list) or not states:
        return None
    required = {"loading", "empty", "error", "keyboard", "responsive"}
    covered = {str(item).strip().lower() for item in states if item}
    return round(100 * len(covered & required) / len(required)), [f"states={sorted(covered & required)}"]


def _score_capability(evidence: dict[str, Any], benchmark: dict[str, Any]) -> tuple[int, list[str]] | None:
    mapping = evidence.get("scenario_mapping") or evidence.get("scenarios") or []
    expected = benchmark.get("acceptance_scenarios") or []
    if not isinstance(mapping, list) or not mapping:
        return None
    covered = 0
    for item in mapping:
        if isinstance(item, dict) and (item.get("covered") or item.get("tested") or item.get("passed")):
            covered += 1
        elif isinstance(item, str) and item.strip():
            covered += 1
    total = max(len(expected), len(mapping), 1)
    return min(100, round(100 * covered / total)), [f"{covered}/{total} scenarios mapped"]


def _score_ratio(actual: int | float, baseline: int | float) -> int:
    """100 at or below the target; declines linearly but never below zero."""
    if baseline <= 0:
        return 0
    return max(0, min(100, round(100 * float(baseline) / max(float(actual), 1))))


def benchmark_scorecard(
    run: dict[str, Any],
    *,
    benchmark: dict[str, Any],
    verify_status: str,
    llm_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Score only objectively observed inputs and expose the missing-evidence gap."""
    state = run.get("state") or {}
    context = run.get("finalize_ctx") or {}
    calls = llm_calls or []
    phase_timings = context.get("phase_timings") or []
    duration_ms = sum(
        int(item.get("ms") or 0)
        for item in phase_timings
        if isinstance(item, dict)
    )
    state_cost = state.get("cost_usd_micros")
    cost = (
        state_cost
        if state_cost is not None
        else (run.get("usage") or {}).get("cost_usd_micros")
    )
    token_observed = any(isinstance(call, dict) for call in calls)
    tokens = sum(
        int(call.get("total_tokens") or 0)
        for call in calls
        if isinstance(call, dict)
    )
    turns = state.get("turns")
    files_changed = state.get("files_changed")
    repair_attempts = len(context.get("repair_log") or [])
    observed_reviews: list[bool] = []
    if state.get("tests_passed") is not None:
        observed_reviews.append(bool(state["tests_passed"]))
    if state.get("adversarial_verdict") not in {None, "skipped", "not_run"}:
        observed_reviews.append(state["adversarial_verdict"] == "survived")
    if state.get("security_verdict") not in {None, "skipped", "not_run"}:
        observed_reviews.append(state["security_verdict"] == "clean")
    if state.get("regressions_ok") is not None:
        observed_reviews.append(bool(state["regressions_ok"]))
    if state.get("consistency_verdict") not in {None, "skipped", "not_run"}:
        observed_reviews.append(
            state["consistency_verdict"] in {"consistent", "pass", "survived"}
        )
    explicit_innovation = state.get("independent_innovation_score")
    innovation_evidence = state.get("independent_innovation_evidence")

    scores: dict[str, dict[str, Any]] = {}

    def observed(dimension: str, score: int, evidence: list[str]):
        scores[dimension] = {"status": "scored", "score": score, "evidence": evidence}

    def unobserved(dimension: str, reason: str):
        scores[dimension] = {"status": "not_scored", "score": None, "reason": reason}

    if verify_status == "pass":
        observed("functionality", 100, ["npm run verify exited 0"])
    elif verify_status == "fail":
        observed("functionality", 0, ["npm run verify failed"])
    else:
        unobserved("functionality", "Authoritative verification was not recorded.")

    capability_evidence = _evidence_blob(run, "capability_evidence", "scenario_mapping")
    capability_score = _score_capability(capability_evidence, benchmark)
    if capability_score:
        observed("capability", capability_score[0], capability_score[1])
    else:
        unobserved(
            "capability",
            "Scenario-to-test mapping has not been attached to the run evidence.",
        )
    interface_evidence = _evidence_blob(run, "interface_evidence", "browser_states", "ui_evidence")
    interface_score = _score_interface(interface_evidence)
    if interface_score:
        observed("interface", interface_score[0], interface_score[1])
    else:
        unobserved(
            "interface",
            "Browser-state evidence has not been attached to the run evidence.",
        )
    ui_score = _score_ui(_evidence_blob(run, "ui_evidence"))
    if ui_score:
        observed("ui", ui_score[0], ui_score[1])
    else:
        unobserved(
            "ui",
            "UI craft evidence (a11y_violations, states_covered, contrast_ok) was not attached.",
        )
    ux_score = _score_ux(_evidence_blob(run, "ux_evidence"))
    if ux_score:
        observed("ux", ux_score[0], ux_score[1])
    else:
        unobserved(
            "ux",
            "UX evidence (JTBD walkthroughs and/or rubric 1–5) was not attached.",
        )
    if observed_reviews:
        observed(
            "quality",
            round(100 * sum(bool(value) for value in observed_reviews) / len(observed_reviews)),
            ["Recorded governed quality-gate outcomes."],
        )
    else:
        unobserved("quality", "No governed quality-gate outcomes were recorded.")
    if duration_ms:
        observed(
            "speed",
            _score_ratio(duration_ms, SCORE_BASELINES["duration_ms"]),
            [f"{duration_ms} ms recorded"],
        )
    else:
        unobserved("speed", "Phase timing evidence was not recorded.")
    if turns is not None and files_changed is not None:
        scenario_count = max(1, len(benchmark.get("acceptance_scenarios") or []))
        turn_score = _score_ratio(int(turns), SCORE_BASELINES["turns"])
        scenario_score = min(100, round(100 * scenario_count / max(int(turns), 1)))
        file_score = _score_ratio(int(files_changed), SCORE_BASELINES["files_changed"])
        observed(
            "efficiency",
            round((turn_score + scenario_score + file_score) / 3),
            ["Recorded turns, changed files, and original-spec scenarios."],
        )
    else:
        unobserved("efficiency", "Turns and changed-file evidence are both required.")
    if cost is not None:
        observed(
            "cost",
            _score_ratio(int(cost), SCORE_BASELINES["cost_usd_micros"]),
            [f"{int(cost)} USD micros recorded"],
        )
    else:
        unobserved("cost", "Observed model cost was not recorded.")
    if token_observed:
        observed(
            "tokens",
            _score_ratio(tokens, SCORE_BASELINES["tokens"]),
            [f"{tokens} tokens recorded"],
        )
    else:
        unobserved("tokens", "Stage token telemetry was not recorded.")
    if turns is not None or files_changed is not None:
        effort_scores = []
        if turns is not None:
            effort_scores.append(_score_ratio(int(turns), SCORE_BASELINES["turns"]))
        if files_changed is not None:
            effort_scores.append(_score_ratio(int(files_changed), SCORE_BASELINES["files_changed"]))
        observed(
            "effort",
            round(sum(effort_scores) / len(effort_scores)),
            ["Recorded implementation turns/files."],
        )
    else:
        unobserved("effort", "Implementation-effort telemetry was not recorded.")
    if isinstance(explicit_innovation, (int, float)) and innovation_evidence:
        observed("innovation", max(0, min(100, round(explicit_innovation))), [str(innovation_evidence)])
    else:
        unobserved("innovation", "Independent innovation assessment with cited evidence is required.")
    if verify_status == "pass":
        observed("reliability", max(0, 100 - min(100, repair_attempts * 20)), [f"{repair_attempts} repair attempt(s) recorded"])
    elif verify_status == "fail":
        observed("reliability", 0, ["Authoritative verification failed"])
    else:
        unobserved("reliability", "Authoritative verification was not recorded.")

    weights = {item["id"]: item["weight"] for item in SCORING_DIMENSIONS}
    scored_weight = sum(
        weights[key] for key, value in scores.items() if value["status"] == "scored"
    )
    weighted_sum = sum(
        weights[key] * value["score"]
        for key, value in scores.items()
        if value["status"] == "scored"
    )
    return {
        "version": SCORECARD_VERSION,
        "baselines": dict(SCORE_BASELINES),
        "dimensions": [
            {**dimension, **scores[dimension["id"]]}
            for dimension in SCORING_DIMENSIONS
        ],
        "observed_weight": scored_weight,
        "evidence_coverage_pct": round(100 * scored_weight / 100),
        "overall_score": round(weighted_sum / scored_weight, 1) if scored_weight else None,
        "overall_status": "provisional" if scored_weight < 100 else "complete",
        "tuning_note": "Compare only runs with the same benchmark revision and scorecard baselines.",
    }


def report_honesty(report: dict[str, Any]) -> dict[str, Any]:
    """Name missing UI/UX/capability evidence. Never invent a score.

    Official /reports must stay evidence-only: a dimension without attached
    evidence is ``not_scored``. A scored dimension with a null score is treated
    as invented and marked dishonest so callers cannot stamp a 5/5 from silence.
    """
    scorecard = report.get("scorecard") or {}
    dimensions = scorecard.get("dimensions") or []
    missing: list[dict[str, Any]] = []
    invented: list[str] = []
    scored: list[str] = []
    for item in dimensions:
        if not isinstance(item, dict):
            continue
        dim_id = str(item.get("id") or "")
        status = item.get("status")
        if status == "not_scored":
            missing.append({"id": dim_id, "reason": item.get("reason")})
        elif status == "scored":
            if item.get("score") is None:
                invented.append(dim_id)
            else:
                scored.append(dim_id)
    return {
        "honest": not invented,
        "scored": scored,
        "missing_evidence": missing,
        "invented_scores": invented,
    }


def tuning_recommendation(report: dict[str, Any]) -> dict[str, Any]:
    """Turn observed benchmark evidence into a proposal-ready, non-mutating packet.

    This deliberately does not choose a model, edit a workflow, or claim causality. It
    identifies measurable gaps and states the control/intervention evidence required
    before a separately supplied PR or config diff can enter the existing governed
    self-improvement workflow.
    """
    scorecard = report.get("scorecard") or {}
    dimensions = scorecard.get("dimensions") or []
    if report.get("run_status") not in {"completed", "failed"}:
        return {
            "status": "not_ready",
            "reason": "The benchmark run is not terminal; wait for durable evidence.",
            "source_run_id": report.get("run_id"),
            "benchmark_id": report.get("benchmark_id"),
        }

    measured_gaps = [
        {
            "dimension": item.get("id"),
            "label": item.get("label"),
            "score": item.get("score"),
            "weight": item.get("weight"),
            "evidence": item.get("evidence") or [],
        }
        for item in dimensions
        if isinstance(item, dict)
        and item.get("status") == "scored"
        and isinstance(item.get("score"), (int, float))
        and item["score"] < 100
    ]
    missing_evidence = [
        item.get("id")
        for item in dimensions
        if isinstance(item, dict) and item.get("status") == "not_scored"
    ]
    observed_weight = int(scorecard.get("observed_weight") or 0)
    if not measured_gaps:
        reason = (
            "No measured score gap is available for tuning. Attach missing evidence or "
            "compare a separately identified regression; do not invent an optimization."
        )
        return {
            "status": "not_ready",
            "reason": reason,
            "source_run_id": report.get("run_id"),
            "benchmark_id": report.get("benchmark_id"),
            "observed_weight": observed_weight,
            "missing_evidence": missing_evidence,
        }

    return {
        "status": "ready_for_candidate",
        "source_run_id": report.get("run_id"),
        "benchmark_id": report.get("benchmark_id"),
        "benchmark_name": report.get("benchmark_name"),
        "scorecard_version": scorecard.get("version"),
        "observed_weight": observed_weight,
        "overall_score": scorecard.get("overall_score"),
        "measured_gaps": measured_gaps,
        "missing_evidence": missing_evidence,
        "required_experiment": (
            "Run the same pinned benchmark and scorecard revision as a control; vary only "
            "the proposed workflow, model-routing, repair-budget, or test-strategy change."
        ),
        "guardrails": [
            "A traceable PR or config-diff reference is required before a proposal can be created.",
            "The proposal is decision-gated and cannot apply a change directly.",
            "A canary and rollback plan are required before rollout.",
        ],
    }


def tuning_proposal_inputs(
    report: dict[str, Any], *, change_ref: str, diff_summary: str
) -> dict[str, Any]:
    """Build the required change-control fields for the existing proposal service."""
    recommendation = tuning_recommendation(report)
    if recommendation["status"] != "ready_for_candidate":
        raise ValueError(recommendation["reason"])
    change_ref = (change_ref or "").strip()
    diff_summary = (diff_summary or "").strip()
    if not change_ref or not diff_summary:
        raise ValueError("change_ref and diff_summary are required for a tuning proposal.")

    gaps = recommendation["measured_gaps"]
    gap_summary = ", ".join(
        f"{gap['dimension']}={gap['score']}" for gap in gaps
    )
    source_ref = (
        f"/v1/qa/benchmark-apps/runs/{recommendation['source_run_id']}/report"
    )
    return {
        "title": f"Tune BOSS workflow from {recommendation['benchmark_name']} evidence",
        "summary": (
            f"Benchmark evidence {source_ref} observed score gaps: {gap_summary}. "
            "This proposal is a candidate for an isolated comparison, not a claim that "
            "the candidate caused an improvement."
        ),
        "change_ref": change_ref,
        "diff_summary": diff_summary,
        "expected_impact": [
            f"Improve measured {gap['dimension']} score from {gap['score']} without regressing functionality."
            for gap in gaps
        ],
        "test_plan": [
            recommendation["required_experiment"],
            f"Attach the control and candidate reports, each using {source_ref}'s benchmark revision.",
            "Require authoritative verification to pass and preserve all existing policy gates.",
        ],
        "canary_scope": [
            f"QA benchmark {recommendation['benchmark_id']} only before any broader rollout.",
            "No production customer workload changes during the benchmark canary.",
        ],
        "rollback_plan": [
            "Revert the referenced PR or config diff through the normal change-control path.",
            "Restore the last approved workflow/model-routing configuration and rerun the control benchmark.",
        ],
        "success_metrics": [
            f"Candidate improves one or more observed gaps ({gap_summary}) against its comparable control.",
            "Functionality remains verified and no observed score dimension regresses.",
            "Cost, token, and reliability evidence remain attached before rollout approval.",
        ],
        "recommendation": recommendation,
    }


def completion_report(run: dict[str, Any], *, llm_calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Render only observed evidence; an absent command is never treated as a pass."""
    spec = run.get("spec") or {}
    context = run.get("finalize_ctx") or {}
    benchmark = context.get("qa_benchmark") or spec.get("qa_benchmark") or {}
    tests = (run.get("state") or {}).get("tests") or []
    observed = tests if isinstance(tests, list) else []
    verify = next(
        (
            row
            for row in observed
            if isinstance(row, dict) and row.get("command") == "npm run verify"
        ),
        None,
    )
    verify_status = (
        "pass"
        if verify and verify.get("exit_code") == 0 and not verify.get("timed_out")
        else "fail"
        if verify
        else "not_recorded"
    )
    plan = (run.get("state") or {}).get("design")
    plan_status = (
        "pass"
        if isinstance(plan, dict) and str(plan.get("plan") or "").strip()
        else "not_recorded"
    )
    command_results = [
        {
            "label": "Implementation plan",
            "command": "governed design phase",
            "status": plan_status,
        },
        {
            "label": "Full benchmark verification",
            "command": "npm run verify",
            "status": verify_status,
            "exit_code": verify.get("exit_code") if isinstance(verify, dict) else None,
            "timed_out": verify.get("timed_out") if isinstance(verify, dict) else None,
        },
    ]
    simulated = bool(
        spec.get("simulated")
        or context.get("simulated")
        or context.get("fixture")
        or run.get("status") == "simulated"
    )
    is_complete = (
        not simulated
        and run.get("status") == "completed"
        and all(row["status"] == "pass" for row in command_results)
    )
    scorecard = benchmark_scorecard(
        run,
        benchmark=benchmark,
        verify_status=verify_status,
        llm_calls=llm_calls,
    )
    return {
        "run_id": run.get("id"),
        "benchmark_id": benchmark.get("id"),
        "benchmark_name": benchmark.get("name"),
        "run_status": run.get("status"),
        "completion_contract": benchmark.get("completion_contract")
        or list(COMPLETION_CONTRACT),
        "required_verification_commands": benchmark.get("verification_commands") or [],
        "results": command_results,
        "simulated": simulated,
        "passed": is_complete,
        "scorecard": scorecard,
        "honesty": report_honesty({"scorecard": scorecard}),
        "message": (
            "Simulated fixture is not a certified benchmark pass."
            if simulated
            else "Benchmark acceptance evidence is complete."
            if is_complete
            else "Benchmark is not certified: missing, failed, or incomplete durable evidence remains visible."
        ),
    }
