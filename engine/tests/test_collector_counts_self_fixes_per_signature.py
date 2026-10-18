"""THE BOSS TEAM COUNTS INCOMING SELF-FIXES PER SIGNATURE (BOSSAPP-571 R17, spec §7.7): a
`self_fix` share passes its own allow-list (the facts and the patch — code kept verbatim, dropped
whole if it holds a credential), and the collector dashboard ranks causes by how many installs
hit them, with the verified rate, versions and engines."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import improve_allowlist as allow
from boss_engine.services import improve_collector as collector

from .test_workflow_capability_runner import eng  # noqa: F401

PATCH = "diff --git a/x.py b/x.py\n+ host = '203.0.113.10'  # 0.0.0.0:8000\n"


def _share(install: str, klass: str, *, verified: bool = True, patch: str = PATCH) -> dict:
    return {"v": 1, "kind": "self_fix", "install_id": install, "secret_hash": "0" * 64,
            "class_signature": klass, "area": "deploy", "engine": "claude",
            "verified_local": verified, "tests": 1, "tier": 3, "upstream_ref": "3fa9c0e1b2d4",
            "title": "fix(deploy): retire", "patch": patch, "evidence": "symptom",
            "context": {"version": "1.0.0-rc.10.7", "edition": "community"},
            "unexpected": "dropped"}


def _install() -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz234567"
    return "bi_" + "".join(alphabet[b % 32] for b in uuid.uuid4().bytes + uuid.uuid4().bytes[:10])


def test_the_allow_list_keeps_the_patch_verbatim_and_drops_the_rest():
    clean, dropped = allow.sanitize(_share(_install(), "deploy:port_held@stack_deploy.plan"),
                                    allow.SELF_FIX)
    assert clean["patch"] == PATCH, "code is never masked: documentation addresses stay"
    assert clean["class_signature"] == "deploy:port_held@stack_deploy.plan"
    assert "unexpected" not in clean and dropped >= 1
    secret = PATCH + "+ token = 'ghp_" + "a1B2" * 9 + "'\n"
    assert "patch" not in allow.sanitize(_share(_install(), "deploy:port_held",
                                                patch=secret), allow.SELF_FIX)[0]


def test_shares_are_counted_per_signature_installs_first(eng, monkeypatch):  # noqa: F811
    monkeypatch.setattr(collector.IP_LIMIT, "allow", lambda *a, **k: True)
    monkeypatch.setattr(collector.INSTALL_LIMIT, "allow", lambda *a, **k: True)
    tag = uuid.uuid4().hex[:6]
    hot, cold = f"deploy:port_held@stack_deploy.p{tag}", f"build:boss:{tag}"
    installs = [_install() for _ in range(3)]
    try:
        with eng.begin() as c:
            for i in installs:
                collector.share(c, _share(i, hot, verified=i != installs[2]))
            collector.share(c, _share(installs[0], cold))
            counts = {r["signature"]: r for r in collector.self_fix_counts(c, limit=500)}
        assert counts[hot]["installs"] == 3 and counts[hot]["verified"] == 2
        assert counts[hot]["verified_rate"] == round(2 / 3, 3)
        assert counts[hot]["engines"] == ["claude"] and counts[hot]["versions"] == [
            "1.0.0-rc.10.7"]
        assert counts[cold]["installs"] == 1
        with eng.begin() as c:
            ranked = [r["signature"] for r in collector.self_fix_counts(c, limit=500)
                      if r["signature"] in (hot, cold)]
        assert ranked == [hot, cold]
        with pytest.raises(collector.CollectorError):
            with eng.begin() as c:
                collector.share(c, {**_share(_install(), hot), "patch": ""})
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM tm_shares WHERE install_id = ANY(:i)"), {"i": installs})
