"""The platform's own contract harness (`python -m boss_engine.platform test <dir>`) passes every
official plugin that runs standalone: loaded through the registry, LocalHost, its broker."""
from __future__ import annotations

import pytest

from boss_engine.infra_plugins import official_dir
from boss_engine.platform.harness import run_contract

from ._harness import docker_ok


@pytest.mark.parametrize("directory", ["sqlite", "postgres", "valkey", "minio-s3", "opentelemetry"])
def test_platform_harness_passes(directory, tmp_path, monkeypatch):
    if directory != "sqlite" and not docker_ok():
        pytest.skip("BOSS_RUN_DOCKER_LIFECYCLE_TEST=1 and Docker needed")
    monkeypatch.setenv("BOSS_INFRA_SQLITE_DIR", str(tmp_path))
    report = run_contract(official_dir() / directory)
    assert report.ok, report.text()
