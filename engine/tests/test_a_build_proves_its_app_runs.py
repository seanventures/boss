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

from boss_engine.services.verification_target import NPM_INSTALL_CMD
import ast
import inspect
import json
import shlex
import textwrap

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
    """Resolution being right is worthless if the command it builds cannot start the app.
    (A named full-stack app: only a declared or named app is started by the proof.)"""
    root = _lead_triage(tmp_path)
    record = ps.prove_app_serves(root, BUILT, known_service=True, timeout=20)
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
    # ...with its dependencies installed first: a fresh preview clone has no node_modules.
    assert ps._detect_command(root, ["inbox/package.json"]) == [
        "sh", "-lc", f"cd inbox && {NPM_INSTALL_CMD} && node s.js"]


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
    record = ps.prove_app_serves(root, built, timeout=20)
    assert record["ok"] is False
    assert record["source"] == "declared"
    assert "ImportError" in record["error"]
    assert "relative import" in record["error"]
    assert record["harness"] is False


def test_a_service_build_that_declares_nothing_runnable_fails_the_check(tmp_path):
    root = _write(tmp_path, {"inbox_core/rules.py": "def score(x):\n    return x\n"})
    record = ps.prove_app_serves(root, ["inbox_core/rules.py"], known_service=True, timeout=5)
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


def _pipeline_source() -> str:
    """The governed pipeline's body: run_governed_code_task is now the wrapper that scopes the
    run's harness latch (harness_failures.run_scope), calls _run_governed_code_task and hands its
    result to _hold_for_github (a build finished while GitHub was down waits for it)."""
    wrapper = inspect.getsource(gov.run_governed_code_task)
    assert "= _run_governed_code_task(" in wrapper
    assert "return _hold_for_github(engine, run_id, result)" in wrapper
    return inspect.getsource(gov._run_governed_code_task)


def test_the_pipeline_gates_on_the_proof_and_records_it():
    """Wiring, checked at the source: the gate comes from prove_app_serves, the evidence
    carries its record, and the repair is handed the app's error."""
    body = _pipeline_source()
    assert "prove_app_serves(" in body
    assert 'gates["app_serves"] = bool(app_serves_record.get("ok"))' in body
    assert '"app_serves": app_serves_record' in body
    assert "'app_serves' failed: " in body and "_app_serves_sentence(app_serves_record)" in body
    assert "app_serves" in gov._ALL_GATE_NAMES


# --------------------------------------------------------------------------------------- #
# (d) A working declaration passes, and the proof leaves the build's tree untouched.
# --------------------------------------------------------------------------------------- #

def test_a_build_with_a_working_declaration_passes_the_check(tmp_path):
    root = _lead_triage(tmp_path, **{
        "boss.preview.command": "python3 -m lead_triage_core.server\n"})
    built = BUILT + ["boss.preview.command"]
    record = ps.prove_app_serves(root, built, known_service=True, timeout=20)
    assert record["ok"] is True and record["proven"] is True, record
    assert record["source"] == "declared"
    assert isinstance(record["port"], int)


def test_the_proof_runs_in_a_copy_so_nothing_it_writes_reaches_the_pull_request(tmp_path):
    """The sandbox diff is `git add -A`: a log or a SQLite file written here would ship."""
    root = _lead_triage(tmp_path, **{
        "boss.preview.command": "touch data.db && python3 -m lead_triage_core.server\n"})
    before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
    built = BUILT + ["boss.preview.command"]
    assert ps.prove_app_serves(root, built, timeout=20)["ok"]
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) == before


def test_a_library_that_serves_nothing_is_left_to_its_tests(tmp_path):
    root = _write(tmp_path, {"scoring/__init__.py": "", "scoring/rank.py": "def r():\n    pass\n",
                             "tests/test_rank.py": "import unittest\n"})
    record = ps.prove_app_serves(root, ["scoring/rank.py", "tests/test_rank.py"],
                                 timeout=5)
    assert record["ok"] is True and record["proven"] is False
    assert record["source"] == "not_declared"


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
    record = ps.prove_app_serves(root, list(files), timeout=5)
    assert record["ok"] is True and record["proven"] is False, record
    assert record["source"] == "not_declared"
    assert record["command"] is None


def test_a_node_cli_with_a_start_script_is_left_to_its_tests(tmp_path):
    files = {"package.json": json.dumps({"name": "tool", "scripts": {"start": "node cli.js"}}),
             "cli.js": "console.log(process.argv.slice(2).join(' '))\n"}
    root = _write(tmp_path, files)
    record = ps.prove_app_serves(root, list(files), timeout=5)
    assert record["ok"] is True and record["source"] == "not_declared", record


def test_a_long_running_worker_that_is_not_a_service_is_not_required_to_serve(tmp_path):
    files = {"serve_queue.py": "import time\n\nif __name__ == '__main__':\n"
                               "    while True:\n        time.sleep(1)\n"}
    root = _write(tmp_path, files)
    record = ps.prove_app_serves(root, list(files), timeout=5)
    assert record["ok"] is True and record["source"] == "not_declared", record


def test_an_undeclared_server_is_left_to_its_tests_unless_it_is_a_named_app(tmp_path):
    """Third review of b63717c: the gate no longer reads code to decide "is this a service".
    A server the build wrote but never declared is left to its tests (the implement prompt
    requires a declaration; the preview still guesses for it); a named full-stack app is
    still started."""
    root = _lead_triage(tmp_path)
    record = ps.prove_app_serves(root, BUILT, timeout=20)
    assert record["ok"] and not record["proven"] and record["source"] == "not_declared", record
    assert record["command"] is None
    assert record["not_a_service"].startswith("not a declared service")
    named = ps.prove_app_serves(root, BUILT, known_service=True, timeout=20)
    assert named["ok"] and named["proven"] and named["source"] == "derived", named


def test_a_docs_only_follow_up_in_a_multi_app_repo_is_not_failed_for_the_repos_shape(tmp_path):
    """Changing a README in a repo holding a/server.py, b/server.py and index.html used to fail
    as 'ambiguous': the build was failed for what the repository contains, not what it did."""
    root = _write(tmp_path, {"a/server.py": SERVER, "b/server.py": SERVER,
                             "index.html": "<html></html>", "a/README.md": "# a\n",
                             "a/tests/test_x.py": "import unittest\n"})
    for changed in (["a/README.md"], ["a/README.md", "a/tests/test_x.py"]):
        record = ps.prove_app_serves(root, changed, known_service=True, timeout=5)
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
    body = _pipeline_source()
    assert "_serve_skip = app_serves_check_skip_reason(" in body


# --------------------------------------------------------------------------------------- #
# Review of cf8c2d4: the operator sees the app's error where they look.
# --------------------------------------------------------------------------------------- #

def test_an_app_that_exits_is_reported_as_exiting_with_its_code_not_as_a_timeout(tmp_path):
    root = _lead_triage(tmp_path, **{
        "boss.preview.command": "python3 lead_triage_core/approvals.py\n"})
    built = BUILT + ["boss.preview.command"]
    record = ps.prove_app_serves(root, built, timeout=20)
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
    assert "never_block" not in reason and "does not serve" in reason
    plain_error, plain_reason = gov.blocked_gate_messages("qg_1", ["review_code"], record)
    assert "ImportError" not in plain_error
    assert plain_reason == "quality gate blocked and never_block is disabled"
    assert "blocked_gate_messages(" in _pipeline_source()


def test_a_docker_runtime_build_is_not_proven_on_the_host():
    """f16b5d4 ran the proof on the host and marked a failure inconclusive afterwards — which
    still started agent-written code on the host the docker runtime exists to keep it off."""
    reason = gov.app_serves_check_skip_reason({}, command_runtime="docker")
    assert reason and reason.startswith("inconclusive")
    assert gov.app_serves_check_skip_reason({}, command_runtime="subprocess") is None


def test_a_config_module_named_like_an_app_does_not_beat_the_server_beside_it(tmp_path):
    """python_source_is_program counts any top-level call, so app_settings.py running
    load_dotenv() qualified — and won rule 1 by coming first in the changed list."""
    root = _write(tmp_path, {
        "app_settings.py": "from dotenv import load_dotenv\n\nload_dotenv()\nDEBUG = False\n",
        "server.py": SERVER.replace("from .approvals import approve\n",
                                    "approve = str\n"),
    })
    assert ps.resolve_app_entry(root, ["app_settings.py", "server.py"]) == ("py", "server.py")


# --------------------------------------------------------------------------------------- #
# Second review of f16b5d4. Each test below fails on f16b5d4.
# --------------------------------------------------------------------------------------- #

# A root program the old fallback would run instead of the declared app: it exits at once.
STRAY_MAIN = "import sys\n\nif __name__ == '__main__':\n    sys.exit('not the app')\n"

# The lineage of a declared app: the run that built it wrote the declaration and the server.
DECLARED_LINEAGE = BUILT + ["boss.preview.command"]


def _declared_app(tmp_path):
    return _lead_triage(tmp_path, **{
        "boss.preview.command": "python3 -m lead_triage_core.server\n",
        "main.py": STRAY_MAIN})


def test_a_follow_up_whose_diff_lacks_the_declaration_is_proved_with_the_declared_command(
        tmp_path):
    """BLOCKER. f16b5d4 gave the proof only this run's diff, so a follow-up/retry/feedback build
    (or a final decomposition slice) of a declared app never saw the boss.preview.command an
    earlier run wrote. It fell back to heuristics and proved `python3 main.py`, which failed,
    while the preview — reading the lineage — would have run the declared command."""
    root = _declared_app(tmp_path)
    follow_up = ["lead_triage_core/store.py"]
    record = ps.prove_app_serves(root, follow_up, lineage=DECLARED_LINEAGE, timeout=20)
    assert record["ok"] and record["proven"], record
    assert record["source"] == "declared"
    assert record["command"] == "python3 -m lead_triage_core.server"


def test_the_proof_and_the_preview_run_the_same_command_for_a_lineage(tmp_path):
    """One function answers "what runs this lineage's app"; the preview and the proof both ask
    it with the lineage's files, so they cannot diverge again."""
    root = _declared_app(tmp_path)
    plan = ps.how_app_runs(root, ["lead_triage_core/store.py"] + DECLARED_LINEAGE)
    assert plan.source == "declared"
    assert ps._detect_command(root, DECLARED_LINEAGE) == plan.command
    assert "how_app_runs(" in inspect.getsource(ps._detect_command)
    assert "how_app_runs(" in inspect.getsource(ps.prove_app_serves)
    body = _pipeline_source()
    assert "lineage = lineage_impl_files(engine, run)" in body
    assert "lineage=lineage" in body


# A CLI that builds a server only under its `serve` subcommand, dispatched through argparse.
CLI_DISPATCHED = '''\
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def serve(args):
    ThreadingHTTPServer(("0.0.0.0", args.port), BaseHTTPRequestHandler).serve_forever()


def main():
    parser = argparse.ArgumentParser(prog="leads")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=serve)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
'''

# The same CLI, but main() calls the server function by name behind an if.
CLI_BRANCHED = '''\
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def serve(port):
    ThreadingHTTPServer(("0.0.0.0", port), BaseHTTPRequestHandler).serve_forever()


def main():
    parser = argparse.ArgumentParser(prog="leads")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("export")
    args = parser.parse_args()
    if args.cmd == "serve":
        serve(8000)


if __name__ == "__main__":
    main()
'''


def test_a_cli_follow_up_flagged_as_a_web_build_is_not_held_to_serving(tmp_path):
    """MAJOR. tools/builtin.py and intent_router set web_app_build on EVERY build-on-existing /
    extend, CLIs included, and f16b5d4 read that flag as "this is a service": a CLI follow-up
    whose entry prints and exits was launched, did not answer HTTP, and was blocked."""
    files = {"main.py": "def main():\n    print('3 leads')\n\n\nif __name__ == '__main__':\n"
                        "    main()\n"}
    root = _write(tmp_path, files)
    extend_fctx = {"root_run_id": "crun_root", "web_app_build": True}
    assert gov.serve_proof_known_service({}, extend_fctx) is False
    record = ps.prove_app_serves(root, list(files), lineage=["main.py", "tests/test_main.py"],
                                 known_service=gov.serve_proof_known_service({}, extend_fctx),
                                 timeout=5)
    assert record["ok"] is True and record["source"] == "not_declared", record


def test_a_library_follow_up_flagged_as_a_web_build_is_not_held_to_serving(tmp_path):
    """The same flag on a library: nothing to run, and f16b5d4 failed it for 'declaring no
    way to run it'."""
    files = {"scoring/__init__.py": "", "scoring/rank.py": "def rank(x):\n    return x\n"}
    root = _write(tmp_path, files)
    extend_fctx = {"root_run_id": "crun_root", "web_app_build": True}
    record = ps.prove_app_serves(root, ["scoring/rank.py"], lineage=list(files),
                                 known_service=gov.serve_proof_known_service({}, extend_fctx),
                                 timeout=5)
    assert record["ok"] is True and record["source"] == "not_declared", record
    body = _pipeline_source()
    assert "known_service=serve_proof_known_service(spec, fctx)" in body


def test_a_named_full_stack_app_is_still_known_to_be_a_service():
    assert gov.serve_proof_known_service({"app_root": "inbox"}, {}) is True


def test_a_cli_whose_server_hides_behind_a_dispatched_subcommand_is_not_a_service(tmp_path):
    """MINOR. f16b5d4 counted a server constructed ANYWHERE in the file, ran the CLI bare,
    argparse exited 2, and the build was blocked."""
    assert not ps.python_source_serves(CLI_DISPATCHED)
    files = {"main.py": CLI_DISPATCHED}
    root = _write(tmp_path, files)
    record = ps.prove_app_serves(root, list(files), timeout=10)
    assert record["ok"] is True and record["source"] == "not_declared", record


def test_a_cli_with_a_reachable_serve_branch_is_never_launched(tmp_path):
    """The static rule cannot tell this CLI from a server (python_source_serves says it
    serves); the gate no longer asks it. Undeclared, it is not started at all."""
    assert ps.python_source_serves(CLI_BRANCHED)
    files = {"main.py": CLI_BRANCHED}
    root = _write(tmp_path, files)
    record = ps.prove_app_serves(root, list(files), timeout=10)
    assert record["ok"] is True and record["source"] == "not_declared", record
    assert record["command"] is None and "exit_code" not in record


def test_a_declared_command_that_prints_usage_is_still_a_failure(tmp_path):
    """The build SAID this command serves; printing usage does not excuse it."""
    root = _write(tmp_path, {"main.py": CLI_BRANCHED, "boss.preview.command": "python3 main.py\n"})
    record = ps.prove_app_serves(root, ["main.py", "boss.preview.command"], timeout=10)
    assert record["ok"] is False and record["source"] == "declared", record


@pytest.mark.parametrize("source", [
    "from http.server import ThreadingHTTPServer\n\n\ndef main():\n"
    "    ThreadingHTTPServer(('', 0), None).serve_forever()\n\n\n"
    "if __name__ == '__main__':\n    main()\n",
    "from flask import Flask\n\n\ndef create_app():\n    return Flask(__name__)\n\n\n"
    "app = create_app()\n",
    "from flask import Flask\napp = Flask(__name__)\n",
])
def test_a_server_reached_from_what_runs_still_serves(source):
    assert ps.python_source_serves(source)


def test_the_proof_runs_the_app_without_the_workers_credentials(tmp_path, monkeypatch):
    """MINOR (security). launch_app used {**os.environ}: the proof ran agent-written code with
    the worker's DATABASE_URL and tokens — the exposure behind the 2026-09-02 table wipe."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://prod")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    env = ps.app_environment(tmp_path, 4321)
    assert "DATABASE_URL" not in env and "GITHUB_TOKEN" not in env
    assert env["PORT"] == "4321" and env["HOST"] == "0.0.0.0" and "PATH" in env
    root = _lead_triage(tmp_path / "app", **{"boss.preview.command": (
        'test -z "$DATABASE_URL" && test -z "$GITHUB_TOKEN" && '
        'python3 -m lead_triage_core.server\n')})
    record = ps.prove_app_serves(root, BUILT + ["boss.preview.command"], timeout=20)
    assert record["ok"] and record["proven"], record


def test_the_proof_is_not_run_on_the_host_for_a_micro_vm_build():
    """MINOR. A micro-VM build's deps live in the guest; the proof would run its code on the
    control-plane host and fail on a missing module that is not the code's fault."""
    reason = gov.app_serves_check_skip_reason({}, use_microvm=True)
    assert reason and reason.startswith("inconclusive")
    body = _pipeline_source()
    assert "use_microvm=use_microvm)" in body


def test_a_failure_with_no_command_never_claims_boss_ran_one():
    """MINOR. f16b5d4 said "BOSS ran `(no run command found)` — " with nothing after it."""
    record = {"ok": False, "command": None, "error": None, "source": "none"}
    sentence = ps.app_serves_failure_sentence(record)
    assert "BOSS ran" not in sentence and "(no run command found)" not in sentence
    assert not sentence.rstrip().endswith(("—", "— ", ":"))
    error, _reason = gov.blocked_gate_messages("qg_1", ["app_serves"], record)
    assert "BOSS ran" not in error and not error.rstrip().endswith("—")


def test_a_failure_names_where_its_command_came_from():
    declared = ps.app_serves_failure_sentence(
        {"command": "python3 -m x.server", "source": "declared", "error": "boom"})
    resolved = ps.app_serves_failure_sentence(
        {"command": "python3 main.py", "source": "resolved", "error": "boom"})
    assert "declared" in declared and "`python3 -m x.server`" in declared
    assert "resolver" in resolved


def test_an_app_that_started_but_never_answered_is_told_apart_from_one_that_exited(tmp_path):
    root = _write(tmp_path, {"boss.preview.command": "sleep 30\n", "app.py": "x = 1\n"})
    record = ps.prove_app_serves(root, ["boss.preview.command", "app.py"], timeout=2)
    assert record["ok"] is False and "exit_code" not in record, record
    assert record["error"].startswith(
        "the app started and kept running but did not answer HTTP"), record


# --- The deploy runs what the build proved (MAJOR, latent) ------------------------------ #

def _git_repo(root, branch="boss/code/crun_x"):
    import subprocess
    for args in (["init", "-q", "-b", branch], ["add", "-A"],
                 ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "app"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return branch


def _build_dist(tmp_path, monkeypatch, repo, changed):
    from types import SimpleNamespace

    from boss_engine.services import deploy_service as ds
    from boss_engine.services import repo_host

    branch = _git_repo(repo)
    monkeypatch.setattr(repo_host, "clone_url_for",
                        lambda *_a, **_k: "file://" + str(repo))
    return ds._build_dist(SimpleNamespace(target_repo_host="github"), "o", "r", branch,
                          changed=changed)


def _serves(directory, shell_command):
    log = str(directory / ".deploy-check.log")
    proc, port_hint = ps.launch_app(directory, ["sh", "-c", shell_command], log)
    try:
        return ps._discover_working_port(log, port_hint, timeout=20, proc=proc) is not None
    finally:
        ps._kill(proc.pid)


def test_the_deploy_runs_a_package_module_server_the_way_the_preview_does(tmp_path, monkeypatch):
    """deploy_service ran a resolved Python entry as `python {entry}`: a package-module server
    dies there on the relative-import ImportError the preview was fixed for, the health gate
    sees nothing, and the deploy rolls back."""
    from boss_engine.services import deploy_service as ds

    repo = _lead_triage(tmp_path / "repo")
    payload, mode, entry, _sha = _build_dist(tmp_path, monkeypatch, repo, BUILT)
    assert (mode, entry) == ("py", "python3 -m lead_triage_core.server")
    assert f"sh -c {shlex.quote(entry)}" in ds._docker_run_cmd("a", 80, "/srv/a", mode, entry)
    assert _serves(payload / "dist", entry)


def test_the_deploy_runs_the_entry_not_the_builds_declared_command(tmp_path, monkeypatch):
    """A declaration is a preview / build-proof contract, not a production command (fourth
    review of dc3aeb7): deploy runs the resolved entry the way python_entry_invocation says,
    whatever the build declared. dc3aeb7 shipped the declared command."""
    repo = _lead_triage(tmp_path / "repo", **{
        "boss.preview.command": "LEAD_TRIAGE_DEV=1 python3 -m lead_triage_core.server --reload\n",
        "main.py": STRAY_MAIN})
    payload, mode, entry, _sha = _build_dist(tmp_path, monkeypatch, repo, DECLARED_LINEAGE)
    assert (mode, entry) == ("py", "python3 -m lead_triage_core.server")
    assert _serves(payload / "dist", entry)


def test_the_deploy_runs_a_plain_script_as_python3_and_it_serves(tmp_path, monkeypatch):
    from boss_engine.services import deploy_service as ds

    repo = _write(tmp_path / "repo", {"serve.py": (
        "import os\nfrom http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer\n"
        "ThreadingHTTPServer(('0.0.0.0', int(os.environ['PORT'])), SimpleHTTPRequestHandler)"
        ".serve_forever()\n")})
    payload, mode, entry, _sha = _build_dist(tmp_path, monkeypatch, repo, ["serve.py"])
    assert (mode, entry) == ("py", "python3 serve.py")
    assert "python:3.12-slim sh -c 'python3 serve.py'" in ds._docker_run_cmd(
        "a", 80, "/srv/a", mode, entry)
    assert _serves(payload / "dist", entry)


def test_a_nested_package_module_deploys_from_above_its_package(tmp_path, monkeypatch):
    """src/ is not a package: the module runs as `-m lead_triage_core.server` from src/."""
    from boss_engine.services import deploy_service as ds

    root = _lead_triage(tmp_path / "lt")
    repo = tmp_path / "repo"
    for rel in BUILT:
        (repo / "src" / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / "src" / rel).write_text((root / rel).read_text())
    changed = [f"src/{rel}" for rel in BUILT]
    payload, mode, entry, _sha = _build_dist(tmp_path, monkeypatch, repo, changed)
    assert (mode, entry) == ("py", "cd src && python3 -m lead_triage_core.server")
    assert f"sh -c {shlex.quote(entry)}" in ds._docker_run_cmd("a", 80, "/srv/a", mode, entry)
    assert _serves(payload / "dist", entry)


def test_the_container_preview_runs_the_builds_declared_command(tmp_path):
    root = _declared_app(tmp_path)
    ps._generate_dockerfile(root, DECLARED_LINEAGE)
    assert 'CMD ["sh", "-lc", "python3 -m lead_triage_core.server"]' in (
        root / "Dockerfile").read_text()


# --------------------------------------------------------------------------------------- #
# Third review of b63717c. Each test below fails on b63717c.
# --------------------------------------------------------------------------------------- #

# A CLI whose main() picks `serve` by hand, if/elif on argv: run bare it lists and exits 0.
CLI_IF_ELIF = '''\
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def serve():
    ThreadingHTTPServer(("0.0.0.0", 8000), BaseHTTPRequestHandler).serve_forever()


def export():
    print("exported 3 leads")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "serve":
        serve()
    elif cmd == "export":
        export()
    else:
        print("3 leads")


if __name__ == "__main__":
    main()
'''

# The same CLI with an optional --serve flag: run bare it prints and exits 0, no usage line.
CLI_SERVE_FLAG = '''\
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def serve(port):
    ThreadingHTTPServer(("0.0.0.0", port), BaseHTTPRequestHandler).serve_forever()


def main():
    parser = argparse.ArgumentParser(prog="leads")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.serve:
        serve(args.port)
    else:
        print("3 leads")


if __name__ == "__main__":
    main()
'''


@pytest.mark.parametrize("source", [CLI_IF_ELIF, CLI_SERVE_FLAG], ids=["if-elif", "serve-flag"])
def test_a_cli_with_a_serve_subcommand_or_flag_is_not_blocked_as_a_service(tmp_path, source):
    """BLOCKER, third review of b63717c. python_source_serves followed main() to serve() by
    name, so a CLI whose main() dispatches `serve` with if/elif or a --serve flag counted as a
    service: run bare it printed "3 leads", exited 0, and was failed for not answering HTTP —
    a gate nothing ships past. The gate now holds only a DECLARED build to serving."""
    files = {"leads.py": source, "tests/test_leads.py": "import unittest\n"}
    root = _write(tmp_path, files)
    record = ps.prove_app_serves(root, list(files), lineage=list(files), timeout=10)
    assert record["ok"] is True, record
    assert record["source"] == "not_declared" and record["command"] is None, record
    assert record["not_a_service"].startswith("not a declared service")
    # The decision reads no code: the static "evidently serves" inference is gone from it.
    code = ast.parse(textwrap.dedent(inspect.getsource(ps.prove_app_serves))).body[0]
    code.body = code.body[1:]  # the docstring tells the history; the code must not read source
    body = ast.unparse(code)
    assert "python_source_serves" not in body and "evidently_serves" not in body
    assert not hasattr(ps, "_entry_evidently_serves")


def test_the_started_app_holds_no_model_credential(tmp_path, monkeypatch):
    """MINOR, third review of b63717c. app_environment passed agent_environment through as is,
    and that allow-list carries the CODING AGENT's model credential on purpose — so every serve
    proof and every host preview started agent-written code holding ANTHROPIC_API_KEY,
    CLAUDE_CODE_OAUTH_TOKEN and ANTHROPIC_BASE_URL."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-secret")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.internal")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    # An operator let more through for the agent; the app gets none of it.
    monkeypatch.setenv("BOSS_AGENT_ENV_ALLOW", "STRIPE_API_KEY,SLACK_BOT_TOKEN,LOG_LEVEL")
    monkeypatch.setenv("STRIPE_API_KEY", "sk_live")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    env = ps.app_environment(tmp_path, 4321)
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL",
                 "OPENAI_API_KEY", "STRIPE_API_KEY", "SLACK_BOT_TOKEN"):
        assert name not in env, name
    assert env["PORT"] == "4321" and env["HOST"] == "0.0.0.0" and env["HOME"] == str(tmp_path)
    assert "PATH" in env and env["LOG_LEVEL"] == "debug"
    # And through the launcher every preview and proof uses: the app sees none of them.
    root = _lead_triage(tmp_path / "app", **{"boss.preview.command": (
        'test -z "$ANTHROPIC_API_KEY" && test -z "$CLAUDE_CODE_OAUTH_TOKEN" && '
        'test -z "$ANTHROPIC_BASE_URL" && test -n "$PORT" && '
        'python3 -m lead_triage_core.server\n')})
    record = ps.prove_app_serves(root, BUILT + ["boss.preview.command"], timeout=20)
    assert record["ok"] and record["proven"], record


def test_the_proof_cannot_modify_the_build_workdir_through_node_modules(tmp_path):
    """MINOR, third review of b63717c. _proof_tree symlinked each node_modules into the real
    build workdir, so a declared `npm install && …` — or any app writing under node_modules —
    wrote through the link into the tree the sandbox diffs and ships."""
    root = _lead_triage(tmp_path, **{
        "node_modules/left-pad/index.js": "module.exports = 1\n",
        "web/node_modules/dep/index.js": "module.exports = 2\n",
        "boss.preview.command": (
            "echo tampered > node_modules/left-pad/index.js && "
            "echo new > node_modules/added.js && rm -rf web/node_modules/dep && "
            "python3 -m lead_triage_core.server\n")})
    before = {p.relative_to(root).as_posix(): p.read_bytes()
              for p in root.rglob("*") if p.is_file()}
    record = ps.prove_app_serves(root, BUILT + ["boss.preview.command"], timeout=20)
    assert record["ok"] and record["proven"], record
    after = {p.relative_to(root).as_posix(): p.read_bytes()
             for p in root.rglob("*") if p.is_file()}
    assert after == before
