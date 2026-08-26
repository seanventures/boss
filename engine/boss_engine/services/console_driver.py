"""Drives a live console and records what each control actually does.

This is the half of the test agent that touches a browser. The comparison logic lives in
`console_test_agent` and is deliberately pure, so the judgement can be tested without
Playwright and cannot be quietly influenced by what the browser happened to do.

WHAT COUNTS AS "WHAT IT DOES"
-----------------------------
Three kinds of traffic, because in this stack no single one tells the whole story:

  * ``/v1/*`` on any origin — a direct browser-to-control-plane call.
  * ``/api/*`` on the console's own origin — a Next.js route handler. Stable path, so it
    compares across a redesign.
  * a Server Action POST — a POST to the page URL carrying a ``next-action`` header.

The third one is recorded as the opaque token ``POST (server-action)`` rather than by its id,
because the id is a per-build hash: comparing them across v1 and v2 would report every control
as broken. What IS comparable is that a server action fired at all, and how many.

That distinction earns its keep immediately. The first live run recorded 40 server actions
fired by *loading* the Factory area with no interaction at all, and 18 invocations of a single
action on Discovery. Every one re-renders the whole route on the server. Filtering to ``/v1/``
— which is what this driver did originally — showed none of it, because the v1 console does
its control-plane calls server-side and the browser never sees them.

WHY THE WALK RE-NAVIGATES
-------------------------
The first version inventoried an area once and then clicked down the list. The third click was
a nav link, so everything after it was recorded from a different page while still labelled with
the original area: the run reported controls for ``overview`` that were read off ``budget``. A
baseline that lies about where a control lives is worse than no baseline, so the walk now
re-establishes the area whenever the URL drifts, and re-queries rather than reusing handles
that a navigation has invalidated.

TIMING
------
Both the control's settle time and its slowest single request are recorded, because the gap
between them is itself a defect class: "Set target" appeared to hang for 115 seconds while its
own API call finished in under one. Recording only the total blames the API; recording only the
request says it was fine.

SAFETY
------
Reads are free; writes are not. ``SAFE_METHODS`` is what the driver may trigger unattended, and
``allow_writes`` is required for anything else. A test harness that deploys something because
it was curious what a button did is not a test harness.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .console_test_agent import Baseline, Control, Request

#: Methods safe to trigger without explicit permission.
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

#: How long to let a control settle before recording what it did. Generous on purpose: the
#: point is to catch slow controls, not to time out on them and call them broken.
SETTLE_MS = 25_000

#: How long to wait after loading an area. The live console needed more than 1.5s before its
#: first control-plane call appeared, and a window that closes early records an empty page load
#: — which reads exactly like an area that fetches nothing.
LOAD_SETTLE_MS = 6_000

#: What a Server Action is recorded as. Deliberately not the action id: that is a build hash
#: and would differ between v1 and v2 even for identical behaviour.
SERVER_ACTION = "(server-action)"

#: Controls that must never be pressed unattended, whatever the method. Matched on visible
#: text. Being wrong here costs money or destroys state, so it is a denylist by name as well
#: as by method.
NEVER_PRESS = re.compile(
    r"\b(delete|destroy|purge|teardown|retire|unbind|cancel|reject|approve|waive|deploy|"
    r"rebuild|retry|merge|publish|send|pay|buy|rotate)\b",
    re.I,
)

_CONTROL_SELECTOR = "button, a[href], [role=button], input[type=submit]"


@dataclass
class DriverConfig:
    base_url: str
    areas: tuple[str, ...]
    allow_writes: bool = False
    headless: bool = True
    settle_ms: int = SETTLE_MS
    load_settle_ms: int = LOAD_SETTLE_MS
    #: Cap per area. A console area with 200 links would otherwise take an hour to walk.
    max_controls_per_area: int = 40


@dataclass
class _Seen:
    """Raw traffic for one window, before it is turned into Requests."""

    events: list[dict[str, Any]] = field(default_factory=list)

    def clear(self) -> None:
        self.events.clear()


def _norm_path(url: str, api_origin: Optional[str]) -> str:
    """Strip the origin so v1 and v2 recordings compare even on different hosts."""
    if api_origin and url.startswith(api_origin):
        return url[len(api_origin):] or "/"
    return re.sub(r"^https?://[^/]+", "", url) or "/"


def _is_control_safe(label: str) -> bool:
    return not NEVER_PRESS.search(label or "")


def _is_interesting(url: str, is_server_action: bool) -> bool:
    """Whether this request says something about behaviour.

    Static assets and fonts do not. A control-plane call, a route handler, or a server action
    do — see the module docstring for why all three are needed.
    """
    if is_server_action:
        return True
    path = re.sub(r"^https?://[^/]+", "", url)
    return path.startswith("/v1/") or path.startswith("/api/")


def _to_requests(events: list[dict[str, Any]], api_origin: Optional[str]) -> list[Request]:
    """Responses only: a request that never came back proves nothing about what happened."""
    out = []
    for e in events:
        if e.get("status") is None:
            continue
        if not _is_interesting(e["url"], e.get("server_action", False)):
            continue
        path = SERVER_ACTION if e.get("server_action") else _norm_path(e["url"], api_origin)
        out.append(Request(e["method"], path, e.get("status")))
    return out


def _count_server_actions(events: list[dict[str, Any]]) -> int:
    """How many server actions fired. Kept as a count, not a set.

    The count is the finding: 40 invocations of 13 distinct actions on one page load is a very
    different fact from 13, and deduping would hide it.
    """
    return sum(1 for e in events if e.get("server_action") and e.get("status") is not None)


def learn(config: DriverConfig, api_origin: Optional[str] = None) -> Baseline:
    """Walk each area, inventory its controls, and record what the safe ones do.

    Raises ImportError with an actionable message if Playwright is missing — the alternative,
    returning an empty Baseline, would be indistinguishable from "this console has no controls"
    and would let a migration pass unchecked.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ImportError(
            "the console driver needs Playwright: pip install playwright==1.60.0 && "
            "python -m playwright install chromium"
        ) from exc

    controls: list[Control] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=config.headless)
        page = browser.new_page()
        seen = _Seen()

        def on_request(r):
            seen.events.append({
                "method": r.method, "url": r.url, "t": time.time(),
                "server_action": bool(r.headers.get("next-action")),
            })

        def on_response(r):
            seen.events.append({
                "method": r.request.method, "url": r.url, "status": r.status, "t": time.time(),
                "server_action": bool(r.request.headers.get("next-action")),
            })

        page.on("request", on_request)
        page.on("response", on_response)

        def area_url(area: str) -> str:
            joiner = "&" if "?" in config.base_url else "?"
            return f"{config.base_url}{joiner}area={area}"

        def goto_area(area: str) -> None:
            seen.clear()
            page.goto(area_url(area), wait_until="domcontentloaded", timeout=config.settle_ms)
            page.wait_for_timeout(config.load_settle_ms)

        for area in config.areas:
            goto_area(area)

            # What the page fetched merely by existing. This is how "one feed costing 13s that
            # only one area displays" — and "40 server actions before you touch anything" —
            # become visible.
            load_requests = _to_requests(seen.events, api_origin)
            actions = _count_server_actions(seen.events)
            controls.append(Control(
                area=area, label="(page load)", kind="load",
                requests=_dedupe(load_requests),
                state_after={
                    "request_count": len(load_requests),
                    "server_actions": actions,
                    "slowest_request_ms": _slowest(seen.events),
                },
                note=(f"issued by rendering the area, before any interaction; "
                      f"{actions} server action(s)"),
            ))

            count = min(len(page.query_selector_all(_CONTROL_SELECTOR)),
                        config.max_controls_per_area)

            for idx in range(count):
                label = "(unknown)"
                try:
                    # Re-establish the area if a previous click navigated away, then re-query:
                    # handles from before a navigation are stale, and reusing them is how the
                    # first run recorded budget's controls under overview's name.
                    if not page.url.endswith(f"area={area}"):
                        goto_area(area)
                    elements = page.query_selector_all(_CONTROL_SELECTOR)
                    if idx >= len(elements):
                        break
                    el = elements[idx]

                    if not el.is_visible():
                        continue
                    label = (el.inner_text() or el.get_attribute("aria-label") or "").strip()
                    label = re.sub(r"\s+", " ", label)
                    if not label or len(label) > 80:
                        continue
                    testid = el.get_attribute("data-testid")

                    inventory = Control(
                        area=area, label=label, testid=testid,
                        kind="link" if el.get_attribute("href") else "button",
                    )

                    if not config.allow_writes and not _is_control_safe(label):
                        # Recorded as present but not exercised. Its existence is still the
                        # evidence that matters for MISSING — the Unbind gap was exactly this:
                        # an API the console never called.
                        inventory.note = "not pressed: would change state"
                        controls.append(inventory)
                        continue

                    seen.clear()
                    started = time.time()
                    el.click(timeout=5_000)
                    page.wait_for_timeout(2_000)
                    elapsed_ms = int((time.time() - started) * 1000)

                    reqs = _to_requests(seen.events, api_origin)
                    if not config.allow_writes:
                        unsafe = [r for r in reqs
                                  if r.method.upper() not in SAFE_METHODS
                                  and r.path != SERVER_ACTION]
                        if unsafe:
                            # A control that looked read-only and was not. Recorded so the
                            # denylist can be corrected, and flagged loudly.
                            inventory.note = (
                                "UNEXPECTED WRITE: issued "
                                + ", ".join(sorted({r.key() for r in unsafe}))
                            )
                    inventory.requests = _dedupe(reqs)
                    # Both timings, because the gap between them is itself a defect: "Set
                    # target" settled in 115s while its own call took under a second.
                    inventory.state_after = {
                        "elapsed_ms": elapsed_ms,
                        "slowest_request_ms": _slowest(seen.events),
                        "server_actions": _count_server_actions(seen.events),
                        "navigated_to": page.url if not page.url.endswith(f"area={area}") else "",
                    }
                    controls.append(inventory)
                except Exception as exc:  # noqa: BLE001 - one bad control must not end the walk
                    controls.append(Control(
                        area=area, label=label,
                        note=f"could not exercise: {type(exc).__name__}: {str(exc)[:120]}"))

        browser.close()

    return Baseline(
        console="v2" if "/v2" in config.base_url else "v1",
        captured_at=datetime.now(timezone.utc).isoformat(),
        controls=controls,
    )


def _dedupe(requests: list[Request]) -> list[Request]:
    out, seen_keys = [], set()
    for r in requests:
        if r.key() in seen_keys:
            continue
        seen_keys.add(r.key())
        out.append(r)
    return out


def _slowest(events: list[dict[str, Any]]) -> int:
    """Longest single request in this window, in ms.

    Paired with the control's own elapsed time it separates "the API is slow" from "the UI
    made you wait for something else" — which is the difference between the 13s audit export
    and the 115s Save, and they need opposite fixes.
    """
    starts: dict[str, float] = {}
    worst = 0.0
    for e in events:
        key = f"{e['method']} {e['url']}"
        if e.get("status") is None:
            starts[key] = e["t"]
        elif key in starts:
            worst = max(worst, e["t"] - starts.pop(key))
    return int(worst * 1000)
