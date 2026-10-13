"""Every built-in template applies, per declared stack (F2 §2.3, F3 §3.8 "per template, per stack").

Without a database this proves the mechanical half of an apply for each built-in and stack:
  * the tree validates (as a built-in AND as a customer import would);
  * the build's directive assembles: the brief, the stack's reference with its tests inlined or
    listed, the test command and `min_tests`, and the routes as acceptance criteria;
  * a build that copies the stack's reference into the app passes `no_committed_secrets` (the
    reference holds nothing credential-shaped, nor anything matching the manifest's own secret
    patterns) and `template_tests` (its test files are in the diff; the report is counted);
  * a test-mode apply needs no pasted secret where the template promises none (login, catalog).

`BOSS_TEMPLATE_TRIALS=1` also RUNS each stack's real test command through the real gate in a
copy of the reference (network for npm/pip/maven, toolchains on PATH): the trial a CI job runs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from boss_engine.templates import apply as apply_service
from boss_engine.templates import service
from boss_engine.templates.gate import evaluate, template_test_files
from boss_engine.templates.validator import tree_from_dir, validate_tree

from ._fixtures import REPO_ROOT

APPS = REPO_ROOT / "templates" / "apps"
BUILTINS = sorted(p.name for p in APPS.iterdir() if p.is_dir())
CASES = []
for _name in BUILTINS:
    _report = validate_tree(tree_from_dir(APPS / _name), source="builtin")
    for _stack in (_report.manifest.stacks if _report.manifest else ()):
        CASES.append((_name, _stack))


@dataclass
class _Result:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


def _load(name: str):
    tree = tree_from_dir(APPS / name)
    report = validate_tree(tree, source="builtin")
    assert report.ok, f"{name}: {report.reasons}"
    return report, tree.files()


def _template(report, stack: str) -> dict:
    return {"id": f"tpl_{report.manifest.id}", "digest": report.digest, "stack": stack,
            "manifest": report.manifest.model_dump(mode="json", by_alias=True)}


def _copied_reference(files: dict[str, bytes], stack: str) -> dict[str, bytes]:
    prefix = f"stacks/{stack}/"
    return {p[len(prefix):]: b for p, b in files.items() if p.startswith(prefix)}


def test_the_service_offers_every_built_in():
    names = sorted(name for name, report, _ in service.builtins() if report.ok)
    assert names == BUILTINS
    for required in ("hello", "login-oauth", "catalog-cart"):
        assert required in BUILTINS


@pytest.mark.parametrize("name", BUILTINS)
def test_a_built_in_would_also_pass_as_a_customer_import(name):
    tree = tree_from_dir(APPS / name)
    # An insecure-by-design built-in imports only as the label requires: by an importer holding
    # template:approve who acknowledges it (the validator's rule, §2.5); without both it is refused.
    insecure = "insecure" in name
    report = validate_tree(tree, source="git", builtin_ids=(), may_label_insecure=insecure, ack_insecure=insecure)
    assert report.ok, report.reasons
    if insecure:
        assert not validate_tree(tree, source="git", builtin_ids=()).ok


@pytest.mark.parametrize("name,stack", CASES)
def test_the_build_directive_assembles_for_every_stack(name, stack):
    report, files = _load(name)
    m = report.manifest
    row = {"source": "builtin", "digest": report.digest}
    text = apply_service.build_directive(row=row, manifest=m, stack=stack, framework=m.stacks[stack].frameworks[0],
                                         files=files, note="", app_name="Shop")
    assert f'"{m.name}" ({m.id} {m.version})' in text
    # A connector is INTEGRATED into the app, in the app's own look; an idea STARTS an app.
    opening = {"connector": "Integrate the connector", "idea": "from the idea",
               "training": f'Apply "{m.name}"'}[m.item_kind]
    assert opening in text
    tests = template_test_files(files, stack)
    assert tests, f"{name}/{stack} has no tests"
    if m.item_kind == "connector":
        assert "read the app's UI conventions" in text and "SECURITY CORE stays exactly" in text
        # PRE-TESTED: the build neither copies, inlines nor runs a connector's own tests.
        assert "PRE-TESTED" in text and "Do NOT write tests for the connector" in text
        assert m.tests[stack].command not in text
        for t in tests:
            assert f"### tests/{t}" not in text
    else:
        assert m.tests[stack].command in text and f"at least {m.tests[stack].min_tests} passing tests" in text
        for t in tests:  # every test file reaches the agent, inline or by name
            assert f"tests/{t}" in text
    assert apply_service.UNTRUSTED_BEGIN not in text
    for route in m.routes:
        assert route in text


@pytest.mark.parametrize("name,stack", CASES)
def test_a_build_that_copies_the_reference_passes_both_gates(name, stack, tmp_path):
    report, files = _load(name)
    tpl = _template(report, stack)
    changed = _copied_reference(files, stack)
    need = report.manifest.tests[stack].min_tests
    junit = "<testsuites>" + "".join(f'<testcase name="t{i}"/>' for i in range(need)) + "</testsuites>"

    def run(command: str, cwd: Path):
        (Path(cwd) / "boss-template-junit.xml").write_text(junit)
        return _Result()

    # as_connector=False: the item's OWN test tier runs its suite (a build of a connector does not).
    r = evaluate(template=tpl, files=files, changed=changed, cwd=tmp_path, run_command=run,
                 as_connector=False)
    assert r.gates == {"template_tests": True, "no_committed_secrets": True}, r.evidence
    assert r.evidence["secret_scan"]["count"] == 0


@pytest.mark.parametrize("name", ["login-oauth", "catalog-cart"])
def test_test_mode_needs_no_pasted_secret(name):
    """F2 §2.1: pick the template, paste nothing (test mode), press Start. Every secret these
    templates declare is generated or a stub (the fake provider), so the apply's secrets check
    (secrets.ensure_test_defaults) has nothing to ask for."""
    report, _ = _load(name)
    assert [s.name for s in report.manifest.secrets if s.test_default == "none"] == []


def test_login_declares_its_provider_credentials_as_secrets_never_as_settings():
    report, files = _load("login-oauth")
    m = report.manifest
    secrets = {s.name for s in m.secrets}
    assert {"GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET",
            "SESSION_SECRET", "OIDC_BOSS_CLIENT_ID", "OIDC_BOSS_CLIENT_SECRET"} <= secrets
    assert not secrets & {e.name for e in m.env}
    assert set(m.stacks) == {"node", "python", "java"}
    # the BOSS provider is OIDC discovery from a setting, ready for "Sign in with BOSS"
    assert "OIDC_BOSS_ISSUER" in {e.name for e in m.env}


# ---- the real trial (opt-in: needs the toolchains and network for dependencies) -----------------

def _toolchain(stack: str) -> bool:
    return {"node": shutil.which("npm") is not None, "python": shutil.which("python3") is not None,
            "java": shutil.which("mvn") is not None}[stack]


@pytest.mark.skipif(os.environ.get("BOSS_TEMPLATE_TRIALS") != "1", reason="set BOSS_TEMPLATE_TRIALS=1 to run")
@pytest.mark.parametrize("name,stack", CASES)
def test_the_real_tests_pass_the_gate(name, stack, tmp_path):
    if not _toolchain(stack):
        pytest.skip(f"no {stack} toolchain on PATH")
    report, files = _load(name)
    app = tmp_path / "app"
    for rel, body in _copied_reference(files, stack).items():
        (app / rel).parent.mkdir(parents=True, exist_ok=True)
        (app / rel).write_bytes(body)

    def run(command: str, cwd: Path):
        argv = command.split() + (["-p", "no:cacheprovider"] if stack == "python" else [])
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=900)
        return _Result(p.returncode, p.stdout[-4000:], p.stderr[-4000:])

    r = evaluate(template=_template(report, stack), files=files, changed=_copied_reference(files, stack),
                 cwd=app, run_command=run, as_connector=False)
    assert r.gates["template_tests"], r.evidence
    assert r.evidence["template_tests"]["passed"] >= report.manifest.tests[stack].min_tests
