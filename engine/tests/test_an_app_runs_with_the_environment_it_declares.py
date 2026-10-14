"""Every runner gives an app the environment it DECLARES (templates.run_env), by one rule.

HelpDesk AI, crun_db538dff872549bf9359 (2026-10-13): all writing and review done in 19.1 min,
then blocked by the app-start proof — "the app exited (code 1) before serving HTTP: Error:
SESSION_SECRET must be set to at least 32 characters (BOSS generates one)". The login-oauth
connector declares SESSION_SECRET `test_default: generated`; BOSS generated it for the deploy and
stored one for the preview, and gave the proof nothing. The same block said "the change holds
something that looks like a committed secret": helpdesk-ai/vitest.config.ts:14-15
(assigned_secret), DEMO_PASSWORD and SESSION_SECRET set for the test workers to values that say
they are test values — a false positive of a class, fixed in secret_scan.
"""
from __future__ import annotations

import hashlib
import inspect
import textwrap

import pytest

from boss_engine.services import governed_code_task_service as gov
from boss_engine.services import preview_service as ps
from boss_engine.templates import gate, run_env
from boss_engine.templates.secret_scan import is_test_only_path, scan_text
from boss_engine.templates.validator import tree_from_dir, validate_tree

from .templates._fixtures import REPO_ROOT


def _login_needs() -> dict:
    """What the real login-oauth connector declares."""
    report = validate_tree(tree_from_dir(REPO_ROOT / "templates" / "apps" / "login-oauth"),
                           source="builtin")
    return run_env.needs_from_specs(report.manifest.secrets)


# An app that refuses to start without a session key of 32+ characters — HelpDesk's authRouter —
# and records a hash of the key it got (never the key) where the test can read it.
def _app(root, seen_dir, *, extra_required: str = "") -> None:
    server = textwrap.dedent(f'''\
        import hashlib, os, sys, time
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        secret = os.environ.get("SESSION_SECRET") or ""
        if len(secret) < 32:
            sys.exit("Error: SESSION_SECRET must be set to at least 32 characters (BOSS generates one)")
        for name in {extra_required!r}.split():
            if not os.environ.get(name):
                sys.exit(f"Error: {{name}} must be set")
        with open(os.path.join({str(seen_dir)!r}, str(time.time_ns())), "w") as fh:
            fh.write(hashlib.sha256(secret.encode()).hexdigest())

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
            def log_message(self, *a):
                pass

        ThreadingHTTPServer(("0.0.0.0", int(os.environ["PORT"])), H).serve_forever()
        ''')
    (root / "helpdesk").mkdir(parents=True, exist_ok=True)
    (root / "helpdesk" / "server.py").write_text(server)
    (root / "helpdesk" / "boss.preview.command").write_text("python3 server.py\n")


BUILT = ["helpdesk/server.py", "helpdesk/boss.preview.command"]


def _seen(seen_dir) -> list[str]:
    return [p.read_text() for p in sorted(seen_dir.iterdir())]


# ---- 1. the proof gets the generated secret, and the app starts --------------------------------

def test_the_proof_gives_the_connectors_generated_session_secret_and_the_app_starts(tmp_path):
    root, seen = tmp_path / "repo", tmp_path / "seen"
    seen.mkdir()
    _app(root, seen)
    record = ps.prove_app_serves(root, BUILT, timeout=20, needs=_login_needs())
    assert record["ok"] is True and record["proven"] is True, record
    assert "SESSION_SECRET" in record["environment"]["generated"]
    # The optional providers stay unset, as on a deploy: the app runs without them.
    assert {"GOOGLE_CLIENT_ID", "GITHUB_CLIENT_SECRET"} <= set(record["environment"]["unset"])
    assert record["environment"]["needs_secret"] == []
    assert len(_seen(seen)) == 1


def test_without_the_declaration_the_same_app_is_the_incident(tmp_path):
    """The control: what the proof did before, and what the app says."""
    root, seen = tmp_path / "repo", tmp_path / "seen"
    seen.mkdir()
    _app(root, seen)
    record = ps.prove_app_serves(root, BUILT, timeout=20)
    assert record["ok"] is False
    assert "SESSION_SECRET must be set" in record["error"]


def test_a_session_key_named_in_env_example_is_generated_with_no_template(tmp_path):
    root, seen = tmp_path / "repo", tmp_path / "seen"
    seen.mkdir()
    _app(root, seen)
    (root / "helpdesk" / ".env.example").write_text(
        "SESSION_SECRET=\nGOOGLE_CLIENT_ID=\nDATABASE_URL=postgres://localhost/helpdesk\n")
    record = ps.prove_app_serves(root, BUILT, timeout=20)
    assert record["ok"] is True, record
    assert record["environment"]["generated"] == ["SESSION_SECRET"]


def test_a_boss_deploy_yaml_generate_entry_is_generated_for_the_proof(tmp_path):
    root, seen = tmp_path / "repo", tmp_path / "seen"
    seen.mkdir()
    _app(root, seen)
    (root / "helpdesk" / "boss.deploy.yaml").write_text(
        "version: 1\nname: helpdesk\nkind: commands\ninstall: ['true']\n"
        "env:\n  SESSION_SECRET: {generate: token}\n")
    record = ps.prove_app_serves(root, BUILT, timeout=20)
    assert record["ok"] is True, record
    assert "SESSION_SECRET" in record["environment"]["generated"]


# ---- 2. fresh per proof, and never a deploy's ---------------------------------------------------

def test_the_generated_value_is_fresh_for_every_proof_and_never_the_bound_one(tmp_path):
    root, seen = tmp_path / "repo", tmp_path / "seen"
    seen.mkdir()
    _app(root, seen)
    stored = "s" * 20 + "stored-build-binding-value"
    for _ in range(2):
        rec = ps.prove_app_serves(root, BUILT, timeout=20, needs=_login_needs(),
                                  bound={"SESSION_SECRET": stored})
        assert rec["ok"] is True, rec
    first, second = _seen(seen)
    assert first != second
    assert hashlib.sha256(stored.encode()).hexdigest() not in (first, second)


def test_the_rule_generates_for_throwaway_runs_and_reads_the_stored_value_for_a_deploy():
    need = run_env.Need("SESSION_SECRET", "generated", True, "template")
    for purpose in run_env.THROWAWAY:
        assert run_env.decide(need, has_value=True, purpose=purpose) == "generate"
    assert run_env.decide(need, has_value=True, purpose="deploy") == "use"
    assert run_env.decide(need, has_value=False, purpose="deploy") == "needs"
    a = run_env.resolve({"SESSION_SECRET": need}, purpose="proof")
    b = run_env.resolve({"SESSION_SECRET": need}, purpose="proof")
    assert len(a.values["SESSION_SECRET"]) >= 32 and a.values != b.values
    deploy = run_env.resolve({"SESSION_SECRET": need}, purpose="deploy",
                             bound={"SESSION_SECRET": "the-stored-deploy-value-0123456789abcdef"})
    assert deploy.values["SESSION_SECRET"] == "the-stored-deploy-value-0123456789abcdef"
    assert deploy.generated == []


def test_resolving_a_throwaway_environment_never_writes_a_binding():
    """Nothing in the throwaway path can store: resolve takes no connection, and the proof's
    declarations are read before it, not written."""
    assert "c" not in inspect.signature(run_env.resolve).parameters
    src = inspect.getsource(run_env)
    assert "put(" not in src and "store_app_secret" not in src and "INSERT" not in src


# ---- 3. a required secret nobody can generate is a named verdict -----------------------------

def test_a_missing_required_secret_is_named_and_the_app_is_not_started(tmp_path):
    root, seen = tmp_path / "repo", tmp_path / "seen"
    seen.mkdir()
    _app(root, seen, extra_required="STRIPE_SECRET_KEY")
    needs = {**_login_needs(),
             **run_env.needs_from_specs([{"name": "STRIPE_SECRET_KEY"}], source="template payments")}
    record = ps.prove_app_serves(root, BUILT, timeout=20, needs=needs)
    assert record["ok"] is False and record["harness"] is False
    assert record["needs_secret"] == ["STRIPE_SECRET_KEY"]
    assert record["error"].startswith("the app needs a secret: STRIPE_SECRET_KEY (declared by "
                                      "template payments)")
    assert _seen(seen) == []                       # never started to crash on it
    assert ps.app_serves_headline(record) == (
        "the app needs a secret: STRIPE_SECRET_KEY (declared by template payments)")
    held = gate.hold_outcome(["app_serves", "template_tests"], app_serves=record,
                             template_tests={"because": "app_serves"})
    assert held["error"].startswith("Blocked: the app needs a secret: STRIPE_SECRET_KEY")


def test_a_bound_value_for_the_required_secret_is_given(tmp_path):
    root, seen = tmp_path / "repo", tmp_path / "seen"
    seen.mkdir()
    _app(root, seen, extra_required="STRIPE_SECRET_KEY")
    needs = {**_login_needs(), **run_env.needs_from_specs([{"name": "STRIPE_SECRET_KEY"}])}
    record = ps.prove_app_serves(root, BUILT, timeout=20, needs=needs,
                                 bound={"STRIPE_SECRET_KEY": "sk_test_" + "a" * 24})
    assert record["ok"] is True, record


def test_a_crash_on_a_listed_variable_is_named_and_is_the_coders_to_fix(tmp_path):
    """A listed name is optional (crun_0d82ab62): named, never a person's "needs a secret"."""
    root, seen = tmp_path / "repo", tmp_path / "seen"
    seen.mkdir()
    _app(root, seen, extra_required="OPENAI_API_KEY")
    (root / "helpdesk" / ".env.example").write_text("SESSION_SECRET=\nOPENAI_API_KEY=\n")
    record = ps.prove_app_serves(root, BUILT, timeout=20)
    assert record["ok"] is False and not record.get("needs_secret")
    assert record["optional_unset"] == ["OPENAI_API_KEY"]
    assert record["error"].startswith(
        "the app must start without OPENAI_API_KEY: it is optional (listed in .env.example)")


def test_the_connector_smoke_check_names_the_secret_instead_of_a_bare_did_not_start():
    _, tt = gate.connector_smoke(None, {"ok": False, "needs_secret": ["STRIPE_SECRET_KEY"]},
                                 tests_passed=True)
    assert "STRIPE_SECRET_KEY" in tt["error"] and tt["because"] == "app_serves"


# ---- every runner uses the one rule -----------------------------------------------------------

def test_the_proof_preview_and_deploy_all_resolve_through_run_env():
    from boss_engine.oidc_provider import app_sign_in
    from boss_engine.templates import secrets

    assert "_app_run_declarations(engine, run)" in inspect.getsource(gov._run_governed_code_task)
    assert "needs=_run_needs, bound=_run_bound" in inspect.getsource(gov._run_governed_code_task)
    assert "app_start_environment(" in inspect.getsource(ps.prove_app_serves)
    assert "resolve(" in inspect.getsource(ps.app_start_environment)
    assert "resolve(" in inspect.getsource(app_sign_in.preview_run_environment)
    for launcher in (ps.start_preview, ps._respawn_process_preview, ps.start_container_preview):
        assert "preview_app_environment(" in inspect.getsource(launcher), launcher.__name__
    assert "decide(" in inspect.getsource(secrets.deploy_secrets)


def test_the_container_preview_never_puts_a_value_in_argv():
    src = inspect.getsource(ps.start_container_preview)
    assert '"--env-file"' in src
    assert 'f"{k}={v}"' not in src.split("--env-file")[0].split("docker_argv =")[-1]


# ---- the committed-secret finding: a test's own self-declared test value ---------------------

@pytest.mark.parametrize("path", ["helpdesk-ai/vitest.config.ts", "app/jest.config.js",
                                  "app/tests/auth.test.ts", "conftest.py", "web/playwright.config.ts"])
def test_a_test_value_in_a_test_only_file_is_not_a_committed_secret(path):
    text = ('    env: {\n'
            '      DEMO_PASSWORD: "vitest-demo-password-1234",\n'
            '      SESSION_SECRET: "test-session-secret-for-the-vitest-workers",\n'
            '    },\n')
    assert is_test_only_path(path)
    assert scan_text(path, text) == []


def test_the_same_literal_in_app_code_is_still_a_finding():
    text = 'const SESSION_SECRET = "test-session-secret-for-the-vitest-workers";\n'
    assert not is_test_only_path("helpdesk-ai/src/server/auth/authRouter.ts")
    assert [f.rule for f in scan_text("helpdesk-ai/src/server/auth/authRouter.ts", text)] == [
        "assigned_secret"]


def test_a_test_file_literal_that_does_not_say_it_is_a_test_value_is_still_a_finding():
    """QA's real-looking password stays a finding (fixture_secret_repair hands it back)."""
    found = scan_text("tests/auth.test.ts", 'const password = "Sup3rH0rse!Battery9";\n')
    assert [f.rule for f in found] == ["assigned_secret"]


def test_a_real_credential_format_in_a_test_file_is_always_a_finding():
    token = "ghp_" + "A1b2C3d4E5" * 4
    found = scan_text("helpdesk-ai/vitest.config.ts", f'GITHUB_TOKEN: "{token}",\n')
    assert "github_token" in [f.rule for f in found]
    found = scan_text("tests/a.test.ts", 'x = "test-in-a-bound-value-1234"\n',
                      values={"PAY_KEY": "test-in-a-bound-value-1234"})
    assert [f.rule for f in found] == ["bound_secret:PAY_KEY"]


def test_the_blocked_line_says_where_the_secret_scan_matched_and_never_what():
    scan = {"count": 4, "findings": [
        {"path": "helpdesk-ai/src/config.ts", "line": 14, "rule": "assigned_secret"},
        {"path": "helpdesk-ai/src/config.ts", "line": 15, "rule": "assigned_secret"},
        {"path": "helpdesk-ai/src/db.ts", "line": 3, "rule": "url_with_password"},
        {"path": "helpdesk-ai/src/db.ts", "line": 9, "rule": "url_with_password"}]}
    held = gate.hold_outcome(["no_committed_secrets"], secret_scan=scan)
    assert ("looks like a committed secret (helpdesk-ai/src/config.ts:14 assigned_secret, "
            "helpdesk-ai/src/config.ts:15 assigned_secret, helpdesk-ai/src/db.ts:3 "
            "url_with_password, and 1 more)") in held["error"]
    assert "secret_scan" in inspect.getsource(gate.held_run)


# ---- a secret only a person can set is the person's act: no repair round, blocked with where --

NEEDS = {"ok": False, "needs_secret": ["STRIPE_SECRET_KEY"], "harness": False,
         "command": "python3 server.py", "source": "declared",
         "error": ("the app needs a secret: STRIPE_SECRET_KEY (declared by template payments). BOSS "
                   "cannot make it; set it in the app's Secrets (environment build), then retry "
                   "the build. BOSS did not start it.")}


def test_a_needs_secret_verdict_is_a_persons_fault_not_the_coders():
    faults = gov.classify_gate_faults(failing=["app_serves"], evidence={"app_serves": NEEDS})
    assert faults == {"app_serves": "PERSON"}
    # The control: the app crashing on its own is still the coder's.
    assert gov.classify_gate_faults(failing=["app_serves"], evidence={"app_serves": {
        "ok": False, "error": "the app exited (code 1)"}}) == {"app_serves": "WORK"}


@pytest.mark.parametrize("never_block", [True, False])
@pytest.mark.parametrize("advisory", [True, False])
def test_no_repair_round_goes_to_the_coder_and_nothing_ships(never_block, advisory):
    failing = ["app_serves", "template_tests", "code_review_passed"]
    faults = gov.classify_gate_faults(failing=failing, evidence={"app_serves": NEEDS})
    action = gov.resolve_gate_failure_action(
        failing=failing, advisory_gate=advisory, rounds_used=0, max_repair_rounds=2,
        never_block=never_block, faults=faults, template_repair_max=2)
    assert action == "block"


def test_the_blocked_build_names_the_secret_and_where_to_set_it():
    error, reason = gov.blocked_gate_messages("qg_1", ["app_serves"], NEEDS)
    assert error.startswith("Blocked: the app needs a secret: STRIPE_SECRET_KEY "
                            "(declared by template payments). Set STRIPE_SECRET_KEY for "
                            "environments build, preview and deploy in the app's Secrets, then press Retry")
    assert "STRIPE_SECRET_KEY" in reason
    held = gate.hold_outcome(["app_serves", "template_tests"], app_serves=NEEDS,
                             template_tests={"because": "app_serves"})
    assert held["next_step"].startswith("Set STRIPE_SECRET_KEY for environments build, preview and deploy in the "
                                        "app's Secrets, then press Retry")
