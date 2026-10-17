"""A COMPOSE APP SHARES ONE PROJECT WITH THE RESOURCES BOSS PROVISIONED FOR IT.

HelpDesk AI's first compose deploy (sdep_3521765ccd404540ba1d, 17 Oct): Postgres came up healthy
as `res-db` in project bossapp-helpdesk-ai-deploy, the app ran as project `helpdesk-ai` on a
network of its own and exited with `getaddrinfo EAI_AGAIN res-db`; the health check then said
only "did not answer within 600s", and a repair build was sent to fix an app that was fine.
"""
from types import SimpleNamespace

from boss_engine.infra_plugins._runtime import compose_project_for
from boss_engine.services import stack_deploy as sd


def _rendered(project="helpdesk-ai"):
    base = f"docker compose -f docker-compose.yml -p {project} --env-file {{dir}}/.boss/deploy.env"
    return {"install": [{"name": "compose up", "run": f"{base} up -d --build"}],
            "update": [{"name": "compose up", "run": f"{base} up -d --build"}],
            "teardown": [{"name": "compose down", "run": f"{base} down"}],
            "health": [], "env_secrets": {}}


def test_the_app_runs_in_the_resources_project():
    shared = compose_project_for("helpdesk ai", "deploy")
    assert shared == "bossapp-helpdesk-ai-deploy"  # where res-db really lives
    out = sd.join_resource_project(_rendered(), "helpdesk-ai", shared)
    for key in ("install", "update", "teardown"):
        assert all(f" -p {shared} " in s["run"] and " -p helpdesk-ai " not in s["run"]
                   for s in out[key])


def test_nothing_changes_without_resources():
    before = _rendered()
    assert sd.join_resource_project(before, "helpdesk-ai", None) == before


def test_an_unreachable_boss_service_is_bosss_to_fix_not_the_apps():
    tail = ("BOSS_UNHEALTHY 000\n--- helpdesk-ai-app-1 (last lines) ---\n"
            "[helpdesk-ai] failed to start: Error: getaddrinfo EAI_AGAIN res-db\n")
    said = sd.explain_failure("health", 1, tail, host="2.29.2.202", url="http://x/readyz",
                              timeout=600)
    assert said["cause"] == sd.CAUSE_HOST and "res-db" in said["headline"]
    # A health check failing for the app's own reason is still the app's.
    assert sd.explain_failure("health", 1, "BOSS_UNHEALTHY 500", host="h", url="u",
                              timeout=600)["cause"] == sd.CAUSE_APP


def test_a_failed_health_check_collects_what_the_containers_said():
    seen = []

    class Access:
        host = "2.29.2.202"

        def run(self, command, timeout=0, write=False):
            seen.append((command, write))
            return SimpleNamespace(returncode=0, output=(
                "bossapp-helpdesk-ai-deploy-app-1 Exited (1)\n--- bossapp-helpdesk-ai-deploy-app-1 "
                "(last lines) ---\n[helpdesk-ai] failed to start: Error: getaddrinfo EAI_AGAIN res-db\n"))

    runner = SimpleNamespace(rendered=sd.join_resource_project(
        _rendered(), "helpdesk-ai", "bossapp-helpdesk-ai-deploy"), access=Access(),
        redact_values=[])
    out = sd.StackRunner._compose_state(runner) if hasattr(sd, "StackRunner") else None
    if out is None:  # the runner class's own name
        cls = next(c for c in vars(sd).values()
                   if isinstance(c, type) and hasattr(c, "_compose_state"))
        out = cls._compose_state(runner)
    assert "EAI_AGAIN res-db" in out
    assert seen and seen[0][1] is False  # read-only
    assert "com.docker.compose.project=bossapp-helpdesk-ai-deploy" in seen[0][0]
