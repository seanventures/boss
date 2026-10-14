"""Every app BOSS starts to check keeps the app's OWN output, its environment by name, and says why.

HelpDesk AI, crun_f246d33024654f889d6a (2026-10-14): blocked with "The backend (npm install
--no-audit --no-fund --loglevel=error && npm start) exited (code 1) before it answered HTTP. Its
output ended: up to date in 142ms." — npm install's line. The app had printed "[helpdesk-ai]
failed to start: Error: …" and exited 1; app_logs held nothing of the start, no event was written,
and the Shepherd filed it "unknown". And the multi-service backend was started with HOME alone:
the SESSION_SECRET and DEMO_PASSWORD BOSS mints for the serve proof were never given on that path.
"""
from __future__ import annotations

import json
import stat
import textwrap
from contextlib import contextmanager
from pathlib import Path

import pytest

from boss_engine.services import app_start, preview_service, stack_verify, stacks
from boss_engine.services import verification_target
from boss_engine.services.llm_telemetry import set_llm_context
from boss_engine.templates import run_env

# Exactly what helpdesk-ai/.env.example declared on crun_f246d330.
HELPDESK_ENV_EXAMPLE = textwrap.dedent("""\
    APP_BASE_URL=http://localhost:3000
    PORT=3000
    HOST=0.0.0.0
    NODE_ENV=development
    DATABASE_URL=postgres://helpdesk:helpdesk@localhost:5432/helpdesk
    SESSION_SECRET=
    DEMO_PASSWORD=
    ALLOW_DEMO_SEED=true
    GOOGLE_CLIENT_ID=
    GOOGLE_CLIENT_SECRET=
    GITHUB_CLIENT_ID=
    GITHUB_CLIENT_SECRET=
    """)
PLACEHOLDER_DB = "postgres://helpdesk:helpdesk@localhost:5432/helpdesk"
SERVICES_DB = "postgres://boss_run:Zq81servicespw77@127.0.0.1:55432/run_f246"
GENERATED = "gen-not-a-real-key-0123456789abcdefghijklmnopq"

# The run's build services and a known generated value, so a test can prove neither is logged.
@pytest.fixture()
def known_values(monkeypatch):
    monkeypatch.setattr(preview_service, "_build_services_env", lambda owner: {"DATABASE_URL": SERVICES_DB})
    monkeypatch.setattr(run_env, "generate_value", lambda: GENERATED)


class _Conn:
    def __init__(self, sink):
        self.sink = sink

    def execute(self, statement, params=None):
        self.sink.append((str(statement), dict(params or {})))


class _Engine:
    def __init__(self):
        self.rows: list = []

    @contextmanager
    def begin(self):
        yield _Conn(self.rows)


@pytest.fixture()
def store(monkeypatch):
    """The current run's engine (llm_telemetry's context), capturing app_logs rows and events."""
    engine = _Engine()
    events: list = []
    from boss_engine.services import core

    monkeypatch.setattr(core, "emit_event", lambda c, p, t, a, et, eid, data: events.append(
        {"project_id": p, "type": t, "entity_id": eid, "data": json.loads(json.dumps(data, default=str))}))
    set_llm_context(db_engine=engine, project_id="proj_hd", run_id="crun_f246d33024654f889d6a")
    try:
        yield engine, events
    finally:
        set_llm_context(db_engine=None, project_id=None, run_id=None)


def _app_logs(engine) -> list[dict]:
    return [p for sql, p in engine.rows if "INSERT INTO app_logs" in sql]


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


# A fake `npm`: install prints npm's own line; start runs the app, then npm's epilogue on failure.
FAKE_NPM = textwrap.dedent("""\
    #!/bin/sh
    if [ "$1" = "install" ]; then echo "up to date in 142ms"; exit 0; fi
    if [ "$1" = "start" ]; then
      echo "> helpdesk-ai@1.0.0 start"
      python3 server.py; code=$?
      if [ $code -ne 0 ]; then
        echo "npm error Lifecycle script \\`start\\` failed with error:" >&2
        echo "npm error code $code" >&2
      fi
      exit $code
    fi
    exit 1
    """)

# HelpDesk's config check, with what it was given written as facts (never values).
SERVER = textwrap.dedent("""\
    import json, os, sys
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen = {
        "session_secret_len": len(os.environ.get("SESSION_SECRET") or ""),
        "demo_password_set": bool(os.environ.get("DEMO_PASSWORD")),
        "database_url_is_placeholder": os.environ.get("DATABASE_URL") == %(placeholder)r,
        "database_url_is_services": os.environ.get("DATABASE_URL") == %(services)r,
        "google_client_id_set": "GOOGLE_CLIENT_ID" in os.environ,
    }
    open(%(seen)r, "w").write(json.dumps(seen))
    # A leaky app: prints its key. BOSS must never keep it.
    print("booting with SESSION_SECRET=" + (os.environ.get("SESSION_SECRET") or ""), flush=True)
    if %(always_fail)r or seen["session_secret_len"] < 32:
        print("[helpdesk-ai] failed to start: Error: SESSION_SECRET must be set to at least 32 "
              "characters (BOSS generates one)", file=sys.stderr, flush=True)
        sys.exit(1)
    if not seen["demo_password_set"]:
        print("[helpdesk-ai] failed to start: Error: demoPassword is required (set DEMO_PASSWORD)",
              file=sys.stderr, flush=True)
        sys.exit(1)
    if not os.environ.get("DATABASE_URL") or seen["database_url_is_placeholder"]:
        print("[helpdesk-ai] failed to start: Error: DATABASE_URL must be set", file=sys.stderr,
              flush=True)
        sys.exit(1)

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *a):
            pass

    ThreadingHTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
    """)

COMMAND = "./npm install --no-audit --no-fund --loglevel=error && ./npm start"


def _helpdesk(tmp_path: Path, *, always_fail: bool) -> tuple[Path, Path]:
    app = tmp_path / "helpdesk-ai"
    back = app / "backend"
    seen = tmp_path / "seen.json"
    _write(back / "requirements.txt", "")
    _write(back / "npm", FAKE_NPM, 0o755)
    _write(back / "server.py", SERVER % {"placeholder": PLACEHOLDER_DB, "services": SERVICES_DB,
                                         "seen": str(seen), "always_fail": always_fail})
    _write(back / ".env.example", HELPDESK_ENV_EXAMPLE)
    _write(back / "boss.preview.command", COMMAND + "\n")
    _write(app / "frontend" / "package.json", json.dumps({"dependencies": {"react": "1"}}))
    return app, seen


class _Sandbox:
    def run(self, command, *, cwd=None, timeout=None, env=None):
        return type("R", (), {"ok": True, "exit_code": 0, "stdout": "", "stderr": "",
                              "command": command})()


def _verify(tmp_path, monkeypatch, *, always_fail: bool):
    app, seen = _helpdesk(tmp_path, always_fail=always_fail)
    plan = stacks.stack_plan(app)
    assert plan.backend_command == ["sh", "-lc", COMMAND]
    monkeypatch.setattr(verification_target, "harden_web_package", lambda d: {"changed": []})
    results, record = stack_verify.run_stack_plan(_Sandbox(), plan, start_timeout=30)
    return results, record, seen


# ---- 1. the app's own words, not npm install's ---------------------------------------------------

def test_the_failure_quotes_the_apps_own_error_not_the_install_tail(tmp_path, monkeypatch,
                                                                    known_values, store):
    results, record, _seen = _verify(tmp_path, monkeypatch, always_fail=True)
    failed = results[-1]
    assert not failed.ok
    first = failed.stderr.splitlines()[0]
    assert first.startswith("The backend exited (code 1) before it answered HTTP: "
                            "[helpdesk-ai] failed to start: Error: SESSION_SECRET must be set")
    # The blocked build's headline is that line, not "up to date in 142ms".
    headline = verification_target.checks_headline(verification_target.failed_check(failed))
    assert "SESSION_SECRET must be set" in headline and "up to date" not in headline
    # npm's own epilogue is not the reason, and the install's output is not the app's.
    start = record["backend"]["start"]
    assert start["error_lines"][-1].startswith("[helpdesk-ai] failed to start")
    assert not any("up to date" in ln for ln in start["output_tail"])
    assert start["install_tail"] == ["up to date in 142ms"]
    assert start["exit_code"] == 1 and start["path"] == "verify"


def test_the_start_is_in_app_logs_and_an_event_with_the_run_id(tmp_path, monkeypatch,
                                                                known_values, store):
    engine, events = store
    _verify(tmp_path, monkeypatch, always_fail=True)
    rows = _app_logs(engine)
    assert rows and all(r["r"] == "crun_f246d33024654f889d6a" for r in rows)
    assert any("SESSION_SECRET must be set" in r["m"] for r in rows)
    assert any("exited (code 1)" in r["m"] and "SESSION_SECRET (generated)" in r["m"] for r in rows)
    starts = [e for e in events if e["type"] == app_start.EVENT]
    assert len(starts) == 1 and starts[0]["entity_id"] == "crun_f246d33024654f889d6a"
    assert starts[0]["data"]["ok"] is False and starts[0]["data"]["exit_code"] == 1


def test_the_environment_record_names_each_variable_and_where_it_came_from(tmp_path, monkeypatch,
                                                                          known_values, store):
    _results, record, _seen = _verify(tmp_path, monkeypatch, always_fail=True)
    env = record["backend"]["start"]["env"]
    assert env["SESSION_SECRET"] == "generated"
    assert env["DEMO_PASSWORD"] == "generated (demo login)"
    assert env["DATABASE_URL"] == "build services"
    assert env["GOOGLE_CLIENT_ID"] == "unset (declared in .env.example)"
    assert env["ALLOW_DEMO_SEED"] == "unset (declared in .env.example)"
    assert env["PORT"] == "launcher"


def test_no_secret_value_is_ever_kept(tmp_path, monkeypatch, known_values, store):
    engine, events = store
    results, record, _seen = _verify(tmp_path, monkeypatch, always_fail=True)
    kept = json.dumps([engine.rows, events, record, results[-1].stderr], default=str)
    assert GENERATED not in kept, "a generated key reached a log"
    assert "Zq81servicespw77" not in kept, "the build services password reached a log"
    # The leaky app's line is kept, with the key cut out.
    assert "booting with SESSION_SECRET=[REDACTED]" in kept


# ---- 2. the one resolver: what HelpDesk's backend is now given ------------------------------------

def test_the_backend_gets_the_declared_environment_and_starts(tmp_path, monkeypatch,
                                                              known_values, store):
    results, record, seen = _verify(tmp_path, monkeypatch, always_fail=False)
    facts = json.loads(seen.read_text())
    assert facts == {"session_secret_len": len(GENERATED), "demo_password_set": True,
                     "database_url_is_placeholder": False, "database_url_is_services": True,
                     "google_client_id_set": False}, facts
    assert all(r.ok for r in results), results[-1].stderr
    assert record["backend"]["ok"] is True and record["backend"]["start"]["ok"] is True


def test_a_placeholder_in_env_example_is_never_a_value():
    declared = run_env.needs_from_env_example(HELPDESK_ENV_EXAMPLE)
    env = run_env.resolve(declared, purpose="proof")
    assert "DATABASE_URL" not in env.values and "DATABASE_URL" in env.unset
    assert set(env.generated) == {"SESSION_SECRET", "DEMO_PASSWORD"}
    supplied = run_env.resolve(declared, purpose="proof", supplied={"DATABASE_URL": SERVICES_DB})
    assert supplied.values["DATABASE_URL"] == SERVICES_DB


def test_every_start_path_resolves_through_the_one_resolver():
    import inspect

    from boss_engine.services import governed_code_task_service as gov

    assert "app_start_environment(" in inspect.getsource(stack_verify._launch)
    assert "app_start_environment(" in inspect.getsource(preview_service.prove_app_serves)
    assert "needs=_sneeds, bound=_sbound" in inspect.getsource(gov._run_governed_code_task)
    assert "needs=_needs, bound=_bound" in inspect.getsource(gov._template_gates)


# ---- 3. the serve proof keeps the same record ----------------------------------------------------

def test_the_serve_proof_records_and_quotes_the_app(tmp_path, known_values, store):
    engine, events = store
    root = tmp_path / "repo"
    back = root / "helpdesk"
    _write(back / "npm", FAKE_NPM, 0o755)
    _write(back / "server.py", SERVER % {"placeholder": PLACEHOLDER_DB, "services": SERVICES_DB,
                                         "seen": str(tmp_path / "seen.json"), "always_fail": True})
    _write(back / ".env.example", HELPDESK_ENV_EXAMPLE)
    _write(back / "boss.preview.command", COMMAND + "\n")
    rec = preview_service.prove_app_serves(root, ["helpdesk/server.py", "helpdesk/boss.preview.command"],
                                           timeout=20)
    assert rec["ok"] is False and rec["exit_code"] == 1
    assert "SESSION_SECRET must be set" in rec["error"] and "up to date" not in rec["error"]
    assert rec["start"]["env"]["SESSION_SECRET"] == "generated"
    assert any(e["type"] == app_start.EVENT and e["data"]["path"] == "serve_proof" for e in events)
    assert GENERATED not in json.dumps([rec, engine.rows, events], default=str)


# ---- 4. the Shepherd and diagnose_run say it ------------------------------------------------------

def test_the_shepherd_classifies_an_app_that_exited_at_start():
    from boss_engine.services import task_shepherd as ts

    error = ("the app's checks failed: npm install --no-audit --no-fund --loglevel=error && npm "
             "start — The backend exited (code 1) before it answered HTTP: [helpdesk-ai] failed")
    assert ts.classify(error) == ts.APP_START_FAILED
    assert ts.CLASS_NAMES[ts.APP_START_FAILED] == "app exited at start"
    assert [r.id for r in ts.REMEDIES[ts.APP_START_FAILED]] == ["repair_with_app_output", "escalate"]
    assert ts.classify("the app exited (code 1) before serving HTTP: Error: x") == ts.APP_START_FAILED


class _Result:
    def __init__(self, row=None, rows=None, scalar=None):
        self.row, self.rows, self._scalar = row, rows or [], scalar

    def mappings(self):
        return self

    def scalars(self):
        return self

    def first(self):
        return self.row

    def all(self):
        return self.rows

    def scalar(self):
        return self._scalar


def test_diagnose_run_surfaces_the_apps_own_error(tmp_path, monkeypatch, known_values, store):
    from datetime import datetime, timezone

    from boss_engine.tools import build_default_registry

    _engine, events = store
    _verify(tmp_path, monkeypatch, always_fail=True)
    data = next(e["data"] for e in events if e["type"] == app_start.EVENT)

    class Conn:
        def execute(self, statement, params=None):
            sql = str(statement)
            if "FROM code_task_runs WHERE id = :r" in sql:
                now = datetime.now(timezone.utc)
                return _Result({"id": "crun_f246", "project_id": "proj_hd", "title": "HelpDesk AI",
                                "status": "blocked", "phase": "verify", "minutes": 20,
                                "error": "the app's checks failed", "created_at": now,
                                "updated_at": now})
            if "event_type = :t" in sql and (params or {}).get("t") == app_start.EVENT:
                return _Result(scalar=data)
            if "FROM events" in sql:
                return _Result(scalar=None)
            if "FROM agent_turns" in sql:
                return _Result({"n": 0})
            return _Result(rows=[])

    class Ctx:
        settings = object()

        @contextmanager
        def begin(self):
            yield Conn()

    tool = {s.name: s for s in build_default_registry().specs()}["diagnose_run"]
    out = tool.handler(Ctx(), project_id="proj_hd", actor="ceo", args={"run_id": "crun_f246"})
    said = json.dumps(out, default=str)
    assert any("SESSION_SECRET must be set" in f and "SESSION_SECRET (generated)" in f
               for f in out["findings"]), out["findings"]
    assert out["app_start"]["exit_code"] == 1
    assert GENERATED not in said and "Zq81servicespw77" not in said


# ---- 5. the marker -------------------------------------------------------------------------------

@pytest.mark.parametrize("script,expect", [
    ("npm install --no-audit --no-fund --loglevel=error && npm start", True),
    ("npm ci && npm run build && node server.js", True),
    ("python3 -m pip install -r requirements.txt && python3 -m app", True),
    ("node server.js", False),
    ("npm start", False),
])
def test_an_install_in_front_of_the_start_is_marked(script, expect):
    out = app_start.marked(["sh", "-lc", script])
    assert (app_start.MARKER in out[-1]) is expect
    if expect:
        install, app = app_start.split_output(
            f"up to date in 142ms\n{app_start.MARKER}\n> start\nError: boom\n")
        assert install.strip() == "up to date in 142ms" and app.startswith("> start")
    assert app_start.marked(["node", "server.js"]) == ["node", "server.js"]


def test_the_fake_npm_is_executable(tmp_path):
    """Guards the fixture: a non-executable ./npm would fail for the wrong reason."""
    app, _ = _helpdesk(tmp_path, always_fail=True)
    assert (app / "backend" / "npm").stat().st_mode & stat.S_IXUSR
