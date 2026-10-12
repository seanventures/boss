"""Any role can read a deployed app's container logs, read-only, by naming the app.

Harbor was asked for HelpDesk's last 40 log lines on 192.0.2.202 and answered "I have no tool to
run docker logs". Two causes: the question took the one-call record answer, which has no
tools by design, and inspect_host needs a container name a compose stack only has on the host.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

from boss_engine.services.subject_answer import _SYSTEM, asks_for_a_live_read, wants_first_answer
from boss_engine.tools import builtin
from boss_engine.tools.builtin import app_logs_shell, build_default_registry, build_discuss_read_registry


def test_a_question_about_the_live_machine_goes_to_the_loop():
    found = SimpleNamespace(found=True)
    asked = ("Read-only check please: on 192.0.2.202, show the last 40 lines of the HelpDesk app "
             "container logs (docker logs) and tell me whether the demo seed ran")
    assert asks_for_a_live_read(asked)
    assert wants_first_answer(asked, found, None) is False
    # A question about the record itself still gets the quick answer.
    assert asks_for_a_live_read("why did this step fail?") is False


def test_the_record_answer_never_claims_a_missing_tool():
    assert "Never say BOSS lacks a tool" in _SYSTEM
    assert "Investigate further" in _SYSTEM


def test_the_logs_tool_is_in_every_roles_registry():
    assert "read_app_logs" in build_default_registry().names()
    assert "read_app_logs" in build_discuss_read_registry().names()


def test_a_compose_stack_is_found_by_its_project_label():
    cmd = app_logs_shell(compose_project="helpdesk-ai", container="helpdesk", tail=40)
    assert "label=com.docker.compose.project=helpdesk-ai" in cmd
    assert "docker logs --tail 40" in cmd and "{{.Names}}" in cmd


def test_a_single_container_is_found_by_its_exact_name():
    cmd = app_logs_shell(compose_project=None, container="helpdesk", tail=9999)
    assert "name=^helpdesk$" in cmd
    assert "--tail 400" in cmd       # bounded


def test_only_docker_ps_and_docker_logs_run():
    cmd = app_logs_shell(compose_project="x", container="y", tail=10)
    verbs = {w for w in cmd.replace("$(", " ").replace(";", " ").split() if w == "docker"}
    assert verbs == {"docker"}
    for word in ("rm", "exec", "stop", "kill", "restart"):
        assert f"docker {word}" not in cmd


def test_hosts_are_also_found_by_their_address():
    src = inspect.getsource(builtin)
    assert "(id = :h OR name = :h OR address = :h)" in src
