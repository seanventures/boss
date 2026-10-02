from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
RELEASE_VERSION = "1.0.0-rc.4"


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


def _stage_body(dockerfile: str, target: str) -> str:
    marker = f"FROM python-base AS {target}"
    start = dockerfile.index(marker)
    nxt = dockerfile.find("\nFROM ", start + 1)
    return dockerfile[start : nxt if nxt > 0 else len(dockerfile)]


def test_container_boundaries_include_the_engine_without_api_source() -> None:
    standalone = (ENGINE / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY engine/boss_engine engine/boss_engine" in standalone
    assert "COPY engine/pyproject.toml engine/README.md engine/LICENSE engine/" in standalone
    # Self-bootstrap data (Plane #306): the standalone image vendors the schema,
    # the spec pack, and the traceability table so `bootstrap` works on a fresh DB.
    assert "COPY db/schema.sql db/schema.sql" in standalone
    assert "COPY packages/specpack packages/specpack" in standalone
    assert "COPY docs/product/TRACEABILITY.md docs/product/TRACEABILITY.md" in standalone
    # GET /v1/v1-lock (and engine-native load_v1_feature_lock) read this path via
    # Settings.repo_root. Missing file yields honest zeros, not invented counts.
    assert (
        "COPY docs/product/V1_FEATURE_LOCK.md docs/product/V1_FEATURE_LOCK.md"
        in standalone
    )
    assert "apps/" not in "\n".join(
        line for line in standalone.splitlines() if line.lstrip().startswith("COPY ")
    )

    for relative in ("infra/containers/api.Dockerfile", "infra/containers/worker.Dockerfile"):
        content = (ROOT / relative).read_text(encoding="utf-8")
        assert "COPY engine ./engine" in content
        assert "/app/engine" in content


def test_runtime_images_copy_v1_lock_and_traceability() -> None:
    """Live GET /v1/v1-lock reads /app/docs/product/V1_FEATURE_LOCK.md.

    Root api + worker stages must vendor the lock (and TRACEABILITY.md, which
    the feature catalog seeds from). Do not dump all of docs/.
    """
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    lock_copy = "COPY docs/product/V1_FEATURE_LOCK.md docs/product/V1_FEATURE_LOCK.md"
    trace_copy = "COPY docs/product/TRACEABILITY.md docs/product/TRACEABILITY.md"
    for target in ("api", "worker"):
        stage = _stage_body(dockerfile, target)
        assert lock_copy in stage, f"{target} stage must COPY V1_FEATURE_LOCK.md"
        assert trace_copy in stage, f"{target} stage must COPY TRACEABILITY.md"
        assert "COPY docs ./" not in stage
        assert "COPY docs ./docs" not in stage


def test_local_entrypoints_include_the_engine_package() -> None:
    root_package = _json(ROOT / "package.json")
    scripts = root_package["scripts"]
    assert isinstance(scripts, dict)
    assert "PYTHONPATH=.:engine" in scripts["dev"]
    assert "PYTHONPATH=.:engine" in scripts["test"]
    assert "--rootdir=." in scripts["test"]
    assert "${PYTHON_BIN:-./.venv/bin/python}" in scripts["test"]

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "PYTHON_REQUIREMENTS_STAMP" in makefile
    assert 'sys.version_info >= (3, 11)' in makefile
    for target in ("seed:", "smoke:", "real-smoke-plan:", "worker-drain-once:"):
        section = makefile.split(target, maxsplit=1)[1].split("\n\n", maxsplit=1)[0]
        assert "PYTHONPATH=.:engine" in section
