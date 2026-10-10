"""A BUILD THAT DECLARES `services: [postgres]` GETS ITS OWN DATABASE — and only its own.

build_services.py: one role + one database per run on the build-services server, DATABASE_URL /
PG* injected into that run's processes only, dropped at run end, reaped when the run is lost,
never logged, and a plain "Build services unavailable: <why>" when it cannot be given.

The isolation cases run against a REAL Postgres server (the test server, standing in for the
build-services container): every database and role they make is prefixed `boss_test_bsvc_<rand>_`
and dropped afterwards. Without a reachable server they skip; the unit cases always run.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ENGINE = Path(__file__).resolve().parents[1]
ROOT = ENGINE.parent
sys.path.insert(0, str(ENGINE))

from boss_engine.services import build_services as bs  # noqa: E402
from boss_engine.services import owned_process  # noqa: E402


# ------------------------------------------------------------------------------------------ #
# fixtures
# ------------------------------------------------------------------------------------------ #

def _server_url() -> str:
    explicit = os.environ.get("BOSS_TEST_BUILD_SERVICES_PG_URL", "").strip()
    if explicit:
        return explicit
    raw = (os.environ.get("BOSS_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()
    if not raw.startswith("postgresql"):
        return ""
    parts = urlsplit(raw.replace("+psycopg2://", "://").replace("+psycopg://", "://"))
    return urlunsplit((parts.scheme, parts.netloc, "/postgres", "", ""))


@pytest.fixture()
def pg_server(monkeypatch):
    url = _server_url()
    if not url:
        pytest.skip("no Postgres server for the build-services cases")
    import psycopg

    try:
        psycopg.connect(url, connect_timeout=3).close()
    except Exception:  # noqa: BLE001
        pytest.skip("the build-services test server is not reachable")
    pre = f"boss_test_bsvc_{secrets.token_hex(3)}_"
    monkeypatch.setenv(bs.SERVER_URL_ENV, url)
    monkeypatch.setenv(bs.PREFIX_ENV, pre)
    monkeypatch.delenv(bs.RUN_HOST_ENV, raising=False)
    monkeypatch.delenv(bs.PASSWORD_FILE_ENV, raising=False)
    yield url, pre
    for run_id in [r for r, lease in list(bs._LEASES.items()) if lease.name.startswith(pre)]:
        bs.teardown(run_id)
    with contextlib.closing(psycopg.connect(url, autocommit=True)) as c:
        for (name,) in c.execute("SELECT datname FROM pg_database WHERE starts_with(datname, %s)",
                                 (pre,)).fetchall():
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        for (name,) in c.execute("SELECT rolname FROM pg_roles WHERE starts_with(rolname, %s)",
                                 (pre,)).fetchall():
            c.execute(f'DROP ROLE IF EXISTS "{name}"')


def _superuser(url):
    import psycopg

    return contextlib.closing(psycopg.connect(url, autocommit=True))


def _exists(url, name) -> tuple[bool, bool]:
    with _superuser(url) as c:
        db = c.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone()
        role = c.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (name,)).fetchone()
    return bool(db), bool(role)


def _connect_as(lease, dbname=None):
    import psycopg

    return psycopg.connect(host=lease.host, port=lease.port, user=lease.name,
                           password=lease.password, dbname=dbname or lease.name,
                           connect_timeout=5, autocommit=True)


# ------------------------------------------------------------------------------------------ #
# opt-in
# ------------------------------------------------------------------------------------------ #

def test_services_are_declared_in_boss_verify_yaml():
    from boss_engine.services.verify_manifest import VerifyManifestError, parse_verify_manifest

    base = ("version: 1\nsuites:\n  - name: app\n    paths: ['**']\n"
            "    run: ['python3 -m pytest -q']\n")
    assert parse_verify_manifest(base).services == ()
    assert parse_verify_manifest(base + "services: [postgres]\n").services == ("postgres",)
    assert parse_verify_manifest(base + "services: [Postgres, postgres]\n").services == ("postgres",)
    with pytest.raises(VerifyManifestError, match="unknown service 'mysql'"):
        parse_verify_manifest(base + "services: [mysql]\n")
    with pytest.raises(VerifyManifestError, match="must be a list"):
        parse_verify_manifest(base + "services: {postgres: true}\n")


def test_the_project_setting_opts_in_and_off_is_silence():
    from boss_engine.services.project_settings import KNOBS, coerce

    assert KNOBS[bs.SETTING_KEY]["choices"] == ("off", "postgres")
    assert coerce(bs.SETTING_KEY, "Postgres") == "postgres"
    with pytest.raises(ValueError):
        coerce(bs.SETTING_KEY, "mysql")
    assert bs.requested((), "postgres") == ("postgres",)
    assert bs.requested((), "off") == () and bs.requested((), None) == ()
    assert bs.requested(("postgres",), "off") == ("postgres",)      # the repo's word stands


def test_a_run_that_asked_for_nothing_provisions_nothing(monkeypatch):
    from boss_engine.services import governed_code_task_service as g

    called = []
    monkeypatch.setattr(bs, "provision", lambda *a, **k: called.append(a))
    monkeypatch.setattr(g, "_transaction", _fake_tx)
    pinned = type("P", (), {"manifest": None})()
    assert g._provision_build_services(None, object(), "crun_x", "p", pinned) is None
    assert called == []


# ------------------------------------------------------------------------------------------ #
# unavailable is said
# ------------------------------------------------------------------------------------------ #

@contextlib.contextmanager
def _fake_tx(_engine):
    class _C:
        def execute(self, *_a, **_k):
            raise RuntimeError("no db in this test")
    yield _C()


def _pinned_postgres():
    manifest = type("M", (), {"services": ("postgres",)})()
    return type("P", (), {"manifest": manifest})()


def test_no_server_configured_fails_the_run_in_plain_words(monkeypatch):
    from boss_engine.services import governed_code_task_service as g
    from boss_engine.services.code_task_service import classify_pipeline_exception

    monkeypatch.delenv(bs.SERVER_URL_ENV, raising=False)
    monkeypatch.setattr(g, "_transaction", _fake_tx)
    with pytest.raises(bs.BuildServicesUnavailable) as err:
        g._provision_build_services(None, object(), "crun_x", "p", _pinned_postgres())
    text = str(err.value)
    assert text.startswith("Build services unavailable:") and "services: [postgres]" in text
    assert classify_pipeline_exception(text + " [substrate=x]") == (
        "needs_human", "build_services_unavailable")


@pytest.mark.parametrize("where", ["microvm", "docker"])
def test_a_runtime_that_cannot_reach_it_is_refused_not_degraded(monkeypatch, where):
    from boss_engine.services import governed_code_task_service as g

    monkeypatch.setenv(bs.SERVER_URL_ENV, "postgresql://u:p@127.0.0.1:1/postgres")
    monkeypatch.setattr(g, "_transaction", _fake_tx)
    settings = type("S", (), {"sandbox_command_runtime": "docker" if where == "docker" else "subprocess"})()
    with pytest.raises(bs.BuildServicesUnavailable, match="Build services unavailable"):
        g._provision_build_services(None, settings, "crun_x", "p", _pinned_postgres(),
                                    use_microvm=where == "microvm")


def test_an_unreachable_server_says_so_without_its_password(monkeypatch):
    monkeypatch.setenv(bs.SERVER_URL_ENV, "postgresql://postgres:hunter2pw@127.0.0.1:1/postgres")
    with pytest.raises(bs.BuildServicesUnavailable) as err:
        bs.provision("crun_unreachable")
    assert "cannot reach the build-services Postgres at 127.0.0.1:1" in str(err.value)
    assert "hunter2pw" not in str(err.value)
    assert bs.lease_for("crun_unreachable") is None
    assert bs.status()["reachable"] is False and "hunter2pw" not in json.dumps(bs.status())


# ------------------------------------------------------------------------------------------ #
# the environment: this run's processes only
# ------------------------------------------------------------------------------------------ #

def _fake_lease(run_id: str) -> bs.PostgresLease:
    lease = bs.PostgresLease(run_id=run_id, name="boss_test_bsvc_fake", host="127.0.0.1",
                             port=5433, password=secrets.token_hex(24))
    bs._LEASES[run_id] = lease
    bs._remember_secret(lease.password)
    return lease


_PRINT_ENV = [sys.executable, "-c",
              "import os,json;print(json.dumps({k:os.environ.get(k) for k in "
              "('DATABASE_URL','PGPASSWORD','PGDATABASE')}))"]


def _child_env_seen(**scope) -> dict:
    proc = owned_process.run(_PRINT_ENV, capture_output=True, text=True, timeout=30,
                             env={"PATH": os.environ.get("PATH", "")})
    return json.loads(proc.stdout)


def test_the_runs_processes_get_its_database_and_no_one_elses():
    a, b = _fake_lease("crun_env_a"), _fake_lease("crun_env_b")
    try:
        with owned_process.owner_scope("code_run", "crun_env_a"):
            seen = _child_env_seen()
            assert seen["DATABASE_URL"] == a.env()["DATABASE_URL"]
            assert seen["PGPASSWORD"] == a.password != b.password
            # A review seat inside the build is the build's.
            with owned_process.owner_scope("review_seat", "seat_1"):
                assert _child_env_seen()["PGPASSWORD"] == a.password
        with owned_process.owner_scope("code_run", "crun_env_b"):
            assert _child_env_seen()["PGPASSWORD"] == b.password
        # Outside any run, and for BOSS's own non-workload spawns: nothing.
        assert _child_env_seen()["PGPASSWORD"] is None
        with owned_process.owner_scope("api", "crun_env_a"):
            assert _child_env_seen()["PGPASSWORD"] is None
    finally:
        bs._LEASES.pop("crun_env_a", None)
        bs._LEASES.pop("crun_env_b", None)


def test_the_server_superuser_never_reaches_an_agent():
    from boss_engine.services.agent_env import agent_environment

    env = agent_environment({"PATH": "/bin", bs.SERVER_URL_ENV: "postgresql://postgres:x@h/p",
                             bs.PASSWORD_FILE_ENV: "/run/x", "BOSS_AGENT_ENV_ALLOW":
                             f"{bs.SERVER_URL_ENV},{bs.PASSWORD_FILE_ENV}"})
    assert bs.SERVER_URL_ENV not in env and bs.PASSWORD_FILE_ENV not in env


def test_the_brief_names_the_variables_never_the_values():
    lease = bs.PostgresLease(run_id="r", name="n", host="h", port=1, password="p" * 48)
    text = bs.brief(lease)
    assert "DATABASE_URL" in text and "services: [postgres]" in text and "p" * 48 not in text
    assert bs.brief(None) == ""
    assert "p" * 48 not in repr(lease)


# ------------------------------------------------------------------------------------------ #
# credentials never written
# ------------------------------------------------------------------------------------------ #

def test_a_live_password_is_scrubbed_from_every_writer(monkeypatch):
    from boss_engine.services import core
    from boss_engine.services.redaction import redact, redact_secrets
    from boss_engine.services.sandbox import EphemeralSandbox

    lease = _fake_lease("crun_scrub")
    try:
        pw, url = lease.password, lease.env()["DATABASE_URL"]
        printed = f"connecting {url}\nPGPASSWORD={pw}\nbare {pw} end"
        # agent turns / activity (redact), activity scrub (redact_secrets)
        assert pw not in redact(printed)[0] and pw not in redact_secrets(printed)
        # any connection URL's password, even one BOSS did not hand out
        assert "zz9plural" not in redact_secrets("postgresql://u:zz9plural@db:5432/x")
        # command output (verify output, failure reasons) — EphemeralSandbox._truncate
        box = EphemeralSandbox.__new__(EphemeralSandbox)
        box.settings = type("S", (), {"sandbox_validation_output_limit_chars": 10_000})()
        assert pw not in box._truncate(printed) and pw not in box._truncate(printed.encode())
        # events
        captured = {}
        monkeypatch.setattr(core, "_emit_event_postgres",
                            lambda *a: captured.setdefault("data", a[-1]) and "evt_1")
        monkeypatch.setattr(core, "_after_event", lambda *a: None)
        monkeypatch.setattr("boss_engine.ports.get_engine_ports", lambda: None)
        core.emit_event(None, "p", "code_task.x", "boss", "code_task_run", "r",
                        {"out": printed, "nested": [{"u": url}]})
        assert pw not in json.dumps(captured["data"])
    finally:
        bs._LEASES.pop("crun_scrub", None)


# ------------------------------------------------------------------------------------------ #
# real Postgres: isolation, teardown, reaping
# ------------------------------------------------------------------------------------------ #

def test_run_a_cannot_reach_run_b(pg_server, caplog):
    import psycopg

    url, pre = pg_server
    caplog.set_level(logging.DEBUG)
    a = bs.provision("crun_iso_a")
    b = bs.provision("crun_iso_b")
    assert a.name.startswith(pre) and b.name.startswith(pre) and a.name != b.name
    assert bs.provision("crun_iso_a") is a                    # idempotent per run
    # A works in its own database.
    with contextlib.closing(_connect_as(a)) as c:
        c.execute("CREATE TABLE t (x int)")
        c.execute("INSERT INTO t VALUES (1)")
        # ...and cannot read anyone's password verifier, become B, or make databases.
        for stmt in ("SELECT rolpassword FROM pg_authid", f'SET ROLE "{b.name}"',
                     f'CREATE DATABASE "{pre}evil"', f'ALTER ROLE "{b.name}" PASSWORD \'x\''):
            with pytest.raises(psycopg.Error):
                c.execute(stmt)
    # A's credentials on B's database: refused.
    with pytest.raises(psycopg.OperationalError, match="permission denied|not permitted|no pg_hba"):
        _connect_as(a, dbname=b.name).close()
    # B's database with a wrong password: refused.
    wrong = bs.PostgresLease(run_id="x", name=b.name, host=b.host, port=b.port, password=a.password)
    with pytest.raises(psycopg.OperationalError):
        _connect_as(wrong).close()
    # The server holds a SCRAM verifier, never the plaintext.
    with _superuser(url) as c:
        stored = c.execute("SELECT rolpassword FROM pg_authid WHERE rolname=%s", (a.name,)).fetchone()[0]
        comment = c.execute("SELECT shobj_description(oid,'pg_database') FROM pg_database "
                            "WHERE datname=%s", (a.name,)).fetchone()[0]
    assert stored.startswith("SCRAM-SHA-256$") and a.password not in stored
    assert "crun_iso_a" not in comment and json.loads(comment)["run"] == bs.run_tag("crun_iso_a")
    # Nothing BOSS logged carries either password.
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert a.password not in logged and b.password not in logged


def test_a_run_process_sees_its_url_and_it_works(pg_server):
    a = bs.provision("crun_proc_a")
    b = bs.provision("crun_proc_b")
    probe = ("import os, psycopg\n"
             "c = psycopg.connect(os.environ['DATABASE_URL'], autocommit=True)\n"
             "c.execute('CREATE TABLE ok (x int)')\n"
             "other = os.environ['OTHER']\n"
             "try:\n"
             "    psycopg.connect(other.replace(other.rsplit('/',1)[1], os.environ['B_DB'])).close()\n"
             "    print('REACHED_B')\n"
             "except Exception as e:\n"
             "    print('REFUSED', type(e).__name__)\n")
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
           "OTHER": "", "B_DB": b.name}
    with owned_process.owner_scope("code_run", "crun_proc_a"):
        env["OTHER"] = a.env()["DATABASE_URL"]
        proc = owned_process.run([sys.executable, "-c", probe], capture_output=True, text=True,
                                 timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr[-500:]
    assert "REFUSED" in proc.stdout and "REACHED_B" not in proc.stdout


def test_run_end_drops_the_database_and_role_even_with_a_session_open(pg_server):
    url, _pre = pg_server
    lease = bs.provision("crun_end")
    session = _connect_as(lease)
    try:
        assert _exists(url, lease.name) == (True, True)
        assert bs.teardown("crun_end") is True
        assert _exists(url, lease.name) == (False, False)
        assert bs.lease_for("crun_end") is None and bs.teardown("crun_end") is False
    finally:
        with contextlib.suppress(Exception):
            session.close()


def test_the_governed_run_ends_its_database(monkeypatch):
    """run_governed_code_task's finally drops the run's database however the attempt ends."""
    from boss_engine.services import governed_code_task_service as g

    src = Path(g.__file__).read_text(encoding="utf-8")
    body = src.split("def run_governed_code_task(", 1)[1].split("\ndef ", 1)[0]
    assert "_services_teardown(run_id)" in body.split("finally:", 1)[1]


def test_a_lost_runs_database_is_reaped_and_nothing_else_is(pg_server):
    url, pre = pg_server
    lost = bs.provision("crun_lost")
    live = bs.provision("crun_live")
    young = bs.provision("crun_young")
    # The process that held them died: the registry is gone; the server still has them.
    for rid in ("crun_lost", "crun_live", "crun_young"):
        bs._LEASES.pop(rid, None)
    foreign = f"{pre}not_ours"
    other_install = f"{pre}other_inst"
    with _superuser(url) as c:
        c.execute(f'CREATE DATABASE "{foreign}"')                       # no BOSS comment
        c.execute(f'CREATE ROLE "{other_install}"')
        theirs = json.dumps({"boss": "build-services", "run": "x", "install": "inst_other",
                             "created": 1})
        c.execute(f"COMMENT ON ROLE \"{other_install}\" IS '{theirs}'")
        for name, rid in ((lost.name, "crun_lost"), (live.name, "crun_live")):
            meta = json.dumps({"boss": "build-services", "run": bs.run_tag(rid),
                               "install": "inst_me", "created": 1})
            c.execute(f"COMMENT ON DATABASE \"{name}\" IS '{meta}'")
            c.execute(f"COMMENT ON ROLE \"{name}\" IS '{meta}'")
    report = bs.reap({"crun_live"}, install="inst_me", min_age=60)
    assert set(report["dropped"]) == {lost.name}
    assert _exists(url, lost.name) == (False, False)
    assert _exists(url, live.name) == (True, True)          # its run is live
    assert _exists(url, young.name) == (True, True)         # inside the grace period
    assert _exists(url, foreign)[0] is True                 # not BOSS's: never touched
    assert _exists(url, other_install)[1] is True           # another install's
    # Unknown liveness keeps everything.
    assert bs.reap({"*"}, install="inst_me", min_age=0)["dropped"] == []


def test_the_reaper_pass_includes_build_databases(monkeypatch):
    from boss_engine.services import resource_reaper

    monkeypatch.setattr(bs, "reap_tick", lambda engine: {"dropped": ["x"]})
    monkeypatch.setattr(resource_reaper, "reap_processes", lambda engine=None: {})
    monkeypatch.setattr(resource_reaper, "reap_dirs", lambda **k: {})
    monkeypatch.setattr(resource_reaper, "docker_available", lambda binary="docker": False)
    assert resource_reaper.tick(None)["build_databases"] == {"dropped": ["x"]}


# ------------------------------------------------------------------------------------------ #
# the install: its own server, its own port, its own secret
# ------------------------------------------------------------------------------------------ #

def test_compose_runs_a_separate_server_the_fence_opens_alone():
    import yaml

    compose = yaml.safe_load((ROOT / "scripts/install/community-compose.yml").read_text())
    svc = compose["services"]["build-services"]
    cmd = " ".join(svc["command"])
    assert "-p 5433" in cmd and "host sameuser all all scram-sha-256" in cmd
    assert svc["environment"]["POSTGRES_PASSWORD_FILE"].startswith("/run/boss-build-services/")
    assert any(str(t).startswith("/var/lib/postgresql/data") for t in svc["tmpfs"])
    assert not svc.get("ports"), "the build-services server is never published"
    api = compose["services"]["api"]["environment"]
    assert "@build-services:5433/" in api["BOSS_BUILD_SERVICES_PG_URL"]
    assert ":" not in urlsplit(api["BOSS_BUILD_SERVICES_PG_URL"].split(":-", 1)[1]).netloc.split("@")[0]
    assert api["BOSS_BUILD_SERVICES_SANDBOX_PORT"] == "5433"
    entry = (ROOT / "scripts/sandbox/entrypoint.sh").read_text()
    assert 'PORTS="$PORTS,$BSVC_PORT"' in entry and "''|5432) ;;" in entry
    assert "chmod 0400 \"$BOSS_BUILD_SERVICES_PG_PASSWORD_FILE\"" in entry
