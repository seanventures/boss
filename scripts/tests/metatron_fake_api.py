"""A scripted stand-in for the BOSS API, over real HTTP, for Metatron's tests and demo cast.

No model and no database: every route answers from the fixtures below, shaped like the real
responses (AgentChatResponse, AgentActionRunResponse, the setup view, …). `delay` makes the
turn route slow, `drop` makes it read the request and close the socket without answering (the
way a proxy or a dropped link ends a turn), and `turn_lands_after` is how many thread reads
pass before the turn's messages appear, as they do when the server commits them.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

PROJECTS = [
    {"id": "boss-dogfood", "name": "BOSS dogfood", "slug": "boss-dogfood", "org_name": "Example Org",
     "workspace_name": "Default"},
    {"id": "helpdesk", "name": "HelpDesk AI", "slug": "helpdesk", "org_name": "Example Org",
     "workspace_name": "Default"},
]

AGENTS = [
    {"role_key": "engineering_captain", "name": "Engineering Captain",
     "mission": "Ships the builds and keeps them green", "lifecycle": "active"},
    {"role_key": "qa_lead", "name": "QA Lead", "mission": "Decides what proves a feature works",
     "lifecycle": "active"},
    {"role_key": "sre", "name": "SRE", "mission": "Keeps the apps up and the alerts honest",
     "lifecycle": "active"},
]

SETUP = {
    "project_id": "boss-dogfood", "ready": True,
    "models": [{"source_id": "src_1", "provider": "anthropic", "label": "Claude (subscription)",
                "kind": "subscription", "model": "claude-sonnet", "used_for": ["chat"],
                "pill": {"state": "ok", "tone": "green", "label": "Working", "tip": ""},
                "source": "connected"}],
    "jobs": [{"job": "chat", "label": "Chat", "more": False, "choice": None, "metered": False,
              "resolved": {"engine": "claude_cli", "model": "claude-sonnet",
                           "label": "Claude Sonnet (subscription)", "why": "Auto: the subscription answers chat"},
              "options": [{"engine": "claude_cli", "model": "claude-sonnet", "label": "Claude Sonnet (subscription)", "metered": False},
                          {"engine": "ollama", "model": "qwen2.5-coder:7b", "label": "Qwen 2.5 Coder 7B (local)", "metered": False},
                          {"engine": "openai", "model": "gpt-5-mini", "label": "GPT-5 mini (API key)", "metered": True}]}],
    "budget": {"period": "day", "spent_usd": 1.84, "cap_usd": 25.0, "state": "ok", "resets_at": "2026-10-24T00:00:00Z"},
    "embedding_model": {"configured": True, "search": True, "label": "Ollama", "model": "nomic-embed-text"},
    "repository": {"connection": {"id": "ghc_1", "auth": "app", "account_login": "example-org", "source": "connected"},
                   "repos": [{"full_name": "example-org/helpdesk", "default": True,
                              "checks": {}, "pill": {"state": "ok", "tone": "green", "label": "Read, write, PRs", "tip": ""}}]},
}

TURNS: dict[str, dict[str, Any]] = {
    "stuck": {
        "reply": "One build is waiting on a failed step: **HelpDesk AI**, step `build`. "
                 "GitHub refused the push (HTTP 403) because the App's install lost write "
                 "access to `example-org/helpdesk`.\n\nThe saved work is intact, so a retry "
                 "pushes it without rebuilding:\n\n```diff\n@@ -12,4 +12,4 @@ steps:\n   - id: build\n"
                 "-    push: token\n+    push: github_app\n     retries: 2\n```",
        "tool_calls": [{"tool": "list_running_work", "args": {"project_id": "boss-dogfood"}, "ok": True, "error": ""},
                       {"tool": "describe_workflow_run", "args": {"run_id": "wr_42"}, "ok": True, "error": ""},
                       {"tool": "test_github_access", "args": {"repo": "example-org/helpdesk"}, "ok": False,
                        "error": "403: Resource not accessible by integration"}],
        "proposed_actions": [{"id": "pa_1", "tool": "retry_workflow_step", "args": {"run_id": "wr_42", "step_id": "build"},
                              "label": "Retry the build step", "why": "the saved work is pushed; no rebuild"}],
        "next_steps": [{"id": "ns_1", "label": "Investigate further", "depth": "investigate",
                        "message": "Investigate further"}],
    },
    "plan": {
        "reply": "Here is the plan for the **support inbox** feature. Nothing starts until you say so.",
        "pending_confirmation": {"plan_id": "plan_7", "summary": "Add a support inbox to HelpDesk AI", "phase_count": 4,
                                 "phases": [{"title": "Spec the inbox with the QA Lead"},
                                            {"title": "Build the API and the screen", "depends_on": [0]},
                                            {"title": "Test against the acceptance list", "depends_on": [1]},
                                            {"title": "Open the pull request", "depends_on": [2]}]},
    },
    "three projects": {
        "reply": "Done. I created **three projects**: `banking-core`, `banking-web` and `banking-ops`.",
        "actions": [{"kind": "create_project", "task_id": None}, {"kind": "create_project"}, {"kind": "create_project"}],
    },
}
DEFAULT_TURN = {"reply": "I read the project and nothing needs you right now."}

ACTION_RESULT = {"ok": True, "tool": "retry_workflow_step",
                 "result": {"ok": True, "summary": "Retried **build**. It pushed the saved work.",
                            "steps": [{"step_id": "spec", "status": "succeeded"},
                                      {"step_id": "build", "status": "running"},
                                      {"step_id": "test", "status": "pending"},
                                      {"step_id": "deploy", "status": "pending"}]}}

RUN = {"id": "wr_42", "project_id": "boss-dogfood", "workflow_key": "build_app", "workflow_name": "HelpDesk AI build",
       "status": "running", "created_at": "2026-10-23T09:12:00Z",
       "steps": [{"step_id": "spec", "status": "succeeded"}, {"step_id": "build", "status": "running"},
                 {"step_id": "test", "status": "pending"}, {"step_id": "deploy", "status": "pending"}]}

DECISIONS = [{"id": "dec_7", "status": "pending", "risk_tier": 2, "question": "Deploy HelpDesk AI to production?"},
             {"id": "dec_8", "status": "delegated", "risk_tier": 1, "question": "Raise the build budget to $40/day?"}]

ALERTS = {"project_id": "boss-dogfood", "state": "firing", "counts": {"firing": 1, "pending": 1, "needs_attention": 1},
          "alerts": [{"id": "al_1", "sev": "sev2", "state": "firing", "state_word": "Firing", "app": "helpdesk",
                      "summary": "p95 latency above 800 ms for 10 min"},
                     {"id": "al_2", "sev": "sev4", "state": "pending", "state_word": "Pending", "app": "helpdesk",
                      "summary": "Disk 81% on the preview host"}]}


class FakeBoss:
    """Start with `with FakeBoss() as boss:`; boss.url is the API origin."""

    def __init__(self, *, delay: float = 0.0, drop: bool = False, turn_lands_after: int = 0,
                 working: bool = True, progress: Optional[list[str]] = None, port: int = 0):
        self.delay = delay
        self.drop = drop
        self.turn_lands_after = turn_lands_after
        self.working = working
        self.progress_labels = list(progress or [])
        self.calls: list[dict[str, Any]] = []
        #: The keys requests carried (compared by tests, never printed).
        self.keys: set[str] = set()
        self.messages: list[dict[str, Any]] = []
        self._pending: list[dict[str, Any]] = []
        self._reads = 0
        self.thread_id = "athr_demo"
        self._server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self._server.daemon_threads = True
        # A dropped turn closes its socket mid-request; that is the point, not an error.
        self._server.handle_error = lambda *_args: None
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> "FakeBoss":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def turn_for(self, message: str) -> dict[str, Any]:
        lowered = message.lower()
        chosen = next((t for key, t in TURNS.items() if key in lowered), DEFAULT_TURN)
        base = {"thread_id": self.thread_id, "reply": "", "actions": [], "pending_confirmation": None,
                "proposed_actions": [], "messages": [], "tool_calls": [], "next_steps": [],
                "act_id": None, "cited_acts": []}
        return {**base, **chosen}

    def _record_turn(self, message: str, turn: dict[str, Any]) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        number = len(self.messages) + len(self._pending)
        user = {"id": f"amsg_{number + 1}", "role": "user", "content": message, "tool_calls": [],
                "actions": [], "created_at": now}
        reply = {"id": f"amsg_{number + 2}", "role": "assistant", "content": turn["reply"],
                 "tool_calls": turn.get("tool_calls") or [], "actions": turn.get("actions") or [],
                 "created_at": now, "proposed_actions": turn.get("proposed_actions") or []}
        if self.turn_lands_after:
            self._pending = [user, reply]
        else:
            self.messages += [user, reply]

    def _handler(self):
        boss = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return None

            def _send(self, status: int, payload: Any) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> Any:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                return json.loads(raw) if raw else None

            def _route(self, method: str) -> None:
                parts = urlsplit(self.path)
                path, query = parts.path, parse_qs(parts.query)
                body = self._body() if method in {"POST", "PUT"} else None
                boss.calls.append({"method": method, "path": path, "query": query, "body": body,
                                   "has_key": bool(self.headers.get("X-Boss-Api-Key"))})
                boss.keys.add(str(self.headers.get("X-Boss-Api-Key") or ""))
                if path == "/health":
                    return self._send(200, {"status": "ok", "database_connected": True,
                                            "github_configured": True, "openai_configured": False})
                if path == "/v1/identity/me":
                    return self._send(200, {"principal_id": "sa_host", "kind": "service_account",
                                            "display_name": "Host tools", "email": None})
                if path == "/v1/projects":
                    return self._send(200, {"items": PROJECTS})
                if path == "/v1/setup":
                    return self._send(200, SETUP)
                if path == "/v1/setup/jobs/chat" and method == "PUT":
                    choice = (body or {}).get("choice")
                    job = dict(SETUP["jobs"][0])
                    if choice:
                        option = next((o for o in job["options"] if o["engine"] == choice.get("engine")), job["options"][0])
                        job = {**job, "choice": choice, "resolved": {**option, "why": "You chose it"}}
                    return self._send(200, {**SETUP, "jobs": [job]})
                if path == "/v1/agents":
                    return self._send(200, {"project_id": query.get("project_id", [""])[0], "items": AGENTS})
                if path == "/v1/agent/threads/ensure":
                    return self._send(200, {"thread_id": boss.thread_id})
                if path.endswith("/progress"):
                    steps = []
                    if boss.progress_labels:
                        steps = [{"id": len(boss.progress_labels), "label": boss.progress_labels.pop(0), "at": None}]
                    return self._send(200, {"thread_id": boss.thread_id, "steps": steps,
                                            "cursor": steps[-1]["id"] if steps else 0, "investigation": None})
                if path.endswith("/messages"):
                    boss._reads += 1
                    if boss._pending and boss._reads > boss.turn_lands_after:
                        boss.messages += boss._pending
                        boss._pending = []
                    return self._send(200, {"thread_id": boss.thread_id, "messages": boss.messages,
                                            "pending_confirmation": None, "proposed_actions": [],
                                            "working": bool(boss._pending) or boss.working})
                if path == "/v1/metatron/session":
                    message = str((body or {}).get("message") or "")
                    if (body or {}).get("confirm_plan_id"):
                        turn = {**boss.turn_for(""), "reply": "Confirmed. **4 phases** dispatched; the "
                                "first is with the QA Lead now.", "actions": [{"kind": "dispatch_plan", "plan_id": "plan_7"}]}
                    else:
                        turn = boss.turn_for(message)
                    boss._record_turn(message, turn)
                    if boss.drop:
                        self.close_connection = True
                        self.connection.close()
                        return None
                    if boss.delay:
                        time.sleep(boss.delay)
                    return self._send(200, turn)
                if path == "/v1/engine/agent/action":
                    return self._send(200, ACTION_RESULT)
                if path == "/v1/tasks":
                    return self._send(200, {"project_id": query.get("project_id", [""])[0], "items": [
                        {"id": "task_1", "status": "running", "title": "Add a support inbox"}]})
                if path == "/v1/decisions/inbox":
                    return self._send(200, {"project_id": query.get("project_id", [""])[0], "items": DECISIONS})
                if path == "/v1/workflow-runs":
                    return self._send(200, {"project_id": query.get("project_id", [""])[0], "items": [RUN]})
                if path.startswith("/v1/workflow-runs/"):
                    return self._send(200, RUN)
                if path == "/v1/alerts":
                    return self._send(200, ALERTS)
                return self._send(404, {"detail": f"no fake route for {method} {path}"})

            def do_GET(self) -> None:  # noqa: N802
                self._route("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._route("POST")

            def do_PUT(self) -> None:  # noqa: N802
                self._route("PUT")

        return Handler
