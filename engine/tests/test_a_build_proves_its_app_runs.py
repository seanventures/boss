"""A build declares how its app runs, and proves it, before it is allowed to be done.

boss-1, 2026-10-02: the Standard Product Delivery build crun_3e370c114b95425d8a7c ("Lead Triage
Inbox", PR paramg/boss-sandbox#218) completed green. Its preview then failed twice:

    app did not serve HTTP within timeout.
    ImportError: attempted relative import with no known parent package

from lead_triage_core/approvals.py. The resolver took any CHANGED .py whose name began with
serve/server/app/main/wsgi as the entrypoint — "approvals" begins with "app" — and ran it as
`python3 <path>`, which breaks every module inside a package. That resolver had already been
patched once per incident (Sudoku for N-Queens, BugBox previewing the repo root, a vite root
with no entry); each patch taught it one more shape.

The design now: the implement stage writes a run declaration, the pipeline STARTS it once the
tests pass and refuses to call the build done if nothing answers HTTP, and the heuristic that
remains for undeclared repos judges a Python file by what it does, not what it is called.
"""

from __future__ import annotations

import inspect
import json

import pytest

from boss_engine.services import governed_code_task_service as gov
from boss_engine.services import preview_service as ps


SERVER = '''\
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .approvals import approve


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = ("<html><body>Lead Triage Inbox: " + approve("lead-1") + "</body></html>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8000"))), Handler)
    print(f"Serving on http://0.0.0.0:{server.server_port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
'''

# The module the old resolver chose: a name that starts with "app", relative imports, and
# nothing that runs. Exactly the shape of lead_triage_core/approvals.py.
APPROVALS = '''\
from .store import STATUSES


def approve(lead_id: str) -> str:
    return f"{lead_id}: {STATUSES[1]}"
'''

STORE = 'STATUSES = ("new", "approved", "rejected")\n'

# What the build wrote, in the order the run recorded it — approvals.py first, as it was.
BUILT = [
    "lead_triage_core/__init__.py",
    "lead_triage_core/approvals.py",
    "lead_triage_core/store.py",
    "lead_triage_core/server.py",
    "tests/test_approvals.py",
]


def _write(root, files: dict[str, str]):
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return root


def _lead_triage(tmp_path, **extra):
    return _write(tmp_path, {
        "lead_triage_core/__init__.py": "",
        "lead_triage_core/approvals.py": APPROVALS,
        "lead_triage_core/store.py": STORE,
        "lead_triage_core/server.py": SERVER,
        "tests/test_approvals.py": "import unittest\n",
        **extra,
    })


def _files(root, paths):
    return {p: (root / p).read_text() for p in paths if (root / p).is_file()}


# --------------------------------------------------------------------------------------- #
# (a) The incident shape resolves to the real server, run as a module.
# --------------------------------------------------------------------------------------- #

def test_the_lead_triage_build_resolves_to_its_server_not_to_approvals(tmp_path):
    """crun_3e370c11: approvals.py matched on its name and was run; the server never was."""
    root = _lead_triage(tmp_path)
    assert ps.resolve_app_entry(root, BUILT) == ("py", "lead_triage_core/server.py")


def test_a_package_module_is_run_with_dash_m_from_above_its_package(tmp_path):
    """`python3 lead_triage_core/server.py` cannot resolve `from .approvals import ...`."""
    root = _lead_triage(tmp_path)
    assert ps._detect_command(root, BUILT) == ["python3", "-m", "lead_triage_core.server"]


def test_a_nested_package_runs_from_the_directory_above_the_outermost_package(tmp_path):
    root = _write(tmp_path, {
        "apps/inbox/lead_triage_core/__init__.py": "",
        "apps/inbox/lead_triage_core/web/__init__.py": "",
        "apps/inbox/lead_triage_core/web/server.py": "if __name__ == '__main__':\n    pass\n",
    })
    assert ps.python_entry_invocation(root, "apps/inbox/lead_triage_core/web/server.py") == (
        "apps/inbox", ["python3", "-m", "lead_triage_core.web.server"])


def test_a_script_outside_any_package_still_runs_as_a_script(tmp_path):
    root = _write(tmp_path, {"serve_scorecard.py": "if __name__ == '__main__':\n    pass\n"})
    assert ps.command_for_entry(root, "py", "serve_scorecard.py") == [
        "python3", "serve_scorecard.py"]


def test_the_container_preview_runs_a_package_module_the_same_way(tmp_path):
    """The host and container paths drifted twice before (PR #62); they share one invocation."""
    root = _lead_triage(tmp_path)
    ps._generate_dockerfile(root, BUILT)
    dockerfile = (root / "Dockerfile").read_text()
    assert 'CMD ["python3", "-m", "lead_triage_core.server"]' in dockerfile


def test_the_resolved_command_actually_serves(tmp_path):
    """Resolution being right is worthless if the command it builds cannot start the app."""
    root = _lead_triage(tmp_path)
    record = ps.prove_app_serves(root, BUILT, read=_files(root, BUILT).get, timeout=20)
    assert record["ok"] and record["proven"], record
    assert record["command"] == "python3 -m lead_triage_core.server"


# --------------------------------------------------------------------------------------- #
# (b) A name is not a program.
# --------------------------------------------------------------------------------------- #

def test_a_name_prefix_match_that_does_nothing_when_run_is_not_chosen(tmp_path):
    """approvals.py, app_utils.py, main_helpers.py: names that look like entrypoints, and
    modules that only define things. Choosing one previews an app that can never start."""
    root = _write(tmp_path, {
        "app_utils.py": "def helper():\n    return 1\n",
        "main_helpers.py": "from .x import y\n\nCONSTANT = 3\n",
    })
    assert ps.resolve_app_entry(root, ["app_utils.py", "main_helpers.py"]) == ("", "")


def test_a_module_named_like_an_app_does_not_make_a_one_app_repo_ambiguous(tmp_path):
    root = _write(tmp_path, {
        "application.py": "class Application:\n    pass\n",
        "serve.py": "if __name__ == '__main__':\n    print('serving')\n",
    })
    assert ps.resolve_app_entry(root, []) == ("py", "serve.py")


@pytest.mark.parametrize("source", [
    "if __name__ == '__main__':\n    main()\n",
    "app = Flask(__name__)\n",
    "print('hi')\n",
    "from http.server import ThreadingHTTPServer\nsrv = ThreadingHTTPServer(('', 1), None)\n",
])
def test_a_file_that_runs_something_is_a_program(source):
    assert ps.python_source_is_program(source)


@pytest.mark.parametrize("source", [
    APPROVALS,
    "",
    "# just a comment\n",
    "import os\nX = os.environ.get('A')\n\ndef f():\n    if __name__ == '__main__':\n        pass\n",
])
def test_a_file_of_definitions_is_not_a_program(source):
    assert not ps.python_source_is_program(source)


def test_a_build_with_the_source_in_hand_declares_its_package_server(tmp_path):
    """declare_app_entry used to see root-level names only, so this build declared nothing and
    the preview fell through to the resolver that picked approvals.py."""
    root = _lead_triage(tmp_path)
    assert ps.declare_app_entry(BUILT, _files(root, BUILT).get) == "py:lead_triage_core/server.py"


def test_a_root_name_match_that_is_not_a_program_is_not_declared(tmp_path):
    files = {"serve_helpers.py": "def fmt(x):\n    return str(x)\n"}
    assert ps.declare_app_entry(list(files), files.get) is None


# --------------------------------------------------------------------------------------- #
# The declaration is what runs.
# --------------------------------------------------------------------------------------- #

def test_the_run_declaration_beats_every_heuristic(tmp_path):
    root = _lead_triage(tmp_path, **{"boss.preview.command": "python3 -m lead_triage_core.server\n"})
    assert ps._detect_command(root, BUILT + ["boss.preview.command"]) == [
        "sh", "-lc", "python3 -m lead_triage_core.server"]


def test_a_declaration_in_an_app_directory_runs_from_that_directory(tmp_path):
    root = _write(tmp_path, {"inbox/package.json": json.dumps(
        {"scripts": {"start": "node s.js"}, "boss": {"preview": {"command": "node s.js"}}})})
    assert ps._detect_command(root, ["inbox/package.json"]) == [
        "sh", "-lc", "cd inbox && node s.js"]


def test_another_apps_root_declaration_does_not_hijack_this_build(tmp_path):
    """A shared repo accumulates apps; a root declaration this lineage never wrote is not its."""
    root = _lead_triage(tmp_path, **{"boss.preview.command": "python3 serve_sudoku.py\n"})
    assert ps._detect_command(root, BUILT) == ["python3", "-m", "lead_triage_core.server"]


def test_the_implement_prompts_require_the_run_declaration():
    """The contract has to be asked for where code is written, or it is never produced."""
    impl = gov._impl_prompt("Lead Triage Inbox", "build it", ["tests/test_a.py"], "pytest")
    solo = gov._solo_prompt("Lead Triage Inbox", "build it", [], "pytest")
    web = gov._solo_prompt("Lead Triage Inbox", "build it", [], "npm run verify",
                           web=True, app_root="inbox")
    for prompt in (impl, solo, web):
        assert "boss.preview.command" in prompt
        assert "python3 -m" in prompt
    # The QA stage writes tests only; it must not be told to write implementation files.
    assert "boss.preview.command" not in gov._qa_prompt("t", "i", [], "pytest")


# --------------------------------------------------------------------------------------- #
# (c) A declared command that does not serve is a build failure, with the real error.
# --------------------------------------------------------------------------------------- #

def test_a_declared_command_that_cannot_start_fails_with_the_apps_own_error(tmp_path):
    """The exact invocation the preview used for crun_3e370c11, now caught at build time."""
    root = _lead_triage(tmp_path, **{
        "boss.preview.command": "python3 lead_triage_core/approvals.py\n"})
    built = BUILT + ["boss.preview.command"]
    record = ps.prove_app_serves(root, built, read=_files(root, built).get, timeout=20)
    assert record["ok"] is False
    assert record["source"] == "declared"
    assert "ImportError" in record["error"]
    assert "relative import" in record["error"]
    assert record["harness"] is False


def test_a_service_build_that_declares_nothing_runnable_fails_the_check(tmp_path):
    root = _write(tmp_path, {"inbox_core/rules.py": "def score(x):\n    return x\n"})
    record = ps.prove_app_serves(root, ["inbox_core/rules.py"], serves_http=True, timeout=5)
    assert record["ok"] is False
    assert "boss.preview.command" in record["error"]


def test_a_failing_app_serves_gate_is_never_shipped_past():
    """never_block ships with dissent once repairs are spent. Not for an app that cannot run:
    PR #218 was a finished build whose app had never started."""
    common = dict(failing=["app_serves"], max_repair_rounds=2, never_block=True,
                  faults=gov.classify_gate_faults(failing=["app_serves"],
                                                  evidence={"app_serves": {"ok": False}}))
    assert gov.resolve_gate_failure_action(advisory_gate=True, rounds_used=0, **common) == "repair"
    assert gov.resolve_gate_failure_action(advisory_gate=False, rounds_used=0, **common) == "repair"
    for advisory in (True, False):
        assert gov.resolve_gate_failure_action(
            advisory_gate=advisory, rounds_used=2, **common) == "block"


def test_a_check_that_could_not_run_is_not_blamed_on_the_code():
    faults = gov.classify_gate_faults(
        failing=["app_serves"], evidence={"app_serves": {"ok": False, "harness": True}})
    assert faults == {"app_serves": "HARNESS"}


def test_the_pipeline_gates_on_the_proof_and_records_it():
    """Wiring, checked at the source: the gate comes from prove_app_serves, the evidence
    carries its record, and the repair is handed the app's error."""
    body = inspect.getsource(gov.run_governed_code_task)
    assert "prove_app_serves(" in body
    assert 'gates["app_serves"] = bool(app_serves_record.get("ok"))' in body
    assert '"app_serves": app_serves_record' in body
    assert "'app_serves' failed: BOSS ran" in body
    assert "app_serves" in gov._ALL_GATE_NAMES


# --------------------------------------------------------------------------------------- #
# (d) A working declaration passes, and the proof leaves the build's tree untouched.
# --------------------------------------------------------------------------------------- #

def test_a_build_with_a_working_declaration_passes_the_check(tmp_path):
    root = _lead_triage(tmp_path, **{
        "boss.preview.command": "python3 -m lead_triage_core.server\n"})
    built = BUILT + ["boss.preview.command"]
    record = ps.prove_app_serves(root, built, read=_files(root, built).get,
                                 serves_http=True, timeout=20)
    assert record["ok"] is True and record["proven"] is True, record
    assert record["source"] == "declared"
    assert isinstance(record["port"], int)


def test_the_proof_runs_in_a_copy_so_nothing_it_writes_reaches_the_pull_request(tmp_path):
    """The sandbox diff is `git add -A`: a log or a SQLite file written here would ship."""
    root = _lead_triage(tmp_path, **{
        "boss.preview.command": "touch data.db && python3 -m lead_triage_core.server\n"})
    before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
    built = BUILT + ["boss.preview.command"]
    assert ps.prove_app_serves(root, built, read=_files(root, built).get, timeout=20)["ok"]
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) == before


def test_a_library_that_serves_nothing_is_left_to_its_tests(tmp_path):
    root = _write(tmp_path, {"scoring/__init__.py": "", "scoring/rank.py": "def r():\n    pass\n",
                             "tests/test_rank.py": "import unittest\n"})
    record = ps.prove_app_serves(root, ["scoring/rank.py", "tests/test_rank.py"],
                                 read=lambda _p: "", timeout=5)
    assert record["ok"] is True and record["proven"] is False
    assert record["source"] == "library"


# --------------------------------------------------------------------------------------- #
# Review of cf8c2d4: the proof holds SERVICES to serving, and only services.
# --------------------------------------------------------------------------------------- #

def test_a_cli_main_with_a_main_block_is_left_to_its_tests(tmp_path):
    """cf8c2d4 launched a non-service build's main.py, saw "hello" and an exit, and failed
    app_serves — a gate nothing may ship past — so a CLI build that used to complete blocked."""
    files = {"main.py": "def main():\n    print('hello')\n\n\nif __name__ == '__main__':\n"
                        "    main()\n",
             "tests/test_main.py": "import unittest\n"}
    root = _write(tmp_path, files)
    record = ps.prove_app_serves(root, list(files), read=files.get, serves_http=False, timeout=5)
    assert record["ok"] is True and record["proven"] is False, record
    assert record["source"] == "library"
    assert record["command"] is None


def test_a_node_cli_with_a_start_script_is_left_to_its_tests(tmp_path):
    files = {"package.json": json.dumps({"name": "tool", "scripts": {"start": "node cli.js"}}),
             "cli.js": "console.log(process.argv.slice(2).join(' '))\n"}
    root = _write(tmp_path, files)
    record = ps.prove_app_serves(root, list(files), read=files.get, serves_http=False, timeout=5)
    assert record["ok"] is True and record["source"] == "library", record


def test_a_long_running_worker_that_is_not_a_service_is_not_required_to_serve(tmp_path):
    files = {"serve_queue.py": "import time\n\nif __name__ == '__main__':\n"
                               "    while True:\n        time.sleep(1)\n"}
    root = _write(tmp_path, files)
    record = ps.prove_app_serves(root, list(files), read=files.get, serves_http=False, timeout=5)
    assert record["ok"] is True and record["source"] == "library", record


def test_a_build_not_flagged_as_a_service_that_writes_a_server_is_still_proven(tmp_path):
    """The relaxation is for things that are not servers; a server the build wrote still runs."""
    root = _lead_triage(tmp_path)
    record = ps.prove_app_serves(root, BUILT, read=_files(root, BUILT).get,
                                 serves_http=False, timeout=20)
    assert record["ok"] and record["proven"] and record["source"] == "derived", record


def test_a_docs_only_follow_up_in_a_multi_app_repo_is_not_failed_for_the_repos_shape(tmp_path):
    """Changing a README in a repo holding a/server.py, b/server.py and index.html used to fail
    as 'ambiguous': the build was failed for what the repository contains, not what it did."""
    root = _write(tmp_path, {"a/server.py": SERVER, "b/server.py": SERVER,
                             "index.html": "<html></html>", "a/README.md": "# a\n",
                             "a/tests/test_x.py": "import unittest\n"})
    for changed in (["a/README.md"], ["a/README.md", "a/tests/test_x.py"]):
        record = ps.prove_app_serves(root, changed, serves_http=True, timeout=5)
        assert record["ok"] is True and record["source"] == "no_runnable_change", record


def test_a_service_change_to_runnable_source_is_still_held_to_serving(tmp_path):
    assert ps.touches_runnable_source(["inbox/models.py"])
    assert ps.touches_runnable_source(["web/index.html"])
    assert not ps.touches_runnable_source(["README.md", "docs/x.py", "tests/test_a.py",
                                           "web/src/app.test.ts"])


def test_a_non_final_decomposition_slice_skips_the_serve_proof():
    """Slice 1 of a decomposed build may be the data model alone; a non-final slice never
    opens a PR, so a failing app_serves went straight to block and stopped the chain."""
    assert gov.app_serves_check_skip_reason(
        {"decomposition_index": 1, "decomposition_total": 3})
    assert gov.app_serves_check_skip_reason(
        {"decomposition_index": 3, "decomposition_total": 3}) is None
    assert gov.app_serves_check_skip_reason({}) is None
    body = inspect.getsource(gov.run_governed_code_task)
    assert "_serve_skip = app_serves_check_skip_reason(fctx)" in body


# --------------------------------------------------------------------------------------- #
# Review of cf8c2d4: the operator sees the app's error where they look.
# --------------------------------------------------------------------------------------- #

def test_an_app_that_exits_is_reported_as_exiting_with_its_code_not_as_a_timeout(tmp_path):
    root = _lead_triage(tmp_path, **{
        "boss.preview.command": "python3 lead_triage_core/approvals.py\n"})
    built = BUILT + ["boss.preview.command"]
    record = ps.prove_app_serves(root, built, read=_files(root, built).get, timeout=20)
    assert record["ok"] is False
    assert record["exit_code"] not in (None, 0)
    assert record["error"].startswith("the app exited (code ")
    assert "within timeout" not in record["error"]
    assert "ImportError" in record["error"]


def test_a_blocked_run_carries_the_apps_error_and_an_honest_rescue_reason():
    """The blocked run's error said only "failing gates: ['app_serves']", and the partial-PR
    reason claimed never_block was disabled when app_serves had forced the block."""
    record = {"ok": False, "command": "python3 lead_triage_core/approvals.py",
              "error": "the app exited (code 1) before serving HTTP: error: ImportError: "
                       "attempted relative import with no known parent package"}
    error, reason = gov.blocked_gate_messages("qg_1", ["app_serves"], record)
    assert "python3 lead_triage_core/approvals.py" in error
    assert "ImportError" in error
    assert "never_block" not in reason and "did not start" in reason
    plain_error, plain_reason = gov.blocked_gate_messages("qg_1", ["review_code"], record)
    assert "ImportError" not in plain_error
    assert plain_reason == "quality gate blocked and never_block is disabled"
    assert "blocked_gate_messages(" in inspect.getsource(gov.run_governed_code_task)


def test_a_proof_on_the_host_for_a_docker_runtime_build_is_not_blamed_on_the_code():
    failed = {"ok": False, "harness": False, "error": "ModuleNotFoundError: No module named 'flask'"}
    marked = gov.app_serves_inconclusive_on_runtime(failed, "docker")
    assert gov.classify_gate_faults(failing=["app_serves"],
                                    evidence={"app_serves": marked}) == {"app_serves": "HARNESS"}
    assert gov.app_serves_inconclusive_on_runtime(failed, "subprocess") is failed


def test_a_config_module_named_like_an_app_does_not_beat_the_server_beside_it(tmp_path):
    """python_source_is_program counts any top-level call, so app_settings.py running
    load_dotenv() qualified — and won rule 1 by coming first in the changed list."""
    root = _write(tmp_path, {
        "app_settings.py": "from dotenv import load_dotenv\n\nload_dotenv()\nDEBUG = False\n",
        "server.py": SERVER.replace("from .approvals import approve\n",
                                    "approve = str\n"),
    })
    assert ps.resolve_app_entry(root, ["app_settings.py", "server.py"]) == ("py", "server.py")
