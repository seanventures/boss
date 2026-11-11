"""Demo users are seeded on rehearsal targets, never on production (2026-11-13 chain proof).

The helpdesk app's seed script gates itself on ALLOW_DEMO_SEED=true — exactly what the platform's
own coder checklist and deploy hint tell an app to do — and nothing ever set it: the preview and
the staging deploy ran without it, Settings revealed a Preview DEMO_PASSWORD, and
demo-agent@helpdesk.local answered "invalid credentials". The password existed; the users did not.

The contract is DECLARED, never a match on the app's name: an app that declares a demo login
password (DEMO_PASSWORD and its kin, run_env kind "demo") or names the flag itself is declaring
demo seeding. BOSS turns it on for throwaway runs (preview, proof, smoke) and staging-kind deploy
targets only; a production-kind target never carries it, whatever the repository's manifest says —
only a person's own deploy value may.
"""
from pathlib import Path

import pytest

from boss_engine.templates.run_env import (
    DEMO_SEED_FLAG,
    declares_demo_seeding,
    demo_seed_target,
    needs_from_app_dir,
    resolve,
    with_demo_seed,
)

#: The generated app's shape: a demo login is declared, the opt-in flag is NOT — exactly what
#: the chain-proof build wrote, and what left the seed switched off everywhere.
MANIFEST = """version: 1
name: helpdesk-ai
kind: compose
compose:
  file: docker-compose.yml
env:
  DEMO_PASSWORD: {generate: password}
  APP_ENV: staging
"""


def _app(tmp_path: Path, manifest: str = MANIFEST) -> Path:
    (tmp_path / "boss.deploy.yaml").write_text(manifest)
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    return tmp_path


class TestAThrowawayRunSeeds:
    @pytest.mark.parametrize("purpose", ["preview", "proof", "smoke"])
    def test_a_declaring_apps_throwaway_run_gets_the_flag(self, tmp_path, purpose):
        env = resolve(needs_from_app_dir(_app(tmp_path)), purpose=purpose)
        assert env.values[DEMO_SEED_FLAG] == "true"
        assert DEMO_SEED_FLAG in env.supplied
        # The password the flag's seeded users sign in with is still minted and shown.
        assert env.values["DEMO_PASSWORD"] and "DEMO_PASSWORD" in env.shown

    def test_the_flag_alone_is_also_a_declaration(self, tmp_path):
        (tmp_path / ".env.example").write_text("ALLOW_DEMO_SEED=\n")
        env = resolve(needs_from_app_dir(tmp_path), purpose="preview")
        assert env.values[DEMO_SEED_FLAG] == "true"

    def test_an_app_declaring_no_demo_seeding_gets_nothing(self, tmp_path):
        (tmp_path / ".env.example").write_text("SESSION_SECRET=\nSOME_FLAG=\n")
        env = resolve(needs_from_app_dir(tmp_path), purpose="preview")
        assert DEMO_SEED_FLAG not in env.values

    def test_the_apps_own_literal_is_respected(self, tmp_path):
        off = MANIFEST + '  ALLOW_DEMO_SEED: "false"\n'
        env = resolve(needs_from_app_dir(_app(tmp_path, off)), purpose="preview")
        assert env.values[DEMO_SEED_FLAG] == "false"

    def test_a_bound_value_is_respected(self, tmp_path):
        env = resolve(needs_from_app_dir(_app(tmp_path)), purpose="preview",
                      bound={DEMO_SEED_FLAG: "false"})
        assert env.values[DEMO_SEED_FLAG] == "false"

    def test_a_deploy_is_never_decided_here(self, tmp_path):
        """The deploy's answer is the target's kind (with_demo_seed), which resolve cannot know."""
        env = resolve(needs_from_app_dir(_app(tmp_path)), purpose="deploy")
        assert DEMO_SEED_FLAG not in env.values


class TestADeployFollowsTheTargetsKind:
    #: What a staging stack deploy composes for the declaring app: the per-target generated
    #: demo password, and no flag anywhere (the manifest never named it).
    STAGING_ENV = {"DEMO_PASSWORD": "x" * 16, "APP_ENV": "staging"}

    def test_a_staging_target_carries_the_flag(self):
        env = with_demo_seed(self.STAGING_ENV, environment="staging")
        assert env[DEMO_SEED_FLAG] == "true"

    def test_a_production_target_never_does(self):
        env = with_demo_seed(self.STAGING_ENV, environment="production")
        assert DEMO_SEED_FLAG not in env

    def test_production_drops_even_the_manifests_own_literal(self):
        """The repository does not get to seed demo users into production."""
        env = with_demo_seed({**self.STAGING_ENV, DEMO_SEED_FLAG: "true"},
                             environment="production")
        assert DEMO_SEED_FLAG not in env

    def test_a_persons_own_deploy_value_survives_production(self):
        """A person's deliberate act is theirs; only the platform's and the repo's are policed."""
        env = with_demo_seed({**self.STAGING_ENV, DEMO_SEED_FLAG: "true"},
                             environment="production", person_set={DEMO_SEED_FLAG})
        assert env[DEMO_SEED_FLAG] == "true"

    @pytest.mark.parametrize("environment", ["", None, "customer-a", "live", "prod"])
    def test_an_unknown_or_unnamed_environment_counts_as_production(self, environment):
        assert not demo_seed_target(environment)
        assert DEMO_SEED_FLAG not in with_demo_seed(self.STAGING_ENV, environment=environment)

    def test_a_target_with_no_demo_login_gets_nothing(self):
        env = with_demo_seed({"APP_ENV": "staging"}, environment="staging")
        assert DEMO_SEED_FLAG not in env

    def test_a_database_password_is_not_a_demo_login(self):
        env = with_demo_seed({"POSTGRES_PASSWORD": "x" * 16}, environment="staging")
        assert DEMO_SEED_FLAG not in env

    def test_the_apps_own_setting_is_kept_on_staging(self):
        env = with_demo_seed({**self.STAGING_ENV, DEMO_SEED_FLAG: "false"},
                             environment="staging")
        assert env[DEMO_SEED_FLAG] == "false"


def test_the_contract_is_declared_not_a_name_match(tmp_path):
    declared = needs_from_app_dir(_app(tmp_path))
    assert declares_demo_seeding(declared)
    (tmp_path / "boss.deploy.yaml").write_text("version: 1\nname: helpdesk-ai\nkind: compose\n"
                                               "compose: {file: docker-compose.yml}\n")
    assert not declares_demo_seeding(needs_from_app_dir(tmp_path))
