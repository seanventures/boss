"""The stack a brief REQUIRES sets the gate, and BOSS never scaffolds Node into such an app.

Northgate (2026-11-15, crun_ce0f6332b459476a9768 / crun_5f8a0293c23847beb7b9, Go turned on): the
brief said "Stack: Go." and the repository held only a README and a .gitignore.

  * The first gate plan saw no go.mod yet and fell to the npm default, so the test author was
    told "BOSS's verify pipeline is npm" and wrote a package.json whose scripts called `go`.
  * BOSS's package hardening then wrote vite, @playwright/test, React types and the eslint-8
    plugins into it: build_scope read "next to its boss.preview.command" (BOSS's own deploy
    brief) as Next.js, so the Go gateway counted as a bundled front end.
  * With that package.json beside the go.mod, every later verify was `npm install && npm run
    build && …` — never `go vet` / `go test` as such.

Now a declared stack (a `Stack:` line or the spec's structured `required_stack`) is the gate
whatever manifests appear, with the toolchain on or off; a Node app with a stray go.mod and no
stack requirement keeps the npm gate; and neither the hardening nor the PRD scaffold adds Node
to a Go app.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from boss_engine.services import build_scope, prd_decomposition, toolchains
from boss_engine.services import verification_target as vt

GO_MOD = "module northgate\n\ngo 1.22\n"
#: Northgate's brief, as the run carried it: the Stack line and BOSS's own deploy paragraph
#: (whose "next to" made the build a bundled front end).
NORTHGATE = (
    "Northgate API Gateway: automate tenant API onboarding\n\n"
    "Stack: Go. (gateway and gwlint: one Go module, static binaries)\n\n"
    "### 1. `northgate-gateway` (Go, medium complexity)\n"
    "A reverse-proxy API gateway, one static binary, configured by YAML.\n\n"
    "THIS APP WILL BE DEPLOYED by BOSS after review. Write a boss.deploy.yaml IN THE APP'S OWN "
    "FOLDER — next to its boss.preview.command.\n")
NORTHGATE_SPEC = {"instructions": NORTHGATE, "acceptance_criteria": ["All tests pass"]}
#: What the test author wrote, told the gate was npm.
AGENT_PKG = {"name": "northgate-gateway", "private": True,
             "scripts": {"build": "go build ./...", "typecheck": "go vet ./...",
                         "lint": "gofmt -l . | (grep . && exit 1 || exit 0)",
                         "test": "go test ./..."}}
PKG = json.dumps({"name": "site", "scripts": {"build": "vite build", "test": "vitest run"},
                  "devDependencies": {"vite": "^5.3.1"}})


def _toolchains(tmp_path, monkeypatch, data) -> None:
    manifest = tmp_path / "toolchains.json"
    manifest.write_text(json.dumps(data))
    monkeypatch.setenv(toolchains.MANIFEST_ENV, str(manifest))
    monkeypatch.setenv(toolchains.CACHE_ROOT_ENV, str(tmp_path / "cache"))


@pytest.fixture
def go_on(tmp_path, monkeypatch):
    _toolchains(tmp_path, monkeypatch, {"go": {"go": "1.27.4"}})


@pytest.fixture
def go_off(tmp_path, monkeypatch):
    _toolchains(tmp_path, monkeypatch, {})


@pytest.fixture
def java_on(tmp_path, monkeypatch):
    _toolchains(tmp_path, monkeypatch, {"java": {"java": "21"}})


def _empty_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Northgate\n")
    (repo / ".gitignore").write_text("node_modules/\n")
    return repo


def _plan(spec, repo: Path, *, pinned=None, changed=None):
    return vt.resolve_gate_plan(spec, "KAN-9 Northgate API Gateway", repo,
                                pinned=pinned or vt.pin_verify_manifest(repo), changed=changed)


# ---- which stack is required -------------------------------------------------------------------

@pytest.mark.parametrize("spec, expected", [
    (NORTHGATE_SPEC, "go"),
    ({"instructions": "Build it.", "required_stack": [{"language": "go"}]}, "go"),
    ({"instructions": "Stack: java/spring"}, "java"),
    ({"instructions": "Stack: python/fastapi"}, "python"),
    # Node keeps the tree-driven plan; several languages are the multi-service planner's.
    ({"instructions": "Stack: node/express"}, None),
    ({"instructions": "Stack: backend=go/chi, frontend=node/react"}, None),
    # Words that only NAME a language are not a declaration.
    ({"instructions": "A gateway written in Go with a small admin page."}, None),
    ({}, None),
])
def test_the_required_stack_is_the_declared_one(spec, expected):
    assert vt.required_app_stack(spec) == expected


# ---- the gate ----------------------------------------------------------------------------------

def test_a_go_brief_on_an_empty_repo_is_gated_by_go_from_the_first_plan(tmp_path, go_on):
    plan = _plan(NORTHGATE_SPEC, _empty_repo(tmp_path))
    assert plan.source == "toolchain" and plan.commands == list(toolchains.GO_GATE)
    assert not plan.needs_human


def test_a_package_json_an_agent_adds_to_a_go_app_never_flips_the_gate(tmp_path, go_on):
    """The Northgate tree: base empty, then the test author's package.json (hardened with vite
    and Playwright) and the implementer's go.mod."""
    repo = _empty_repo(tmp_path)
    base = vt.pin_verify_manifest(repo)
    (repo / "package.json").write_text(json.dumps(
        {**AGENT_PKG, "devDependencies": {"vite": "^5.3.1", "@playwright/test": "^1.45.0"}}))
    (repo / "go.mod").write_text(GO_MOD)
    (repo / "cmd").mkdir()
    (repo / "cmd" / "main.go").write_text("package main\n\nfunc main() {}\n")
    plan = _plan(NORTHGATE_SPEC, repo, pinned=base,
                 changed=["package.json", "go.mod", "cmd/main.go"])
    assert plan.source == "toolchain" and plan.commands == list(toolchains.GO_GATE)
    assert plan.cwd == repo
    assert not any(c.startswith("npm") for c in plan.commands)


def test_a_structured_required_stack_sets_the_gate_too(tmp_path, go_on):
    repo = _empty_repo(tmp_path)
    (repo / "package.json").write_text(json.dumps(AGENT_PKG))
    plan = _plan({"instructions": "Build the gateway.", "required_stack": ["go"]}, repo)
    assert plan.commands == list(toolchains.GO_GATE)


def test_with_go_off_a_go_brief_stops_for_a_person_not_as_npm(tmp_path, go_off):
    repo = _empty_repo(tmp_path)
    (repo / "package.json").write_text(json.dumps(AGENT_PKG))
    plan = _plan(NORTHGATE_SPEC, repo)
    assert plan.needs_human and plan.source == "toolchain_missing"
    assert "(its brief requires Go)" in plan.error
    assert "`boss toolchains enable go`" in plan.error


def test_with_go_off_and_a_go_mod_the_words_name_the_go_mod(tmp_path, go_off):
    repo = _empty_repo(tmp_path)
    (repo / "go.mod").write_text(GO_MOD)
    plan = _plan(NORTHGATE_SPEC, repo)
    assert plan.needs_human and "(it has a go.mod)" in plan.error


def test_a_java_brief_on_an_empty_repo_is_gated_by_maven(tmp_path, java_on):
    repo = _empty_repo(tmp_path)
    (repo / "package.json").write_text(json.dumps(AGENT_PKG))
    plan = _plan({"instructions": "Stack: java/spring"}, repo)
    assert plan.source == "toolchain" and plan.commands == list(toolchains.MAVEN_GATE)


def test_a_python_brief_with_a_package_json_is_gated_as_python(tmp_path, go_off):
    repo = _empty_repo(tmp_path)
    (repo / "package.json").write_text(PKG)
    (repo / "requirements.txt").write_text("fastapi\n")
    plan = _plan({"instructions": "Stack: python/fastapi"}, repo)
    assert plan.commands in ([vt.PYTHON_TEST_CMD], [vt.PYTEST_CMD])


@pytest.mark.parametrize("fixture", ["go_on", "go_off"])
def test_a_hugo_style_node_app_with_no_stack_requirement_stays_npm(tmp_path, fixture, request):
    request.getfixturevalue(fixture)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text(GO_MOD)
    (repo / "package.json").write_text(PKG)
    plan = _plan({"instructions": "Add a search box to the docs site."}, repo)
    assert plan.commands and plan.commands[0].startswith("npm ")


def test_explicit_test_commands_still_win(tmp_path, go_on):
    plan = _plan({**NORTHGATE_SPEC, "test_commands": ["make test"]}, _empty_repo(tmp_path))
    assert plan.source == "task" and plan.commands == ["make test"]


# ---- no Node scaffolding for a non-Node app ----------------------------------------------------

def test_northgates_brief_is_not_a_bundled_front_end():
    scope = build_scope.decide("KAN-9 Northgate API Gateway", NORTHGATE)
    assert scope["bundler"] is False


@pytest.mark.parametrize("brief, bundler", [
    ("React + Vite + Tailwind front end", True),
    ("A Next.js storefront", True),
    ("Put the config file next to the binary.", False),
    ("Stack: Go.\nA Go gateway with a React admin dashboard.", False),
])
def test_the_bundler_scope_reads_front_end_words_only(brief, bundler):
    assert build_scope.decide("App", brief)["bundler"] is bundler


def test_the_hardening_adds_no_node_deps_to_a_go_apps_package_json(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps(AGENT_PKG, indent=2) + "\n")
    before = (tmp_path / "package.json").read_text()
    token = build_scope.set_scope(build_scope.decide("KAN-9 Northgate API Gateway", NORTHGATE))
    try:
        vt.ensure_verify_script(tmp_path)
        out = vt.harden_web_package(tmp_path)
    finally:
        build_scope._SCOPE.reset(token)
    assert out["changed"] == []
    assert (tmp_path / "package.json").read_text() == before


def test_the_prd_scaffold_is_not_seeded_into_a_go_app():
    go_brief = "Build the gateway and its admin dashboard.\n\nStack: Go.\n"
    assert prd_decomposition._looks_like_web_app(go_brief) is False
    assert prd_decomposition._looks_like_web_app("Build an admin dashboard.") is True


# ---- a required stack in a multi-service tree (PR #142 review) ---------------------------------

WEB_PKG = json.dumps({"name": "web", "scripts": {"build": "vite build",
                                                 "test:e2e": "playwright test"},
                      "devDependencies": {"vite": "^5.3.1", "react": "^18.3.1"}})


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    for rel, body in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(body)
    return repo


@pytest.mark.parametrize("changed", [None, ["api/main.py", "web/src/main.tsx"]])
def test_a_python_api_beside_a_web_front_end_keeps_the_multi_service_plan(tmp_path, go_off,
                                                                           changed):
    repo = _tree(tmp_path, {"api/requirements.txt": "fastapi\nuvicorn\n",
                            "api/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
                            "web/package.json": WEB_PKG, "web/src/main.tsx": "export {}\n"})
    plan = _plan({"instructions": "Stack: Python"}, repo, changed=changed)
    assert plan.source == "stack"
    dirs = {d for _, d in plan.steps}
    assert repo / "api" in dirs and repo / "web" in dirs   # the front end is built too


def test_a_go_backend_beside_a_web_front_end_keeps_the_multi_service_plan(tmp_path, go_on):
    repo = _tree(tmp_path, {"backend/go.mod": GO_MOD,
                            "backend/main.go": "package main\n\nimport \"net/http\"\n\n"
                                               "func main() { http.ListenAndServe(\":8080\", nil) }\n",
                            "web/package.json": WEB_PKG})
    plan = _plan(NORTHGATE_SPEC, repo)
    assert plan.source == "stack"
    assert ("go vet ./...", repo / "backend") in plan.steps
    assert not any(d == repo and c.startswith("go ") for c, d in plan.steps)


def test_a_go_module_in_a_subfolder_is_gated_where_its_go_mod_is(tmp_path, go_on):
    """No front end to plan, an agent's package.json at the root: `go vet` runs in gateway/,
    never at a root with no go.mod (which fails every round)."""
    repo = _tree(tmp_path, {"README.md": "x\n", "package.json": json.dumps(AGENT_PKG),
                            "gateway/go.mod": GO_MOD, "gateway/main.go": "package main\n"})
    plan = _plan(NORTHGATE_SPEC, repo)
    assert plan.source == "toolchain" and plan.commands == list(toolchains.GO_GATE)
    assert plan.cwd == repo / "gateway"
    assert all(d == repo / "gateway" for _, d in plan.steps)


def test_the_build_file_folder_is_the_shallowest_one(tmp_path):
    repo = _tree(tmp_path, {"a/b/go.mod": GO_MOD, "svc/go.mod": GO_MOD,
                            "node_modules/x/go.mod": GO_MOD})
    assert vt.required_stack_dir(repo, "go") == repo / "svc"
    assert vt.required_stack_dir(repo, "java") == repo


# ---- how a stack is declared -------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("Tech stack: Go", "go"),
    ("**Tech Stack:** Go (one module)", "go"),
    ("- Technology stack: java/spring", "java"),
    # A Stack line inside a code fence is sample text, not the brief's declaration.
    ("Build the CLI.\n\n```yaml\nStack: go\n```\n", None),
    ("Example:\n~~~\nStack: python\n~~~\nStack: Go", "go"),
])
def test_tech_stack_lines_count_and_fenced_ones_do_not(text, expected):
    assert vt.required_app_stack({"instructions": text}) == expected


@pytest.mark.parametrize("brief", ["A Next-js storefront", "Edit next.config.js to add a rewrite",
                                   "NextJS app router pages"])
def test_next_js_spellings_are_still_a_bundled_front_end(brief):
    assert build_scope.decide("App", brief)["bundler"] is True


def test_a_follow_ups_structured_stack_keeps_the_bundler_off():
    spec = {"instructions": "Fix the React-to-alerts wording in the README.",
            "required_stack": [{"language": "go", "from": "lineage"}]}
    assert build_scope.decide("Follow-up", spec["instructions"])["bundler"] is True
    assert build_scope.decide("Follow-up", spec["instructions"], spec=spec)["bundler"] is False


# ---- no declared stack, toolchain on: the base tree decides (#138's rule, both ways) ------------

def test_a_package_json_an_agent_adds_beside_a_base_go_mod_stays_go(tmp_path, go_on):
    repo = _tree(tmp_path, {"go.mod": GO_MOD, "main.go": "package main\n\nfunc main() {}\n"})
    base = vt.pin_verify_manifest(repo)
    (repo / "package.json").write_text(json.dumps(AGENT_PKG))
    plan = _plan({"instructions": "Add a /healthz route."}, repo, pinned=base,
                 changed=["package.json", "main.go"])
    assert plan.source == "toolchain" and plan.commands == list(toolchains.GO_GATE)


def test_a_go_mod_and_package_json_both_added_to_an_empty_repo_are_go(tmp_path, go_on):
    repo = _empty_repo(tmp_path)
    base = vt.pin_verify_manifest(repo)
    (repo / "go.mod").write_text(GO_MOD)
    (repo / "main.go").write_text("package main\n\nfunc main() {}\n")
    (repo / "package.json").write_text(json.dumps(AGENT_PKG))
    plan = _plan({"instructions": "Build a gateway."}, repo, pinned=base,
                 changed=["go.mod", "main.go", "package.json"])
    assert plan.commands == list(toolchains.GO_GATE)


def test_a_base_node_app_with_a_stray_go_mod_stays_npm_with_go_on(tmp_path, go_on):
    repo = _tree(tmp_path, {"go.mod": GO_MOD, "package.json": PKG})
    plan = _plan({"instructions": "Add a page."}, repo, pinned=vt.pin_verify_manifest(repo),
                 changed=["src/page.ts"])
    assert plan.commands and plan.commands[0].startswith("npm ")
