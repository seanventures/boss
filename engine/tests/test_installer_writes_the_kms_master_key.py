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
