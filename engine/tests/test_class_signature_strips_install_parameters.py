"""THE UPSTREAM CLASS SIGNATURE carries no install parameter (BOSSAPP-571 F13, spec §7.4): the
port, a service this install named, a path — gone; BOSS's own resource names and the code the fix
lives in — kept. Two installs that hit one bug search for, and are counted under, one string."""
from __future__ import annotations

import pytest

from boss_engine.services import self_fix as sf
from boss_engine.services import self_fix_upstream as up


@pytest.mark.parametrize("local, klass", [
    ("deploy:port_held:8000", "deploy:port_held"),
    ("deploy:port_held:3000", "deploy:port_held"),
    ("deploy:unwired:res-db", "deploy:unwired:res-db"),
    ("deploy:unwired:payroll-cache", "deploy:unwired"),
    ("build:boss:3fa9c0e1", "build:boss:3fa9c0e1"),
])
def test_the_cause_without_this_installs_parameters(local, klass):
    assert sf.class_signature(local) == klass


def test_the_location_is_the_module_and_function_never_a_path():
    assert up.location_key("engine/boss_engine/services/stack_deploy.py::plan_stack_deploy") \
        == "stack_deploy.plan_stack_deploy"
    assert up.location_key("/opt/boss/engine/x/app_start.py::Starter.run") == "app_start.run"
    assert up.location_key("") == ""


def test_the_upstream_class_joins_them():
    sfx = {"class_signature": "deploy:port_held",
           "location": "engine/boss_engine/services/stack_deploy.py::plan_stack_deploy"}
    assert up.upstream_class(sfx) == "deploy:port_held@stack_deploy.plan_stack_deploy"
    assert up.upstream_class({"class_signature": "deploy:port_held"}) == "deploy:port_held"
    # It passes the allow-list the collector applies (SELF_FIX.class_signature).
    from boss_engine.services import improve_allowlist as allow

    assert allow._CLASS_SIGNATURE.fullmatch(up.upstream_class(sfx))
