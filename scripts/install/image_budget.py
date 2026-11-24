#!/usr/bin/env python3
"""Hold every BOSS image to its size budget (BOSSAPP-1458).

A fresh 30 GB enterprise box could not finish a first install: every Python service was built from
one fat stage (6.84 GB each on disk), and the build ran out of room. The images are now a family
(Dockerfile.community, "THE IMAGE FAMILY"), each carrying only what its service runs. This check
keeps them that way: an image that grows past its budget fails the check, in words that say which
image, by how much, and what to do.

    python3 scripts/install/image_budget.py [--tag TAG] [--docker "sudo docker"]

Sizes are the image's uncompressed size: the sum of its layers (`docker history`), the same with
the containerd image store and the classic one (`docker image inspect .Size` is the COMPRESSED
size under containerd, the unpacked one under overlay2). An image that is not present (the console on a headless install) is skipped and said. Exit 0 when
every present image fits, 1 when one does not, 2 when Docker cannot be asked.

The budgets are for the default toolchains (java,go). BOSS_TOOLCHAINS=security adds about 1 GB to
the api, worker and runner images: their budget grows by EXTRA_FOR_SECURITY when the image's
toolchain manifest names it.
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys

GB = 1_000_000_000

#: Uncompressed bytes. Measured on the enterprise lane (arm64, 2026-11-24) plus ~10% headroom:
#: a budget is a ceiling that catches a new fat layer, not a target to fill. Before BOSSAPP-1458
#: api, worker, egress and mcp-tools were all one 4.99 GB image.
BUDGETS: dict[str, int] = {
    "api": int(4.4 * GB),
    "worker": int(4.4 * GB),
    "runner": int(4.4 * GB),
    "egress": int(0.9 * GB),
    "mcp-tools": int(1.6 * GB),
    "web": int(2.5 * GB),
}
#: What a first install needs free on Docker's disk, measured on the enterprise lane (2026-11-24:
#: 24 GB free before, 9.1 GB at the lowest, so 15 GB at the peak: the images plus one family's
#: build cache) plus the 6 GB the e2e gate needs after it. install.sh's
#: BOSS_INSTALL_NEEDS_GB default is this number (test_a_first_install_fits_a_30gb_box).
FIRST_INSTALL_NEEDS_GB = 21
#: The opt-in security toolchain (Semgrep, OSV-Scanner, ...): allowed on top of the full images.
EXTRA_FOR_SECURITY = int(1.5 * GB)
FULL_IMAGES = ("api", "worker", "runner")


def budget_for(role: str, toolchains: set[str]) -> int:
    extra = EXTRA_FOR_SECURITY if role in FULL_IMAGES and "security" in toolchains else 0
    return BUDGETS[role] + extra


def over_budget(role: str, size: int, toolchains: set[str]) -> str | None:
    """The sentence for an image past its budget, or None when it fits."""
    limit = budget_for(role, toolchains)
    if size <= limit:
        return None
    return (f"The {role} image is {size / GB:.2f} GB, over its {limit / GB:.2f} GB budget by "
            f"{(size - limit) / GB:.2f} GB. Find the layer that grew with "
            f"`docker history boss-community-{role}:<tag>` and slim it in Dockerfile.community; "
            f"raise BUDGETS in scripts/install/image_budget.py only for a reviewed reason.")


def _size(docker: list[str], ref: str) -> int | None:
    """The image's uncompressed bytes (the sum of its layers), or None when it is not here."""
    proc = subprocess.run([*docker, "history", "--no-trunc", "--human=false", "--format", "{{.Size}}", ref],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    try:
        return sum(int(line) for line in proc.stdout.split() if line.strip())
    except ValueError:
        return None


def _toolchains(docker: list[str], ref: str) -> set[str] | None:
    """The toolchains the image's manifest names, or None when it cannot be read."""
    proc = subprocess.run([*docker, "run", "--rm", "--pull", "never", "--entrypoint", "cat", ref,
                           "/opt/toolchains/manifest.json"], capture_output=True, text=True)
    try:
        return set(json.loads(proc.stdout)) if proc.returncode == 0 else None
    except ValueError:
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--tag", default="boss-community")
    ap.add_argument("--docker", default="docker", help='the docker command, e.g. "sudo docker"')
    args = ap.parse_args(argv)
    docker = shlex.split(args.docker)
    if subprocess.run([*docker, "version"], capture_output=True).returncode != 0:
        print(f"Docker could not be asked for the image sizes ({args.docker} version failed). "
              "Start Docker, or pass --docker \"sudo docker\", and run this check again.")
        return 2
    failed = False
    for role in BUDGETS:
        ref = f"boss-community-{role}:{args.tag}"
        size = _size(docker, ref)
        if size is None:
            print(f"  {role:<10} not built here (skipped)")
            continue
        toolchains: set[str] | None = set()
        if role in FULL_IMAGES:
            toolchains = _toolchains(docker, ref)
            if toolchains is None:
                print(f"  {role:<10} its toolchain manifest could not be read: budgeted without the "
                      "security toolchain's allowance")
                toolchains = set()
        problem = over_budget(role, size, toolchains)
        mark = "OVER" if problem else "ok"
        print(f"  {role:<10} {size / GB:6.2f} GB  budget {budget_for(role, toolchains) / GB:5.2f} GB  {mark}")
        if problem:
            print("    " + problem)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
