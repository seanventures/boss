"""THE APP'S DIRECTORY FOR A BUILD (services/app_root.py) — one resolver, every gate planner.

HelpDesk (helpdesk-ai/, one npm project) in paramg/boss-sandbox, whose ROOT holds a pom.xml from
an old Java demo, was refused "This project needs Java" three times. Each earlier fix patched one
planner (find_stack_root) and tested that function alone, so the gate plan's own Java branch —
`toolchains.detect_java(<repo root>)` — kept firing: on the first plan of every build (made with
no change list) and on a resumed repair (crun_7768e91ecf2d420c8b72, wfr_fb4fb5ded8c84a4dab12)
whose own diff was two files; the template gate then ran where that plan pointed.

These tests drive EVERY planner with tonight's exact tree, and a guard fails if any planner asks
`detect_java` about anything but the directory app_root resolved.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from boss_engine.services import app_root, preview_service, stacks, toolchains, verification_target

ENGINE = Path(__file__).resolve().parents[1] / "boss_engine"

BRIEF = ("Build HelpDesk AI, a support desk with AI triage and a web UI (Express API + React "
         "front end), as ONE npm project in the `helpdesk-ai/` folder of this repository.")
#: The same brief naming no folder: only the change list and the lineage say where the app is.
UNNAMED = "Build HelpDesk AI, a support desk with AI triage and a web UI (Express API + React front end)."

#: The 29 files the resumed repair was seeded with (workspace_seeded files=29), abridged.
SEEDED = ["helpdesk-ai/package.json", "helpdesk-ai/src/server.js", "helpdesk-ai/src/triage.js",
          "helpdesk-ai/src/db.js", "helpdesk-ai/public/index.html", "helpdesk-ai/boss.preview.command",
          "helpdesk-ai/tests/triage.test.js", "helpdesk-ai/tests/api.test.js",
          ".github/workflows/helpdesk-ci.yml", "README.md"]

#: What THIS attempt changed: two files, one outside the app (workspace_saved files=2).
THIS_ATTEMPT = ["helpdesk-ai/src/triage.js", "README.md"]


@pytest.fixture
def java_off(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest-none.json"
    manifest.write_text("{}\n")
    monkeypatch.setenv(toolchains.MANIFEST_ENV, str(manifest))
    return manifest


def _sandbox_repo(tmp_path: Path, *, start: bool = True) -> Path:
    """paramg/boss-sandbox as it stood: a root pom.xml (an old Java demo, with its src/), other
    apps beside it, and HelpDesk in helpdesk-ai/."""
    repo = tmp_path / "boss-sandbox"
    (repo / "src" / "main" / "java").mkdir(parents=True)
    (repo / "pom.xml").write_text("<project><modelVersion>4.0.0</modelVersion></project>\n")
    (repo / "src" / "main" / "java" / "Demo.java").write_text("class Demo {}\n")
    (repo / "web").mkdir()
    (repo / "web" / "index.html").write_text("<html>sudoku</html>")
    (repo / "notes-lite").mkdir()
    (repo / "notes-lite" / "requirements.txt").write_text("flask\n")
    app = repo / "helpdesk-ai"
    (app / "src").mkdir(parents=True)
    (app / "tests").mkdir()
    scripts = {"test": "node --test", "build": "node -e 0"}
    if start:
        scripts["start"] = "node src/server.js"
    (app / "package.json").write_text(json.dumps({"name": "helpdesk-ai", "scripts": scripts}))
    for rel in SEEDED:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text("// x\n")
    return repo


# ---- (a) tonight's scenario, through every planner ----------------------------------------------

#: (spec, changed, lineage): the brief names the folder; or it does not and the evidence is the
#: two-file diff; or an empty diff and the seeded workspace; or only the lineage's files.
IDENTITIES = [
    pytest.param({"instructions": BRIEF}, None, None, id="first-plan-brief-names-the-folder"),
    pytest.param({"instructions": BRIEF}, THIS_ATTEMPT, SEEDED, id="resumed-repair-with-brief"),
    pytest.param({"instructions": UNNAMED}, THIS_ATTEMPT, None, id="two-file-diff-no-brief"),
    pytest.param({"instructions": UNNAMED}, ["README.md", ".github/workflows/helpdesk-ci.yml"], SEEDED,
                 id="diff-outside-the-app-lineage-decides"),
    pytest.param({"instructions": UNNAMED}, [], SEEDED, id="empty-diff-lineage-decides"),
    pytest.param({"instructions": UNNAMED}, SEEDED, None, id="seeded-paths-as-changed"),
]


@pytest.mark.parametrize("spec,changed,lineage", IDENTITIES)
def test_the_verification_gate_plans_helpdesk_in_its_folder_with_node(tmp_path, java_off, spec,
                                                                        changed, lineage):
    repo = _sandbox_repo(tmp_path)
    plan = verification_target.resolve_gate_plan(spec, "HelpDesk AI", repo, changed=changed,
                                                 lineage=lineage)
    assert plan.source not in ("toolchain", "toolchain_missing"), plan.description
    assert not plan.error, plan.error
    assert "Java" not in plan.description and "mvn" not in plan.description
    assert plan.cwd == repo / "helpdesk-ai"
    assert plan.commands and plan.commands[0].startswith("npm ")


@pytest.mark.parametrize("spec,changed,lineage", IDENTITIES)
def test_the_resolver_and_the_template_gate_run_in_helpdesk_ai(tmp_path, spec, changed, lineage):
    repo = _sandbox_repo(tmp_path)
    named = verification_target.app_scope(spec, {})
    assert app_root.app_dir(repo, app_id=named, changed=changed, lineage=lineage) == repo / "helpdesk-ai"
    assert app_root.java_build(repo, app_id=named, changed=changed, lineage=lineage)[1] is None
    # The template gate's directory when no verify recorded one (governed _gate_app_dir).
    assert app_root.gate_dir(repo, app_id=named, changed=changed, lineage=lineage) == repo / "helpdesk-ai"


def test_the_template_gate_runs_its_tests_in_the_app_never_the_root(tmp_path):
    """templates.gate.evaluate installs and runs the template's tests where the build's gate
    directory is (the governed pipeline's fallback when no verify recorded one)."""
    from boss_engine.templates.gate import evaluate
    from boss_engine.templates.validator import tree_from_dir, validate_tree

    from .templates._fixtures import HELLO

    repo = _sandbox_repo(tmp_path)
    where = app_root.gate_dir(repo, changed=THIS_ATTEMPT, lineage=SEEDED)
    report = validate_tree(tree_from_dir(HELLO), source="builtin")
    tpl = {"id": "tpl_x", "digest": report.digest, "stack": "node",
           "manifest": report.manifest.model_dump(mode="json", by_alias=True)}
    ran: list[tuple[str, Path]] = []

    class _R:
        exit_code, stdout, stderr = 0, "", ""

    def run(command, cwd):
        ran.append((command, Path(cwd)))
        (Path(cwd) / "boss-template-junit.xml").write_text(
            '<testsuites><testcase name="a"/><testcase name="b"/></testsuites>')
        return _R()

    r = evaluate(template=tpl, files=tree_from_dir(HELLO).files(), run_command=run, cwd=where,
                 changed={"helpdesk-ai/hello.js": "x",
                          "helpdesk-ai/tests/template_hello/hello.test.mjs": "x"})
    assert r.gates["template_tests"], r.evidence
    assert ran and all(cwd == repo / "helpdesk-ai" for _c, cwd in ran)


def test_the_stack_plan_never_takes_the_shared_root(tmp_path):
    repo = _sandbox_repo(tmp_path)
    for changed in (THIS_ATTEMPT, [], None, SEEDED):
        assert stacks.find_stack_root(repo, changed) is None
    assert stacks.find_stack_root(repo, THIS_ATTEMPT, "helpdesk-ai") is None


def test_the_preview_and_the_serve_check_run_helpdesk_with_node(tmp_path):
    repo = _sandbox_repo(tmp_path)
    for changed in (THIS_ATTEMPT, SEEDED):
        assert preview_service.resolve_app_entry(repo, changed) == ("node", "helpdesk-ai")
        assert preview_service.preview_start_timeout(repo, 180.0, changed=changed) == 180.0


def test_an_app_with_no_server_is_never_previewed_as_the_roots_java_demo(tmp_path):
    """No candidate in the app folder: the old fallback ran the root's pom.xml as "the app"."""
    repo = _sandbox_repo(tmp_path, start=False)
    (repo / "web" / "index.html").unlink()
    entry = preview_service.resolve_app_entry(repo, THIS_ATTEMPT)
    assert entry[0] != "java", entry


# ---- the root is still the app when it genuinely is ---------------------------------------------

def test_a_java_app_at_the_root_still_gates_on_maven(tmp_path, java_off):
    repo = tmp_path / "svc"
    (repo / "src" / "main" / "java").mkdir(parents=True)
    (repo / "pom.xml").write_text("<project/>")
    for changed in (None, [], ["src/main/java/App.java"], ["pom.xml", "src/main/java/App.java"]):
        plan = verification_target.resolve_gate_plan({}, "", repo, changed=changed)
        assert plan.source == "toolchain_missing" and plan.cwd == repo


def test_a_root_maven_build_with_modules_is_the_app(tmp_path):
    repo = tmp_path / "multi"
    (repo / "api").mkdir(parents=True)
    (repo / "pom.xml").write_text("<project><modules><module>api</module></modules></project>")
    (repo / "api" / "pom.xml").write_text("<project/>")
    assert app_root.app_dir(repo, changed=["api/src/main/java/A.java"]) == repo
    assert app_root.java_build(repo, changed=["api/src/main/java/A.java"]) == (repo, "maven")


def test_a_java_app_in_its_own_folder_gates_there(tmp_path, java_off):
    repo = _sandbox_repo(tmp_path)
    (repo / "bank-api").mkdir()
    (repo / "bank-api" / "pom.xml").write_text("<project/>")
    plan = verification_target.resolve_gate_plan({}, "", repo, changed=["bank-api/src/A.java"])
    assert plan.source == "toolchain_missing" and plan.cwd == repo / "bank-api"


def test_the_root_manifest_in_the_lineage_makes_the_root_the_app(tmp_path):
    repo = _sandbox_repo(tmp_path)
    assert app_root.app_dir(repo, changed=["pom.xml", "helpdesk-ai/src/server.js"]) == repo
    assert app_root.app_dir(repo, changed=["helpdesk-ai/src/a.js", "notes-lite/app.py"]) == repo


# ---- (b) the guard: no planner asks detect_java about anything app_root did not resolve ----------

#: (module, function) allowed to call toolchains.detect_java, and why each argument is an app dir.
DETECT_JAVA_ALLOWED = {
    ("services/app_root.py", "java_build"),        # THE resolver
    ("services/toolchains.py", "detect_java"),     # the definition
    ("services/stacks.py", "_java_steps"),         # a service dir from detect_services(app dir)
    ("services/stacks.py", "_start_java"),         # the same service dir
    ("services/preview_service.py", "command_for_entry"),  # the entry resolve_app_entry chose
}


def _calls(name: str):
    for path in ENGINE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(ENGINE).as_posix()
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    f = node.func
                    called = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                    if called == name:
                        yield rel, fn.name, node


def test_no_gate_planner_detects_java_outside_the_resolver():
    found = {(rel, fn) for rel, fn, _ in _calls("detect_java")}
    assert found, "the scan found nothing: it no longer sees the engine"
    stray = found - DETECT_JAVA_ALLOWED
    assert not stray, (f"{sorted(stray)} ask detect_java directly. Ask app_root.java_build: a "
                       "repository root's pom.xml is not a subfolder app's stack.")


def test_every_gate_plan_in_the_build_passes_the_apps_identity():
    """Every resolve_gate_plan call in the build pipeline carries the lineage, and the gate
    directory fallbacks go through app_root (never resolve_verify_cwd on the bare root)."""
    src = (ENGINE / "services" / "governed_code_task_service.py").read_text(encoding="utf-8")
    calls = [n for rel, _fn, n in _calls("resolve_gate_plan")
             if rel == "services/governed_code_task_service.py"]
    assert calls
    for node in calls:
        assert "lineage" in {k.arg for k in node.keywords}, ast.get_source_segment(src, node)
    direct = [fn for rel, fn, _ in _calls("resolve_verify_cwd")
              if rel == "services/governed_code_task_service.py"]
    assert not direct, f"{direct} resolve a gate directory without app_root.gate_dir"


def test_the_gate_plan_resolves_the_app_through_app_root():
    src = ast.get_source_segment(
        (ENGINE / "services" / "verification_target.py").read_text(encoding="utf-8"),
        next(n for n in ast.parse((ENGINE / "services" / "verification_target.py").read_text(
            encoding="utf-8")).body if isinstance(n, ast.FunctionDef) and n.name == "resolve_gate_plan"))
    assert "app_root.java_build(" in src and "app_scope(spec" in src
