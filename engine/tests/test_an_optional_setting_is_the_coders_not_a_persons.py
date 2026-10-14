"""A setting the app only LISTS is optional: a crash on it is the coder's to fix, never a person's.

HelpDesk AI, crun_0d82ab62bb6443d4a576 (2026-10-13): blocked after 19.4 min with "the app needs
a secret: DEMO_PASSWORD (declared by .env.example) … Set DEMO_PASSWORD … then press Retry (the
coder is not asked to work around a missing secret)". The app had refused to start:
`DEMO_PASSWORD must be set (the seed script and demo logins refuse to run without it)`.
DEMO_PASSWORD was only listed in .env.example (run_env kind "listed" → "unset"), but
explain_crash turned a crash naming ANY declared variable into "needs a secret", and the PERSON
fault class blocked with no repair round. Only a variable the app declares as REQUIRED and only a
person can supply (a SecretSpec with required=true and no test default, boss.deploy.yaml
`{secret}`) is a person's act.

The same record listed DATABASE_URL (and PORT, HOST) as "unset" for an app built with its own
build-services Postgres. The value was never stripped (owned_process injects the run's lease into
every workload process after the app's environment filter), but the record said the app was not
given it, and a crash naming it would have been blamed on a missing setting. What BOSS supplies
itself — the launcher's PORT/HOST, the run's database, a preview's issued APP_BASE_URL — wins.
"""
from __future__ import annotations

import hashlib
import inspect
import textwrap

import pytest

from boss_engine.services import build_services as bs
from boss_engine.services import code_agent
from boss_engine.services import governed_code_task_service as gov
from boss_engine.services import owned_process
from boss_engine.services import preview_service as ps
from boss_engine.templates import run_env

BUILT = ["helpdesk/server.py", "helpdesk/boss.preview.command"]


def _app(root, seen_dir, *, required: str) -> None:
    """An app that refuses to start without each of `required`, and records a hash of its
    DATABASE_URL (never the value) where the test can read it."""
    server = textwrap.dedent(f'''\
        import hashlib, os, sys
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        for name in {required!r}.split():
            if not os.environ.get(name):
                sys.exit('{{"event":"startup_failed","message":"' + name + ' must be set"}}')
        with open(os.path.join({str(seen_dir)!r}, "db"), "w") as fh:
            fh.write(hashlib.sha256(os.environ.get("DATABASE_URL", "").encode()).hexdigest())

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
    (root / "helpdesk" / ".env.example").write_text(
        "SESSION_SECRET=\nDEMO_PASSWORD=\nDATABASE_URL=postgres://localhost/helpdesk\n"
        "APP_BASE_URL=http://localhost:3000\nPORT=3000\nHOST=0.0.0.0\nGOOGLE_CLIENT_ID=\n"
        "SUPPORT_WEBHOOK_URL=\n")


# ---- 1. who must supply a variable --------------------------------------------------------------

@pytest.mark.parametrize("need,person", [
    (run_env.Need("SUPPORT_WEBHOOK_URL", "listed", False, ".env.example"), False),
    (run_env.Need("DEMO_PASSWORD", "demo", False, ".env.example"), False),
    (run_env.Need("DATABASE_URL", "secret", True, "boss.deploy.yaml", deploy_only=True), False),
    (run_env.Need("GOOGLE_CLIENT_ID", "secret", False, "template login"), False),
    (run_env.Need("PAY_KEY", "stub", True, "template payments"), False),
    (run_env.Need("SESSION_SECRET", "generated", True, "template login"), False),
    (run_env.Need("STRIPE_SECRET_KEY", "secret", True, "template payments"), True),
])
def test_only_a_required_secret_with_no_test_default_is_a_persons(need, person):
    assert need.person_must_supply() is person


def test_a_deploy_yaml_secret_and_a_required_spec_are_a_persons_and_env_example_never_is():
    class _Item:
        def __init__(self, key, source):
            self.key, self.source = key, source

    class _Manifest:
        env = [_Item("PAY_KEY", "secret")]

    # A deploy file's {secret} is the DEPLOY's: never a person's act for a throwaway run.
    pay = run_env.needs_from_deploy_manifest(_Manifest())["PAY_KEY"]
    assert pay.deploy_only and not pay.person_must_supply()
    assert run_env.decide(pay, has_value=False, purpose="proof") == "unset"
    assert run_env.decide(pay, has_value=False, purpose="deploy") == "needs"
    assert run_env.needs_from_specs([{"name": "K"}])["K"].person_must_supply()
    assert not run_env.needs_from_specs([{"name": "K", "required": False}])["K"].person_must_supply()
    listed = run_env.needs_from_env_example("DEMO_PASSWORD=\nOPENAI_API_KEY=\nSTRIPE_SECRET_KEY=\n")
    assert not any(n.person_must_supply() for n in listed.values())


# ---- 2. the incident: a crash on a listed variable is WORK, with a precise note ----------------

def test_a_crash_on_a_listed_variable_is_the_coders_with_a_precise_note(tmp_path):
    root, seen = tmp_path / "repo", tmp_path / "seen"
    seen.mkdir()
    _app(root, seen, required="SUPPORT_WEBHOOK_URL")
    record = ps.prove_app_serves(root, BUILT, timeout=20)
    assert record["ok"] is False
    assert not record.get("needs_secret"), record
    assert record["optional_unset"] == ["SUPPORT_WEBHOOK_URL"]
    assert record["error"].startswith(
        "the app must start without SUPPORT_WEBHOOK_URL: it is optional (listed in .env.example). "
        "Turn off what needs it when it is unset")
    assert "SUPPORT_WEBHOOK_URL must be set" in record["error"]     # the app's own words, kept
    # WORK: the coder gets its repair round; nothing blocks for a person.
    faults = gov.classify_gate_faults(failing=["app_serves"], evidence={"app_serves": record})
    assert faults == {"app_serves": "WORK"}
    action = gov.resolve_gate_failure_action(
        failing=["app_serves"], advisory_gate=False, rounds_used=0, max_repair_rounds=2,
        never_block=True, faults=faults, template_repair_max=2)
    assert action == "repair"
    # The repair prompt and the blocked line both carry it by name.
    assert "must start without SUPPORT_WEBHOOK_URL" in ps.app_serves_failure_sentence(record)
    assert ps.app_serves_headline(record).startswith(
        "the app must start without SUPPORT_WEBHOOK_URL: it is optional")
    error, reason = gov.blocked_gate_messages("qg_1", ["app_serves"], record)
    assert "Set SUPPORT_WEBHOOK_URL" not in error and "only a person can set" not in reason


def test_the_incident_app_that_requires_demo_password_starts_with_one_boss_generated(tmp_path):
    """crun_0d82ab62's app, as its brief wants it ("the seed refuses to run without
    DEMO_PASSWORD"): BOSS generates a demo login password for the proof, like a session key."""
    root, seen = tmp_path / "repo", tmp_path / "seen"
    seen.mkdir()
    _app(root, seen, required="DEMO_PASSWORD")
    record = ps.prove_app_serves(root, BUILT, timeout=20)
    assert record["ok"] is True and record["proven"] is True, record
    assert "DEMO_PASSWORD" in record["environment"]["generated"]
    assert record["environment"]["shown"] == ["DEMO_PASSWORD"]


@pytest.mark.parametrize("name,demo", [
    ("DEMO_PASSWORD", True), ("ADMIN_PASSWORD", True), ("SEED_ADMIN_PASSWORD", True),
    ("DEMO_AGENT_PASSWORD", True), ("TEST_USER_PASSWORD", True),
    ("POSTGRES_PASSWORD", False), ("DB_ADMIN_PASSWORD", False), ("SMTP_PASSWORD", False),
    ("PGPASSWORD", False), ("DEMO_MODE", False), ("GITHUB_CLIENT_SECRET", False),
])
def test_a_demo_login_password_is_told_by_its_name(name, demo):
    assert run_env.is_demo_login_name(name) is demo
    need = run_env.needs_from_env_example(f"{name}=\n")[name]
    assert (need.kind == "demo") is demo
    if demo:
        for purpose in run_env.THROWAWAY:
            assert run_env.decide(need, has_value=False, purpose=purpose) == "generate"
        # A deploy: the person's value, else BOSS's stored one (secrets.ensure_demo_logins).
        assert run_env.decide(need, has_value=False, purpose="deploy") == "unset"
        assert run_env.decide(need, has_value=True, purpose="preview") == "use"


def test_a_crash_is_blamed_on_the_line_the_app_died_on_not_a_mention():
    declared = run_env.needs_from_env_example("DATABASE_URL=\nSUPPORT_WEBHOOK_URL=\n")
    env = run_env.resolve(declared, purpose="proof")
    log = ("connecting with DATABASE_URL from the environment\n"
           "listening soon\n"
           '{"event":"startup_failed","message":"SUPPORT_WEBHOOK_URL must be set"}\n')
    assert env.unset_named_in(log) == "SUPPORT_WEBHOOK_URL"
    assert env.unset_named_in("DATABASE_URL is postgres://… (banner)\nready\n") is None
    assert env.unset_named_in("Error: DATABASE_URL is required\nError: SUPPORT_WEBHOOK_URL missing\n") \
        == "SUPPORT_WEBHOOK_URL"


def test_a_person_supplied_secret_names_every_env_and_the_api_accepts_each():
    from boss_engine.templates.secrets import TemplateError, deploy_value_spec

    action = run_env.needs_secret_action(["PAY_KEY"])
    assert action.startswith("Set PAY_KEY for environments build, preview and deploy in the app's Secrets")
    # An app-declared name may be set for every one of them; nothing declared stays deploy-only.
    assert deploy_value_spec("PAY_KEY", ["build", "preview", "deploy"], "test", app_declares=True)
    with pytest.raises(TemplateError):
        deploy_value_spec("PAY_KEY", ["build"], "test")


def test_a_crash_on_an_optional_template_secret_is_the_coders_too(tmp_path):
    root, seen = tmp_path / "repo", tmp_path / "seen"
    seen.mkdir()
    _app(root, seen, required="GOOGLE_CLIENT_ID")
    needs = run_env.needs_from_specs([{"name": "GOOGLE_CLIENT_ID", "required": False}],
                                     source="template login")
    record = ps.prove_app_serves(root, BUILT, timeout=20, needs=needs)
    assert record["optional_unset"] == ["GOOGLE_CLIENT_ID"] and not record.get("needs_secret")
    assert gov.classify_gate_faults(failing=["app_serves"], evidence={"app_serves": record}) == {
        "app_serves": "WORK"}


def test_the_app_that_starts_without_the_optional_setting_passes(tmp_path):
    root, seen = tmp_path / "repo", tmp_path / "seen"
    seen.mkdir()
    _app(root, seen, required="")
    record = ps.prove_app_serves(root, BUILT, timeout=20)
    assert record["ok"] is True and record["proven"] is True, record
    assert "SUPPORT_WEBHOOK_URL" in record["environment"]["unset"]
    assert "DEMO_PASSWORD" in record["environment"]["generated"]


def test_a_required_secret_only_a_person_has_is_still_their_act():
    env = run_env.resolve({"STRIPE_SECRET_KEY": run_env.Need("STRIPE_SECRET_KEY", "secret", False,
                                                             "template payments")},
                          purpose="proof")
    env.declared["STRIPE_SECRET_KEY"] = run_env.Need("STRIPE_SECRET_KEY", "secret", True,
                                                     "template payments")
    log = "Error: STRIPE_SECRET_KEY must be set"
    assert env.crash_needs_person(log) == "STRIPE_SECRET_KEY"
    assert env.explain_crash(log).startswith(
        "needs a secret: STRIPE_SECRET_KEY (declared by template payments)")


# ---- 3. what BOSS supplies wins over listed/unset ---------------------------------------------

def test_a_build_services_database_url_reaches_the_started_app(tmp_path):
    root, seen = tmp_path / "repo", tmp_path / "seen"
    seen.mkdir()
    _app(root, seen, required="DATABASE_URL")
    run_id = "crun_optional_db"
    lease = bs.PostgresLease(run_id=run_id, name=f"boss_run_{run_id}", host="build-services",
                             port=5433, password="pw-optional-db", ledger_handle="")
    bs._LEASES[run_id] = lease
    try:
        with owned_process.owner_scope("code_run", run_id):
            record = ps.prove_app_serves(root, BUILT, timeout=20)
    finally:
        bs._LEASES.pop(run_id, None)
    assert record["ok"] is True and record["proven"] is True, record
    got = (seen / "db").read_text()
    assert got == hashlib.sha256(lease.env()["DATABASE_URL"].encode()).hexdigest()
    environment = record["environment"]
    assert {"DATABASE_URL", "PORT", "HOST"} <= set(environment["supplied"])
    assert not {"DATABASE_URL", "PGHOST", "PORT", "HOST"} & set(environment["unset"])
    assert "SUPPORT_WEBHOOK_URL" in environment["unset"]


def test_without_build_services_a_listed_database_url_is_unset_and_the_crash_is_work(tmp_path):
    root, seen = tmp_path / "repo", tmp_path / "seen"
    seen.mkdir()
    _app(root, seen, required="DATABASE_URL")
    record = ps.prove_app_serves(root, BUILT, timeout=20)
    assert record["optional_unset"] == ["DATABASE_URL"] and not record.get("needs_secret")


def test_supplied_values_win_over_unset_and_needs():
    declared = {"DATABASE_URL": run_env.Need("DATABASE_URL", "secret", True, "template db"),
                "APP_BASE_URL": run_env.Need("APP_BASE_URL", "listed", False, ".env.example"),
                "PORT": run_env.Need("PORT", "listed", False, ".env.example")}
    env = run_env.resolve(declared, purpose="preview",
                          supplied={"DATABASE_URL": "postgresql://r:p@h:5433/r"})
    assert env.needs == [] and env.values["DATABASE_URL"].startswith("postgresql://")
    assert env.unset == ["APP_BASE_URL"] and "PORT" in env.supplied
    env.supply({"APP_BASE_URL": "https://preview.example"})
    assert env.unset == [] and env.values["APP_BASE_URL"] == "https://preview.example"


def test_a_previews_own_database_is_supplied_to_its_environment(tmp_path):
    (tmp_path / ".env.example").write_text("DATABASE_URL=\nDEMO_PASSWORD=\nSUPPORT_WEBHOOK_URL=\n")
    preview_id = "prev_optional_db"
    bs._LEASES[preview_id] = bs.PostgresLease(run_id=preview_id, name="boss_run_p", host="h",
                                              port=5433, password="pw-prev", ledger_handle="")
    try:
        env = ps.preview_app_environment(None, project_id="p", run_id=None, preview_id=preview_id,
                                         app_dir=tmp_path)
    finally:
        bs._LEASES.pop(preview_id, None)
    assert "DATABASE_URL" in env.supplied and "DATABASE_URL" not in env.unset
    assert env.unset == ["SUPPORT_WEBHOOK_URL"]
    # No app known (no DB): the demo login is fresh for this preview, and shown to its reviewer.
    assert env.shown == ["DEMO_PASSWORD"] and env.shown_values()["DEMO_PASSWORD"]


# ---- 4. the coder is told up front -------------------------------------------------------------

def test_the_coder_is_told_optional_settings_never_stop_the_app():
    assert run_env.OPTIONAL_SETTINGS_RULE in gov.FIRST_PASS_CHECKLIST
    code_agent._SANDBOX_FACTS.clear()
    assert run_env.OPTIONAL_SETTINGS_RULE in code_agent.sandbox_facts()
    assert "OPTIONAL_SETTINGS_RULE" in inspect.getsource(code_agent.sandbox_facts)
