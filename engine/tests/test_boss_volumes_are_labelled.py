"""BOSSAPP-1193: the volumes BOSS's Compose creates are labelled so the Infra Agent's host helper
(scripts/install/steward.py) can tell a rebuildable one from data.

The helper removes an orphan volume only when it carries `boss.install_id` = this install AND a
rebuildable `boss.kind`. Before this, nothing in production wrote those labels, so orphan-volume
cleanup could never remove anything on a real install. The guard:

  * every named volume in community-compose.yml is classified: rebuildable (listed in
    steward.REBUILDABLE_COMPOSE_VOLUMES) or data;
  * a rebuildable volume carries this install's id (from .env) and its rebuildable kind;
  * a data volume carries no boss.* label at all: no job can name it, and Compose never asks an
    operator "Recreate (data will be lost)?" over the live database because its labels changed.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "scripts" / "install" / "community-compose.yml").read_text())


def _steward():
    spec = importlib.util.spec_from_file_location("_steward_labels", ROOT / "scripts" / "install" / "steward.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


steward = _steward()
VOLUMES = {name: (spec or {}) for name, spec in (COMPOSE.get("volumes") or {}).items()}
#: Volumes that hold something a person made or BOSS cannot rebuild. Adding a volume to Compose
#: means deciding which list it belongs to; this test fails until it is in one.
DATA_VOLUMES = {
    "boss_community_cli_home", "boss_community_build_services_secret", "boss_community_claude_home",
    "boss_community_grok_home", "boss_community_infisical_redis", "boss_community_pg",
    "boss_community_prometheus", "boss_community_grafana", "boss_community_grafana_dashboards",
    "boss_community_artifacts", "boss_community_identity", "boss_community_toolchain_registry",
    "boss_community_repos", "boss_community_recovery", "boss_community_runner",
    "boss_community_sandboxes", "boss_community_backups",
}


def _labels(spec) -> dict[str, str]:
    labels = spec.get("labels") or {}
    if isinstance(labels, list):
        labels = dict(item.split("=", 1) for item in labels)
    return {str(k): str(v) for k, v in labels.items()}


def test_every_compose_volume_is_classified_rebuildable_or_data():
    rebuildable = set(steward.REBUILDABLE_COMPOSE_VOLUMES)
    assert not rebuildable & DATA_VOLUMES
    unclassified = set(VOLUMES) - rebuildable - DATA_VOLUMES
    assert not unclassified, f"decide: rebuildable (steward.REBUILDABLE_COMPOSE_VOLUMES) or data: {unclassified}"
    assert rebuildable <= set(VOLUMES), "the steward lists a volume Compose no longer has"


def test_every_rebuildable_volume_carries_the_install_id_and_a_rebuildable_kind():
    for name, kind in steward.REBUILDABLE_COMPOSE_VOLUMES.items():
        labels = _labels(VOLUMES[name])
        assert labels.get(steward.LABEL) == "${BOSS_INSTALL_ID:-}", name
        assert labels.get(steward.KIND_LABEL) == kind, name
        assert kind in steward.REBUILDABLE_VOLUME_KINDS, name


def test_no_data_volume_carries_a_boss_label():
    for name in DATA_VOLUMES:
        labels = _labels(VOLUMES[name])
        assert not any(k.startswith("boss.") for k in labels), (name, labels)
        assert labels.get(steward.KIND_LABEL) not in steward.REBUILDABLE_VOLUME_KINDS, name


def test_only_the_listed_volumes_say_they_are_rebuildable():
    """A rebuildable kind written in Compose but missing from the steward's list would never be
    relabelled on an existing install; the two lists are one."""
    kinds = {name: _labels(spec).get(steward.KIND_LABEL) for name, spec in VOLUMES.items()}
    said = {n for n, k in kinds.items() if k in steward.REBUILDABLE_VOLUME_KINDS}
    assert said == set(steward.REBUILDABLE_COMPOSE_VOLUMES)


def test_labels_come_with_a_new_key_never_on_an_existing_one():
    """Compose hashes a volume's labels; changing them on an existing volume makes it ask
    "Recreate (data will be lost)?" on every `up`. So a rebuildable volume got its labels under a
    new `_v2` key, the old key is gone from Compose, and the steward knows it as retired."""
    for old, kind in steward.RETIRED_COMPOSE_VOLUMES.items():
        assert old not in VOLUMES, f"{old} is retired: no service may mount it again"
        assert steward.REBUILDABLE_COMPOSE_VOLUMES.get(f"{old}_v2") == kind, old
    text = (ROOT / "scripts" / "install" / "community-compose.yml").read_text()
    for old in steward.RETIRED_COMPOSE_VOLUMES:
        assert f"- {old}:" not in text, f"a service still mounts the retired {old}"


def test_the_upgrade_and_dev_up_run_the_relabel_step():
    """Existing installs: the steward's --relabel-volumes removes the retired volumes once the
    services have moved off them, best effort — from upgrade.sh after the installer, and from
    `boss dev up` after db-test has moved."""
    upgrade = (ROOT / "scripts" / "install" / "upgrade.sh").read_text()
    step = upgrade.index("--relabel-volumes </dev/null")
    assert upgrade.index('bash "$FETCHED_INSTALLER"') < step < upgrade.index("Upgrade complete:")
    assert "echo 'BOSS volume labels were not checked" in upgrade[step:], "a failure never fails the upgrade"
    boss = (ROOT / "scripts" / "install" / "boss.sh").read_text()
    up = boss.index("compose --profile dev up -d db-test </dev/null")
    assert boss.index("--relabel-volumes", up) > up, "after db-test has moved to the labelled volume"
    # the id the volume is labelled with exists before Compose first creates it
    assert boss.index('set_env_value BOSS_INSTALL_ID "inst_$(openssl rand -hex 8)"') < up


def test_a_pre_relabel_upgrade_sh_still_relabels(tmp_path):
    """An upgrade started by a pre-#131 upgrade.sh never reaches the relabel that the new
    upgrade.sh runs after the installer. That old script's last act is to run the fetched
    install.sh, so the relabel has to be in there, after the containers are up, and a failure
    of it still leaves the upgrade successful."""
    install = (ROOT / "install.sh").read_text()
    started = install.index('ui_ok "Containers started"')
    step = install.index("--relabel-volumes </dev/null", started)
    assert "echo 'BOSS volume labels were not checked" in install[step:]
    block_at = install.index("# VOLUME LABELS (BOSSAPP-1193)", started)
    echo_at = install.index("were not checked", block_at)
    block = install[block_at:install.index("\nfi\n", echo_at) + len("\nfi\n")]
    # The driver that is already on disk has no relabel of its own. It only runs the installer.
    old = "#!/usr/bin/env bash\nset -euo pipefail\nbash \"$FETCHED_INSTALLER\"\n"
    assert "--relabel-volumes" not in old

    install_dir = tmp_path / "boss"
    steward_dir = install_dir / "scripts" / "install"
    steward_dir.mkdir(parents=True)
    log = install_dir / "relabel.log"
    (steward_dir / "steward.py").write_text(
        "# --relabel-volumes\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[sys.argv.index('--install-dir') + 1], 'relabel.log')"
        ".write_text('relabelled\\n')\n"
        "raise SystemExit(0)\n")
    fetched = tmp_path / "install.sh"
    fetched.write_text("#!/usr/bin/env bash\nset -euo pipefail\n"
                       f"INSTALL_DIR={str(install_dir)!r}\n" + block)
    fetched.chmod(0o755)
    driver = tmp_path / "upgrade.sh"
    driver.write_text(old)
    driver.chmod(0o755)
    import os
    import subprocess

    ran = subprocess.run(["bash", str(driver)], cwd=tmp_path,
                         env={**os.environ, "FETCHED_INSTALLER": str(fetched)},
                         capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr
    assert log.read_text() == "relabelled\n"

    (steward_dir / "steward.py").write_text("# --relabel-volumes\nimport sys\nraise SystemExit(1)\n")
    failed = subprocess.run(["bash", str(driver)], cwd=tmp_path,
                            env={**os.environ, "FETCHED_INSTALLER": str(fetched)},
                            capture_output=True, text=True)
    assert failed.returncode == 0, failed.stderr
    assert "volume labels were not checked" in failed.stderr


def test_boss_scripts_never_let_compose_ask_a_question():
    """Compose's only question here is "Recreate (data will be lost)?". Every compose call in
    boss.sh that could create a volume (up, create, run, start, restart) reads stdin from
    /dev/null, so no operator is ever asked it by BOSS's own scripts."""
    import re

    boss = (ROOT / "scripts" / "install" / "boss.sh").read_text()
    for line in boss.splitlines():
        code = line.split("#", 1)[0]
        if re.search(r"\bcompose\b.*\b(up|create|run|start|restart|stop)\b", code) and "exec" not in code:
            assert "</dev/null" in code, line


def test_the_plan_takes_the_measured_reclaimable_figure_not_every_volume_on_the_disk():
    """`volumes_gb` is every volume Docker holds, data included; planning the orphan-volume step
    on it promised space the helper may never remove. The step is planned only on the helper's
    measured `volumes_reclaimable`; a volume that needs a relabel is shown on the Storage card,
    not turned into a step no job can act on."""
    from boss_engine.services import storage_cleanup, storage_watch

    applies = storage_cleanup.TOOL_BY_NAME["storage.remove_orphan_volumes"].applies
    assert not applies({"volumes_gb": 50.0, "by_type": {"volumes": 50.0}}, {})
    assert not applies({"volumes_gb": 50.0, "by_type": {"volumes_reclaimable": 0.0}}, {})
    assert applies({"volumes_gb": 50.0, "by_type": {"volumes_reclaimable": 2.5}}, {})
    assert not applies({"volumes_gb": 50.0, "by_type": {"volumes_reclaimable": 0.0, "volumes_needs_relabel_n": 1.0}}, {})
    assert storage_watch.volume_reading({"by_type": {"volumes_reclaimable": 2.5, "volumes_needs_relabel_n": 2.0}}) \
        == {"reclaimable_gb": 2.5, "needs_relabel": 2}
    assert storage_watch.volume_reading({}) == {"reclaimable_gb": 0.0, "needs_relabel": 0}
