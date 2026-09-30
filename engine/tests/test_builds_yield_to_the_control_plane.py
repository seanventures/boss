"""The control plane and the work it supervises share a container.

`npm install`, `vite build`, `tsc` and Playwright execute as child processes of the API server,
and they are among the most parallel workloads there are. Linux CFS shares CPU roughly equally
per runnable thread at equal priority, so the API's few threads compete with dozens of build
processes. The API does not need more CPU — it cannot get SCHEDULED.

Measured on goldeneye:

    /v1/code-tasks?limit=5   quiet box          0.37s  (p50, n=6)
    /v1/code-tasks?limit=5   code task running  47.1s, then 39.5s, then a Traefik 404

Ruled out by measurement, not argument: the console's own ~90-call server-render burst. With no
job running it moves the same query 0.37s -> 0.78s (max 1.18s) and produces NO 404s. It cannot
account for 47s.

`preview_service._low_priority` already carried this exact diagnosis, in these exact words, and
was applied to preview builds ONLY — the two heavier, longer-running consumers got nothing.
That is S3: one rule understood in one place out of three.
"""

import inspect
import re
from pathlib import Path

from boss_engine.services import code_agent, preview_service, sandbox
from boss_engine.services.scheduling import (
    NICE_LEVEL,
    is_low_priority,
    low_priority,
)


def test_it_lowers_priority_rather_than_changing_the_command():
    """nice is scheduling WEIGHT under contention. It must not reorder or drop anything."""
    assert low_priority(["npm", "run", "build"])[-3:] == ["npm", "run", "build"]


def test_the_level_actually_yields():
    """nice 19 is ~1/68th the weight of a normal process. Anything milder does not preempt."""
    assert int(NICE_LEVEL) >= 15


def test_an_empty_command_is_left_alone():
    assert low_priority([]) == []


def test_double_wrapping_is_detectable():
    assert is_low_priority(low_priority(["npm"])) is True
    assert is_low_priority(["npm"]) is False


def test_it_degrades_when_ionice_is_missing(monkeypatch):
    """ionice is absent on macOS and some slim images. A missing tool must never stop the work —
    an unniced build is slow for the API; a failed build is a broken pipeline."""
    monkeypatch.setattr("boss_engine.services.scheduling.shutil.which", lambda _n: None)
    out = low_priority(["npm", "test"])
    assert out[:3] == ["nice", "-n", NICE_LEVEL]
    assert "ionice" not in out


# --------------------------------------------------------------------------- #
# The shape: every in-container build must yield, not just the one that surfaced
# --------------------------------------------------------------------------- #
def test_the_code_agent_yields():
    assert "low_priority(cmd)" in inspect.getsource(code_agent)


def test_the_sandbox_yields():
    assert "low_priority(argv)" in inspect.getsource(sandbox)


def test_preview_builds_still_yield():
    assert "low_priority(command)" in inspect.getsource(preview_service._low_priority)


def test_there_is_exactly_one_definition_of_the_rule():
    """Three copies of a rule is how two of them go stale. preview_service kept its NAME (the
    preview paths call it) but must not keep its own implementation."""
    root = Path("engine/boss_engine/services")
    definers = [
        p.name for p in root.glob("*.py")
        if re.search(r'prefix\s*=\s*\["nice"', p.read_text())
    ]
    assert definers == ["scheduling.py"], f"more than one place builds the nice prefix: {definers}"


#: Modules whose subprocess is an `ssh` to another machine. No local CPU is consumed, so the
#: scheduling rule does not apply to them.
_REMOTE_ONLY = {"deploy_service.py", "ssh_host_adapter.py"}


def test_the_exclusions_really_are_remote(): 
    """An exclusion list is how a rule quietly stops applying. Each entry must actually ssh."""
    root = Path("engine/boss_engine/services")
    for name in _REMOTE_ONLY:
        src = (root / name).read_text()
        assert '"ssh"' in src or "'ssh'" in src, f"{name} is excluded but does not ssh"


def test_no_in_container_build_launches_at_normal_priority():
    """The guard that would have caught the original miss.

    Any subprocess that runs a package manager, a bundler or a browser driver inside the API's
    container must be wrapped. Checked over the source so a NEW launch site is covered too.
    """
    root = Path("engine/boss_engine/services")
    offenders = []
    for path in root.glob("*.py"):
        src = path.read_text()
        for m in re.finditer(r"subprocess\.(run|Popen)\(\s*\n?\s*([A-Za-z_][A-Za-z0-9_]*)", src):
            arg = m.group(2)
            if arg in ("low_priority", "_low_priority"):
                continue
            line = src[: m.start()].count("\n") + 1
            # argv-carrying variables are the build launches; literal lists (e.g. ["git", ...])
            # are cheap metadata calls and are deliberately out of scope.
            #
            # EXCLUDED, with reason: an `ssh` to a remote host consumes essentially no LOCAL
            # cpu — it is network wait. Niceing it would be cargo cult, and pretending it
            # belongs here would make the rule look arbitrary to the next reader.
            if arg in ("cmd", "argv", "command") and path.name not in _REMOTE_ONLY:
                offenders.append(f"{path.name}:{line} launches {arg!r} at normal priority")
    assert not offenders, (
        "these run a build in the API's own container without yielding to it:\n  "
        + "\n  ".join(offenders))
