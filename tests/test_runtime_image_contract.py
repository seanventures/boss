"""The api image must carry the tools the governed pipeline shells out to.

BOSS accepted code tasks on goldeneye for weeks and could not perform a single one. Runs were
created, sat at `queued`, and were eventually closed as abandoned. Nothing anywhere said the
reason, because nothing checked it: the consolidated `Dockerfile` dropped git, node, npm and
the Claude Code CLI that `infra/containers/api.Dockerfile` had installed — with a comment
explaining exactly why they were needed.

It worked on every developer machine throughout, which is precisely why it survived. A laptop
has git and node; a `python:3.11-slim` container does not.

This asserts the CONTRACT rather than the commands: the governed pipeline runs
`local_subprocess`, so whatever executes it must be able to clone a repo and run the project's
own build. Checking the Dockerfile is a proxy — the real check is the last one, which runs the
built image.

    PYTHONPATH=engine .venv/bin/python tests/test_runtime_image_contract.py
    BOSS_IMAGE=goldeneye.tail95d980.ts.net:5000/boss-api:TAG ... (to check a published image)
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

# What the governed pipeline shells out to. Each entry names the caller, so a future reader
# removing one can see what breaks rather than guessing whether it is still needed.
REQUIRED_TOOLS = {
    "git": "the pipeline shallow-clones the target repo",
    "node": "project builds and the e2e verification step",
    "npm": "`npm ci` / `npm run build` in the task's own repo",
    "claude": "the claude_code agent shells out to `claude -p` for builder roles",
}

_failures: list[str] = []


def check(name: str, ok: bool, why: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        _failures.append(f"{name}{f' — {why}' if why else ''}")


def api_stage() -> str:
    text = DOCKERFILE.read_text()
    start = text.index("FROM python-base AS api")
    nxt = text.find("\nFROM ", start + 1)
    return text[start:nxt if nxt > 0 else len(text)]


def main() -> int:
    print("== the Dockerfile installs the toolchain ==")
    stage = api_stage()
    for tool, why in REQUIRED_TOOLS.items():
        present = tool in stage or (tool == "claude" and "@anthropic-ai/claude-code" in stage)
        check(f"api stage provides `{tool}` ({why})", present)
    check("the boss user has a real home (npm and the CLI write there)",
          bool(re.search(r"--home\s+/home/boss", DOCKERFILE.read_text())))

    print("\n== the BUILT image can actually run them ==")
    image = os.environ.get("BOSS_IMAGE")
    if not shutil.which("docker"):
        print("  SKIP  docker unavailable — the Dockerfile checks above still ran")
        return 1 if _failures else 0
    if not image:
        image = "boss-api-image-contract-test"
        print(f"  building {image} (target=api)…")
        built = subprocess.run(
            ["docker", "build", "--platform", "linux/amd64", "--target", "api",
             "-t", image, str(ROOT)],
            capture_output=True, text=True, timeout=3600)
        if built.returncode != 0:
            check("image builds", False, built.stderr[-300:])
            return 1

    for tool, why in REQUIRED_TOOLS.items():
        probe = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "sh", image, "-c",
             f"command -v {tool}"],
            capture_output=True, text=True, timeout=300)
        check(f"`{tool}` resolves inside the image", probe.returncode == 0,
              f"missing — {why}")

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("the api image can perform the work the control plane accepts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
