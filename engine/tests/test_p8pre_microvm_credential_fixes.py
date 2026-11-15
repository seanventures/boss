"""P8-pre (BOSSAPP-1061): today's credential defects in the micro-VM lane, and the two local-lane
hardenings that came with them.

  * P8pre-T2 — inside the guest, after the clone, `git remote -v` shows no credential; a failed
    task leaves no spec.json and a credential-free .git/config (the Go side — owner-only task
    files, the retention scrub and the fs refusals — is pinned in services/sandbox-service);
  * P8pre-T3 — a claude_code run on a stored Anthropic key writes a secret_access_events row
    naming the run (the CLI credential path used to read the store directly); an out-of-scope
    read is refused, and the refusal is logged;
  * P8pre-T5 — a claude_code micro-VM submit carries no OpenAI or Azure key, and an unknown
    engine_env name is refused by name — by the same table Go uses.

(P8pre-T4, the MCP gateway on by default, lives with the gateway in test_mcp_gateway_814.py.)

The DB cases need BOSS_TEST_DATABASE_URL naming a disposable database.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import sandbox_contract as sc

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_GO = ROOT / "services" / "sandbox-service" / "internal" / "contract" / "contract.go"
GUEST = ROOT / "sandbox-host" / "guest" / "in_guest_agent.py"
URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not URL or "test" not in URL.rsplit("/", 1)[-1].lower(),
                              reason="needs BOSS_TEST_DATABASE_URL naming a disposable database")

TOKEN = "ghs_" + "T" * 36                        # shaped like an installation token; not one
KEY = "sk-ant-api03-" + "p" * 48                 # shaped like an API key; not one


# ── P8pre-T5: only the selected engine's credential, and unknown names refused ─────────────

def _go_table(var: str) -> str:
    src = CONTRACT_GO.read_text(encoding="utf-8")
    start = src.index(f"var {var} = map[string]")
    return src[start:src.index("\n}\n", start)]


def test_python_and_go_agree_on_every_engine_env_name():
    go_names = set(re.findall(r'"([A-Z_]+)":\s*"[a-z_]+"', _go_table("engineEnvToSpec")))
    assert go_names == set(sc.GUEST_ENGINE_ENV_NAMES)
    go_by_engine = {
        engine: set(re.findall(r'"([A-Z_]+)"', names))
        for engine, names in re.findall(r'"([a-z_]+)":\s*\{([^}]*)\}', _go_table("engineEnvByEngine"))
    }
    py_by_engine = {e: {n for n in names if n in sc.GUEST_ENGINE_ENV_NAMES}
                    for e, names in sc.ENGINE_ENV_BY_ENGINE.items()}
    assert go_by_engine == py_by_engine


def test_a_claude_code_submit_carries_no_openai_or_azure_key():
    spec = {"task_id": "crun_t5", "owner": "o", "repo": "r", "engine": "claude_code",
            "claude_oauth_token": "oat-not-real", "openai_api_key": "sk-not-real",
            "openai_base_url": "https://api.example/v1", "azure_key": "az-not-real",
            "azure_endpoint": "https://az.example", "model": "gpt"}
    body = sc.build_submit_request(spec, "p")
    assert body["engine_env"] == {"CLAUDE_CODE_OAUTH_TOKEN": "oat-not-real"}
    assert sc.validate_submit_request(body) == []
    # The openai lane keeps its own names and nothing of claude's.
    body = sc.build_submit_request({**spec, "engine": "openai"}, "p")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in body["engine_env"]
    assert body["engine_env"]["OPENAI_API_KEY"] == "sk-not-real"
    assert body["engine_env"]["AZURE_OPENAI_KEY"] == "az-not-real"


def test_code_task_service_builds_no_foreign_model_fields_for_claude_code():
    from boss_engine.services.code_task_service import _guest_model_fields

    settings = Settings().model_copy(update={"openai_api_key": "sk-not-real",
                                             "azure_openai_key": "az-not-real"})
    assert _guest_model_fields(settings, "claude_code") == {}
    assert _guest_model_fields(settings, "echo") == {}
    lane = _guest_model_fields(settings, "openai")
    assert lane["openai_api_key"] == "sk-not-real" and lane["azure_key"] == "az-not-real"


def test_an_unknown_engine_env_name_is_refused_by_name_never_by_value():
    body = sc.build_submit_request({"task_id": "crun_t5", "owner": "o", "repo": "r",
                                    "engine": "openai"}, "p")
    body["engine_env"]["SOMETHING_ELSE"] = "value-not-printed"
    body["engine_env"]["CLAUDE_CODE_OAUTH_TOKEN"] = "value-not-printed"
    problems = sc.validate_submit_request(body)
    assert problems == [
        "engine_env_not_for_engine: engine_env CLAUDE_CODE_OAUTH_TOKEN is not read by engine openai",
        "engine_env_unknown: engine_env SOMETHING_ELSE is not a name the guest reads",
    ]
    with pytest.raises(sc.EngineEnvRefused) as refused:
        sc.ensure_engine_env_valid(body)
    assert refused.value.reason == sc.ENGINE_ENV_NOT_FOR_ENGINE
    assert "value-not-printed" not in str(refused.value)


def test_the_micro_vm_provider_refuses_before_it_submits(monkeypatch):
    from boss_engine.services.sandbox_provider import MicroVMProvider

    provider = MicroVMProvider.__new__(MicroVMProvider)
    with pytest.raises(sc.EngineEnvRefused) as refused:
        provider._submit_body({"task_id": "crun_t5", "owner": "o", "repo": "r",
                               "engine": "claude_code", "anthropic_api_key": KEY}, "p")
    assert refused.value.reason == sc.ENGINE_ENV_UNKNOWN and KEY not in str(refused.value)


def test_the_lane_stays_frozen_by_default():
    assert Settings().microvm_claims_frozen is True


# ── P8pre-T2: the guest keeps no token ────────────────────────────────────────────────────

def _guest(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(f"in_guest_agent_{uuid4().hex}", GUEST)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "SPEC", tmp_path / "work" / "spec.json")
    monkeypatch.setattr(mod, "RESULT", tmp_path / "work" / "result.json")
    monkeypatch.setattr(mod, "WORK", tmp_path / "work" / "repo")
    return mod


def _upstream(tmp_path, monkeypatch) -> None:
    """A local bare repo standing in for github.com: git rewrites the tokened URL to it, and
    records the URL exactly as given — as a real clone of the tokened URL would."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main"]
    subprocess.run([*git, "init", "-q", str(seed)], check=True)
    (seed / "README.md").write_text("hi\n")
    subprocess.run([*git, "-C", str(seed), "add", "-A"], check=True)
    subprocess.run([*git, "-C", str(seed), "commit", "-q", "-m", "seed"], check=True)
    bare = tmp_path / "upstream.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.file://{bare}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"https://x-access-token:{TOKEN}@github.com/o/r.git")


def test_inside_the_guest_after_the_clone_the_remote_holds_no_credential(tmp_path, monkeypatch):
    _upstream(tmp_path, monkeypatch)
    guest = _guest(monkeypatch, tmp_path)
    guest._prepare_repo({"owner": "o", "repo": "r", "base_branch": "main", "token": TOKEN})
    remotes = subprocess.run(["git", "-C", str(guest.WORK), "remote", "-v"], check=True,
                             capture_output=True, text=True).stdout
    assert "https://github.com/o/r.git" in remotes
    assert TOKEN not in remotes and "x-access-token" not in remotes
    assert TOKEN not in (guest.WORK / ".git" / "config").read_text()


def test_a_failed_guest_task_leaves_no_spec_and_no_token(tmp_path, monkeypatch):
    _upstream(tmp_path, monkeypatch)
    guest = _guest(monkeypatch, tmp_path)
    guest.SPEC.parent.mkdir(parents=True)
    guest.SPEC.write_text(json.dumps({"owner": "o", "repo": "r", "base_branch": "main",
                                      "token": TOKEN, "engine": "no-such-engine"}))
    guest.main()
    result = json.loads(guest.RESULT.read_text())
    assert result["status"] == "error"                 # the task failed after the clone
    assert not guest.SPEC.exists(), "the spec (and its token) must not be retained"
    leftovers = [p for p in tmp_path.joinpath("work").rglob("*")
                 if p.is_file() and TOKEN in p.read_text(errors="ignore")]
    assert leftovers == []


def test_the_scrub_removes_any_userinfo_from_a_git_config(tmp_path, monkeypatch):
    guest = _guest(monkeypatch, tmp_path)
    git_dir = guest.WORK / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "config").write_text(
        f'[remote "origin"]\n\turl = https://x-token-auth:{TOKEN}@bitbucket.org/o/r.git\n')
    guest._scrub_git_config()
    assert (git_dir / "config").read_text() == \
        '[remote "origin"]\n\turl = https://bitbucket.org/o/r.git\n'


# ── P8pre-T3: the CLI credential path is audited ──────────────────────────────────────────

class _Mem:
    store: dict = {}

    def get(self, key):
        from boss_engine.services.secret_backends import SecretNotFound

        if key not in self.store:
            raise SecretNotFound(f"{key} is not stored")
        return self.store[key]

    def put(self, key, value):
        self.store[key] = value

    def health(self):
        return {"ok": True}


@pytest.fixture(scope="module")
def engine():
    from boss_engine.db import ensure_schema

    eng = create_engine(URL, future=True)
    ensure_schema(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def project(engine, monkeypatch):
    from boss_engine.services import (
        run_secret_redaction,
        secret_access_log,
        secret_backends,
        secrets_service,
    )

    _Mem.store = {}
    monkeypatch.setattr(secret_backends, "backend_for", lambda kind, settings: _Mem())
    monkeypatch.setattr(secrets_service, "backend_for", lambda kind, settings: _Mem())
    monkeypatch.setattr(secret_backends, "pick_default_backend", lambda settings: ("database", []))
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    secret_access_log.reset_for_tests()
    run_secret_redaction.reset_for_tests()
    pid = f"p_p8pre_{uuid4().hex[:8]}"
    with engine.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_p8pre', 'o') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_p8pre', 'org_p8pre', "
                       "'w') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "(:p, 'ws_p8pre', :p, :p)"), {"p": pid})
    yield pid
    run_secret_redaction.reset_for_tests()


def _settings() -> Settings:
    return Settings().model_copy(update={"claude_code_oauth_token": "", "anthropic_api_key": None})


def _store_install_key(engine, pid) -> str:
    """The Anthropic key stored install-wide, as the CLI path reads it (by name)."""
    from boss_engine.services.cli_harness import SUBSCRIPTION_SECRET_SCOPE
    from boss_engine.services.secrets_service import put_secret

    with engine.begin() as c:
        return put_secret(c, None, project_id=pid, name="ANTHROPIC_API_KEY", value=KEY,
                          backend="database", scope_id=SUBSCRIPTION_SECRET_SCOPE,
                          actor="usr_writer")["secret_ref_id"]


def _events(engine, ref):
    with engine.begin() as c:
        return c.execute(text("SELECT * FROM secret_access_events WHERE secret_ref_id = :r "
                              "ORDER BY accessed_at, id"), {"r": ref}).mappings().all()


@needs_db
def test_a_claude_run_on_the_stored_key_writes_an_access_row_naming_the_run(engine, project):
    from boss_engine.services import claude_credential as cc
    from boss_engine.services.harness_failures import run_scope

    ref = _store_install_key(engine, project)
    run = f"crun_p8pre_{uuid4().hex[:6]}"
    with run_scope(run), engine.begin() as c:
        cred = cc.resolve_anthropic_api_key(_settings(), connection=c, project_id=project)
    assert (cred.kind, cred.value) == (cc.KIND_STORED_KEY, KEY)
    (row,) = _events(engine, ref)
    assert row["outcome"] == "resolved" and row["run_id"] == run
    assert row["principal"] == "system:boss"
    assert KEY not in str(dict(row))


@needs_db
def test_a_read_with_no_connection_still_writes_its_row(engine, project, monkeypatch):
    """The chat adapter and other callers hold no connection: the read opens its own."""
    from boss_engine.services import cli_harness
    from boss_engine.services.harness_failures import run_scope

    name = f"P8PRE_TOKEN_{uuid4().hex[:8].upper()}"
    _Mem.store[name] = "tok-not-real-" + "x" * 20     # by name only: no reference row
    run = f"crun_p8pre_{uuid4().hex[:6]}"
    settings = Settings().model_copy(update={"database_url": URL})
    with run_scope(run):
        assert cli_harness.stored_credential(settings, name, include_env=False).startswith("tok-")
    (row,) = _events(engine, f"store:{name}")
    assert row["outcome"] == "resolved" and row["run_id"] == run
    assert row["principal"] == "system:boss"


@needs_db
def test_an_out_of_scope_read_is_refused_and_logged(engine, project):
    from boss_engine.services import claude_credential as cc
    from boss_engine.services.cli_harness import CredentialStoreUnavailable
    from boss_engine.services.harness_failures import run_scope

    ref = _store_install_key(engine, project)
    with engine.begin() as c:                       # the key now belongs to one agent only
        c.execute(text("UPDATE secret_refs SET scope_type = 'agent', scope_id = 'deployer' "
                       "WHERE id = :r"), {"r": ref})
    run = f"crun_p8pre_{uuid4().hex[:6]}"
    with run_scope(run), engine.begin() as c, pytest.raises(CredentialStoreUnavailable):
        cc.resolve_anthropic_api_key(_settings(), connection=c, project_id=project)
    rows = _events(engine, ref)
    assert [r["outcome"] for r in rows] == ["refused"]
    assert rows[0]["run_id"] == run and "another agent" in rows[0]["reason"]
