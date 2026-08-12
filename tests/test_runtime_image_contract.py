"""Runtime images must carry the tools the governed pipeline shells out to.

BOSS accepted code tasks on goldeneye for weeks and could not perform a single one. Runs were
created, sat at `queued`, and were eventually closed as abandoned. Nothing anywhere said the
reason, because nothing checked it: the consolidated `Dockerfile` dropped git, node, npm and
the Claude Code CLI that `infra/containers/api.Dockerfile` had installed — with a comment
explaining exactly why they were needed.

A later live QA (BOSS-342) found the same class of failure on the *worker* image: multi-target
consolidation left `FROM python-base AS worker` without git while api kept it. Worker tag
`norebuild` lagged api/web `view2`; code.task deadlettered with
`No such file or directory: 'git'`. `infra/containers/worker.Dockerfile` already installed
git + node; the root multi-target file did not.

It worked on every developer machine throughout, which is precisely why it survived. A laptop
has git and node; a `python:3.11-slim` container does not.

This asserts the CONTRACT rather than the commands: the governed pipeline runs
`local_subprocess`, so whatever executes it must be able to clone a repo and run the project's
own build. Checking the Dockerfile is a proxy — optional docker probes check a built image.

    python tests/test_runtime_image_contract.py
    BOSS_IMAGE=goldeneye…/boss-api:TAG python tests/test_runtime_image_contract.py
    BOSS_WORKER_IMAGE=goldeneye…/boss-worker:TAG python tests/test_runtime_image_contract.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"

# What the governed pipeline shells out to on the API image. Each entry names the caller, so a
# future reader removing one can see what breaks rather than guessing whether it is still needed.
API_REQUIRED_TOOLS = {
    "git": "the pipeline shallow-clones the target repo",
    "node": "project builds and the e2e verification step",
    "npm": "`npm ci` / `npm run build` in the task's own repo",
    "claude": "the claude_code agent shells out to `claude -p` for builder roles",
}

# Worker contract is intentionally narrower: git + node/npm for local_subprocess stages that
# may run on the worker host. Playwright/Claude live on the api image today.
WORKER_REQUIRED_TOOLS = {
    "git": "EphemeralSandbox / local_subprocess clone and diff (BOSS-342)",
    "node": "web verification target: build / typecheck / lint",
    "npm": "`npm ci` / `npm run build` on co-located worker substrates",
}

_failures: list[str] = []


def check(name: str, ok: bool, why: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        _failures.append(f"{name}{f' — {why}' if why else ''}")


def stage_body(target: str) -> str:
    text = DOCKERFILE.read_text()
    marker = f"FROM python-base AS {target}"
    start = text.index(marker)
    nxt = text.find("\nFROM ", start + 1)
    return text[start : nxt if nxt > 0 else len(text)]


def tool_present_in_stage(stage: str, tool: str) -> bool:
    if tool == "claude":
        return "claude" in stage or "@anthropic-ai/claude-code" in stage
    return tool in stage


def probe_image(image: str, tools: dict[str, str], label: str) -> None:
    for tool, why in tools.items():
        probe = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "sh",
                image,
                "-c",
                f"command -v {tool}",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        check(
            f"`{tool}` resolves inside {label} image",
            probe.returncode == 0,
            f"missing — {why}",
        )


def build_target(target: str, tag: str) -> bool:
    print(f"  building {tag} (target={target})…")
    built = subprocess.run(
        [
            "docker",
            "build",
            "--platform",
            "linux/amd64",
            "--target",
            target,
            "-t",
            tag,
            str(ROOT),
        ],
        capture_output=True,
        text=True,
        timeout=3600,
    )
    if built.returncode != 0:
        check(f"{target} image builds", False, (built.stderr or built.stdout)[-300:])
        return False
    return True


def main() -> int:
    print("== the Dockerfile installs the toolchain ==")
    api_stage = stage_body("api")
    worker_stage = stage_body("worker")

    for tool, why in API_REQUIRED_TOOLS.items():
        check(
            f"api stage provides `{tool}` ({why})",
            tool_present_in_stage(api_stage, tool),
        )
    for tool, why in WORKER_REQUIRED_TOOLS.items():
        check(
            f"worker stage provides `{tool}` ({why})",
            tool_present_in_stage(worker_stage, tool),
        )
    check(
        "the boss user has a real home (npm and the CLI write there)",
        bool(re.search(r"--home\s+/home/boss", DOCKERFILE.read_text())),
    )

    print("\n== the BUILT images can actually run them ==")
    if not shutil.which("docker"):
        print("  SKIP  docker unavailable — the Dockerfile checks above still ran")
        return 1 if _failures else 0

    api_image = os.environ.get("BOSS_IMAGE")
    worker_image = os.environ.get("BOSS_WORKER_IMAGE")
    # When neither published image is supplied, build local contract tags so a bare
    # `python tests/test_runtime_image_contract.py` still proves both targets.
    build_local = not api_image and not worker_image

    if api_image or build_local:
        if not api_image:
            api_image = "boss-api-image-contract-test"
            if not build_target("api", api_image):
                return 1
        probe_image(api_image, API_REQUIRED_TOOLS, "api")

    if worker_image or build_local:
        if not worker_image:
            worker_image = "boss-worker-image-contract-test"
            if not build_target("worker", worker_image):
                return 1
        probe_image(worker_image, WORKER_REQUIRED_TOOLS, "worker")

    if not api_image and not worker_image and not build_local:
        print("  SKIP  set BOSS_IMAGE and/or BOSS_WORKER_IMAGE to probe published images")

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("api and worker images can perform the work the control plane accepts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
