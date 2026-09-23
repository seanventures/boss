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
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RESTORE = REPO / "scripts" / "install" / "restore_preserved_env.sh"
GENERATE = REPO / "scripts" / "install" / "gen_infisical_keys.sh"
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
