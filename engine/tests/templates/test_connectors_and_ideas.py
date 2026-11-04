"""The catalogue is CONNECTORS and IDEAS, not templates (operator, 2026-10-13).

"These are not templates, these are connectors: login, payments, checkout can be added to any
application … it has to be customized to their application ui … Hello endpoint, product catalog,
banking starter can be examples." A connector is added to any app and built in that app's look;
an idea is an app to start from; the insecure banking starter is a training lab, never offered
unless asked for.
"""
from __future__ import annotations

from boss_engine.templates import service
from boss_engine.templates.manifest import kind_of

EXPECTED = {
    "login-oauth": "connector", "stripe-checkout": "connector", "paypal-checkout": "connector",
    "transactional-email": "connector",
    "banking-starter": "idea", "catalog-cart": "idea", "hello": "idea",
    # BOSSAPP-810: the sample project (New project › Try a sample) builds from it.
    "hello-world": "idea",
    "banking-starter-insecure": "training",
}


def test_every_built_in_says_what_it_is():
    seen = {}
    for name, report, _ in service.builtins():
        assert report.ok, (name, report.reasons)
        seen[report.manifest.id] = report.manifest.item_kind
    by_dir = {name: report.manifest.item_kind for name, report, _ in service.builtins()}
    assert by_dir == EXPECTED, by_dir
    assert set(seen.values()) == {"connector", "idea", "training"}


def test_an_older_import_without_a_kind_is_read_safely():
    # Stored rows from before the field: a capability by default, and an insecure-by-design
    # item is ALWAYS training whatever it declares, so a lab app is never offered as an idea.
    assert kind_of({"labels": []}) == "connector"
    assert kind_of({"labels": [], "kind": "idea"}) == "idea"
    assert kind_of({"labels": ["insecure_by_design"], "kind": "idea"}) == "training"
    assert kind_of({"labels": [], "kind": "bogus"}) == "connector"
    assert kind_of(None) == "connector"


def test_a_card_carries_its_kind_and_training_is_listed_only_when_asked(monkeypatch):
    rows = []
    for _, report, _ in service.builtins():
        m = report.manifest.model_dump(mode="json", by_alias=True)
        rows.append({"id": f"tpl_{m['id']}", "template_key": m["id"], "version": m["version"],
                     "source": "builtin", "status": "active", "manifest": m, "trial": {},
                     "digest": report.digest, "git_url": None, "commit_sha": None,
                     "imported_by": None, "approved_by": None, "created_at": None,
                     "approved_at": None})

    class _Rows:
        def mappings(self):
            return self

        def all(self):
            return rows

    class _Conn:
        def execute(self, *_a, **_k):
            return _Rows()

    monkeypatch.setattr(service, "sync_builtins", lambda *_a, **_k: 0)
    monkeypatch.setattr(service, "ensure_builtins", lambda *_a, **_k: None)
    everything = service.list_templates(_Conn(), "org")
    assert {c["kind"] for c in everything} == {"connector", "idea"}  # no training by default
    connectors = service.list_templates(_Conn(), "org", kind="connector")
    assert sorted(c["template_key"] for c in connectors) == ["login-oauth", "paypal-checkout",
                                                             "stripe-checkout",
                                                             "transactional-email"]
    ideas = service.list_templates(_Conn(), "org", kind="idea")
    assert sorted(c["template_key"] for c in ideas) == ["banking-starter", "catalog-cart", "hello",
                                                                 "hello-world"]
    training = service.list_templates(_Conn(), "org", kind="training")
    assert [c["template_key"] for c in training] == ["banking-starter-insecure"]
