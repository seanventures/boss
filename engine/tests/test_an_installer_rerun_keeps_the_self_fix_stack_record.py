"""An installer rerun (boss update, the upgrade's fetched installer) keeps the local self-fix
stack's record, so the stack is re-applied after it and never dropped silently (BOSSAPP-571 R8).
The real install.sh, with the installer test's fake Docker (engine/tests/
test_community_installer_upgrade_backup.py)."""
from __future__ import annotations

from .test_community_installer_upgrade_backup import _install, install_pair  # noqa: F401


def test_the_stack_record_survives_a_rerun(install_pair):
    installed, old, _new, env, _tmp = install_pair
    head = "c" * 40
    (installed / ".boss-release").write_text(
        "BOSS_RELEASE_REF=main\nBOSS_STACK_REF=refs/boss/stack/main\n"
        f"BOSS_STACK_HEAD={head}\n")
    _install(installed, env)
    record = dict(line.split("=", 1) for line in
                  (installed / ".boss-release").read_text().splitlines() if "=" in line)
    assert record["BOSS_STACK_REF"] == "refs/boss/stack/main"
    assert record["BOSS_STACK_HEAD"] == head
    assert record["BOSS_RELEASE_CHANNEL"], "install.sh rewrote the record"


def test_a_plain_install_writes_no_stack_record(install_pair):
    installed, _old, _new, env, _tmp = install_pair
    _install(installed, env)
    text = (installed / ".boss-release").read_text()
    assert "BOSS_STACK_REF" not in text and "BOSS_STACK_HEAD" not in text
