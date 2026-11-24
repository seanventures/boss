#!/usr/bin/env python3
"""The order BOSS builds its images in (BOSSAPP-1458): each image once, each family together.

    docker compose ... config --format json | python3 scripts/install/build_order.py \
        [--profile NAME]... [--only "svc svc"]

Prints one line per build, `<service> <family index>`, for every service with a `build:` that an
active profile selects (and, with --only, that is named there). Used by every path that builds the
images: install.sh, scripts/install/build_images.sh (boss toolchains, remote updates, the self-fix
installer), so they all build the same way.

  * ONCE PER IMAGE. Two services on one image with one build (kg-mcp runs the worker image) build
    it once. A second build after BuildKit's garbage collector had evicted the cache made a second,
    unshared copy of every layer, and a fresh 30 GB box ran out of disk doing it. The service named
    like the image (worker, not kg-mcp) is the one built, so the log names it.
  * FAMILIES TOGETHER. A family is the stage a target grows from: follow `FROM <stage>` inside its
    Dockerfile to the first stage built FROM an outside image. The Python services all grow from
    python-base; the console is its own family. Images of a family share layers, so they build back
    to back while the cache is warm; between families the caller may drop the finished family's
    cache (prune.sh --room --cache-only).

Never prints the configuration (it holds resolved credentials). Exit 2 on a malformed config.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

_FROM = re.compile(r"(?m)^FROM\s+(?:--\S+\s+)*(\S+)(?:\s+[Aa][Ss]\s+(\S+))?\s*$")
_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")


def family(build: dict) -> str:
    """The stage a build target grows from, as `<dockerfile>#<stage>`; the Dockerfile's path alone
    when it cannot be read (each such Dockerfile is then its own family)."""
    context = build.get("context") or "."
    path = os.path.join(context, build.get("dockerfile") or "Dockerfile")
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return path
    stages: dict[str, str] = {}
    last = None
    for match in _FROM.finditer(text):
        if match.group(2):
            stages[match.group(2)] = match.group(1)
            last = match.group(2)
    stage = build.get("target") or last
    seen: set[str] = set()
    while stage in stages and stages[stage] in stages and stage not in seen:
        seen.add(stage)
        stage = stages[stage]
    return f"{path}#{stage}"


def order(services: dict, active: set[str], only: set[str] | None = None) -> list[tuple[str, int]]:
    """[(service, family index)] in build order."""
    families: dict[str, list[str]] = {}
    images: dict[tuple[str, str], str] = {}
    for name, service in services.items():
        profiles = set(service.get("profiles") or [])
        if "build" not in service or (profiles and not active & profiles and "*" not in active):
            continue
        if only is not None and name not in only:
            continue
        if not _NAME.fullmatch(name):
            raise SystemExit("Invalid build service name in Compose configuration")
        build = service["build"] if isinstance(service["build"], dict) else {"context": service["build"]}
        key = (str(service.get("image") or name), json.dumps(build, sort_keys=True))
        group = families.setdefault(family(build), [])
        if key in images:
            if re.search(r"-" + re.escape(name) + r"(:|$)", key[0]) and images[key] in group:
                group[group.index(images[key])] = name
                images[key] = name
            continue
        images[key] = name
        group.append(name)
    return [(name, index) for index, names in enumerate(families.values()) for name in names]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--only", default=None, help="space-separated service names (default: all)")
    args = parser.parse_args(argv)
    try:
        services = json.load(sys.stdin)["services"]
    except (ValueError, KeyError, TypeError):
        print("The Compose configuration could not be read as JSON; no image was built. Check the "
              "Compose files and .env with: docker compose config", file=sys.stderr)
        return 2
    only = set(args.only.split()) if args.only and args.only.strip() else None
    for name, index in order(services, set(args.profile), only):
        print(name, index)
    return 0


if __name__ == "__main__":
    sys.exit(main())
