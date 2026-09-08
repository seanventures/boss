"""Every surface the operator touches, driven against a RUNNING BOSS.

WHY THIS EXISTS
---------------
"whenever I start conversation with the AI agent I'm finding different set of bugs.. I can't
 scale my testing if you dont help me to produce the quality output"

Fair, and the reason is in my own test suite. It is ~3,500 unit tests over pure functions plus a
large number of source greps, and neither kind runs the system. Every defect the operator found
in one session was invisible to both:

    the workflow picker 500        an endpoint called TWICE hits a unique index. One call
                                   passes. Nothing called it twice.
    the template page z-index      two `.bx-shell` elements, `isolation: isolate`. Only visible
                                   in a browser with the console streamed.
    JSON in the change record      a fallback path nothing exercised end to end.
    the chat that hung             a server-action round trip that rejects, with no catch. The
                                   operator's message was never even recorded.

A source grep cannot see any of those. Something that presses the buttons can.

WHAT THIS IS
------------
A smoke suite against a deployed BOSS: it calls the endpoints the console calls, in the order
the console calls them, and asserts the SHAPES the console depends on. It is read-mostly and
free, so it can run after every deploy rather than when someone remembers.

    python3 tests/smoke_deployed.py                      # localhost:8000
    python3 tests/smoke_deployed.py --base https://boss-api.local.sean.network
    python3 tests/smoke_deployed.py --base ... --with-agent   # spends ONE model turn

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
No writes that cost money, and no agent turn, unless asked. An automatic suite that quietly
spends on every run is one that gets switched off. `--with-agent` is the opt-in, and it is the
only check here that costs anything.

It also does not replace a browser. The z-index bug needed a rendered page; this suite would not
have caught it, and pretending otherwise would be the same overclaim it exists to prevent.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import ssl
import time
import urllib.error
import urllib.request

PROJECT = os.environ.get("BOSS_SMOKE_PROJECT", "boss-dogfood")
# The identity the console actually uses. Testing as anything else misses the membership checks.
ACTOR = "executive_console"

_failures: list[str] = []
_skipped: list[str] = []


def _key() -> str:
    for source in (os.environ.get("BOSS_API_KEY"),
                   pathlib.Path.home() / ".config" / "boss-console-api-key"):
        if isinstance(source, str) and source.strip():
            return source.strip()
        if isinstance(source, pathlib.Path) and source.exists():
            return source.read_text(encoding="utf-8").strip()
    return ""


def _tls() -> ssl.SSLContext:
    """A context that trusts what curl trusts.

    The tailnet certificate is ordinary Let's Encrypt, but a framework Python on macOS ships
    with an empty trust store, so every call failed CERTIFICATE_VERIFY_FAILED while curl to the
    same URL worked. A suite that cannot reach the system it tests reports twelve failures and
    means nothing.
    """
    context = ssl.create_default_context()
    if not context.get_ca_certs():
        try:
            import certifi
            context.load_verify_locations(certifi.where())
        except Exception:  # noqa: BLE001 — fall through to whatever the platform has
            pass
    return context


_TLS = _tls()


def call(base: str, method: str, path: str, body: dict | None = None,
         timeout: int = 120) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    key = _key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(f"{base}{path}", method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_TLS) as response:
            raw = response.read()
            try:
                return response.status, json.loads(raw)
            except Exception:
                return response.status, {"_bytes": len(raw), "_head": raw[:16].decode(
                    "utf-8", "replace")}
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except Exception:
            return error.code, {"_raw": raw[:200].decode("utf-8", "replace")}
    except Exception as error:  # noqa: BLE001 — a dead endpoint is a result, not a crash
        return 0, {"_error": f"{type(error).__name__}: {error}"}


def check(name: str, ok: bool, detail: object = "") -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
    if not ok:
        text = detail if isinstance(detail, str) else json.dumps(detail)[:240]
        if text:
            print(f"          {text}")
        _failures.append(name)
    return ok


def skip(name: str, why: str) -> None:
    print(f"  --    {name}  ({why})")
    _skipped.append(name)


# ── The checks ───────────────────────────────────────────────────────────────────────────────

def page_loads(base: str) -> None:
    """Everything the console reads to paint itself. A 500 here is a blank area."""
    print("== the console's own reads ==")
    for label, path in (
        ("dashboard", f"/v1/dashboard?project_id={PROJECT}"),
        ("deployment targets", f"/v1/deployment-targets?project_id={PROJECT}"),
        ("deployment hosts", f"/v1/deployment-hosts?project_id={PROJECT}"),
        ("workflow runs", f"/v1/delivery-workflows/runs?project_id={PROJECT}"),
        ("installed workflows", f"/v1/delivery-workflows?view=installed&project_id={PROJECT}"),
        ("workforce", f"/v1/console/v2/workforce?project_id={PROJECT}"),
        ("roles", f"/v1/roles?project_id={PROJECT}"),
        ("agent briefing", f"/v1/agent/briefing?project_id={PROJECT}"),
        ("agent threads", f"/v1/agent/threads?project_id={PROJECT}&actor={ACTOR}"),
    ):
        code, body = call(base, "GET", path)
        check(f"GET {label}", code == 200, {"code": code, "body": body})


def workflow_picker(base: str) -> None:
    """Pressed twice, which is the only way the bug was ever going to show.

    apply() called create_mapping unconditionally against a unique index, so the first call
    passed and every one after it returned 500 — including re-picking what was already set.
    """
    print("== the workflow picker, pressed twice ==")
    code, body = call(base, "GET", f"/v1/delivery-workflows?view=installed&project_id={PROJECT}")
    keys = [w.get("workflow_key") for w in (body or {}).get("items", [])] if code == 200 else []
    if "research_led_discovery" not in keys or "standard_product_delivery" not in keys:
        skip("apply twice", "the catalogue is not installed in this project")
        return
    for attempt in (1, 2):
        code, body = call(base, "POST", "/v1/delivery-workflows/apply", {
            "project_id": PROJECT, "workflow_key": "research_led_discovery",
            "work_class": "project.new", "actor": ACTOR})
        check(f"apply #{attempt}", code == 200, {"code": code, "body": body})
    # The second one must be a no-op, not a second row.
    check("the second press is idempotent",
          isinstance(body, dict) and body.get("outcome") == "unchanged", body)
    # And the confirmation must be readable — this said "New project.new work now uses
    # 'research_led_discovery'." beside a dropdown reading "Research-Led Discovery".
    note = (body or {}).get("note", "") if isinstance(body, dict) else ""
    check("its confirmation is in words, not keys",
          "project.new" not in note and "research_led_discovery" not in note, note)
    code, body = call(base, "POST", "/v1/delivery-workflows/apply", {
        "project_id": PROJECT, "workflow_key": "standard_product_delivery",
        "work_class": "project.new", "actor": ACTOR})
    check("restored to the standard default", code == 200, body)


def agent_surfaces(base: str) -> None:
    """The chat, without spending a turn: every read the panel makes to render itself."""
    print("== the agent panel's reads ==")
    code, body = call(base, "GET", f"/v1/agent/threads?project_id={PROJECT}&actor={ACTOR}")
    if not check("threads list", code == 200, body):
        return
    threads = (body or {}).get("threads") or []
    check("threads carry the id the console reopens them with",
          all(t.get("thread_id") for t in threads), threads[:2])

    if not threads:
        skip("thread messages", "no threads in this project yet")
        return
    thread_id = threads[0]["thread_id"]
    code, body = call(
        base, "GET",
        f"/v1/agent/threads/{thread_id}/messages?project_id={PROJECT}&actor={ACTOR}")
    if not check("thread messages", code == 200, body):
        return
    # THE STUCK-CHAT FIX. Without this the console promised "the answer will appear here on its
    # own" and polled in silence for twenty minutes over a turn that had already died.
    check("the thread says whether a turn is actually running",
          isinstance(body, dict) and isinstance(body.get("working"), bool), body)
    messages = (body or {}).get("messages") or []
    check("messages have a role and content",
          all(m.get("role") and "content" in m for m in messages), messages[:1])


def documents(base: str) -> None:
    """A change record that a person can read, and a PDF they can open."""
    print("== change records ==")
    code, body = call(base, "GET", f"/v1/artifacts?project_id={PROJECT}&limit=50")
    if not check("artifacts list", code == 200, body):
        return
    records = [a for a in (body or {}).get("items", [])
               if a.get("artifact_type") == "change_record"]
    if not records:
        skip("change record content", "no change record in this project yet")
        return
    artifact_id = records[0]["id"]
    code, body = call(base, "GET", f"/v1/artifacts/{artifact_id}")
    if not check("read one record", code == 200, body):
        return
    markdown = ((body or {}).get("content") or {}).get("markdown") or ""
    check("it has a body", bool(markdown.strip()))
    check("it opens with a summary", "## Summary" in markdown, markdown[:120])
    # The defect reported twice: a data structure printed instead of rendered.
    leaked = [m for m in ('{"', '": [', '": {') if m in markdown]
    check("no data structure is printed in it", not leaked, leaked)
    code, body = call(base, "GET", f"/v1/artifacts/{artifact_id}/pdf")
    check("and it downloads as a PDF",
          code == 200 and isinstance(body, dict) and body.get("_head", "").startswith("%PDF"),
          {"code": code, "body": body})


def working_means_working(base: str) -> None:
    """Agents doing research or sitting on a panel count as working.

    `_live_work` read only code_task_runs and agent_process_steps, so an agent showed WORKING
    while writing code and IDLE for the twenty minutes it spent arguing a spec.
    """
    print("== the workforce screen ==")
    code, body = call(base, "GET", f"/v1/console/v2/workforce?project_id={PROJECT}")
    if not check("workforce", code == 200, body):
        return
    agents = (body or {}).get("agents") or []
    check("it lists agents", bool(agents))
    counts = (body or {}).get("counts") or {}
    check("the counts match the list",
          sum(counts.get(k, 0) for k in ("WORKING", "STUCK", "IDLE", "AT GATE")) == len(agents),
          {"counts": counts, "listed": len(agents)})
    # Live meetings and discovery runs must be reflected. Only assertable when one is running,
    # so this reports rather than fails — a suite that fails on a quiet system gets ignored.
    meetings = [a for a in agents if "panel" in str(a.get("task") or "")]
    research = [a for a in agents if a.get("id") == "researcher"]
    print(f"          (in flight now: {len(meetings)} panel seat(s), "
          f"{len(research)} research agent(s))")


def one_real_turn(base: str) -> None:
    """The only check here that costs money, so it is opt-in.

    A turn that fails to START is the failure the console could not see: the operator's message
    was never recorded while the panel showed a turn in flight.
    """
    print("== one real agent turn (spends money) ==")
    started = time.time()
    code, body = call(base, "POST", "/v1/engine/agent/chat", {
        "project_id": PROJECT, "actor": ACTOR, "surface": "chat",
        "message": "Reply with the single word OK. Do not call any tools.",
    }, timeout=300)
    took = round(time.time() - started)
    if not check(f"the turn answered ({took}s)", code == 200, {"code": code, "body": body}):
        return
    check("it returned a thread id", bool((body or {}).get("thread_id")), body)
    messages = (body or {}).get("messages") or []
    check("the operator's message was recorded",
          any(m.get("role") == "user" for m in messages), messages[:2])
    check("and an assistant answered",
          any(m.get("role") == "assistant" for m in messages), messages[-1:] if messages else [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("BOSS_API", "http://localhost:8000"))
    parser.add_argument("--with-agent", action="store_true",
                        help="also run one real agent turn (costs one model call)")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    print(f"BOSS smoke — {base} — project {PROJECT}\n")
    page_loads(base)
    workflow_picker(base)
    agent_surfaces(base)
    documents(base)
    working_means_working(base)
    if args.with_agent:
        one_real_turn(base)
    else:
        skip("one real agent turn", "pass --with-agent; it spends a model call")

    print()
    if _skipped:
        print(f"{len(_skipped)} skipped: {', '.join(_skipped)}")
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("every surface answered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
