"""An app's deploy settings reach its stack the same way they reach a single container.

HelpDesk seeds its demo logins only when DEMO_PASSWORD and ALLOW_DEMO_SEED are set. No
template declares them, so the app's Setup refused them; and the stack deploy passed none of the
app's deploy secrets anyway, so even a declared one never reached .boss/deploy.env.

  * secrets.deploy_value_spec: a person may set a value no template declares, for DEPLOY only,
    as a plain value — never for build/preview, never as a live key, never a BOSS_ name.
  * stack_deploy.merge_app_env: the app's deploy values (the one reader both paths use,
    templates.base_url.deploy_environment) join the manifest's; a person's value beats a literal
    from the repository, never a key the manifest generates (the host was initialised with it);
    every value but the app's own URL is redacted.
"""
from __future__ import annotations

import inspect

import pytest

from boss_engine.services import stack_deploy
from boss_engine.services.app_deploy_manifest import load_manifest
from boss_engine.services.stack_deploy import merge_app_env
from boss_engine.templates.secrets import deploy_value_spec
from boss_engine.templates.service import TemplateError


def test_a_setting_no_template_declares_is_accepted_for_deploy_only():
    assert deploy_value_spec("DEMO_PASSWORD", ["deploy"], "test").name == "DEMO_PASSWORD"
    assert deploy_value_spec("ALLOW_DEMO_SEED", ["deploy"], "test").pattern is None


@pytest.mark.parametrize("name,envs,mode,code", [
    ("DEMO_PASSWORD", ["build"], "test", "secret_not_declared"),
    ("DEMO_PASSWORD", ["deploy", "preview"], "test", "secret_not_declared"),
    ("DEMO_PASSWORD", ["deploy"], "live", "bad_mode"),
    ("BOSS_DEPLOY_DIR", ["deploy"], "test", "reserved_name"),
    ("demo password", ["deploy"], "test", "bad_name"),
])
def test_everything_else_is_refused_in_words(name, envs, mode, code):
    with pytest.raises(TemplateError) as refused:
        deploy_value_spec(name, envs, mode)
    assert refused.value.body["error"] == code and refused.value.body["message"]


MANIFEST = load_manifest("""version: 1
name: helpdesk-ai
kind: compose
compose: {file: docker-compose.yml}
env:
  POSTGRES_PASSWORD: {generate: password}
  LOG_LEVEL: info
""")


def test_the_apps_values_join_the_manifests_without_replacing_what_it_generated():
    values = {"POSTGRES_PASSWORD": "pg-made-once", "LOG_LEVEL": "info"}
    app_env = {"DEMO_PASSWORD": "demo-pw", "ALLOW_DEMO_SEED": "true", "LOG_LEVEL": "debug",
               "POSTGRES_PASSWORD": "would-lock-the-app-out", "SESSION_SECRET": "sess",
               "APP_BASE_URL": "http://192.0.2.202:8000"}
    merged, hidden = merge_app_env(MANIFEST, values, ["pg-made-once"], app_env)
    assert merged["POSTGRES_PASSWORD"] == "pg-made-once"
    assert merged["LOG_LEVEL"] == "debug"                       # a person's value beats a literal
    assert merged["DEMO_PASSWORD"] == "demo-pw" and merged["ALLOW_DEMO_SEED"] == "true"
    assert {"demo-pw", "sess", "pg-made-once"} <= set(hidden)
    assert "http://192.0.2.202:8000" not in hidden


def test_both_paths_read_the_apps_deploy_environment_through_one_function():
    """The stack's apply reads deploy_environment (the host path's reader) and its plan checks
    app_secret_problem (the host path's check), names on the decision."""
    assert "deploy_environment(" in inspect.getsource(stack_deploy.app_deploy_env)
    apply_src = inspect.getsource(stack_deploy.apply_on_host)
    assert "app_deploy_env(" in apply_src and "merge_app_env(" in apply_src
    plan_src = inspect.getsource(stack_deploy.plan_stack_deploy)
    assert "app_secret_problem(" in plan_src and '"app_secrets": app_secret_names' in plan_src
