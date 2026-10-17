"""A deploy asks the machine for every program its manifest runs, before anything is written.

The laptop's HelpDesk deploy (sdep_f145247613894c0bbff9) ran prepare, upload, environment and
switch on a Docker host and only then found `bash: line 1: npm: command not found` at its
install step. The preflight now asks — read-only, in the same SSH session — for each program a
`kind: commands` manifest runs and for the runtimes the machine has, and refuses the plan in
words when one is missing. A failure also says whose fix it is: the app's (a repair build can
fix it) or the host's (a person's).
"""
from __future__ import annotations

import subprocess

from boss_engine.services.app_deploy_manifest import load_manifest
from boss_engine.services.stack_deploy import (
    CAUSE_APP,
    CAUSE_HOST,
    HostAccess,
    command_executables,
    explain_failure,
    host_facts_from,
    manifest_executables,
    preflight_host,
)

KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----"
HELPDESK = """
version: 1
name: helpdesk-ai
kind: commands
install: ["npm install --no-audit --no-fund --loglevel=error", "npm start &"]
health: [{url: "{target.public_url}", expect: 200}]
"""


class DockerHost:
    """A machine with Docker and no Node, answering the preflight probe as a shell would."""

    def __init__(self, *, lacks=("node", "npm", "python3")):
        self.calls: list[dict] = []
        self.lacks = set(lacks)

    def stack_exec(self, **kwargs):
        self.calls.append(kwargs)
        assert kwargs["write"] is False, "a preflight must never write"
        command = kwargs["command"]
        out = ("BOSS_SSH_OK\nDIR_CREATABLE\nSUDO_OK\nDOCKER_PRESENT\nDOCKER_USABLE\n"
               "TAR_OK\nCURL_OK\n")
        for tool in ("docker", "node", "npm", "python3", "git"):
            if tool in command:
                out += f"BOSS_{'LACKS' if tool in self.lacks else 'HAS'} {tool}\n"
        out += "BOSS_VERSION docker Docker version 27.1.1, build 6312585\n"
        return subprocess.CompletedProcess([], 0, out, "")


def _access(host):
    return HostAccess("2.29.2.202", "deploy", 22, KEY, host)


def test_a_commands_manifest_running_a_program_the_machine_lacks_is_refused_read_only():
    host = DockerHost()
    problems = preflight_host(_access(host), load_manifest(HELPDESK))

    assert len(host.calls) == 1, "one read-only session asks everything"
    assert "command -v" in host.calls[0]["command"] and "npm" in host.calls[0]["command"]
    missing = [p for p in problems if p.get("missing_tool")]
    assert [p["missing_tool"] for p in missing] == ["npm"]
    said = missing[0]["headline"] + " " + missing[0]["action"]
    assert "has no `npm`" in said and "it has: docker" in said
    assert "runs commands on the machine directly" in said
    assert "kind: compose" in said
    assert missing[0]["cause"] == CAUSE_APP
    # What the machine has travels with the verdict, for the build and the deploy agent.
    assert problems.facts["tools"]["docker"] is True
    assert problems.facts["tools"]["npm"] is False
    assert problems.facts["versions"]["docker"].startswith("Docker version 27.1.1")


def test_a_host_that_has_the_program_passes_and_still_reports_its_facts():
    problems = preflight_host(_access(DockerHost(lacks=())), load_manifest(HELPDESK))
    assert list(problems) == []
    assert problems.facts["tools"]["npm"] is True


def test_a_program_the_probe_did_not_mention_is_unknown_never_missing():
    facts = host_facts_from("BOSS_SSH_OK\nDOCKER_PRESENT\nBOSS_HAS git\n")
    assert facts["tools"] == {"git": True, "docker": True}
    assert "npm" not in facts["tools"]


def test_the_programs_a_command_runs_are_read_the_way_a_shell_runs_them():
    assert command_executables("npm install --no-audit") == ["npm"]
    assert command_executables("npm start &") == ["npm"]
    assert command_executables("PORT=1 nohup node server.js > log 2>&1 &") == ["node"]
    assert command_executables("cd app && sudo -E npm ci") == ["npm"]
    assert command_executables("timeout 30 python3 -m app") == ["python3"]
    assert command_executables("if [ -f x ]; then go run .; fi") == ["go"]
    # The app's own files, a check for a program, and a wrapper hiding its program are not
    # judged — never guessed at.
    assert command_executables("./install.sh") == []
    assert command_executables("command -v node || exit 1") == []
    assert command_executables("sudo -u bob npm ci") == []
    assert command_executables("echo 'unterminated") == []


def test_nothing_after_a_step_that_installs_software_is_refused():
    manifest = load_manifest(
        "version: 1\nname: app\ninstall: ['curl -fsSL https://deb.nodesource.com/setup_20.x "
        "| sudo bash', 'npm ci']\n")
    assert [t for t, _ in manifest_executables(manifest)] == ["curl", "bash"], \
        "npm is installed by the step before it, so it is not asked for"
    compose = load_manifest("version: 1\nname: app\nkind: compose\n"
                            "compose: {file: docker-compose.yml, project: app}\n")
    assert manifest_executables(compose) == [], "compose runs docker, asked by requires.docker"


def test_an_unreachable_host_is_a_persons_problem():
    class Down:
        def stack_exec(self, **kwargs):
            return subprocess.CompletedProcess(
                [], 255, "", "ssh: connect to host 2.29.2.202 port 22: Connection refused\n")

    problems = preflight_host(_access(Down()), load_manifest(HELPDESK))
    assert problems and problems[0]["cause"] == CAUSE_HOST
    assert "could not connect" in problems[0]["headline"]


def test_a_failure_says_whose_fix_it_is():
    npm = explain_failure("install: install 1", 127, "bash: line 1: npm: command not found\n",
                          host="deploy@2.29.2.202")
    assert npm["cause"] == CAUSE_APP and "`npm`" in npm["headline"]
    assert "kind: compose" in npm["action"]
    assert explain_failure("health", 1, "BOSS_UNHEALTHY 502", url="http://x")["cause"] \
        == CAUSE_APP
    assert explain_failure("install: x", 1, "npm ERR! missing script: start")["cause"] \
        == CAUSE_APP
    # The app's own database refusing it is the app's error, not BOSS's connection…
    assert explain_failure("install: x", 1, "ECONNREFUSED: connection refused")["cause"] \
        == CAUSE_APP
    # …while ssh's own exit, the machine's disk, key, Docker daemon and BOSS's plumbing are not.
    for step, code, output in (
            ("install: x", 255, "ssh: connect to host h port 22: Connection refused"),
            ("install: x", 1, "write /var/lib/docker: no space left on device"),
            ("prepare", 255, "Permission denied (publickey)."),
            ("install: x", 1, "permission denied while trying to connect to the docker daemon"),
            ("upload", 1, "tar: broken pipe")):
        assert explain_failure(step, code, output)["cause"] == CAUSE_HOST, (step, output)
