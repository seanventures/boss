"""Disk hygiene removes only what BOSS made (stability spec §4a.8).

boss-1 sat at 80% disk with 46.5 GB of build cache growing ~1.3 GB per deploy; nothing pruned.
The fix prunes after every deploy and above 75% disk — and must never become the thing that
deletes a customer's volumes or another project's images.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))

PRUNE = ROOT / "scripts" / "install" / "prune.sh"


def test_the_host_prune_never_touches_volumes_or_prunes_the_system():
    body = PRUNE.read_text(encoding="utf-8")
    code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
    for forbidden in ("system prune", "--volumes", "volume rm", "volume prune", "rmi -f $(",
                      "container prune"):
        assert forbidden not in code, forbidden
    # Build cache is shared across projects: pruned only on a host that runs nothing but BOSS.
    assert 'if [[ "$DEDICATED" -eq 1 ]]; then' in code


def test_every_deploy_path_prunes_after_it_succeeds():
    boss = (ROOT / "scripts" / "install" / "boss.sh").read_text(encoding="utf-8")
    remote = (ROOT / "scripts" / "install" / "remote.sh").read_text(encoding="utf-8")
    assert boss.count("post_deploy_prune") >= 3          # defined + upgrade + update
    assert remote.count("prune_remote") >= 3             # defined + install + update
    assert "prune.sh --dedicated" in remote


def _fake_docker(tmp_path: Path) -> Path:
    log = tmp_path / "docker.log"
    exe = tmp_path / "docker"
    exe.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


def test_the_reaper_prunes_build_cache_only_on_a_dedicated_host(tmp_path, monkeypatch):
    from boss_engine.services import resource_reaper

    exe = _fake_docker(tmp_path)
    full = {"total_gb": 77.0, "free_gb": 16.0, "used_fraction": 0.8}
    monkeypatch.delenv("BOSS_DEDICATED_HOST", raising=False)
    out = resource_reaper.prune_disk(binary=str(exe), install_id="inst_x", usage=full)
    log = (tmp_path / "docker.log").read_text()
    assert out["pruned"] and "builder" not in log
    assert "image prune -f --filter label=boss.install_id=inst_x" in log

    monkeypatch.setenv("BOSS_DEDICATED_HOST", "1")
    resource_reaper.prune_disk(binary=str(exe), install_id="inst_x", usage=full)
    log = (tmp_path / "docker.log").read_text()
    assert "builder prune -f --filter until=72h --keep-storage 15gb" in log
    assert "volume" not in log and "system" not in log


def test_below_the_threshold_nothing_is_pruned(tmp_path, monkeypatch):
    from boss_engine.services import resource_reaper

    exe = _fake_docker(tmp_path)
    monkeypatch.setenv("BOSS_DEDICATED_HOST", "1")
    out = resource_reaper.prune_disk(binary=str(exe), install_id="inst_x",
                                     usage={"total_gb": 77.0, "free_gb": 40.0,
                                            "used_fraction": 0.48})
    assert out["pruned"] is False
    assert not (tmp_path / "docker.log").exists()
    assert os.environ["BOSS_DEDICATED_HOST"] == "1"
