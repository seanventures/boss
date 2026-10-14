"""SEVERAL CONNECTORS IN ONE BUILD (BOSSAPP-526, templates.compose), without a database.

Login + Email (or Login + Stripe) compose: one order, one merged manifest (secrets, env, egress,
routes), one brief that names every connector and tells the build to merge the app's shell, and a
wiring smoke check that probes every connector's routes. Stripe + PayPal do NOT compose as they
are (both serve GET /api/orders/:id and both own payments/store.js): every conflict is listed,
never resolved by one connector silently overwriting the other.
"""
from __future__ import annotations

import pytest

from boss_engine.templates import compose, connector_policy
from boss_engine.templates.gate import connector_smoke, probe_paths
from boss_engine.templates.manifest import validate_manifest
from boss_engine.templates.service import TemplateError
from boss_engine.templates.validator import tree_from_dir, validate_tree

from ._fixtures import MANIFEST, REPO_ROOT


def _connector(name):
    tree = tree_from_dir(REPO_ROOT / "templates" / "apps" / name)
    report = validate_tree(tree, source="builtin")
    assert report.ok, report.reasons
    return report.manifest, tree.files(), report.digest


def _members(*names):
    return [_connector(n)[:2] for n in names]


def _fake(mid, **over):
    m = {**MANIFEST, "id": mid, "labels": ["connector"], "secrets": [], **over}
    return validate_manifest(m)


# ---- order ---------------------------------------------------------------------------------------

def test_the_given_order_is_kept_when_nothing_requires_anything():
    a, b, c = _fake("aaa"), _fake("bbb"), _fake("ccc")
    assert [m.id for m in compose.order([c, a, b])] == ["ccc", "aaa", "bbb"]


def test_a_member_another_requires_is_wired_first():
    base = _fake("base-auth")
    needs = _fake("needs-auth", requires=["base-auth"])
    assert [m.id for m in compose.order([needs, base])] == ["base-auth", "needs-auth"]


def test_a_requires_cycle_is_refused_in_words():
    a = _fake("aaa", requires=["bbb"])
    b = _fake("bbb", requires=["aaa"])
    with pytest.raises(TemplateError) as exc:
        compose.order([a, b])
    assert exc.value.status == 422 and exc.value.body["error"] == "compose_cycle"
    assert "aaa -> bbb -> aaa" in exc.value.body["message"]


# ---- conflicts -----------------------------------------------------------------------------------

def test_login_and_email_and_stripe_compose_on_node():
    assert compose.conflicts(_members("login-oauth", "transactional-email", "stripe-checkout"), "node") == []


def test_stripe_and_paypal_conflict_visibly_on_routes_and_files():
    found = compose.conflicts(_members("stripe-checkout", "paypal-checkout"), "node")
    kinds = {f["kind"] for f in found}
    assert {"route", "file"} <= kinds
    whats = {f["what"] for f in found}
    assert "GET /api/orders/:id" in whats and "POST /api/orders/:id/refund" in whats
    assert "stacks/node/payments/store.js" in whats
    # The app's shell is a merge point, never a conflict.
    assert not whats & {"stacks/node/package.json", "stacks/node/server.js", "stacks/node/schema.sql"}
    assert all(f["connectors"] == ["stripe-checkout", "paypal-checkout"] for f in found)


def test_a_route_with_another_parameter_name_is_the_same_route():
    assert compose.route_shape("GET /api/orders/:id") == compose.route_shape("GET /api/orders/{order_id}")
    a = _fake("aaa", routes=["GET /api/orders/:id"])
    b = _fake("bbb", routes=["GET /api/orders/:order_id"])
    found = compose.conflicts([(a, {}), (b, {})], "node")
    assert found and found[0]["kind"] == "route"


def test_a_route_the_app_already_serves_from_another_template_conflicts():
    m, files = _members("transactional-email")[0]
    shape = compose.route_shape("GET /api/email/status")
    found = compose.conflicts([(m, files)], "node", {shape: "some-other"})
    assert found and "already served by some-other" in found[0]["what"]
    # The same connector applied before is not a conflict with itself.
    assert compose.conflicts([(m, files)], "node", {shape: "transactional-email"}) == []


def test_env_and_secret_conflicts_are_named():
    a = _fake("aaa", env=[{"name": "MODE", "default": "test"}],
              secrets=[{"name": "TOKEN", "test_default": "stub", "pattern": "^a_"}])
    b = _fake("bbb", env=[{"name": "MODE", "default": "live"}],
              secrets=[{"name": "TOKEN", "test_default": "stub", "pattern": "^b_"}])
    c = _fake("ccc", env=[{"name": "TOKEN", "default": "x"}])
    found = compose.conflicts([(a, {}), (b, {}), (c, {})], "node")
    whats = " | ".join(f["what"] for f in found)
    assert "MODE: default 'test' in aaa, 'live' in bbb" in whats
    assert "TOKEN: two different patterns" in whats
    assert "TOKEN is a secret in aaa and plain env in ccc" in whats


def test_an_empty_default_yields_to_a_real_one():
    """login-oauth says APP_BASE_URL=http://localhost:3000, stripe-checkout says '': not a clash."""
    login, stripe = _connector("login-oauth")[0], _connector("stripe-checkout")[0]
    env = {e["name"]: e["default"] for e in compose.merged_env([stripe, login])}
    assert env["APP_BASE_URL"] == "http://localhost:3000"


# ---- the merged manifest -------------------------------------------------------------------------

def test_the_merged_manifest_unions_every_declaration_in_order():
    login, email = _connector("login-oauth")[0], _connector("transactional-email")[0]
    merged = compose.composite_manifest([login, email], "node", "compose-0123456789abcdef")
    assert merged.item_kind == "connector" and compose.COMPOSITION_LABEL in merged.labels
    assert list(merged.stacks) == ["node"]
    assert list(merged.egress) == list(login.egress) + list(email.egress)
    assert set(merged.routes) == set(login.routes) | set(email.routes)
    names = [s.name for s in merged.secrets]
    assert names[: len(login.secrets)] == [s.name for s in login.secrets]
    assert {"RESEND_API_KEY", "SMTP_PASSWORD", "SESSION_SECRET"} <= set(names)
    assert len(names) == len(set(names))
    envs = [e.name for e in merged.env]
    assert envs.count("APP_BASE_URL") == 1 and "EMAIL_FROM" in envs


def test_a_secret_is_required_if_any_member_requires_it():
    a = _fake("aaa", secrets=[{"name": "TOKEN", "required": False, "test_default": "none"}])
    b = _fake("bbb", secrets=[{"name": "TOKEN", "required": True, "test_default": "generated"}])
    spec = compose.merged_secrets([a, b])[0]
    assert spec["required"] is True and spec["test_default"] == "generated"


def test_the_wiring_smoke_check_probes_every_connectors_routes():
    login, email = _connector("login-oauth")[0], _connector("transactional-email")[0]
    merged = compose.composite_manifest([login, email], "node", "compose-0123456789abcdef")
    paths = probe_paths(merged)
    assert "/auth/providers" in paths and "/api/email/status" in paths
    probes = {p: 200 for p in paths}
    probes["/api/email/status"] = 404
    ok, tt = connector_smoke(merged, {"proven": True, "route_probes": probes}, tests_passed=True)
    assert not ok and "/api/email/status" in tt["error"]   # one connector unwired fails the gate
    ok, _ = connector_smoke(merged, {"proven": True, "route_probes": {p: 200 for p in paths}},
                            tests_passed=True)
    assert ok


def test_the_composite_files_keep_each_connector_apart_and_leave_their_tests_out():
    members = _members("login-oauth", "transactional-email")
    files = compose.composite_files(members, "node")
    assert "stacks/node/login-oauth/auth/routes.mjs" in files
    assert "stacks/node/transactional-email/email/routes.mjs" in files
    assert "stacks/node/login-oauth/package.json" in files
    assert not any("/tests/" in p for p in files)
    brief = files["brief.md"].decode()
    assert brief.index("login-oauth") < brief.index("transactional-email")


def test_the_brief_names_every_connector_in_order_and_says_merge_never_overwrite():
    names = ("login-oauth", "transactional-email")
    loaded = [_connector(n) for n in names]
    merged = compose.composite_manifest([m for m, _f, _d in loaded], "node", "compose-0123456789abcdef")
    members = [({"source": "builtin", "digest": d}, m, f) for m, f, d in loaded]
    text = compose.build_directive(members=members, stack="node", framework=None, note="keep it small",
                                   app_name="Shop", merged=merged)
    assert text.index("1. \"Login with Google") < text.index("2. \"Email (SMTP or Resend)")
    assert connector_policy.POLICY in text and "COMPOSE, NEVER OVERWRITE" in text
    assert "keep it small" in text and "api.resend.com" in text and "accounts.google.com" in text
    assert "## Connector 1: Login" in text and "## Connector 2: Email" in text
    assert "UNTRUSTED" not in text
    # An imported member's material is wrapped; the built-in one beside it is not.
    members[1] = ({"source": "git", "digest": "x"}, members[1][1], members[1][2])
    text = compose.build_directive(members=members, stack="node", framework=None, note="",
                                   app_name=None, merged=merged)
    assert text.count("<<<BEGIN UNTRUSTED TEMPLATE MATERIAL>>>") == 1
    assert text.index("## Connector 1") < text.index("<<<BEGIN UNTRUSTED")


def test_the_receipt_dedupe_key_is_one_per_connector_per_build():
    a = compose.receipt_dedupe_key("wfr_1", "login-oauth")
    assert a == compose.receipt_dedupe_key("wfr_1", "login-oauth")
    assert a != compose.receipt_dedupe_key("wfr_1", "transactional-email")
    assert a != compose.receipt_dedupe_key("wfr_2", "login-oauth")
