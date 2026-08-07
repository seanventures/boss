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

Add domain counters where they answer a real operational question (orders placed, jobs failed).
Prefer a handful that someone would page on over exhaustive coverage.

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

DEPENDENCIES
Prefer the standard library. BOSS runs many apps with no build step and no package install, so
a metrics client library is usually a deployment failure waiting to happen. A small in-process
registry (counter + histogram + a `render()` function) is enough and is easy to unit-test.

TESTING
Rendering must be testable without making HTTP requests: exposition text parses, histogram
buckets are cumulative and monotonic, `le="+Inf"` equals `_count`, label values with quotes or
backslashes are escaped, and templated routes collapse to one series.
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


def platform_standards(instructions: str = "", *, force: bool = False) -> str:
    """The standards block to hand an agent, or "" when none apply.

    Returning an empty string matters: appending an empty heading to every prompt trains agents
    to skim past the section, and a standard nobody reads is not a standard.
    """
    if not (force or applies_to(instructions)):
        return ""
    return ("\n\n=== BOSS PLATFORM STANDARDS (mandatory — these come from the platform, not "
            "from this repository) ===\n" + STANDARDS["observability"])
