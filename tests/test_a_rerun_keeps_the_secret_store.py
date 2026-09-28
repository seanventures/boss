"""Re-running the installer must not mint a new key for an old secret store.

install.sh rewrites .env on every run. The database password was preserved after a second
run locked the API out of Postgres. The secret store was not: the rewrite deletes
INFISICAL_ENCRYPTION_KEY and BOSS_INFISICAL_TOKEN, then the installer generates a fresh
encryption key because the line is gone. Infisical keeps its data in the Postgres volume,
encrypted with the key that was just deleted, and it issues the machine-identity token
once. The next start cannot read a stored credential, and bootstrap cannot fetch another
token.

The same rewrite blanks OPENAI_API_KEY. A remote update runs `install.sh -y --skip-model-key`
and does not pass the key again, so a working install comes back with no model credential.

restore_preserved_env.sh runs on the new file before it replaces the live one, and before
a missing encryption key would be generated.
"""

from __future__ import annotations

import os
import json
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RESTORE = REPO / "scripts" / "install" / "restore_preserved_env.sh"
GENERATE = REPO / "scripts" / "install" / "gen_infisical_keys.sh"
RECONCILE = REPO / "scripts" / "install" / "reconcile_api_credentials.py"
INSTALL = REPO / "install.sh"

OLD_KEY = "0123456789abcdef0123456789abcdef"
TOKEN = "eyJhbGciOiJIUzI1NiJ9.payload.sig+/=tail="


def _write(path: Path, text: str) -> None:
    path.write_text(text)
    os.chmod(path, 0o600)


def _restore(old: Path, new: Path, *, explicit_public_url: str = "0") -> None:
    env = os.environ.copy()
    env["BOSS_PUBLIC_URL_EXPLICIT"] = explicit_public_url
    subprocess.check_call(["sh", str(RESTORE), str(old), str(new)], env=env)


def _val(text: str, key: str) -> str:
    for line in text.splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1]
    raise AssertionError(f"{key} missing from:\n{text}")


def _reconcile(path: Path) -> None:
    subprocess.check_call(["python3", str(RECONCILE), str(path)])


def test_a_fresh_encryption_key_does_not_replace_the_one_the_volume_was_encrypted_with(tmp_path):
    """The failure mode: the rewrite already generated a new key. The old one still wins,
    and it wins once — a second line would leave compose reading whichever it sees first."""
    old = tmp_path / "old.env"
    new = tmp_path / "new.env"
    _write(old, f"INFISICAL_ENCRYPTION_KEY={OLD_KEY}\nINFISICAL_AUTH_SECRET=auth-secret-1\n"
           f"BOSS_INFISICAL_TOKEN={TOKEN}\nBOSS_INFISICAL_PROJECT_ID=proj_1\n"
           "BOSS_INFISICAL_URL=http://infisical:8080\n")
    _write(new, "BOSS_EDITION=community\n"
           "INFISICAL_ENCRYPTION_KEY=ffffffffffffffffffffffffffffffff\n"
           "INFISICAL_AUTH_SECRET=brand-new\n")
    _restore(old, new)
    text = new.read_text()
    assert text.count("INFISICAL_ENCRYPTION_KEY=") == 1
    assert _val(text, "INFISICAL_ENCRYPTION_KEY") == OLD_KEY
    assert _val(text, "INFISICAL_AUTH_SECRET") == "auth-secret-1"
    assert _val(text, "BOSS_INFISICAL_TOKEN") == TOKEN
    assert _val(text, "BOSS_INFISICAL_PROJECT_ID") == "proj_1"
    assert stat.S_IMODE(new.stat().st_mode) == 0o600


def test_generation_stops_once_the_previous_key_is_back(tmp_path):
    old = tmp_path / "old.env"
    new = tmp_path / "new.env"
    _write(old, f"INFISICAL_ENCRYPTION_KEY={OLD_KEY}\nINFISICAL_AUTH_SECRET=auth-secret-1\n")
    _write(new, "BOSS_EDITION=community\n")
    _restore(old, new)
    subprocess.check_call(["sh", str(GENERATE), str(new)], stderr=subprocess.DEVNULL)
    text = new.read_text()
    assert text.count("INFISICAL_ENCRYPTION_KEY=") == 1
    assert _val(text, "INFISICAL_ENCRYPTION_KEY") == OLD_KEY


def test_a_skipped_model_key_does_not_blank_the_one_already_stored(tmp_path):
    """`-y --skip-model-key` writes OPENAI_API_KEY= with nothing after the equals."""
    old = tmp_path / "old.env"
    new = tmp_path / "new.env"
    _write(old, "OPENAI_API_KEY=sk-existing\nOPENAI_BASE_URL=http://ollama:11434/v1\n"
           "ANTHROPIC_API_KEY=sk-ant-existing\nBOSS_GITHUB_TOKEN=ghp_existing\n"
           "CLAUDE_CODE_OAUTH_TOKEN=claude-token\nCODEX_AUTH_JSON={\"a\":1}\n")
    _write(new, "OPENAI_API_KEY=\nOPENAI_BASE_URL=\nANTHROPIC_API_KEY=\nBOSS_GITHUB_TOKEN=\n"
           "OPENAI_MODEL=gpt-4o-mini\n")
    _restore(old, new)
    text = new.read_text()
    assert _val(text, "OPENAI_API_KEY") == "sk-existing"
    assert _val(text, "OPENAI_BASE_URL") == "http://ollama:11434/v1"
    assert _val(text, "ANTHROPIC_API_KEY") == "sk-ant-existing"
    assert _val(text, "BOSS_GITHUB_TOKEN") == "ghp_existing"
    assert _val(text, "CLAUDE_CODE_OAUTH_TOKEN") == "claude-token"
    assert _val(text, "CODEX_AUTH_JSON") == '{"a":1}'


def test_a_token_is_not_interpreted_by_the_shell(tmp_path):
    """The value is data. A backslash or a $(...) in it must be stored, not executed."""
    old = tmp_path / "old.env"
    new = tmp_path / "new.env"
    token = r'ab\c$def`whoami`$(id)"quote'
    _write(old, f"BOSS_INFISICAL_TOKEN={token}\n")
    _write(new, "BOSS_EDITION=community\n")
    _restore(old, new)
    assert _val(new.read_text(), "BOSS_INFISICAL_TOKEN") == token


def test_an_explicit_new_model_key_is_kept(tmp_path):
    old = tmp_path / "old.env"
    new = tmp_path / "new.env"
    _write(old, "OPENAI_API_KEY=sk-old\n")
    _write(new, "OPENAI_API_KEY=sk-new\n")
    _restore(old, new)
    assert _val(new.read_text(), "OPENAI_API_KEY") == "sk-new"


def test_a_second_api_key_and_a_chosen_model_survive(tmp_path):
    old = tmp_path / "old.env"
    new = tmp_path / "new.env"
    _write(old, "BOSS_API_KEYS=key-a,key-b\nBOSS_API_KEY_PRINCIPALS=key-a:ceo,key-b:worker\n"
           "OPENAI_MODEL=anthropic/claude-sonnet-4.5\n")
    _write(new, "BOSS_API_KEYS=key-a\nBOSS_API_KEY_PRINCIPALS=key-a:ceo\nOPENAI_MODEL=gpt-4o-mini\n")
    _restore(old, new)
    text = new.read_text()
    assert _val(text, "BOSS_API_KEYS") == "key-a,key-b"
    assert _val(text, "BOSS_API_KEY_PRINCIPALS") == "key-a:ceo,key-b:worker"
    assert _val(text, "OPENAI_MODEL") == "anthropic/claude-sonnet-4.5"


def _resolved_compose_env(env_file: Path) -> dict:
    if not shutil.which("docker"):
        pytest.skip("Docker Compose CLI is unavailable")
    environment = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
    result = subprocess.run(
        ["docker", "compose", "--profile", "web", "--env-file", str(env_file),
         "-f", str(REPO / "scripts/install/community-compose.yml"),
         "config", "--format", "json"],
        text=True, capture_output=True, env=environment, check=False,
    )
    if "docker: 'compose' is not a docker command" in result.stderr:
        pytest.skip("Docker Compose CLI is unavailable")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["services"]


def test_compose_default_worker_identity_matches_the_installer_key(tmp_path):
    env_file = tmp_path / "fresh.env"
    _write(env_file, "BOSS_API_KEY=fresh-key\nBOSS_WORKER_API_KEY=worker-key\n"
           "BOSS_DB_PASSWORD=test-db\n"
           "BOSS_CONSOLE_PASSWORD=test-console\n"
           "BOSS_CONSOLE_SESSION_SECRET=test-session\n"
           "INFISICAL_ENCRYPTION_KEY=test-encryption\n"
           "INFISICAL_AUTH_SECRET=test-auth\n")
    services = _resolved_compose_env(env_file)
    api = services["api"]["environment"]
    worker = services["worker"]["environment"]
    assert api["BOSS_API_KEYS"] == "fresh-key,worker-key"
    assert api["BOSS_API_KEY_PRINCIPALS"] == "fresh-key:ceo,worker-key:system"
    assert api["BOSS_WORKER_PRINCIPAL"] == "system"
    assert worker["BOSS_WORKER_API_KEY"] == "worker-key"
    assert "BOSS_API_KEY" not in worker
    assert services["web"]["environment"]["BOSS_API_KEY"] == "fresh-key"


def test_restored_multi_key_map_reaches_api_after_rerun(tmp_path):
    old = tmp_path / "old.env"
    new = tmp_path / "new.env"
    _write(old, "BOSS_API_KEY=key-a\nBOSS_API_KEYS=key-a,key-b\n"
           "BOSS_API_KEY_PRINCIPALS=key-a:ceo,key-b:auditor\n")
    _write(new, "BOSS_API_KEY=key-a\nBOSS_WORKER_API_KEY=key-c\nBOSS_API_KEYS=key-a,key-c\n"
           "BOSS_API_KEY_PRINCIPALS=key-a:ceo\nBOSS_DB_PASSWORD=test-db\n"
           "BOSS_CONSOLE_PASSWORD=test-console\n"
           "BOSS_CONSOLE_SESSION_SECRET=test-session\n"
           "INFISICAL_ENCRYPTION_KEY=test-encryption\n"
           "INFISICAL_AUTH_SECRET=test-auth\n")
    _restore(old, new)
    _reconcile(new)
    api = _resolved_compose_env(new)["api"]["environment"]
    assert set(api["BOSS_API_KEYS"].split(",")) == {"key-a", "key-b", "key-c"}
    assert set(api["BOSS_API_KEY_PRINCIPALS"].split(",")) == {
        "key-a:ceo", "key-b:auditor", "key-c:system",
    }
    assert api["BOSS_WORKER_PRINCIPAL"] == "system"


def test_worker_key_survives_rerun_and_old_multi_key_map_is_merged(tmp_path):
    old = tmp_path / "old.env"
    new = tmp_path / "new.env"
    _write(old, "BOSS_API_KEY=owner\nBOSS_WORKER_API_KEY=existing-worker\n"
           "BOSS_API_KEYS=owner,existing-worker,auditor\n"
           "BOSS_API_KEY_PRINCIPALS=owner:ceo,existing-worker:system,auditor:auditor\n")
    _write(new, "BOSS_API_KEY=owner\nBOSS_WORKER_API_KEY=new-worker\n"
           "BOSS_API_KEYS=owner,new-worker\n"
           "BOSS_API_KEY_PRINCIPALS=owner:ceo,new-worker:system\n")
    _restore(old, new)
    _reconcile(new)
    text = new.read_text()
    assert _val(text, "BOSS_WORKER_API_KEY") == "existing-worker"
    assert _val(text, "BOSS_API_KEYS") == "owner,existing-worker,auditor"
    assert set(_val(text, "BOSS_API_KEY_PRINCIPALS").split(",")) == {
        "owner:ceo", "existing-worker:system", "auditor:auditor",
    }


def test_reconciliation_rejects_shared_key_without_replacing_env(tmp_path):
    env_file = tmp_path / ".env"
    _write(env_file, "BOSS_API_KEY=same\nBOSS_WORKER_API_KEY=same\n"
           "BOSS_API_KEYS=same\nBOSS_API_KEY_PRINCIPALS=same:ceo\n")
    before = env_file.read_bytes()
    result = subprocess.run(
        ["python3", str(RECONCILE), str(env_file)],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert "same" not in result.stderr
    assert env_file.read_bytes() == before


def test_archive_update_migrates_legacy_single_key_and_is_idempotent(tmp_path):
    env_file = tmp_path / ".env"
    _write(env_file, "BOSS_API_KEY=owner\nBOSS_API_KEYS=owner,auditor\n"
           "BOSS_API_KEY_PRINCIPALS=owner:ceo,auditor:auditor\n"
           "BOSS_DB_PASSWORD=existing-db\n")
    for _ in range(2):
        subprocess.check_call([
            "python3", str(RECONCILE), "--ensure-worker", str(env_file),
        ])
        text = env_file.read_text()
        worker = _val(text, "BOSS_WORKER_API_KEY")
        assert worker and worker != "owner"
        assert _val(text, "BOSS_API_KEYS") == f"owner,auditor,{worker}"
        assert set(_val(text, "BOSS_API_KEY_PRINCIPALS").split(",")) == {
            "owner:ceo", "auditor:auditor", f"{worker}:system",
        }
        assert _val(text, "BOSS_DB_PASSWORD") == "existing-db"
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
        if _ == 0:
            first_worker = worker
        else:
            assert worker == first_worker


def test_the_console_url_stays_unless_this_run_set_one(tmp_path):
    old = tmp_path / "old.env"
    new = tmp_path / "new.env"
    _write(old, "NEXT_PUBLIC_BOSS_API_URL=http://203.0.113.5:8000\n")
    _write(new, "NEXT_PUBLIC_BOSS_API_URL=http://127.0.0.1:8000\n")
    _restore(old, new, explicit_public_url="0")
    assert _val(new.read_text(), "NEXT_PUBLIC_BOSS_API_URL") == "http://203.0.113.5:8000"

    _write(new, "NEXT_PUBLIC_BOSS_API_URL=http://198.51.100.8:8000\n")
    _restore(old, new, explicit_public_url="1")
    assert _val(new.read_text(), "NEXT_PUBLIC_BOSS_API_URL") == "http://198.51.100.8:8000"


def test_custom_ports_survive_and_legacy_public_bind_becomes_loopback(tmp_path):
    old = tmp_path / "old.env"
    new = tmp_path / "new.env"
    _write(old, "BOSS_API_PORT=8111\nBOSS_WEB_PORT=3111\n"
           "BOSS_POSTGRES_PORT=5544\nBOSS_BIND_ADDRESS=0.0.0.0\n")
    _write(new, "BOSS_API_PORT=8000\nBOSS_WEB_PORT=3000\n"
           "BOSS_POSTGRES_PORT=5432\nBOSS_BIND_ADDRESS=127.0.0.1\n")
    _restore(old, new)
    text = new.read_text()
    assert _val(text, "BOSS_API_PORT") == "8111"
    assert _val(text, "BOSS_WEB_PORT") == "3111"
    assert _val(text, "BOSS_POSTGRES_PORT") == "5544"
    assert _val(text, "BOSS_BIND_ADDRESS") == "127.0.0.1"


def test_operator_added_settings_survive_without_evaluating_their_values(tmp_path):
    old = tmp_path / "old.env"
    new = tmp_path / "new.env"
    marker = tmp_path / "must-not-exist"
    raw = f"literal$(touch {marker})`whoami`"
    _write(old, "BOSS_CODE_AGENT_ENGINE=codex\nBOSS_CHAT_LLM_ENGINE=claude_code\n"
           "BOSS_ACCOUNT_CONNECT_URL=https://account.example.invalid\n"
           f"CUSTOM_FLAG={raw}\nINVALID-KEY=ignored\n")
    _write(new, "BOSS_EDITION=community\nBOSS_ACCOUNT_CONNECT_URL=\n")
    _restore(old, new)
    text = new.read_text()
    assert _val(text, "BOSS_CODE_AGENT_ENGINE") == "codex"
    assert _val(text, "BOSS_CHAT_LLM_ENGINE") == "claude_code"
    assert _val(text, "BOSS_ACCOUNT_CONNECT_URL") == "https://account.example.invalid"
    assert _val(text, "CUSTOM_FLAG") == raw
    assert "INVALID-KEY" not in text
    assert not marker.exists()


def test_console_login_secrets_survive_a_rerun(tmp_path):
    old = tmp_path / "old.env"
    new = tmp_path / "new.env"
    _write(old, "BOSS_CONSOLE_PASSWORD=" + "a" * 48 + "\n"
           "BOSS_CONSOLE_SESSION_SECRET=" + "b" * 64 + "\n")
    _write(new, "BOSS_CONSOLE_PASSWORD=" + "c" * 48 + "\n"
           "BOSS_CONSOLE_SESSION_SECRET=" + "d" * 64 + "\n")
    _restore(old, new)
    assert _val(new.read_text(), "BOSS_CONSOLE_PASSWORD") == "a" * 48
    assert _val(new.read_text(), "BOSS_CONSOLE_SESSION_SECRET") == "b" * 64


def test_the_live_env_is_replaced_only_after_the_old_key_is_back():
    """The generator runs on the installed .env. The previous key has to already be in that
    file, and the live file must not be truncated before the restore succeeds."""
    text = INSTALL.read_text()
    restore_at = text.index('restore_preserved_env.sh" "$_ENV_SNAPSHOT"')
    replace_at = text.index('mv "$_new_env" "$ENV_FILE"')
    generate_at = text.index("if ! grep -qs '^INFISICAL_ENCRYPTION_KEY='")
    assert restore_at < replace_at < generate_at
    assert 'cat > "$ENV_FILE"' not in text
    # A non-interactive update does not pass --secret-store. The token check that keeps the
    # secrets profile has to run outside the interactive prompt.
    detect_at = text.index("grep -qs '^BOSS_INFISICAL_TOKEN=.'")
    prompt_at = text.index('prompt "Secret store:')
    assert detect_at < prompt_at
    assert "SECRET_STORE_EXPLICIT=1" in text
