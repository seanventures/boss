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


def test_the_upgrade_and_dev_up_run_the_relabel_step():
    """Docker cannot relabel a volume: existing installs get theirs recreated by the steward's
    --relabel-volumes, best effort, from upgrade.sh (after the new release runs) and `boss dev up`
    (before db-test starts)."""
    upgrade = (ROOT / "scripts" / "install" / "upgrade.sh").read_text()
    step = upgrade.index("--relabel-volumes </dev/null")
    assert step < upgrade.index("Upgrade complete:"), "after the installer, before the final line"
    assert "echo 'BOSS volume labels were not checked" in upgrade[step:], "a failure never fails the upgrade"
    boss = (ROOT / "scripts" / "install" / "boss.sh").read_text()
    assert boss.index("--relabel-volumes") < boss.index("compose --profile dev up -d db-test")


def test_the_plan_takes_the_measured_reclaimable_figure_not_every_volume_on_the_disk():
    """`volumes_gb` is every volume Docker holds, data included; planning the orphan-volume step
    on it promised space the helper may never remove. The step is planned on the helper's
    measured `volumes_reclaimable`, or to name volumes that need a relabel."""
    from boss_engine.services import storage_cleanup

    applies = storage_cleanup.TOOL_BY_NAME["storage.remove_orphan_volumes"].applies
    assert not applies({"volumes_gb": 50.0, "by_type": {"volumes": 50.0}}, {})
    assert not applies({"volumes_gb": 50.0, "by_type": {"volumes_reclaimable": 0.0}}, {})
    assert applies({"volumes_gb": 50.0, "by_type": {"volumes_reclaimable": 2.5}}, {})
    assert applies({"volumes_gb": 50.0, "by_type": {"volumes_reclaimable": 0.0, "volumes_needs_relabel_n": 1.0}}, {})
