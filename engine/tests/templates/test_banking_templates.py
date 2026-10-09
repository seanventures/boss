"""The banking built-ins (F3 §3.5, §3.6): they validate as built-ins, their build directive
assembles for the java stack, and their real JUnit suites pass the real `template_tests` gate.

The gate run needs Java 21 + Maven: either `mvn` on PATH with a JDK 21, or the toolchain image
named by BOSS_TEST_JAVA_IMAGE (default `boss-s3-tcjava:dev`) under docker. Neither = skipped.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from boss_engine.templates import apply as apply_service
from boss_engine.templates.gate import evaluate, template_test_files
from boss_engine.templates.validator import tree_from_dir, validate_tree

from ._fixtures import REPO_ROOT

APPS = REPO_ROOT / "templates" / "apps"
SECURE = APPS / "banking-starter"
JAVA_IMAGE = os.environ.get("BOSS_TEST_JAVA_IMAGE", "boss-s3-tcjava:dev")


def _report(path: Path):
    return validate_tree(tree_from_dir(path), source="builtin")


def test_the_banking_starter_is_a_valid_built_in():
    r = _report(SECURE)
    assert r.ok, r.reasons
    m = r.manifest
    assert m.id == "banking-starter" and not m.insecure_by_design
    assert set(m.stacks) == {"java"} and m.stacks["java"].frameworks == ("dropwizard",)
    assert m.tests["java"].min_tests >= 14
    assert m.secrets == ()                      # nothing to leak: opaque tokens, no signing key
    assert {"POST /api/login", "POST /api/accounts/{id}/withdraw", "POST /api/transfers",
            "POST /api/password", "GET /openapi.json"} <= set(m.routes)
    files = tree_from_dir(SECURE).files()
    tests = template_test_files(files, "java")
    assert "bank/BankingSecurityTest.java" in tests and "bank/BankingFlowTest.java" in tests
    assert b"<testSourceDirectory>tests</testSourceDirectory>" in files["stacks/java/pom.xml"]
    assert b"${PORT:-8080}" in files["stacks/java/config.yml"]


def test_the_banking_starters_directive_carries_the_java_reference():
    r = _report(SECURE)
    files = tree_from_dir(SECURE).files()
    text = apply_service.build_directive(row={"source": "builtin", "digest": r.digest}, manifest=r.manifest,
                                         stack="java", framework="dropwizard", files=files, note="",
                                         app_name="Bank")
    assert "Apply the template \"Banking starter\" (banking-starter 1.0.0)" in text
    assert "at least 14 passing tests" in text
    assert apply_service.UNTRUSTED_BEGIN not in text


# ---- the real JUnit suite through the real gate ---------------------------------------------------

@dataclass
class _Result:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


def _java_runner():
    """(run_command, how) for the template's `mvn` command, or None when Java 21 is unavailable."""
    if shutil.which("mvn") and shutil.which("java"):
        v = subprocess.run(["java", "-version"], capture_output=True, text=True)
        if 'version "21' in (v.stderr + v.stdout) or 'version "2' in (v.stderr + v.stdout):
            def local(command: str, cwd: Path):
                p = subprocess.run(command.split(), cwd=cwd, capture_output=True, text=True, timeout=900)
                return _Result(p.returncode, p.stdout, p.stderr)
            return local
    if shutil.which("docker"):
        probe = subprocess.run(["docker", "image", "inspect", JAVA_IMAGE], capture_output=True)
        if probe.returncode == 0:
            def in_image(command: str, cwd: Path):
                argv = ["docker", "run", "--rm", "-v", f"{cwd}:/w", "-w", "/w",
                        "-v", "boss-test-m2:/root/.m2", "-e", "JAVA_HOME=/opt/toolchains/java/jdk",
                        JAVA_IMAGE, "sh", "-c", "PATH=/opt/toolchains/bin:$PATH " + command]
                p = subprocess.run(argv, capture_output=True, text=True, timeout=900)
                return _Result(p.returncode, p.stdout, p.stderr)
            return in_image
    return None


RUNNER = _java_runner()


def _apply_and_gate(template_dir: Path, tmp_path: Path):
    """Copy the java reference as the app (a new project from the template), then run the gate."""
    r = _report(template_dir)
    assert r.ok, r.reasons
    files = tree_from_dir(template_dir).files()
    app = tmp_path / "app"
    changed = {}
    for path, body in files.items():
        if path.startswith("stacks/java/"):
            rel = path[len("stacks/java/"):]
            (app / rel).parent.mkdir(parents=True, exist_ok=True)
            (app / rel).write_bytes(body)
            changed[rel] = body
    tpl = {"id": "tpl_x", "digest": r.digest, "stack": "java",
           "manifest": r.manifest.model_dump(mode="json", by_alias=True)}
    return evaluate(template=tpl, files=files, changed=changed, cwd=app, run_command=RUNNER)


@pytest.mark.skipif(RUNNER is None, reason=f"no JDK 21 + mvn, and no docker image {JAVA_IMAGE}")
def test_the_banking_starter_applies_and_passes_the_template_tests_gate(tmp_path):
    result = _apply_and_gate(SECURE, tmp_path)
    assert result.gates == {"template_tests": True, "no_committed_secrets": True}, result.evidence
    assert result.evidence["template_tests"]["passed"] >= 14
    assert result.evidence["template_tests"]["failures"] == 0
