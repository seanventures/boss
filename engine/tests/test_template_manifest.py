"""boss-template.yaml, schema boss.template/v1 (APP_TEMPLATES_AND_SECURITY_LOOP.md §2.2). FROZEN.

The spec's own example is valid; every invalid manifest is refused with a reason that names the
field and says what is wrong; the field set is pinned so a change is a new schema id.
"""
from __future__ import annotations

import copy

import pytest
import yaml

from boss_engine.templates.manifest import (
    SCHEMA_ID,
    ManifestError,
    TemplateManifest,
    load_manifest,
    validate_manifest,
)

#: The example in §2.2, verbatim.
SPEC_EXAMPLE = """\
schema: boss.template/v1
id: stripe-checkout            # [a-z0-9-]{3,48}, unique per org
name: Stripe payments
version: 1.0.0                 # semver; an app records the version it applied
summary: Checkout + webhooks, test mode by default
adds: [routes, ui, db_tables, webhook]
stacks:
  node:   {frameworks: [express, nextjs], min_toolchain: node20}
  python: {frameworks: [fastapi, flask],  min_toolchain: python311}
  java:   {frameworks: [spring-boot, dropwizard], min_toolchain: jdk21}
requires: []                   # template ids that must already be applied
works_with: [catalog-cart]     # optional integration; the brief says how
secrets:
  - name: STRIPE_SECRET_KEY
    purpose: Server-side API calls
    test_default: none         # none | generated | stub   (none = operator must paste a test key)
    pattern: '^sk_test_[A-Za-z0-9]+$'          # what preview/test accept
    live_pattern: '^sk_live_[A-Za-z0-9]+$'     # only accepted for env=deploy, with a decision
  - name: STRIPE_WEBHOOK_SECRET
    test_default: generated
env:      [{name: STRIPE_PRICE_CURRENCY, default: usd}]
egress:   [api.stripe.com]     # hosts the RUNNING app needs; shown before apply, approved at apply
webhooks: [{path: /webhooks/stripe, provider: stripe}]
routes:   [POST /api/checkout, POST /webhooks/stripe, GET /checkout/success]
tests:
  node:   {command: npm test -- --runInBand, report: junit, min_tests: 6}
  python: {command: pytest -q tests/template_stripe, report: junit, min_tests: 6}
  java:   {command: mvn -q test -Dtest='Stripe*', report: junit, min_tests: 6}
security_note: SECURITY.md
guide: guide.md
labels: []                     # e.g. insecure_by_design (see F3 §4.6)
license: Apache-2.0
min_boss: 1.0.0
"""

MINIMAL = {
    "schema": "boss.template/v1", "id": "login-oauth", "name": "Login", "version": "0.1.0",
    "summary": "Sign in with GitHub", "stacks": {"node": {"frameworks": ["express"], "min_toolchain": "node20"}},
    "tests": {"node": {"command": "npm test", "report": "junit", "min_tests": 1}},
    "license": "MIT", "min_boss": "1.0.0",
}


def _with(**changes):
    data = copy.deepcopy(MINIMAL)
    for key, value in changes.items():
        if value is ...:
            data.pop(key, None)
        else:
            data[key] = value
    return data


def _reasons(data) -> list[str]:
    with pytest.raises(ManifestError) as caught:
        validate_manifest(data)
    return caught.value.reasons


# ---- valid ------------------------------------------------------------------------------------

def test_the_spec_example_is_valid():
    m = validate_manifest(SPEC_EXAMPLE)
    assert m.id == "stripe-checkout" and m.version == "1.0.0"
    assert set(m.stacks) == {"node", "python", "java"}
    assert m.stacks["java"].frameworks == ("spring-boot", "dropwizard")
    assert m.secret("STRIPE_SECRET_KEY").test_default == "none"
    assert m.secret("STRIPE_WEBHOOK_SECRET").test_default == "generated"
    assert m.tests["java"].min_tests == 6
    assert m.egress == ("api.stripe.com",)
    assert not m.insecure_by_design


def test_a_minimal_manifest_takes_the_defaults():
    m = validate_manifest(MINIMAL)
    assert (m.security_note, m.guide) == ("SECURITY.md", "guide.md")
    assert m.secrets == () and m.egress == () and m.labels == ()


def test_the_insecure_label_is_visible():
    assert validate_manifest(_with(labels=["insecure_by_design"])).insecure_by_design


def test_a_prerelease_version_and_an_spdx_expression_are_valid():
    m = validate_manifest(_with(version="2.0.0-rc.1+build.5", license="MIT OR Apache-2.0"))
    assert m.version == "2.0.0-rc.1+build.5"


def test_it_loads_from_a_template_directory(tmp_path):
    (tmp_path / "boss-template.yaml").write_text(SPEC_EXAMPLE, encoding="utf-8")
    assert load_manifest(tmp_path).id == "stripe-checkout"


def test_the_manifest_is_immutable():
    m = validate_manifest(MINIMAL)
    with pytest.raises(Exception):
        m.id = "other"  # type: ignore[misc]


def test_the_schema_is_frozen():
    # A change here is a new schema id (boss.template/v2), not an edit.
    assert SCHEMA_ID == "boss.template/v1"
    fields = {f.alias or name for name, f in TemplateManifest.model_fields.items()}
    assert fields == {"schema", "id", "name", "version", "summary", "adds", "stacks", "requires",
                      "works_with", "secrets", "env", "egress", "webhooks", "routes", "tests",
                      "security_note", "guide", "labels", "license", "min_boss"}
    assert set(yaml.safe_load(SPEC_EXAMPLE)) == fields


# ---- invalid, each with a reason that names the field ---------------------------------------

@pytest.mark.parametrize("data, field, words", [
    (_with(schema="boss.template/v2"), "schema", "boss.template/v1"),
    (_with(schema=...), "schema", "required"),
    (_with(id="Payments_Stripe"), "id", "3-48 characters"),
    (_with(id="ab"), "id", "3-48 characters"),
    (_with(version="1.0"), "version", "semantic version like 1.0.0"),
    (_with(min_boss="latest"), "min_boss", "semantic version"),
    (_with(name=""), "name", "at least 1"),
    (_with(license=...), "license", "required"),
    (_with(colour="red"), "colour", "unknown field"),
    (_with(stacks={}), "stacks", "at least 1"),
    (_with(stacks={"ruby": {"frameworks": ["rails"], "min_toolchain": "ruby3"}}), "stacks.ruby", "must be one of node, python, java"),
    (_with(stacks={"node": {"frameworks": [], "min_toolchain": "node20"}}), "stacks.node.frameworks", ""),
    (_with(stacks={"node": {"frameworks": ["express"]}}), "stacks.node.min_toolchain", "required"),
    (_with(tests={"node": {"command": "npm test", "report": "tap", "min_tests": 1}}), "tests.node.report", "junit"),
    (_with(tests={"node": {"command": "npm test", "report": "junit", "min_tests": 0}}), "tests.node.min_tests", "1"),
    (_with(secrets=[{"name": "stripe_key"}]), "secrets.0.name", "environment variable name"),
    (_with(secrets=[{"name": "KEY", "test_default": "random"}]), "secrets.0.test_default", "none"),
    (_with(secrets=[{"name": "KEY", "pattern": "^sk_(test"}]), "secrets.0.pattern", "regular expression"),
    (_with(secrets=[{"name": "KEY", "value": "sk_test_123"}]), "secrets.0.value", "unknown field"),
    (_with(egress=["*.stripe.com"]), "egress", "wildcards"),
    (_with(egress=["10.0.0.5"]), "egress", "IP address"),
    (_with(egress=["https://api.stripe.com/v1"]), "egress", "bare hostname"),
    (_with(egress=["localhost"]), "egress", "fully qualified"),
    (_with(egress=["db.internal"]), "egress", "local or reserved"),
    (_with(routes=["/api/checkout"]), "routes", "METHOD /path"),
    (_with(routes=["FETCH /x"]), "routes", "METHOD /path"),
    (_with(webhooks=[{"path": "webhooks/stripe", "provider": "stripe"}]), "webhooks.0.path", "absolute URL path"),
    (_with(guide="../../etc/passwd"), "guide", "relative path"),
    (_with(security_note="/SECURITY.md"), "security_note", "relative path"),
    (_with(requires=["Not An Id"]), "requires", "template id"),
    (_with(adds=["routes", "routes"]), "adds", "twice"),
])
def test_an_invalid_manifest_is_refused_with_a_clear_reason(data, field, words):
    reasons = _reasons(data)
    assert any(r.startswith(f"{field}") and words in r for r in reasons), reasons


def test_every_declared_stack_needs_a_test_command_and_no_test_is_orphaned():
    reasons = _reasons(_with(stacks={"node": MINIMAL["stacks"]["node"],
                                     "python": {"frameworks": ["fastapi"], "min_toolchain": "python311"}}))
    assert any("every declared stack needs a test command" in r and "python" in r for r in reasons), reasons
    reasons = _reasons(_with(tests={**MINIMAL["tests"],
                                    "java": {"command": "mvn test", "report": "junit", "min_tests": 1}}))
    assert any("not declared under stacks" in r for r in reasons), reasons


def test_cross_field_rules():
    assert any("cannot name itself" in r for r in _reasons(_with(requires=["login-oauth"])))
    assert any("listed twice" in r for r in _reasons(_with(secrets=[{"name": "K"}, {"name": "K"}])))
    assert any("also a secret" in r for r in _reasons(_with(secrets=[{"name": "K"}], env=[{"name": "K"}])))


def test_every_problem_is_reported_at_once():
    reasons = _reasons(_with(id="X", version="one", egress=["*.x.com"]))
    assert {r.split(":")[0] for r in reasons} >= {"id", "version", "egress"}


@pytest.mark.parametrize("source, words", [
    ("schema: [unclosed", "not valid YAML"),
    ("- a list\n- not a mapping\n", "must be a YAML mapping"),
    ("", "must be a YAML mapping"),
])
def test_a_manifest_that_is_not_a_mapping(source, words):
    assert any(words in r for r in _reasons(source))


def test_the_error_message_lists_the_reasons():
    with pytest.raises(ManifestError) as caught:
        validate_manifest(_with(id="X"))
    assert "invalid boss-template.yaml" in str(caught.value) and "id:" in str(caught.value)
