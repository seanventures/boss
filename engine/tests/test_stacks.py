"""WHAT BOSS CAN BUILD, and how it builds an app made of more than one service (services.stacks).

crun_78d197f7614f40f38993: login-oauth on the Python stack, a FastAPI backend plus a React/Vite
front end in `helpdesk-ai/`. The build treated the app as one npm package: a Playwright harness at
`helpdesk-ai/`, `npm install && npm run build && … && npm run test:e2e` there, the Python API
never started (every auth route 404), the web contract short of vite, the template's pytest suite
never run — an hour and $26. And a brief asking for Go would have been accepted at Start, designed
and approved, and refused only by the build.

These pin the class: one registry derived from the build code, refusals at Start, architecture
approval and the build, and a per-service plan that starts the backend before the e2e.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from boss_engine.services import stacks, toolchains, verification_target
from boss_engine.services import stack_verify

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def no_java(monkeypatch, tmp_path):
    monkeypatch.setenv(toolchains.MANIFEST_ENV, str(tmp_path / "none.json"))


@pytest.fixture
def with_java(monkeypatch, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"java": {"jdk": "21", "maven": "3.9", "gradle": "8.14"}}))
    monkeypatch.setenv(toolchains.MANIFEST_ENV, str(manifest))


# ── The registry ────────────────────────────────────────────────────────────────────────────

def _shipped_templates():
    for path in sorted((ROOT / "templates" / "apps").glob("*/boss-template.yaml")):
        yield path.parent.name, yaml.safe_load(path.read_text())


def test_every_shipped_template_stack_is_in_the_registry_and_buildable(with_java):
    seen = set()
    for name, manifest in _shipped_templates():
        for stack, spec in (manifest.get("stacks") or {}).items():
            seen.add(stack)
            assert stack in stacks.LANGUAGES, f"{name}: stack {stack!r} is not one BOSS can build"
            lang = stacks.LANGUAGES[stack]
            assert callable(lang.steps) and callable(lang.start), f"{name}: {stack} has no build code"
            known = {f.key for f in lang.frameworks}
            for fw in spec.get("frameworks") or []:
                assert fw in known, f"{name}: framework {fw!r} of {stack} is not in the registry"
            assert stacks.language_ready(stack), f"{name}: {stack} is not ready with its toolchain on"
            # A reference that carries its own manifest is detected as that language by the same
            # code the gate plans with (a fragment like hello's is adapted into the app's own).
            svc = stacks._service_in(ROOT / "templates" / "apps" / name / "stacks" / stack, ".")
            assert svc is None or svc.language == stack, (name, stack, svc)
    assert seen == {"node", "python", "java"}


def test_the_registry_reads_the_toolchains_this_image_has(no_java):
    caps = stacks.capabilities()
    by = {l["key"]: l for l in caps["languages"]}
    assert by["python"]["ready"] and by["node"]["ready"]
    assert not by["java"]["ready"] and by["java"]["enable"] == "boss toolchains enable java"
    assert "Go" in caps["not_supported"]
    block = stacks.prompt_block()
    assert "Python (FastAPI" in block and "boss toolchains enable java" in block and "Go" in block


# ── Fail fast: Start, architecture, build ──────────────────────────────────────────────────

@pytest.mark.parametrize("brief", ["Build it in Go", "A golang microservice for invoices",
                                   "Rust backend with a React UI", "a Ruby on Rails app",
                                   "an ASP.NET core API"])
def test_an_unsupported_stack_is_refused_at_start(brief, with_java):
    verdict = stacks.check_text(brief)
    assert not verdict.ok
    assert "BOSS cannot build" in verdict.message and "BOSS builds Python" in verdict.message
    with pytest.raises(stacks.StackUnsupported) as refused:
        stacks.refuse_unsupported_start(None, version_id=None,
                                        run_input={"title": "x", "directive": brief})
    assert refused.value.detail()["error"] == "stack_unsupported"
    # Started anyway, on the operator's word.
    stacks.refuse_unsupported_start(None, version_id=None,
                                    run_input={"directive": brief}, allow=True)


@pytest.mark.parametrize("brief", ["Users log in, go to the dashboard and go back",
                                   "We need a go-to-market page in React",
                                   "visit example.net for the docs", "Express checkout, in Python",
                                   "not in Go — use FastAPI", "a rust-proof paint catalogue",
                                   "a JavaScript widget"])
def test_ordinary_words_are_not_a_stack(brief, with_java):
    assert stacks.check_text(brief).ok, stacks.check_text(brief).message


def test_java_when_the_toolchain_is_off_says_how_to_turn_it_on(no_java):
    verdict = stacks.check_text("a Spring Boot service")
    assert not verdict.ok and verdict.off == ["java"]
    assert "boss toolchains enable java" in verdict.message
    assert not stacks.check_text("", template_stack="java").ok


def test_a_workflow_that_does_not_build_may_discuss_go(with_java):
    class Conn:
        def execute(self, *_a, **_k):
            class R:
                def scalar(self):
                    return {"steps": [{"id": "research", "binding": {"capability": "research"}}]}
            return R()

    stacks.refuse_unsupported_start(Conn(), version_id="wv_1",
                                    run_input={"directive": "should we rewrite it in Go?"})


ARCH_GO = """## Context
Ticketing.
## Components
Stack: backend=go/gin, frontend=node/react
- api: a Go service
## Deployment
Containers.
"""
ARCH_OK_REJECTED_GO = """## Context
Ticketing.
## Components
Stack: backend=python/fastapi, frontend=node/react
## Decisions and alternatives
- **Decision** — FastAPI; **Alternatives** — Go, Rust; **Why** — the team knows Python.
"""


def test_a_go_architecture_is_refused_at_approval(with_java):
    verdict = stacks.check_architecture(ARCH_GO)
    assert not verdict.ok and verdict.unsupported == ["go"]
    assert "The architecture asks for Go" in verdict.message
    # Rejected alternatives are not the stack.
    assert stacks.check_architecture(ARCH_OK_REJECTED_GO).ok
    # No Stack line: only the sections that describe what is built are read.
    prose = "## Components\nA Go service and a Postgres database.\n## Risks\nRust was rejected.\n"
    assert stacks.check_architecture(prose).unsupported == ["go"]


def test_the_approval_fails_the_step_before_a_build(monkeypatch, with_java):
    from boss_engine.services import architecture_docs

    executed = []

    class Conn:
        def execute(self, statement, params=None):
            executed.append((str(statement), params or {}))

            class R:
                def scalar(self_inner):
                    return {"markdown": ARCH_GO}
            return R()

    review = {"id": "arch_1", "project_id": "p", "workflow_run_id": "wr", "decision_id": "d1",
              "status": "awaiting_approval", "current_version": 1, "current_artifact_id": "a1"}
    monkeypatch.setattr(architecture_docs, "_review", lambda *a, **k: dict(review))
    monkeypatch.setattr(architecture_docs, "emit_event", lambda *a, **k: None)
    architecture_docs.on_decision_answered(
        Conn(), {"id": "d1", "recommendation": {"architecture_review_id": "arch_1", "version": 1}},
        action="approve", actor="ceo", note=None)
    updates = [p for s, p in executed if "UPDATE architecture_reviews" in s]
    assert updates and "BOSS cannot build" in updates[0]["e"]
    assert not any("status = 'approved'" in s for s, _ in executed)


def test_the_architect_is_told_what_boss_can_build():
    from boss_engine.services import architecture_docs

    prompt = architecture_docs._prompt("PRD", title="t", constraints="")
    assert "WHAT BOSS CAN BUILD" in prompt and "Stack: backend=python/fastapi" in prompt


def test_the_panel_is_told_what_boss_can_build():
    from boss_engine.services import meeting_service

    base = meeting_service._make_base("Build a helpdesk", {}, {}, "")
    assert "WHAT BOSS CAN BUILD" in base and "Not supported at all" in base


TEMPLATE_BRIEF = """Apply the template "Login" (login-oauth 1.0.0) to a new app, stack python, framework fastapi.

The operator's note:
FastAPI backend plus React/Vite frontend in helpdesk-ai/ (backend/ and frontend/)

## The template's brief (brief.md)

`ui/LoginButtons.jsx` (React) … the Java stack uses Spring Boot …

---

THE PRODUCT SPEC BELOW IS THE BRIEF. Alternatives: Go.
"""


def test_the_build_guard_reads_the_request_not_the_template_material(no_java):
    assert stacks.check_build(TEMPLATE_BRIEF, template_stack="python").ok
    assert not stacks.check_build("Build the helpdesk in Go.\n\n---\n\nspec").ok
    assert not stacks.check_build("helpdesk", template_stack="java").ok  # toolchain off
    approved = "Build it.\n\n---\n\nTHE APPROVED ARCHITECTURE BELOW IS BINDING.\n" + ARCH_GO
    assert stacks.check_build(approved).unsupported == ["go"]


# ── Full-stack: the per-service plan ───────────────────────────────────────────────────────

def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _helpdesk(root: Path) -> list[str]:
    app = root / "helpdesk-ai"
    _write(app / "backend" / "requirements.txt", "fastapi==0.115.0\nuvicorn==0.30.0\n")
    _write(app / "backend" / "main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    _write(app / "backend" / "tests" / "test_health.py", "def test_ok():\n    assert True\n")
    _write(app / "frontend" / "package.json", json.dumps({
        "name": "web", "scripts": {"build": "vite build", "typecheck": "tsc --noEmit",
                                   "lint": "eslint .", "test": "echo \"Error: no test specified\" && exit 1",
                                   "test:e2e": "playwright test"},
        "dependencies": {"react": "^18.3.1"},
        "devDependencies": {"vite": "^5.3.1", "@playwright/test": "^1.45.0"}}))
    _write(app / "frontend" / "playwright.config.ts", "export default { webServer: {} }\n")
    return ["helpdesk-ai/backend/main.py", "helpdesk-ai/frontend/package.json"]


def test_a_python_and_react_app_gets_per_service_commands_and_its_backend_started(tmp_path):
    changed = _helpdesk(tmp_path)
    plan = verification_target.resolve_gate_plan({}, "HelpDesk AI", tmp_path, changed=changed)
    assert plan.source == "stack" and plan.runs_steps and plan.error is None
    app = tmp_path / "helpdesk-ai"
    assert plan.cwd == app
    steps = [(c, d.relative_to(app).as_posix()) for c, d in plan.steps]
    assert steps == [
        (stacks.PIP_INSTALL, "backend"),
        (stacks.PYTEST, "backend"),
        (verification_target.NPM_INSTALL_CMD, "frontend"),
        ("npm run typecheck", "frontend"),
        ("npm run lint", "frontend"),
        ("npm run build", "frontend"),
    ]
    stack = plan.stack
    assert stack.backend.rel == "backend" and stack.backend.framework == "fastapi"
    assert "python3 -m uvicorn main:app" in stack.backend_command[-1]
    assert [(c, d.name) for c, d in stack.e2e_steps] == [
        (verification_target.PLAYWRIGHT_INSTALL_CMD, "frontend"), ("npm run test:e2e", "frontend")]
    assert not stack.serve_frontend  # its Playwright config serves it
    # Not one npm package at the app root.
    assert not any(c.startswith("npm") and d == app for c, d in plan.steps)
    # A template on the python stack runs in the backend.
    assert stack.service_for("python").rel == "backend"


def test_the_backend_is_up_before_the_e2e_runs_and_the_e2e_is_told_where(tmp_path, monkeypatch):
    changed = _helpdesk(tmp_path)
    plan = verification_target.resolve_gate_plan({}, "t", tmp_path, changed=changed).stack
    events: list = []

    class Result:
        def __init__(self, ok=True):
            self.ok, self.exit_code, self.stdout, self.stderr = ok, 0 if ok else 1, "", ""

    class Sandbox:
        def run(self, command, *, cwd=None, timeout=None, env=None):
            events.append(("run", command, Path(cwd).name, dict(env or {})))
            return Result()

    import contextlib

    @contextlib.contextmanager
    def fake_backend(p, timeout=None):
        events.append(("backend_up", p.backend_command[-1]))
        s = stack_verify.Session("http://127.0.0.1:5555", 5555, p.backend_command[-1])
        yield s
        events.append(("backend_down",))

    monkeypatch.setattr(stack_verify, "backend_running", fake_backend)
    monkeypatch.setattr(verification_target, "harden_web_package", lambda d: {"changed": []})
    results, record = stack_verify.run_stack_plan(Sandbox(), plan)
    kinds = [e[0] if e[0] != "run" else e[1] for e in events]
    up = kinds.index("backend_up")
    assert kinds.index("npm run build") < up < kinds.index("npm run test:e2e")
    assert kinds[-1] == "backend_down"
    e2e = next(e for e in events if e[0] == "run" and e[1] == "npm run test:e2e")
    assert e2e[2] == "frontend" and e2e[3]["BACKEND_URL"] == "http://127.0.0.1:5555"
    assert all(e[3] == {} for e in events if e[0] == "run" and e[1] != "npm run test:e2e"
               and "playwright" not in e[1])
    assert all(r.ok for r in results)


def test_a_backend_that_does_not_start_fails_with_its_words(tmp_path, monkeypatch):
    changed = _helpdesk(tmp_path)
    plan = verification_target.resolve_gate_plan({}, "t", tmp_path, changed=changed).stack

    class Sandbox:
        def run(self, command, *, cwd=None, timeout=None, env=None):
            return type("R", (), {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""})()

    import contextlib

    @contextlib.contextmanager
    def dead(p, timeout=None):
        raise stack_verify.BackendDidNotStart("The backend exited (code 1): ModuleNotFoundError")
        yield  # pragma: no cover

    monkeypatch.setattr(stack_verify, "backend_running", dead)
    monkeypatch.setattr(verification_target, "harden_web_package", lambda d: {"changed": []})
    results, record = stack_verify.run_stack_plan(Sandbox(), plan)
    assert not results[-1].ok and "ModuleNotFoundError" in results[-1].stderr
    assert record["backend"]["ok"] is False


def test_the_backend_really_starts_and_answers(tmp_path):
    """The real launcher (preview_service.launch_app) on a stdlib server: up, then down."""
    app = tmp_path / "app"
    _write(app / "backend" / "requirements.txt", "")
    _write(app / "backend" / "boss.preview.command",
           'python3 -m http.server "$PORT" --bind 127.0.0.1\n')
    _write(app / "frontend" / "package.json", json.dumps({"dependencies": {"react": "1"}}))
    plan = stacks.stack_plan(app)
    assert plan.backend_command == ["sh", "-lc", 'python3 -m http.server "$PORT" --bind 127.0.0.1']
    with stack_verify.backend_running(plan, timeout=30) as session:
        assert stack_verify.answers(session.port)
        port = session.port
    assert not stack_verify.answers(port)


def test_a_go_service_in_the_tree_fails_the_plan_in_words(tmp_path, with_java):
    app = tmp_path / "app"
    _write(app / "api" / "go.mod", "module x\n")
    _write(app / "web" / "package.json", json.dumps({"dependencies": {"react": "1"}}))
    plan = verification_target.resolve_gate_plan({}, "t", tmp_path, changed=["app/api/go.mod"])
    assert plan.source == "stack" and "Go, which BOSS cannot build" in plan.error


def test_a_full_stack_brief_with_no_services_yet_says_what_is_missing(tmp_path):
    layout = stacks.build_layout(TEMPLATE_BRIEF, template_stack="python")
    assert layout is not None and layout.backend_language == "python"
    assert (layout.backend_dir, layout.frontend_dir) == ("backend", "frontend")
    _write(tmp_path / "helpdesk-ai" / "package.json", json.dumps({"devDependencies": {"@playwright/test": "1"}}))
    plan = verification_target.resolve_gate_plan({}, "t", tmp_path, changed=["helpdesk-ai/package.json"],
                                                  layout=layout)
    assert plan.source == "stack_missing" and "backend/" in plan.error


def test_the_test_author_and_coder_are_told_the_layout():
    from boss_engine.services import governed_code_task_service as g

    layout = stacks.build_layout(TEMPLATE_BRIEF, template_stack="python")
    note = stacks.layout_prompt(layout, "helpdesk-ai", template_stack="python",
                                template_command="python3 -m pytest -q tests/template_login_oauth")
    assert "helpdesk-ai/backend/" in note and "helpdesk-ai/frontend/" in note
    assert "no package.json, no Playwright config, no tests" in note
    assert "BACKEND_URL" in note and "tests/template_login_oauth" in note
    qa = g._qa_prompt("t", "i", [], "steps", stack_note=note)
    assert "Do NOT put tests at the monorepo root" not in qa and note in qa
    assert "unittest" not in qa.split("THIS APP IS TWO SERVICES")[0]
    assert g._app_id_hint({"instructions": TEMPLATE_BRIEF}, {}, layout) == "helpdesk-ai"


# ── Single-service apps: exactly as before ─────────────────────────────────────────────────

def test_a_node_only_app_is_unchanged(tmp_path):
    _write(tmp_path / "web" / "package.json", json.dumps({
        "scripts": {"build": "vite build"}, "dependencies": {"react": "1", "express": "4"}}))
    plan = verification_target.resolve_gate_plan({"project_kind": "web"}, "a UI", tmp_path,
                                                  changed=["web/package.json"])
    assert plan.source == "default"
    assert plan.commands == verification_target.WEB_TEST_COMMANDS
    assert plan.cwd == tmp_path / "web"
    # A Node backend and a React UI named in words do not force a layout: the tree decides.
    assert stacks.build_layout("an Express API with a React front end") is None


def test_a_python_only_app_is_unchanged(tmp_path):
    _write(tmp_path / "svc" / "requirements.txt", "flask\n")
    _write(tmp_path / "svc" / "app.py", "from flask import Flask\napp = Flask(__name__)\n")
    _write(tmp_path / "conftest.py", "")
    plan = verification_target.resolve_gate_plan({"project_kind": "python"}, "an API", tmp_path,
                                                  changed=["svc/app.py"])
    assert plan.source == "default" and plan.commands == [verification_target.PYTEST_CMD]
    assert stacks.build_layout("a Flask API for invoices", template_stack="python") is None


def test_a_monorepo_of_many_apps_is_not_one_multi_service_app(tmp_path):
    for i in range(6):
        _write(tmp_path / "apps" / f"a{i}" / "package.json", json.dumps({"dependencies": {"express": "4"}}))
    assert stacks.find_stack_root(tmp_path, ["apps/a1/package.json"]) is None


# ── The adapter registry ───────────────────────────────────────────────────────────────────

def test_every_framework_the_build_code_knows_belongs_to_an_adapter():
    covered = {(c.language, f) for a in stacks.ADAPTERS for c in a.components for f in c.frameworks}
    for lang in stacks.LANGUAGES.values():
        for f in lang.frameworks:
            assert (lang.key, f.key) in covered, f"{lang.key}/{f.key} has no adapter"
    for a in stacks.ADAPTERS:
        assert a.support_level in stacks.SUPPORT_LEVELS
        for c in a.components:
            known = {f.key for f in stacks.LANGUAGES[c.language].frameworks}
            assert set(c.frameworks) <= known, (a.id, c)


def test_the_initial_levels_and_go_is_unsupported():
    level = {a.id: a.support_level for a in stacks.ADAPTERS}
    assert level["react-vite+express+postgres"] == "experimental"
    assert level["react-vite+fastapi+postgres"] == "experimental"
    assert level["nextjs+fastapi+postgres"] == "experimental"
    assert level["java-spring"] == "experimental"
    assert not any(a.support_level == "certified" for a in stacks.ADAPTERS)
    unsupported = {u["id"]: u for u in stacks.unsupported_adapters()}
    assert unsupported["go"]["support_level"] == "unsupported"
    assert unsupported["go"]["detection_files"] == ["go.mod"]


def test_an_adapter_is_derived_from_the_build_code(with_java):
    entry = stacks.adapter("react-vite+fastapi+postgres").as_dict()
    back = next(c for c in entry["components"] if c["role"] == "backend")
    front = next(c for c in entry["components"] if c["role"] == "frontend")
    assert back["commands"] is stacks.LANGUAGES["python"].commands or \
        back["commands"] == dict(stacks.LANGUAGES["python"].commands)
    assert back["commands"]["install"] == stacks.PIP_INSTALL and back["commands"]["test"] == stacks.PYTEST
    assert back["detection_files"] == list(stacks.PYTHON_MANIFESTS)
    assert "python3 -m uvicorn" in back["start"] and back["health_path"].startswith("/")
    assert front["commands"]["install"] == verification_target.NPM_INSTALL_CMD
    assert front["commands"]["e2e"] == "npm run test:e2e" and front["package_manager"] == "npm"
    assert entry["ready"] is True and entry["support_level"] == "experimental"
    nxt = stacks.adapter("nextjs+fastapi+postgres").as_dict()
    assert "next start" in nxt["components"][0]["serve"] or "npm run start" in nxt["components"][0]["serve"]


def test_the_promotion_rule_needs_a_recorded_passing_run():
    passing = {"stack": "react-vite+express+postgres", "run_id": "wfr_1", "result": "success",
               "tests_passed": 12, "tests_total": 12, "deployment_smoke_test": "pass"}
    assert stacks.certification_evidence([passing], "react-vite+express+postgres") == ["wfr_1"]
    for broken in ({"tests_total": None}, {"tests_passed": 11}, {"deployment_smoke_test": "not-run"},
                   {"result": "failure"}, {"tests_total": 0, "tests_passed": 0}):
        assert stacks.certification_evidence([{**passing, **broken}],
                                             "react-vite+express+postgres") == []


def test_apps_are_matched_to_their_adapter(tmp_path):
    _helpdesk(tmp_path)
    services = stacks.detect_services(tmp_path / "helpdesk-ai")
    assert stacks.adapter_for(services).id == "react-vite+fastapi+postgres"
    _write(tmp_path / "n" / "web" / "package.json",
           json.dumps({"dependencies": {"next": "14", "react": "18"}}))
    _write(tmp_path / "n" / "api" / "requirements.txt", "fastapi\n")
    services = stacks.detect_services(tmp_path / "n")
    assert {s.framework for s in services} == {"next", "fastapi"}
    assert stacks.adapter_for(services).id == "nextjs+fastapi+postgres"
    plan = stacks.stack_plan(tmp_path / "n")
    assert [c for c, _ in plan.steps if "npm" in c][0] == verification_target.NPM_INSTALL_CMD


def test_the_messages_say_the_support_level(with_java):
    assert "experimental" in stacks.check_text("build it in Go").message


def test_supported_stacks_doc_matches_the_registry():
    doc = (ROOT / "docs" / "community" / "SUPPORTED_STACKS.md").read_text(encoding="utf-8")
    assert doc == stacks.render_supported_stacks_md(), (
        "docs/community/SUPPORTED_STACKS.md is stale: run `python3 -m boss_engine.services.stacks "
        "--write-docs ../docs/community/SUPPORTED_STACKS.md` from engine/")
