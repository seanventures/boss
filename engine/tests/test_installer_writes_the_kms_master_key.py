"""BOSSAPP-897: no installer ever wrote BOSS_KMS_MASTER_KEY, so every database-store install kept its
secret-store key in Postgres and refused to store a Claude sign-in ("needs the database store's key
to live outside the database ... Re-run the installer — it writes the key"). The installer now
generates it on a first install, keeps it on every rerun (updates rerun install.sh), and the
preserved-env list keeps it across a rollback."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALL = (ROOT / "install.sh").read_text()


def test_a_first_install_generates_a_32_byte_urlsafe_key():
    assert 'BOSS_KMS_MASTER_KEY="$(openssl rand -base64 32 | tr \'+/\' \'-_\'' in INSTALL


def test_the_env_file_carries_it():
    assert "\nBOSS_KMS_MASTER_KEY=${BOSS_KMS_MASTER_KEY}\n" in INSTALL


def test_a_rerun_never_replaces_it():
    keep = INSTALL.index('_keep_kms="$(_existing BOSS_KMS_MASTER_KEY')
    write = INSTALL.index("\nBOSS_KMS_MASTER_KEY=${BOSS_KMS_MASTER_KEY}\n")
    assert keep < write
    assert 'if [ -n "$_keep_kms" ]; then BOSS_KMS_MASTER_KEY="$_keep_kms"; fi' in INSTALL
    assert "BOSS_KMS_MASTER_KEY" in (ROOT / "scripts/install/restore_preserved_env.sh").read_text()


def _reconcile(tmp_path, body: str, ensure_worker: bool) -> dict[str, str]:
    import importlib.util
    spec = importlib.util.spec_from_file_location("reconcile", ROOT / "scripts/install/reconcile_api_credentials.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    env = tmp_path / ".env"
    env.write_text(body)
    mod.reconcile(env, ensure_worker=ensure_worker)
    return dict(line.split("=", 1) for line in env.read_text().splitlines() if "=" in line)


def test_an_archive_update_writes_a_missing_key(tmp_path):
    import base64
    out = _reconcile(tmp_path, "BOSS_API_KEY=a1\nBOSS_WORKER_API_KEY=b2\n", ensure_worker=True)
    assert len(base64.urlsafe_b64decode(out["BOSS_KMS_MASTER_KEY"])) == 32


def test_an_existing_key_is_never_replaced(tmp_path):
    for ensure in (True, False):
        out = _reconcile(tmp_path, "BOSS_API_KEY=a1\nBOSS_WORKER_API_KEY=b2\nBOSS_KMS_MASTER_KEY=keep-me\n", ensure)
        assert out["BOSS_KMS_MASTER_KEY"] == "keep-me"
