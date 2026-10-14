"""Every binary a coding engine declares (harness_registry.RuntimeBinary) is installed by
Dockerfile.community's runtime stage — the stage the API and the worker run.

A HelpDesk build pinned to claude_agent_sdk failed every stage that ran a command with
"Sandbox dependencies not available: socat not installed". bubblewrap had been added to the image
after the same failure a day earlier; socat had not, and nothing connected "the engine needs
this" to "the image has this". Now the engine declares it and this test reads the Dockerfile:
adding an engine requirement without the package fails here, naming the engine and the package.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from boss_engine.services import harness_registry as hr

REPO = Path(__file__).resolve().parents[2]


def _runtime_stage() -> str:
    text = (REPO / "Dockerfile.community").read_text(encoding="utf-8")
    stage = text.split(" AS python-runtime", 1)[1].split("\nFROM ", 1)[0]
    # One logical line per RUN: continuations joined, comment lines dropped.
    lines = [ln for ln in stage.splitlines() if not ln.lstrip().startswith("#")]
    return "\n".join(lines).replace("\\\n", " ")


def installed_by_image(stage: str) -> dict[str, set[str]]:
    """What the runtime stage installs: apt packages, global npm packages (version stripped),
    and the paths it COPYs in."""
    apt: set[str] = set()
    for group in re.findall(r"apt-get install ((?:-\S+\s+)*)([^&\n]+)", stage):
        apt.update(p for p in group[1].split() if not p.startswith("-"))
    npm: set[str] = set()
    for group in re.findall(r"npm install (?:-g|--global) ([^&\n]+)", stage):
        for package in group.split():
            if package.startswith("-"):
                continue
            scoped = package.startswith("@")
            npm.add(package.rsplit("@", 1)[0] if package.count("@") > scoped else package)
    copied = set(re.findall(r"^COPY\s.*?(\S+)\s*$", stage, re.M))
    return {"apt": apt, "npm": npm, "image": copied}


def missing_from_image(requirements: dict[str, tuple[hr.RuntimeBinary, ...]],
                       stage: str) -> list[str]:
    have = installed_by_image(stage)
    out = []
    for engine, needs in requirements.items():
        for need in needs:
            if need.via == "operator":
                continue
            if need.package not in have[need.via]:
                out.append(f"engine {engine} needs `{need.binary}` ({need.why or 'runtime'}): "
                           f"{need.via} package {need.package!r} is not installed by "
                           "Dockerfile.community's runtime stage")
    return out


def test_every_engine_binary_is_in_the_image():
    missing = missing_from_image(hr.all_runtime_requirements(), _runtime_stage())
    assert not missing, "\n".join(missing)


def test_no_engine_needs_the_claude_cli_sandbox():
    """The SDK engine no longer forces the Claude CLI's Linux sandbox (its session holds a
    model-proxy run token, not the key), so no engine declares bubblewrap or socat — and the
    image does not carry them for nobody."""
    for engine, needs in hr.all_runtime_requirements().items():
        assert not {"bwrap", "socat"} & {r.binary for r in needs}, engine
    have = installed_by_image(_runtime_stage())["apt"]
    assert not {"bubblewrap", "socat"} & have


def test_the_check_names_the_engine_and_the_package():
    stage = "RUN apt-get install -y --no-install-recommends curl bubblewrap \\\n && true"
    needs = {"claude_agent_sdk": (hr.RuntimeBinary("bwrap", "bubblewrap"),
                                  hr.RuntimeBinary("socat", "socat", why="sandbox proxy"))}
    found = missing_from_image(needs, stage.replace("\\\n", " "))
    assert len(found) == 1
    assert "claude_agent_sdk" in found[0] and "'socat'" in found[0]


def test_npm_and_copied_binaries_are_read():
    stage = ("COPY --from=node:22 /usr/local/bin/node /usr/local/bin/node\n"
             "RUN npm install -g @anthropic-ai/claude-code@2.1.197 @openai/codex@0.158.0 "
             "pnpm@9 && true")
    have = installed_by_image(stage)
    assert {"@anthropic-ai/claude-code", "@openai/codex", "pnpm"} <= have["npm"]
    assert "/usr/local/bin/node" in have["image"]


def test_an_operator_cli_is_only_a_bring_your_own_one():
    """`operator` exempts a binary from the image: only a CLI the image deliberately does not
    ship (it has its own install hint) may say so."""
    from boss_engine.services.cli_harness import BUILTIN_CLI_HARNESSES

    byo = {spec.name for spec in BUILTIN_CLI_HARNESSES.values()
           if any(r.via == "operator" for r in spec.runtime)}
    for name, needs in hr.all_runtime_requirements().items():
        if any(r.via == "operator" for r in needs):
            assert name in byo, name
    for name in byo:
        assert BUILTIN_CLI_HARNESSES[name].install_hint, name


def test_an_unknown_via_is_refused():
    with pytest.raises(ValueError):
        hr.RuntimeBinary("x", "x", via="brew")


def test_missing_runtime_reads_path():
    assert [r.binary for r in hr.missing_runtime(
        "claude_code", which=lambda b: None if b == "claude" else "/usr/bin/" + b)] == ["claude"]
    assert hr.missing_runtime("claude_agent_sdk", which=lambda b: None) == []
    assert hr.missing_runtime("no_such_engine") == []
