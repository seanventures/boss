"""The banking built-ins (F3 §3.5, §3.6): they validate as built-ins, their build directive
assembles for the java stack, the training variant's answer key (vulns.yaml) matches its code and
stays out of the build, and their real JUnit suites pass the real `template_tests` gate.

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
import yaml

from boss_engine.templates import apply as apply_service
from boss_engine.templates.gate import evaluate, template_test_files
from boss_engine.templates.validator import tree_from_dir, validate_tree

from ._fixtures import REPO_ROOT

APPS = REPO_ROOT / "templates" / "apps"
SECURE = APPS / "banking-starter"
INSECURE = APPS / "banking-starter-insecure"
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


# ---- the training variant (F3 §3.6) ----------------------------------------------------------------

def test_the_insecure_variant_is_a_valid_built_in_labelled_insecure_by_design():
    r = _report(INSECURE)
    assert r.ok, r.reasons
    m = r.manifest
    assert m.id == "banking-starter-insecure" and m.insecure_by_design
    assert m.tests["java"].min_tests >= 14
    secure_routes = set(_report(SECURE).manifest.routes)
    assert secure_routes <= set(m.routes)       # same routes as the starter (+ the banner page)
    # As an import, the label needs template:approve and the acknowledgement (§2.5).
    imported = validate_tree(tree_from_dir(INSECURE), source="git")
    assert not imported.ok and any("insecure_by_design" in x for x in imported.reasons)


def _vulns() -> dict:
    return yaml.safe_load((INSECURE / "vulns.yaml").read_text())


def test_vulns_yaml_catalogues_every_planted_flaw_with_a_checkable_location_and_fix():
    doc = _vulns()
    assert doc["template"] == "banking-starter-insecure" and doc["secure_reference"] == "banking-starter"
    vulns = doc["vulns"]
    ids = [v["id"] for v in vulns]
    assert len(ids) == len(set(ids))
    assert {f"V{i}" for i in range(1, 11)} <= set(ids)          # the spec's table, V1-V10
    spec_cwes = {"V1": "CWE-89", "V2": "CWE-639", "V3": "CWE-916", "V4": "CWE-20", "V5": "CWE-620",
                 "V6": "CWE-798", "V7": "CWE-209", "V8": "CWE-693", "V9": "CWE-1395", "V10": "CWE-307"}
    for v in vulns:
        assert v["title"] and v["owasp"].startswith("A") and v["severity"] in {"low", "medium", "high", "critical"}
        assert v["cwe"] and all(c.startswith("CWE-") for c in v["cwe"])
        if v["id"] in spec_cwes:
            assert spec_cwes[v["id"]] in v["cwe"], v["id"]
        assert v["detect"] and all(d["by"] and d["how"] for d in v["detect"])
        assert v["locations"], v["id"]
        for loc in v["locations"]:
            lines = (INSECURE / loc["file"]).read_text().splitlines()
            assert loc["match"] in lines[loc["line"] - 1], (v["id"], loc)
        assert v["fix"]["summary"] and v["fix"]["reference"]
        for ref in v["fix"]["reference"]:
            assert ref["file"].startswith("banking-starter/")
            assert (APPS / ref["file"]).is_file(), (v["id"], ref)


def test_the_answer_key_never_reaches_the_build_directive():
    r = _report(INSECURE)
    files = tree_from_dir(INSECURE).files()
    assert "vulns.yaml" in files
    text = apply_service.build_directive(row={"source": "builtin", "digest": r.digest}, manifest=r.manifest,
                                         stack="java", framework="dropwizard", files=files, note="",
                                         app_name="Bank lab")
    assert "CWE-" not in text and "vulns.yaml" not in text
    for v in _vulns()["vulns"]:
        assert v["title"] not in text


def test_the_insecure_variants_tests_check_functionality_not_exploits():
    tests = {p: b.decode() for p, b in tree_from_dir(INSECURE).files().items()
             if p.startswith("stacks/java/tests/")}
    assert tests
    body = "\n".join(tests.values())
    for marker in ("\"none\"", "1=1", "' OR", "-100", "alg"):
        assert marker not in body, marker


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


@pytest.mark.skipif(RUNNER is None, reason=f"no JDK 21 + mvn, and no docker image {JAVA_IMAGE}")
def test_the_insecure_variant_applies_and_passes_the_template_tests_gate(tmp_path):
    result = _apply_and_gate(INSECURE, tmp_path)
    assert result.gates == {"template_tests": True, "no_committed_secrets": True}, result.evidence
    assert result.evidence["template_tests"]["passed"] >= 14
