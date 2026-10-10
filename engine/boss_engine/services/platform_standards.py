"""Standards BOSS REQUIRES of every app it builds, as opposed to ones it infers from a repo.

`_standards_prompt` asks the agent to read the repository and summarise the conventions already
there. That is right for style, naming and test layout — a change should look like its
neighbours. It cannot produce a convention the repo has never had.

Observability is exactly that case. BOSS deploys an OpenTelemetry collector that scrapes every
app it ships, so "expose /metrics in this shape" is a property of the PLATFORM, not of any one
repository. Left to per-task instructions it applies to whichever app someone remembered to
write it into — which is what happened: the metric rules were prose in a single task, so one
app got them and the next would not.

Two rules govern what belongs here:

  * It must be true for EVERY app of that kind, not a preference. A standard that is sometimes
    wrong teaches agents to ignore standards.
  * It must state WHY, because an agent that understands a constraint applies it to cases the
    text did not enumerate. "Never label with user input" is followed narrowly; "an unbounded
    label multiplies series until Prometheus falls over" generalises.
"""
from __future__ import annotations

from contextvars import ContextVar

from sqlalchemy import text as _sql

# Applies to anything that serves HTTP. A CLI tool or a library has no endpoint to scrape, and
# demanding one would produce cargo-culted dead code.
OBSERVABILITY_STANDARD = """
## Observability (BOSS platform standard — applies to every network service)

BOSS deploys an OpenTelemetry collector alongside your app and scrapes `GET /metrics` every
60 seconds. Expose it in the Prometheus TEXT EXPOSITION FORMAT (0.0.4), content-type
`text/plain; version=0.0.4; charset=utf-8`.

REQUIRED SERIES
- `http_requests_total{method,route,status}` — counter
- `http_request_duration_seconds{method,route}` — histogram (buckets ~0.005s .. 10s)
- `app_info{version}` — gauge fixed at 1, carrying build/version as labels
- `app_uptime_seconds` — gauge

FUNCTIONAL METRICS ARE REQUIRED, NOT OPTIONAL
Every feature in the acceptance criteria gets at least one metric that shows whether it is
WORKING, not merely whether the process is up. A dashboard of request counts tells an operator
the server is alive while the thing the app exists to do is silently failing.

One counter per user-facing outcome, named for the domain action and split by result:
`verifications_started_total`, `verifications_completed_total{result="success|failure"}`,
`invites_sent_total{result}`, `imports_rejected_total{reason}`. Where an operation can be slow
enough to matter, add a duration histogram for it too. Where the app holds a queue or a
backlog, add a gauge for its depth.

The test is simple: read the acceptance criteria back and ask, for each one, "which series
would tell me this broke at 3am?" If there is no answer, that feature is not instrumented.
Prefer a handful somebody would page on over exhaustive coverage — but a feature with NO
signal is not finished.

ROUTE MUST BE A TEMPLATE, NEVER A PATH
`/api/quote/AAPL` and `/api/quote/MSFT` must both report `route="/api/quote/:symbol"`. A label
whose value comes from user input creates one time series per distinct value: it will multiply
series until the metrics backend falls over, and it is the most common way a self-instrumented
app takes down the monitoring meant to watch it. The same rule rules out user ids, session ids,
order ids, email addresses and free-text search terms as labels.

NEVER PUT DATA IN LABELS
Metrics are counts and durations, not records. No usernames, no request bodies, no identifiers
that could single out a person. If you would not print it in a public log line, it is not a
label.

DO NOT SELF-REPORT DEPLOYMENT METADATA
`app`, `environment`, `project`, `repo` and `target_id` are attached BY THE COLLECTOR from the
deployment registry. An app that reports its own environment starts lying the moment it is
promoted, and BOSS already knows the truth.

THE ENDPOINT ITSELF
- No authentication: the collector scrapes it directly and holds no credentials.
- Scrapes must NOT appear in the app's own request metrics — a scrape every 60s would otherwise
  dominate the request rate and hide real traffic.
- Cheap to render. If `/metrics` is slow, the series count is already a problem.

DECLARE IT IN boss.deploy.yaml — OR BOSS NEVER SCRAPES IT
After every deploy BOSS scrapes the path the app DECLARES, through the same route its health
probe uses, and turns it into request rate, error rate (5xx / total) and p50/p95 latency in
Observability, with an alert when more than 5% of requests fail over 5 minutes. An undeclared
endpoint is invisible. Add to the repository's boss.deploy.yaml:

    metrics: {path: /metrics}
    # optional: interval: 60 (15..3600 s), via: auto|public|host,
    #           alert: {error_rate_percent: 5, window_minutes: 5, min_requests: 20}

The path is a path only (no scheme or host). BOSS reads `http_requests_total` with a
`status` (or `code`) label and the `http_request_duration_seconds` histogram; the
prometheus-fastapi-instrumentator and starlette-exporter names are accepted too.

DEPENDENCIES
An app that already installs packages (FastAPI, Flask, Express) uses its ecosystem's client:
`prometheus_client` for Python. An app with no build step and no package install keeps to the
standard library — a small in-process registry (counter + histogram + a `render()` function)
is enough and is easy to unit-test.

FastAPI, with prometheus_client (add `prometheus-client` to requirements):

    import time
    from fastapi import FastAPI, Request, Response
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

    REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
    LATENCY = Histogram("http_request_duration_seconds", "HTTP request latency",
                        ["method", "route"],
                        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))
    app = FastAPI()

    @app.middleware("http")
    async def metrics_middleware(request: Request, call_next):
        if request.url.path == "/metrics":          # scrapes stay out of the app's own numbers
            return await call_next(request)
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            template = getattr(route, "path", "unmatched")   # the TEMPLATE, never the raw path
            REQUESTS.labels(request.method, template, str(status)).inc()
            LATENCY.labels(request.method, template).observe(time.perf_counter() - started)

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

TESTING
Rendering must be testable without making HTTP requests: exposition text parses, histogram
buckets are cumulative and monotonic, `le="+Inf"` equals `_count`, label values with quotes or
backslashes are escaped, and templated routes collapse to one series.
""".strip()


#: What QA must assert about instrumentation, in the QA stage's own words.
#:
#: The observability standard told the BUILDER what to expose and said rendering "must be
#: testable" — and nothing ever tested it. A standard no stage verifies is a suggestion, and
#: the evidence is on the host: three apps deployed, none of them scraped, and a monitoring
#: page linking to dashboards for metrics nobody had checked existed.
#:
#: Written as test CASES rather than as a rule, because this text goes to the stage whose only
#: job is to write tests.
METRICS_TEST_REQUIREMENT = """
METRICS TESTS ARE PART OF THE SUITE, NOT AN EXTRA
BOSS scrapes `GET /metrics` on every app it deploys, so an app that ships without it is
invisible the moment it is live. Include these cases:

- `/metrics` answers 200 with content-type `text/plain; version=0.0.4`.
- The four platform series are present: `http_requests_total`, `http_request_duration_seconds`,
  `app_info`, `app_uptime_seconds`.
- EVERY feature in the acceptance criteria has at least one domain series, and exercising that
  feature moves its counter. Assert the delta, not merely that the name appears — a metric that
  never increments is the same as no metric, and is harder to notice.
- Route labels are templates: hit the same route with two different path parameters and assert
  ONE series, not two. This is the failure that takes the metrics backend down rather than the
  app, so it is worth a test of its own.
- A scrape does not appear in the app's own request counters.
- When the repository has a boss.deploy.yaml, it declares `metrics: {path: /metrics}` — the
  path the test scrapes. Without it BOSS never scrapes the endpoint after deploy.

If the app cannot serve `/metrics` at all, say so plainly in your summary rather than writing
tests that pass around the gap.
""".strip()


# Keyed so a task can request the standards that apply to what it is building, rather than
# receiving every rule BOSS has for every kind of software.
STANDARDS: dict[str, str] = {
    "observability": OBSERVABILITY_STANDARD,
}

# Signals that a change is (or will be) a network service, and therefore in scope. Deliberately
# generous: a false positive costs an agent reading a paragraph it does not need, while a false
# negative ships an app the platform cannot see.
_SERVICE_HINTS = (
    "http", "server", "serve", "api", "endpoint", "web app", "webapp", "service",
    "flask", "fastapi", "express", "nginx", "port", "/metrics", "rest",
)


def applies_to(text: str) -> bool:
    """Does this task look like it produces something that serves traffic?"""
    lowered = (text or "").lower()
    return any(hint in lowered for hint in _SERVICE_HINTS)


#: Standards the workforce mandated, for the task currently being built.
#:
#: `_ctx_block` is called from eleven prompt builders and has no database handle, and threading
#: one through all eleven to fetch the same rows eleven times would be worse than this. The run
#: loads them once at the top (`load_mandated_standards`) and every stage of that task reads the
#: same snapshot — which also means a standard mandated MID-BUILD cannot change the prompt
#: halfway through and silently break the prompt cache the shared preamble exists to preserve.
_MANDATED: ContextVar[tuple[dict, ...]] = ContextVar("boss_mandated_standards", default=())


def load_mandated_standards(connection, project_id: str) -> int:
    """Load this project's mandated standards for the current task. Returns how many.

    Call once per run, before the first prompt is built.

    The hardcoded standards are what the platform itself guarantees. These are what the
    workforce has decided — the Security role concluding that a port must be checked for public
    exposure before it is assigned, say. Before this table that conclusion lived in one
    meeting's minutes and the next build never saw it, so a role could mandate precisely
    nothing.

    `project_id IS NULL` rows apply to EVERY project; that is the "common to all projects" case.
    """
    if connection is None or not project_id:
        _MANDATED.set(())
        return 0
    try:
        rows = connection.execute(
            _sql("SELECT title, body, scope, mandated_by_role, rationale "
                 "FROM engineering_standards "
                 "WHERE status = 'active' AND (project_id IS NULL OR project_id = :p) "
                 "ORDER BY project_id NULLS FIRST, created_at"),
            {"p": project_id},
        ).mappings().all()
    except Exception:  # noqa: BLE001 — a build must not fail because a table is missing
        _MANDATED.set(())
        return 0
    _MANDATED.set(tuple(dict(r) for r in rows))
    return len(rows)


def _mandated_block(*, service: bool) -> str:
    """Render the loaded standards, or "" when none apply to this kind of build."""
    rows = [r for r in _MANDATED.get()
            if r.get("scope") == "all" or (r.get("scope") == "service" and service)]
    if not rows:
        return ""
    out = []
    for row in rows:
        # The role and the reason are part of the standard, not decoration. A rule with no
        # author cannot be argued with, and an agent that understands WHY applies it to the
        # cases the text did not enumerate.
        out.append(
            f"\n\n### {row['title']} (mandated by {row['mandated_by_role']})\n"
            + (f"WHY THIS EXISTS: {row['rationale']}\n" if row.get("rationale") else "")
            + str(row["body"]).strip()
        )
    return ("\n\n=== STANDARDS THIS WORKFORCE HAS MANDATED (binding on this build, same "
            "force as the platform's own) ===" + "".join(out))


#: Links the operator told this build to work against, for the task currently being built.
#: Same per-run snapshot mechanism as the mandated standards above, and for the same reason:
#: `_ctx_block` is shared by eleven prompt builders and has no database handle.
_REFERENCES: ContextVar[tuple[str, ...]] = ContextVar("boss_reference_links", default=())


def load_reference_links(links) -> int:
    """Hold the reference material for this run. Returns how many."""
    clean = tuple(str(link).strip() for link in (links or []) if str(link).strip())
    _REFERENCES.set(clean)
    return len(clean)


def reference_material() -> str:
    """Required reading for this build, or "".

    Deliberately worded as an instruction to OPEN them. The agent has a fetch tool; nothing
    here is indexed, embedded or searched, and describing it as a knowledge base would promise
    retrieval that does not exist. A link the agent cannot reach is reported as unreachable
    rather than guessed at — a build that invents the contents of an API doc it could not open
    is worse than one that says it could not open it.
    """
    links = _REFERENCES.get()
    if not links:
        return ""
    listed = "\n".join(f"- {link}" for link in links)
    return (
        "\n\n=== REFERENCE MATERIAL FOR THIS BUILD (the operator chose these) ===\n"
        "OPEN each of these before you design or write anything, and follow what they say over "
        "your own assumptions. They are not indexed — fetch them.\n"
        f"{listed}\n"
        "If one cannot be reached, SAY SO in your output and continue; do not invent what it "
        "would have contained."
    )


def platform_standards(instructions: str = "", *, force: bool = False) -> str:
    """The standards block to hand an agent, or "" when none apply.

    Returning an empty string matters: appending an empty heading to every prompt trains agents
    to skim past the section, and a standard nobody reads is not a standard.
    """
    service = bool(force or applies_to(instructions))
    mandated = _mandated_block(service=service)
    if not service:
        # A library still gets rules explicitly scoped to 'all' — an org-wide licence header,
        # say — but not the operational ones, which would be cargo-culted dead code.
        return mandated
    return ("\n\n=== BOSS PLATFORM STANDARDS (mandatory — these come from the platform, not "
            "from this repository) ===\n" + STANDARDS["observability"] + mandated)
