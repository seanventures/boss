from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
RELEASE_VERSION = "1.0.0-rc.10.12"
PYTHON_RELEASE_VERSION = "1.0.0rc10.post12"


def _json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_release_versions_are_aligned() -> None:
    assert _json(ROOT / "package.json")["version"] == RELEASE_VERSION
    assert _json(ROOT / "apps/web/package.json")["version"] == RELEASE_VERSION
    assert _json(ROOT / "packages/api-client/package.json")["version"] == RELEASE_VERSION

    pyproject = (ENGINE / "pyproject.toml").read_text(encoding="utf-8")
    init = (ENGINE / "boss_engine/__init__.py").read_text(encoding="utf-8")
    assert f'version = "{PYTHON_RELEASE_VERSION}"' in pyproject
    assert f'__version__ = "{PYTHON_RELEASE_VERSION}"' in init
    assert f"version: {RELEASE_VERSION}" in (ROOT / "api/openapi.yaml").read_text()


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
    start = re.search(rf"^FROM .+ AS {re.escape(target)}$", dockerfile, re.MULTILINE)
    assert start is not None, f"missing Community stage {target}"
    nxt = dockerfile.find("\nFROM ", start.end())
    return dockerfile[start.start():nxt if nxt > 0 else len(dockerfile)]


def test_community_runtime_vendors_its_local_dependencies() -> None:
    dockerfile = (ROOT / "Dockerfile.community").read_text()
    runtime = _stage_body(dockerfile, "python-runtime")
    for path in ("engine/boss_engine", "apps/api", "apps/worker", "apps/egress",
                 "db", "api", "packages/specpack", "tools"):
        assert f"COPY {path} ./" in runtime
        assert (ROOT / path).is_dir()
    assert "PYTHONPATH=/app:/app/engine" in runtime
    for target in ("api", "worker", "egress", "mcp-tools"):
        assert f"FROM python-runtime AS {target}" in dockerfile
    assert "COPY . " not in dockerfile


def test_runtime_images_copy_v1_lock_and_traceability() -> None:
    dockerfile = (ROOT / "Dockerfile.community").read_text()
    runtime = _stage_body(dockerfile, "python-runtime")
    for path in ("docs/product/V1_FEATURE_LOCK.md", "docs/product/TRACEABILITY.md",
                 "LICENSE", "THIRD-PARTY-NOTICES.md"):
        assert f"COPY {path} ./" in runtime
        assert (ROOT / path).is_file()
    assert "COPY docs ./" not in dockerfile
    web = _stage_body(dockerfile, "web")
    assert "NODE_ENV=production" in web
    assert "USER boss" in runtime and "USER boss" in web


def test_runtime_context_excludes_tests_and_scratch_but_retains_runtime_sources() -> None:
    # Tests remain in the public Git/source distribution for contributors; broad COPY
    # instructions must not carry them into API/worker or Next runtime images.
    ignored = set((ROOT / ".dockerignore").read_text().splitlines())
    for path in ("apps/api/tests", "apps/worker/tests", "apps/web/tests",
                 "packages/api-client/tests", "tools/tests", "**/__probe_tmp",
                 "scripts/seed_demo.py",
                 "engine/boss_engine/identity/seed_accounts.py",
                 "engine/boss_engine/cli/console_test.py",
                 "engine/boss_engine/services/console_driver.py",
                 "engine/boss_engine/services/console_test_agent.py",
                 "engine/boss_engine/services/console_screen_check.py"):
        assert path in ignored
    assert "COPY scripts/seed_demo.py" not in (ROOT / "Dockerfile.community").read_text()
    for path in ("apps/api/app", "apps/worker/boss_worker", "apps/web/app", "apps/web/lib"):
        assert path not in ignored and (ROOT / path).is_dir()


def test_identity_cli_refuses_qa_seeding_before_database_access(monkeypatch, capsys, tmp_path) -> None:
    from boss_engine import db
    from boss_engine.identity import cli

    def unexpected_database_access():
        pytest.fail("A rejected QA command must not enter the database transaction")

    monkeypatch.setattr(db, "begin", unexpected_database_access)
    output = tmp_path / "never-created-qa-secrets.json"
    with pytest.raises(SystemExit) as refused:
        cli.main(["seed-test-accounts", "--org", "org_owned_fixture",
                  "--owner-email", "owner@example.invalid", "--out", str(output), "--i-know"])
    assert refused.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
    assert not output.exists()

    with pytest.raises(SystemExit) as help_exit:
        cli.main(["--help"])
    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "seed-test-accounts" not in help_text
    for command in ("recover-owner", "setup-link", "claim-status", "host-key", "mcp-key", "egress-key"):
        assert command in help_text


def test_identity_cli_keeps_the_real_claim_status_command(monkeypatch, capsys) -> None:
    from contextlib import contextmanager

    from boss_engine import db
    from boss_engine.identity import cli, migration

    connection = object()

    @contextmanager
    def owned_transaction():
        yield connection

    def owned_claim_status(org_id, *, db):
        assert org_id == "org_owned_fixture" and db is connection
        return "unclaimed"

    monkeypatch.setattr(db, "begin", owned_transaction)
    monkeypatch.setattr(migration, "ensure_seeded", lambda db: "org_owned_fixture")
    monkeypatch.setattr(cli, "claim_status", owned_claim_status)
    assert cli.main(["claim-status"]) == 0
    assert capsys.readouterr().out.strip() == "unclaimed"


def test_local_entrypoints_include_the_engine_package() -> None:
    root_package = _json(ROOT / "package.json")
    scripts = root_package["scripts"]
    assert isinstance(scripts, dict)
    assert "PYTHONPATH=.:engine" in scripts["dev"]
    assert "PYTHONPATH=.:engine" in scripts["test"]
    assert "--rootdir=." in scripts["test"]
    assert "${PYTHON_BIN:-./.venv/bin/python}" in scripts["test"]

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "scripts/install/community-compose.yml --env-file .env" in makefile
    assert "dev: .env docker-ready" in makefile
    assert "$(COMPOSE) --profile web up -d --build" in makefile
    assert "seed: .env docker-ready" in makefile
    assert "$(COMPOSE) exec -T -u boss api python" in makefile
    assert "./scripts/install/checkup.sh" in makefile
