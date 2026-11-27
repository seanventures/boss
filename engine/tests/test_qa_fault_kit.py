"""THE FAULT KIT'S GENERATORS (scripts/install/qa_fault.py): every fault the test plan's tester gets
from `boss qa fault` really breaks the app the way the case says, on every stack it claims, and a
stack or shape it cannot break is refused in words — never a half-applied fault.

Each case runs on a copy of a known-good sample app (tests/e2e/fixtures/plain-node/plain, and
tests/e2e/fixtures/fault-kit/{python,go}), and the proof is the app's OWN toolchain failing the way
the plan describes: its tests, its build, its vet, npm refusing the engine, the app exiting on
start. A toolchain missing on this machine skips that proof, not the generator."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "install"))
import qa_fault  # noqa: E402

FIXTURES = {"node": REPO / "tests/e2e/fixtures/plain-node/plain",
            "python": REPO / "tests/e2e/fixtures/fault-kit/python",
            "go": REPO / "tests/e2e/fixtures/fault-kit/go"}
TOOLS = {"node": "node", "python": sys.executable, "go": "go"}


def _app(tmp_path: Path, stack: str) -> Path:
    dest = tmp_path / stack
    shutil.copytree(FIXTURES[stack], dest)
    return dest


def _need(stack: str) -> None:
    if stack != "python" and not shutil.which(TOOLS[stack]):
        pytest.skip(f"{TOOLS[stack]} is not on this machine")


def _run(cmd: list[str], cwd: Path, env: dict | None = None, timeout: int = 120):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, "GOFLAGS": "-mod=mod", "GOPROXY": "off",
                               "GOTOOLCHAIN": "local", **(env or {})})


def _tests(stack: str, app: Path):
    return {"node": lambda: _run(["node", "--test"], app),
            "python": lambda: _run([sys.executable, "-m", "unittest", "-q"], app),
            "go": lambda: _run(["go", "test", "-count=1", "./..."], app)}[stack]()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(stack: str, app: Path, env: dict | None = None, seconds: float = 6.0):
    """Start the app as its declaration says; (exit code or None if still running, output)."""
    command = {"node": ["node", "src/server.js"], "python": [sys.executable, "app.py"],
               "go": [str(app / "app.bin")]}[stack]
    if stack == "go":       # built first: killing `go run` would leave its child serving
        built = _run(["go", "build", "-o", "app.bin", "."], app)
        assert built.returncode == 0, built.stderr
    port = _free_port()
    proc = subprocess.Popen(command, cwd=app, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, env={**os.environ, "PORT": str(port), "GOPROXY": "off",
                                            "GOFLAGS": "-mod=mod", **(env or {})})
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.2)
    code = proc.poll()
    if code is None:
        proc.kill()
    out = proc.communicate(timeout=10)[0]
    return code, out, port


# ── every known-good app is good before the fault ─────────────────────────────────────────────

@pytest.mark.parametrize("stack", qa_fault.STACKS)
def test_each_sample_app_passes_its_own_tests_before_any_fault(tmp_path, stack):
    _need(stack)
    app = _app(tmp_path, stack)
    done = _tests(stack, app)
    assert done.returncode == 0, done.stdout + done.stderr
    assert qa_fault.detect_stack(app) == stack


# ── the faults, proven by the app's own toolchain ─────────────────────────────────────────────

@pytest.mark.parametrize("stack", qa_fault.STACKS)
def test_rec01_a_broken_existing_test_fails_that_test(tmp_path, stack):
    _need(stack)
    app = _app(tmp_path, stack)
    done = qa_fault.apply("REC-01", app)
    assert len(done.changed) == 1 and done.changed[0] in qa_fault.App(app).tests()
    assert _tests(stack, app).returncode != 0


def test_rec02_a_type_error_breaks_the_go_build_and_plain_js_is_refused(tmp_path):
    with pytest.raises(qa_fault.Unsupported, match="TypeScript"):
        qa_fault.apply("REC-02", _app(tmp_path, "node"))
    with pytest.raises(qa_fault.Unsupported, match="mypy or pyright"):
        qa_fault.apply("REC-02", _app(tmp_path, "python"))
    _need("go")
    app = _app(tmp_path, "go")
    qa_fault.apply("REC-02", app)
    built = _run(["go", "build", "./..."], app)
    assert built.returncode != 0 and "cannot use 42" in built.stderr


def test_rec02_on_typescript_assigns_a_number_to_a_string_field(tmp_path):
    app = tmp_path / "ts"
    (app / "src").mkdir(parents=True)
    (app / "package.json").write_text('{"name": "t", "type": "module", "scripts": {"start": '
                                      '"tsx src/server.ts"}}')
    (app / "tsconfig.json").write_text("{}")
    (app / "src/server.ts").write_text('import http from "node:http";\nhttp.createServer();\n')
    done = qa_fault.apply("REC-02", app)
    assert done.changed == ["src/server.ts"]
    assert "const bossFaultLabel: { name: string } = { name: 42 };" in (app / "src/server.ts") \
        .read_text()


def test_rec03_a_lint_error_needs_the_apps_own_lint_config(tmp_path):
    with pytest.raises(qa_fault.Unsupported, match="ESLint"):
        qa_fault.apply("REC-03", _app(tmp_path, "node"))
    app = _app(tmp_path, "python")
    with pytest.raises(qa_fault.Unsupported, match="ruff, flake8 or pylint"):
        qa_fault.apply("REC-03", app)
    (app / "ruff.toml").write_text("line-length = 100\n")
    done = qa_fault.apply("REC-03", app)
    assert "import os as _boss_fault_unused" in (app / done.changed[0]).read_text()
    _need("go")
    app = _app(tmp_path / "g", "go")
    qa_fault.apply("REC-03", app)
    assert _run(["go", "build", "./..."], app).returncode == 0, "it is a lint error, not a build one"
    vet = _run(["go", "vet", "./..."], app)
    assert vet.returncode != 0 and "Sprintf" in vet.stderr


@pytest.mark.parametrize("stack", qa_fault.STACKS)
def test_rec04_a_missing_dependency_fails_where_the_app_imports_it(tmp_path, stack):
    _need(stack)
    app = _app(tmp_path, stack)
    done = qa_fault.apply("REC-04", app)
    if stack == "go":
        built = _run(["go", "build", "./..."], app, env={"GOFLAGS": "-mod=readonly"})
        assert built.returncode != 0 and "github.com/google/uuid" in built.stderr
        return
    # Imported by a module the tests import, so the tests fail on it too.
    assert done.changed[0] in ("src/slug.js", "recipes.py")
    failed = _tests(stack, app)
    if stack == "python" and failed.returncode == 0:
        pytest.skip("this machine happens to have the package installed")
    assert failed.returncode != 0
    assert any(n in failed.stdout + failed.stderr for n in ("dayjs", "pendulum", "arrow"))


def test_rec05_lockfile_drift_needs_a_lockfile_and_breaks_npm_ci_and_go(tmp_path):
    with pytest.raises(qa_fault.Unsupported, match="package-lock.json"):
        qa_fault.apply("REC-05", _app(tmp_path, "node"))
    app = _app(tmp_path / "locked", "node")
    (app / "package-lock.json").write_text(
        '{"name": "plain-node", "version": "1.0.0", "lockfileVersion": 3, "requires": true, '
        '"packages": {"": {"name": "plain-node", "version": "1.0.0", "engines": '
        '{"node": ">=20"}}}}\n')
    done = qa_fault.apply("REC-05", app)
    assert done.changed == ["package.json"] and "dayjs" in (app / "package.json").read_text()
    if shutil.which("npm"):
        ci = _run(["npm", "ci", "--offline", "--ignore-scripts", "--no-audit", "--no-fund"], app)
        # Offline, npm reaches for the package the lockfile never had (online: "not in sync").
        assert ci.returncode != 0 and ("in sync" in ci.stderr or "dayjs" in ci.stderr)
    assert "dayjs" not in (app / "package-lock.json").read_text()
    with pytest.raises(qa_fault.Unsupported, match="Python lockfile"):
        qa_fault.apply("REC-05", _app(tmp_path, "python"))
    _need("go")
    app = _app(tmp_path, "go")
    qa_fault.apply("REC-05", app)
    built = _run(["go", "build", "./..."], app, env={"GOFLAGS": "-mod=readonly"})
    assert built.returncode != 0 and "missing go.sum entry" in built.stderr


def test_rec06_and_rec07_declare_a_runtime_no_box_has(tmp_path):
    app = _app(tmp_path, "node")
    qa_fault.apply("REC-06", app)
    assert "engine-strict=true" in (app / ".npmrc").read_text()
    if shutil.which("npm"):
        done = _run(["npm", "install", "--offline", "--ignore-scripts", "--no-audit",
                     "--no-fund"], app)
        assert done.returncode != 0 and "EBADENGINE" in done.stderr + done.stdout
    app = _app(tmp_path, "python")
    qa_fault.apply("REC-07", app)
    assert 'requires-python = ">=3.99"' in (app / "pyproject.toml").read_text()
    with pytest.raises(qa_fault.Unsupported, match="REC-07 is the Python one"):
        qa_fault.apply("REC-06", app)
    with pytest.raises(qa_fault.Unsupported, match="REC-06 is the Node one"):
        qa_fault.apply("REC-07", _app(tmp_path / "n", "node"))


@pytest.mark.parametrize("stack", qa_fault.STACKS)
def test_rec08_a_flaky_test_both_passes_and_fails(tmp_path, stack):
    _need(stack)
    app = _app(tmp_path, stack)
    qa_fault.apply("REC-08", app)
    seen = set()
    for _ in range(24):
        seen.add(_tests(stack, app).returncode == 0)
        if seen == {True, False}:
            break
    assert seen == {True, False}, "a flaky test must pass some runs and fail others"


@pytest.mark.parametrize("stack", qa_fault.STACKS)
def test_rec09_the_app_exits_on_start_and_its_tests_still_pass(tmp_path, stack):
    _need(stack)
    app = _app(tmp_path, stack)
    qa_fault.apply("REC-09", app)
    code, out, _ = _start(stack, app, seconds=6)
    assert code not in (None, 0) and "BOSS fault REC-09" in out
    if stack != "node":       # the Node sample's tests never import its server
        assert _tests(stack, app).returncode == 0, "a crash on START, not a broken module"


@pytest.mark.parametrize("stack", qa_fault.STACKS)
def test_rec10_the_app_ignores_its_port(tmp_path, stack):
    _need(stack)
    app = _app(tmp_path, stack)
    qa_fault.apply("REC-10", app)
    assert qa_fault.WRONG_PORT in "".join((app / f).read_text()
                                          for f in qa_fault.App(app).sources())
    if stack == "go":
        assert _run(["go", "vet", "./..."], app).returncode == 0


@pytest.mark.parametrize("stack", qa_fault.STACKS)
def test_rec11_the_home_page_answers_404(tmp_path, stack):
    _need(stack)
    import urllib.error
    import urllib.request

    app = _app(tmp_path, stack)
    qa_fault.apply("REC-11", app)
    command = {"node": ["node", "src/server.js"], "python": [sys.executable, "app.py"],
               "go": [str(app / "app.bin")]}[stack]
    if stack == "go":
        assert _run(["go", "build", "-o", "app.bin", "."], app).returncode == 0
    port = _free_port()
    proc = subprocess.Popen(command, cwd=app, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env={**os.environ, "PORT": str(port)})
    try:
        status = None
        for _ in range(150):
            try:
                status = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2).status
            except urllib.error.HTTPError as exc:
                status = exc.code
            except OSError:
                time.sleep(0.2)
                continue
            break
        assert status == 404
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.parametrize("case, name", [("REC-12", "PAGE_SIZE"), ("REC-13", "PAYMENTS_API_KEY")])
@pytest.mark.parametrize("stack", qa_fault.STACKS)
def test_rec12_and_rec13_the_app_refuses_to_start_naming_the_variable(tmp_path, stack, case,
                                                                    name):
    _need(stack)
    app = _app(tmp_path, stack)
    qa_fault.apply(case, app)
    code, out, _ = _start(stack, app, seconds=6)
    assert code not in (None, 0) and name in out
    code, _, _ = _start(stack, app, env={name: "set"}, seconds=2)
    assert code is None, "given the variable, it starts"
    if case == "REC-13":
        example = (app / ".env.example").read_text()
        assert "PAYMENTS_API_KEY=" in example and "Required secret" in example


def test_rec14_reads_a_table_no_migration_creates(tmp_path):
    with pytest.raises(qa_fault.Unsupported, match="SQLite"):
        qa_fault.apply("REC-14", _app(tmp_path, "python"))
    app = _app(tmp_path / "db", "python")
    (app / "store.py").write_text("import sqlite3\n\n\ndef open_db(path):\n    db = "
                                  "sqlite3.connect(path)\n    return db\n")
    done = qa_fault.apply("REC-14", app)
    assert done.changed == ["store.py"]
    sys.path.insert(0, str(app))
    try:
        import importlib

        store = importlib.import_module("store")
        with pytest.raises(Exception, match="no such table: boss_fault_widgets"):
            store.open_db(str(tmp_path / "x.db"))
    finally:
        sys.path.remove(str(app))
        sys.modules.pop("store", None)


# ── refusals ──────────────────────────────────────────────────────────────────────────────────

def test_a_stack_the_kit_does_not_break_is_refused_by_name(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    with pytest.raises(qa_fault.Unsupported, match=r"Java \(Maven\) is not a stack"):
        qa_fault.apply("REC-01", tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(qa_fault.Unsupported, match="no Node, Python or Go app"):
        qa_fault.apply("REC-01", empty)


def test_a_refused_fault_leaves_the_app_untouched(tmp_path):
    app = _app(tmp_path, "node")
    before = {p: p.read_bytes() for p in app.rglob("*") if p.is_file()}
    with pytest.raises(qa_fault.Unsupported):
        qa_fault.apply("REC-05", app)
    assert {p: p.read_bytes() for p in app.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("raw, case", [("REC-01", "REC-01"), ("ent-rec-1", "REC-01"),
                                       ("13", "REC-13"), ("REC-09", "REC-09")])
def test_case_names_are_forgiving(raw, case):
    assert qa_fault.normalise_case(raw) == case


def test_a_case_without_a_generator_points_at_the_plan(capsys):
    with pytest.raises(SystemExit, match="Machine-level faults"):
        qa_fault.normalise_case("REC-29")


def test_the_app_is_chosen_by_name_or_must_be_named(tmp_path):
    for name in ("recipe-box", "neighborhood-board"):
        shutil.copytree(FIXTURES["node"], tmp_path / name)
    assert qa_fault.find_apps(tmp_path) == ["neighborhood-board", "recipe-box"]
    assert qa_fault.pick_app(tmp_path, "Recipe box") == "recipe-box"
    with pytest.raises(qa_fault.Unsupported, match="several apps"):
        qa_fault.pick_app(tmp_path, None)
    with pytest.raises(qa_fault.Unsupported, match="no app 'tile swap'"):
        qa_fault.pick_app(tmp_path, "tile swap")


def test_every_case_in_the_list_has_a_generator_and_the_list_prints(capsys):
    assert list(qa_fault.CASES) == [f"REC-{n:02d}" for n in range(1, 15)]
    assert qa_fault.main(["fault", "--list"]) == 0
    out = capsys.readouterr().out
    assert "ENT-REC-13" in out and "boss qa fault --revert" in out



def test_a_symlink_is_never_read_or_written_through(tmp_path):
    app = _app(tmp_path, "node")
    outside = tmp_path / "outside.js"
    outside.write_text("export const x = 1;\n")
    (app / "src" / "linked.js").symlink_to(outside)
    assert "src/linked.js" not in qa_fault.App(app).files()
    (app / "test").rename(app / "real-test")
    (app / "test").symlink_to(app / "real-test")
    with pytest.raises(qa_fault.Unsupported, match="symlink"):
        qa_fault.App(app).write("test/slug.test.js", "x")
    assert outside.read_text() == "export const x = 1;\n"


def test_every_deployment_target_page_is_checked_and_a_failed_page_refuses(monkeypatch):
    pages = {"0": [{"id": f"t{i}", "status": "created"} for i in range(200)],
             "200": [{"id": "prod-target", "status": "created", "environment": "production"}]}
    asked = []

    def get(self, path, **q):
        if path == "/v1/autonomy/resolve":
            asked.append(q.get("target_id"))
            prod = q.get("target_id") == "prod-target"
            return 200, {"verdict": {"prod": prod}, "words": "production" if prod else "dev"}
        return 200, {"items": pages.get(q["offset"], [])}

    monkeypatch.setattr(qa_fault.Boss, "get", get)
    boss = qa_fault.Boss("http://x", "tok")
    why = qa_fault.production_refusal(boss, {"id": "p", "name": "P"}, {"default_branch": "main"})
    assert why and "prod-target" in why
    monkeypatch.setattr(qa_fault.Boss, "get", lambda self, path, **q: (
        (200, {"verdict": {"prod": False}}) if path == "/v1/autonomy/resolve"
        else (200, {"items": pages["0"]}) if q["offset"] == "0" else (502, None)))
    with pytest.raises(SystemExit, match="did not list"):
        qa_fault.production_refusal(boss, {"id": "p", "name": "P"}, {"default_branch": "main"})
