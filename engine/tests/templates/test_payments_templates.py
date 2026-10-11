"""The payments built-ins (F3 §3.2 `stripe-checkout`, §3.3 `paypal-checkout`, stream S3-payments):
each manifest validates and applies, per declared stack.

"Applies" here is everything apply and the build gates do with a template that needs no database:
the tree validates as a built-in; the build's directive carries the brief, the stack's reference
and its test files; the template's secrets accept only test-shaped values; and the stack's
reference, copied into an app as the agent is told to, passes the `no_committed_secrets` scan and
the `template_tests` gate. The API suite (apps/api/tests/test_templates_api.py) covers apply's
database side for every built-in.

With BOSS_TEMPLATE_REAL_RUN=1 the last test runs each stack's REAL test command (npm / pytest /
mvn, with Stripe and PayPal faked in-process; no network beyond package registries) and feeds the
real JUnit report through the gate.
"""
from __future__ import annotations

import os
import re
import secrets
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from boss_engine.templates import apply as apply_service
from boss_engine.templates.gate import evaluate, template_test_files
from boss_engine.templates.secrets import shape_of
from boss_engine.templates.validator import tree_from_dir, validate_tree

from ._fixtures import REPO_ROOT

APPS = REPO_ROOT / "templates" / "apps"
PAYMENTS = ("stripe-checkout", "paypal-checkout")


def _present() -> list[str]:
    return [t for t in PAYMENTS if (APPS / t / "boss-template.yaml").exists()]


def _report(template_id: str):
    report = validate_tree(tree_from_dir(APPS / template_id), source="builtin")
    assert report.ok, report.reasons
    return report


def _cases() -> list[tuple[str, str]]:
    out = []
    for t in _present():
        for stack in _report(t).manifest.stacks:
            out.append((t, stack))
    return out


CASES = _cases()


def test_the_stripe_template_is_present():
    assert "stripe-checkout" in _present()


@pytest.mark.parametrize("template_id", _present())
def test_each_payments_manifest_validates_with_test_mode_rules(template_id):
    m = _report(template_id).manifest
    assert m.id == template_id
    assert {"routes", "webhook", "db_tables"} <= set(m.adds)
    assert "catalog-cart" in m.works_with
    assert m.webhooks and all(w.path.startswith("/webhooks/") for w in m.webhooks)
    assert m.egress, "a payments app talks to its provider"
    for spec in m.tests.values():
        assert spec.min_tests >= 6, "F3 §3.2/3.3: at least 6-8 tests per stack"
    for s in m.secrets:
        assert s.test_default in ("none", "generated")  # never a stub standing in for a provider
    if template_id == "stripe-checkout":
        # Every pasted Stripe value has a test pattern, so a live key is refused for build/preview.
        for s in m.secrets:
            assert s.pattern, f"{s.name}: a pasted test value must have a pattern"
        assert m.egress == ("api.stripe.com",)
    else:
        # PayPal credentials have no test/live shape: the guard is the sandbox host and PAYPAL_ENV.
        env = {e.name: e.default for e in m.env}
        assert env["PAYPAL_ENV"] == "sandbox" and env["APP_ENV"] != "production"
        assert m.egress == ("api-m.sandbox.paypal.com",), "the live host needs the live-keys decision"


def _rand(n: int = 24) -> str:
    return secrets.token_hex(n // 2)


# Values built at run time: a literal key-shaped string in this file would (rightly) trip the scan.
SHAPES = {
    "STRIPE_SECRET_KEY": ("sk_" + "test_" + _rand(), "sk_" + "live_" + _rand()),
    "STRIPE_PUBLISHABLE_KEY": ("pk_" + "test_" + _rand(), "pk_" + "live_" + _rand()),
    "STRIPE_WEBHOOK_SECRET": ("whsec_" + _rand(), None),
}


@pytest.mark.parametrize("template_id", _present())
def test_secrets_accept_only_test_shaped_values(template_id):
    m = _report(template_id).manifest
    for s in m.secrets:
        if s.name in SHAPES:
            test_value, live_value = SHAPES[s.name]
            assert shape_of(test_value, s) == "matches_test_pattern"
            if live_value:
                assert shape_of(live_value, s) == "matches_live_pattern"
            assert shape_of("not-a-key", s) == "unknown"
            assert shape_of(test_value + "!", s) == "unknown"
        if s.name.startswith("STRIPE_"):
            assert s.pattern and re.match(r"^\^(sk_test_|pk_test_|whsec_)", s.pattern), s.name


@pytest.mark.parametrize("template_id,stack", CASES)
def test_the_build_directive_carries_the_brief_the_reference_and_the_tests(template_id, stack):
    report = _report(template_id)
    files = tree_from_dir(APPS / template_id).files()
    row = {"source": "builtin"}
    directive = apply_service.build_directive(row=row, manifest=report.manifest, stack=stack, framework=None,
                                              files=files, note="", app_name="Shop")
    tests = template_test_files(files, stack)
    assert tests
    for t in tests:
        assert t in directive
    assert report.manifest.tests[stack].command in directive
    assert "UNTRUSTED" not in directive  # a built-in is not wrapped as untrusted material
    for route in report.manifest.routes:
        assert route in directive


@pytest.mark.parametrize("template_id,stack", CASES)
def test_every_declared_route_is_served_by_the_stacks_reference(template_id, stack):
    report = _report(template_id)
    files = tree_from_dir(APPS / template_id).files()
    source = "\n".join(b.decode("utf-8", errors="replace") for p, b in files.items()
                       if p.startswith(f"stacks/{stack}/") and "/tests/" not in p and "/test/" not in p)
    for route in report.manifest.routes:
        _method, path = route.split(" ", 1)
        literal = path.split("/:")[0].split("/{")[0]
        assert literal in source, f"{template_id}/{stack}: {route} not found in the reference"


def _stack_as_app(template_id: str, stack: str) -> dict[str, bytes]:
    prefix = f"stacks/{stack}/"
    return {p[len(prefix):]: b for p, b in tree_from_dir(APPS / template_id).files().items() if p.startswith(prefix)}


def _template(template_id: str, stack: str) -> tuple[dict, dict[str, bytes]]:
    report = _report(template_id)
    return ({"id": "tpl_x", "digest": report.digest, "stack": stack,
             "manifest": report.manifest.model_dump(mode="json", by_alias=True)},
            tree_from_dir(APPS / template_id).files())


@dataclass
class _Result:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


@pytest.mark.parametrize("template_id,stack", CASES)
def test_the_reference_copied_into_an_app_passes_both_gates(template_id, stack, tmp_path):
    tpl, files = _template(template_id, stack)
    changed = _stack_as_app(template_id, stack)
    min_tests = tpl["manifest"]["tests"][stack]["min_tests"]

    def runner(command: str, cwd: Path) -> _Result:
        cases = "".join(f'<testcase name="t{i}"/>' for i in range(min_tests))
        (Path(cwd) / "boss-template-junit.xml").write_text(f"<testsuites>{cases}</testsuites>")
        return _Result()

    r = evaluate(template=tpl, files=files, changed=changed, cwd=tmp_path, run_command=runner)
    assert r.evidence["secret_scan"]["count"] == 0, r.evidence["secret_scan"]
    assert r.gates == {"template_tests": True, "no_committed_secrets": True}, r.evidence


@pytest.mark.parametrize("template_id,stack", CASES)
def test_a_committed_secret_in_the_app_fails_the_secret_gate(template_id, stack, tmp_path):
    """Stripe keys are caught by shape (the manifest's pattern); PayPal's, which have no shape, by
    the bound value the gate is given."""
    tpl, files = _template(template_id, stack)
    if template_id == "stripe-checkout":
        name, leaked, bound = "STRIPE_SECRET_KEY", SHAPES["STRIPE_SECRET_KEY"][0], {}
    else:
        name, leaked = "PAYPAL_CLIENT_SECRET", "E" + secrets.token_urlsafe(40)
        bound = {name: leaked}
    changed = dict(_stack_as_app(template_id, stack))
    changed[".env"] = f"{name}={leaked}\n".encode()
    r = evaluate(template=tpl, files=files, changed=changed, cwd=tmp_path, bound_values=bound,
                 run_command=lambda c, w: _Result())
    assert r.gates["no_committed_secrets"] is False
    assert leaked not in repr(r.evidence)


_TOOLS = {"node": "npm", "python": "python3", "java": "mvn"}


@pytest.mark.skipif(os.environ.get("BOSS_TEMPLATE_REAL_RUN") != "1",
                    reason="set BOSS_TEMPLATE_REAL_RUN=1 to run each stack's real tests")
@pytest.mark.parametrize("template_id,stack", CASES)
def test_the_stacks_real_tests_pass_through_the_gate(template_id, stack, tmp_path):
    if not shutil.which(_TOOLS[stack]):
        pytest.skip(f"{_TOOLS[stack]} is not installed")
    tpl, files = _template(template_id, stack)
    app = tmp_path / "app"
    for rel, content in _stack_as_app(template_id, stack).items():
        target = app / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def runner(command: str, cwd: Path) -> _Result:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("STRIPE_", "PAYPAL_"))}
        proc = subprocess.run(shlex.split(command), cwd=cwd, env=env, capture_output=True, text=True, timeout=900)
        return _Result(proc.returncode, proc.stdout[-4000:], proc.stderr[-4000:])

    r = evaluate(template=tpl, files=files, changed=_stack_as_app(template_id, stack), cwd=app, run_command=runner)
    assert r.gates["template_tests"], r.evidence["template_tests"]
    assert r.evidence["template_tests"]["passed"] >= tpl["manifest"]["tests"][stack]["min_tests"]


# Was a strict xfail (the S2 48 KB inline limit dropped tests/): fixed by inlining tests first
# and giving the build the whole file set (templates.run_ref).
@pytest.mark.parametrize("template_id,stack", CASES)
def test_the_directive_inlines_every_template_test_file(template_id, stack):
    report = _report(template_id)
    files = tree_from_dir(APPS / template_id).files()
    directive = apply_service.build_directive(row={"source": "builtin"}, manifest=report.manifest, stack=stack,
                                              framework=None, files=files, note="", app_name="Shop")
    for t in template_test_files(files, stack):
        assert f"### tests/{t}" in directive, f"{t} is not inlined"
