from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
RELEASE_VERSION = "0.8.0"


def _json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_release_versions_are_aligned() -> None:
    assert _json(ROOT / "package.json")["version"] == RELEASE_VERSION
    assert _json(ROOT / "apps/web/package.json")["version"] == RELEASE_VERSION
    assert _json(ROOT / "packages/api-client/package.json")["version"] == RELEASE_VERSION

    pyproject = (ENGINE / "pyproject.toml").read_text(encoding="utf-8")
    init = (ENGINE / "boss_engine/__init__.py").read_text(encoding="utf-8")
    chart = (ROOT / "deploy/helm/boss/Chart.yaml").read_text(encoding="utf-8")
    assert f'version = "{RELEASE_VERSION}"' in pyproject
    assert f'__version__ = "{RELEASE_VERSION}"' in init
    assert f"version: {RELEASE_VERSION}" in chart
    assert f'appVersion: "{RELEASE_VERSION}"' in chart


def test_wheel_discovers_the_complete_engine_package_tree() -> None:
    pyproject = (ENGINE / "pyproject.toml").read_text(encoding="utf-8")
    assert '[tool.setuptools.packages.find]' in pyproject
    assert 'include = ["boss_engine*"]' in pyproject
    assert not re.search(r'^packages\s*=\s*\[', pyproject, flags=re.MULTILINE)
    assert (ENGINE / "boss_engine/services/core.py").is_file()


def test_distribution_has_apache_license_evidence() -> None:
    assert "Apache License" in (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in (ENGINE / "LICENSE").read_text(encoding="utf-8")
    assert (ROOT / "packages/api-client/LICENSE").is_file()
    assert (ROOT / "packages/specpack/LICENSE").is_file()


def test_container_boundaries_include_the_engine_without_api_source() -> None:
    standalone = (ENGINE / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY boss_engine ./boss_engine" in standalone
    assert "COPY pyproject.toml README.md LICENSE ./" in standalone
    assert "apps/" not in "\n".join(
        line for line in standalone.splitlines() if line.lstrip().startswith("COPY ")
    )

    for relative in ("infra/containers/api.Dockerfile", "infra/containers/worker.Dockerfile"):
        content = (ROOT / relative).read_text(encoding="utf-8")
        assert "COPY engine ./engine" in content
        assert "/app/engine" in content


def test_local_entrypoints_include_the_engine_package() -> None:
    root_package = _json(ROOT / "package.json")
    scripts = root_package["scripts"]
    assert isinstance(scripts, dict)
    assert "PYTHONPATH=.:engine" in scripts["dev"]
    assert "PYTHONPATH=.:engine" in scripts["test"]

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    for target in ("seed:", "smoke:", "real-smoke-plan:", "worker-drain-once:"):
        section = makefile.split(target, maxsplit=1)[1].split("\n\n", maxsplit=1)[0]
        assert "PYTHONPATH=.:engine" in section
