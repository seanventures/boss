#!/usr/bin/env python3
"""Stdlib-only restore preflight and persistent Compose recovery contract.

Inputs containing resolved environments remain private and are never printed.
This helper creates no Docker resources and performs no application imports.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

INTERFACE = "boss-recovery-hold/v1"
ROOT = "/var/lib/boss/recovery"
GUARD = "/usr/local/libexec/boss/recovery_guard.py"
ROLES = {
    "api": ["python", "-m", "apps.api.serve", "--host", "0.0.0.0", "--port", "8000"],
    "worker": ["python", "-m", "apps.worker.boss_worker.main"],
    "egress": ["python", "-m", "apps.egress.boss_egress"],
    "mcp-tools": ["python", "-m", "tools.mcp_tools_mux"],
}


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def source_hashes(source: Path):
    paths = {GUARD: "scripts/install/recovery_guard.py", "/usr/local/bin/boss-entrypoint": "scripts/sandbox/entrypoint.sh"}
    try:
        for relative in (*paths.values(), "Dockerfile.community", "scripts/install/community-compose.yml"):
            path = source / relative
            if path.is_symlink() or not path.is_file():
                raise ValueError("unsafe source")
        compose = (source / "scripts/install/community-compose.yml").read_text()
        dockerfile = (source / "Dockerfile.community").read_text()
        entrypoint = (source / "scripts/sandbox/entrypoint.sh").read_text()
        if not all(name in compose for name in ("BOSS_RECOVERY_API_IMAGE_ID", "BOSS_RECOVERY_DB_IMAGE_ID", ROOT)):
            raise ValueError("source lacks persistent pins")
        if f'io.boss.recovery-guard="{INTERFACE}"' not in dockerfile:
            raise ValueError("source lacks interface")
        if f"python3 {GUARD} --check" not in entrypoint:
            raise ValueError("source lacks API guard")
        return {target: hashlib.sha256((source / relative).read_bytes()).hexdigest() for target, relative in paths.items()}
    except (OSError, ValueError) as exc:
        raise SystemExit("Source predates or lacks the reviewed recovery guard. Use explicit --offline-data-only; no application startup is permitted.") from exc


def qualify_image(source, manifest, metadata, actual_hashes):
    expected = source_hashes(source)
    if metadata != {"image_id": manifest["api_image_id"], "interface": INTERFACE,
                    "source_commit": manifest["source_commit"],
                    "entrypoint": ["/usr/local/bin/boss-entrypoint"], "user": "boss"}:
        raise SystemExit("Archived API image has no matching guard interface/source revision. No target resources may be created.")
    if actual_hashes != expected:
        raise SystemExit("Archived image guard files differ from the matching source. No target resources may be created.")


def recovery_layout(config, layout, project, offline):
    if offline:
        name = project + "_boss_community_recovery"
    else:
        sources = set()
        for role in ROLES:
            service = config["services"][role]
            mounts = [v for v in service.get("volumes", []) if v.get("target") == ROOT]
            if len(mounts) != 1 or mounts[0].get("type") != "volume" or mounts[0].get("read_only") is not True:
                raise SystemExit("Every backend role needs the persistent read-only recovery volume.")
            sources.add(mounts[0]["source"])
            if role != "api" and service.get("entrypoint") != ["python3", GUARD]:
                raise SystemExit("Non-API roles must use the common guard directly.")
            if role != "api" and service.get("command") != ROLES[role]:
                raise SystemExit("Recovery roles need explicit role commands.")
        if len(sources) != 1:
            raise SystemExit("Backend recovery volumes differ.")
        source = sources.pop()
        layout["recovery_source"] = source
        volume = config["volumes"][source]
        if volume.get("external"):
            raise SystemExit("Recovery refuses an external hold volume.")
        name = volume["name"]
        if any(v.get("target", "").startswith("/var/lib/boss/identity") for v in config["services"]["mcp-tools"].get("volumes", [])):
            raise SystemExit("MCP recovery must not mount identity material.")
    if name in (layout["db_volume"], layout["identity_volume"], layout["artifacts_volume"]):
        raise SystemExit("Recovery hold must have an independent volume.")
    layout["recovery_volume"] = name
    if len({layout[key] for key in ("db_volume", "identity_volume", "artifacts_volume", "recovery_volume")}) != 4:
        raise SystemExit("Recovery database, identity, artifacts and hold volumes must be distinct.")
    layout["all_volumes"] = sorted(set(layout["all_volumes"] + [name]))
    return layout


def prepare(config, layout, manifest, project, output: Path, offline):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]+", project):
        raise SystemExit("Invalid target project.")
    layout = recovery_layout(config, layout, project, offline)
    write_json(output / "layout.json", layout)
    volume_plan = {}
    for kind in ("identity", "artifacts", "recovery"):
        if offline:
            logical = "boss_community_" + kind
        elif kind == "recovery":
            logical = layout["recovery_source"]
        else:
            logical = next(v["source"] for v in config["services"]["api"]["volumes"] if v.get("target") == "/var/lib/boss/" + kind)
        physical = layout[kind + "_volume"]
        if not all(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", value) for value in (logical, physical)):
            raise SystemExit("Recovery volume identifiers are invalid.")
        volume_plan[kind] = {"logical": logical, "name": physical}
    write_json(output / "volume-plan.json", volume_plan)
    services = {"db": {"image": manifest["db_image_id"], "pull_policy": "never"}}
    if not offline:
        for role, command in ROLES.items():
            services[role] = {"image": manifest["api_image_id"], "pull_policy": "never", "command": command}
            services[role]["volumes"] = [{"type": "volume", "source": layout["recovery_source"], "target": ROOT, "read_only": True}]
            if role != "api":
                services[role]["entrypoint"] = ["python3", GUARD]
    override = {"services": services}
    if not offline:
        override["volumes"] = {layout["recovery_source"]: {"name": layout["recovery_volume"]}}
    write_json(output / "images.json", override)
    if offline:
        # Literal variable references preserve the private .env contract. Never
        # persist docker compose's resolved environment into a source file.
        volumes = {"boss_community_" + kind: {"name": layout[key]} for kind, key in (
            ("pg", "db_volume"), ("identity", "identity_volume"),
            ("artifacts", "artifacts_volume"), ("recovery", "recovery_volume"))}
        base = {"services": {"db": {
            "image": "${BOSS_RECOVERY_DB_IMAGE_ID:?reviewed immutable database image required}",
            "pull_policy": "never", "restart": "unless-stopped",
            "mem_reservation": "256m", "oom_score_adj": -900,
            "environment": {"POSTGRES_DB": "boss", "POSTGRES_USER": "boss", "POSTGRES_PASSWORD": "${BOSS_DB_PASSWORD:?private restored password required}"},
            "ports": ["127.0.0.1:${BOSS_POSTGRES_PORT:-0}:5432"],
            "volumes": ["boss_community_pg:/var/lib/postgresql/data"],
            "healthcheck": {"test": ["CMD-SHELL", "pg_isready -U boss -d boss"], "interval": "3s", "timeout": "5s", "retries": 30},
        }}, "volumes": volumes}
        write_json(output / "offline-compose.json", base)
    marker = {"interface": INTERFACE, "state": "held", "source_commit": manifest["source_commit"],
              "project": project, "api_image_id": manifest["api_image_id"], "db_image_id": manifest["db_image_id"]}
    write_json(output / "hold.json", marker)


def persist_env(path: Path, manifest, project):
    # Do not evaluate the archived environment as shell code.
    keys = {"BOSS_POSTGRES_PORT", "BOSS_RECOVERY_DB_IMAGE_ID", "BOSS_RECOVERY_API_IMAGE_ID", "BOSS_RECOVERY_PULL_POLICY", "COMPOSE_PROJECT_NAME", "BOSS_COMPOSE_PROJECT_NAME"}
    lines = [line for line in path.read_text().splitlines() if line.partition("=")[0].strip().removeprefix("export ").strip() not in keys]
    lines += ["BOSS_POSTGRES_PORT=0", "BOSS_RECOVERY_PULL_POLICY=never", "BOSS_RECOVERY_DB_IMAGE_ID=" + manifest["db_image_id"], "BOSS_RECOVERY_API_IMAGE_ID=" + manifest["api_image_id"], "COMPOSE_PROJECT_NAME=" + project, "BOSS_COMPOSE_PROJECT_NAME=" + project]
    path.write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    source = sub.add_parser("source")
    source.add_argument("checkout", type=Path)
    image = sub.add_parser("image")
    for name in ("checkout", "manifest", "metadata", "hashes"):
        image.add_argument(name, type=Path)
    prep = sub.add_parser("prepare")
    for name in ("config", "layout", "manifest", "output"):
        prep.add_argument(name, type=Path)
    prep.add_argument("project")
    prep.add_argument("--offline-data-only", action="store_true")
    env = sub.add_parser("env")
    env.add_argument("path", type=Path)
    env.add_argument("manifest", type=Path)
    env.add_argument("project")
    args = parser.parse_args()
    if args.command == "source":
        print(json.dumps(source_hashes(args.checkout), sort_keys=True))
    elif args.command == "image":
        qualify_image(args.checkout, read_json(args.manifest), read_json(args.metadata), read_json(args.hashes))
    elif args.command == "prepare":
        prepare(read_json(args.config), read_json(args.layout), read_json(args.manifest), args.project, args.output, args.offline_data_only)
    else:
        persist_env(args.path, read_json(args.manifest), args.project)


if __name__ == "__main__":
    main()
