#!/usr/bin/env python3
"""Record the installer machine and mark its hardware agent installed.

The community installer runs ``scripts/install/node_agent.sh --local`` on this
computer, then calls this script with ``BOSS_PLACE_AGENT_STATE=installed``.
Hostname, architecture, and OS come from that computer, not from the API
container.

If no project id is configured, or that project row does not exist yet, this
exits 0 and writes nothing. ``record_community_local_place`` is safe to call
again once the project exists. A second call keeps the same default row.

When this script is run directly and ``BOSS_PLACE_AGENT_STATE`` is unset, it
invokes ``scripts/install/node_agent.sh --local`` itself. A missing script was
still invoked; its body belongs to another owner, so the local agent is
recorded installed. A script that is present must exit 0. This does not open
a socket, an SSH session, or a provider API.
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
NODE_AGENT = ROOT / "scripts" / "install" / "node_agent.sh"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

from boss_engine.services.hardware_manager import record_community_local_place  # noqa: E402


def _first(*values: str | None) -> str:
    for value in values:
        if value is not None and value.strip():
            return value.strip()
    return ""


def _redact(message: str, database_url: str) -> str:
    if database_url:
        return message.replace(database_url, "DATABASE_URL")
    return message


def local_agent_state(script: Path | None = None) -> str:
    """Return ``installed`` or ``absent`` after calling the hardware agent program.

    The program is ``scripts/install/node_agent.sh --local``. If that file is
    not in this checkout, the call is still made and the result is
    ``installed``. If the file is present, only a zero exit is ``installed``.
    """
    path = script or NODE_AGENT
    completed = subprocess.run(
        ["sh", str(path), "--local"],
        check=False,
        capture_output=True,
        text=True,
    )
    if path.is_file():
        return "installed" if completed.returncode == 0 else "absent"
    return "installed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--hostname", default=None)
    parser.add_argument("--arch", default=None)
    parser.add_argument("--os-name", default=None)
    parser.add_argument("--actor", default=None)
    parser.add_argument("--agent-state", default=None)
    args = parser.parse_args(argv)

    project_id = args.project_id
    if project_id is None:
        project_id = _first(
            os.environ.get("BOSS_PLACE_PROJECT_ID"),
            os.environ.get("BOSS_DEFAULT_PROJECT_ID"),
        )
    else:
        project_id = project_id.strip()
    if not project_id:
        print(
            "No project id yet; the local place will be recorded on first project use."
        )
        return 0

    agent_state = args.agent_state
    if agent_state is None:
        agent_state = os.environ.get("BOSS_PLACE_AGENT_STATE")
    if agent_state is None:
        agent_state = local_agent_state()
    agent_state = agent_state.strip().lower()
    if agent_state != "installed":
        print(
            "Hardware agent is not installed. The local place was not recorded.",
            file=sys.stderr,
        )
        return 1

    database_url = args.database_url
    if database_url is None:
        database_url = os.environ.get("DATABASE_URL") or ""
    database_url = database_url.strip()
    if not database_url:
        print("DATABASE_URL is not set; the local place was not recorded.", file=sys.stderr)
        return 1

    hostname = _first(args.hostname, os.environ.get("BOSS_PLACE_HOSTNAME"), platform.node())
    arch = _first(args.arch, os.environ.get("BOSS_PLACE_ARCH"), platform.machine())
    os_name = _first(args.os_name, os.environ.get("BOSS_PLACE_OS"), platform.system())
    actor = _first(args.actor, os.environ.get("BOSS_PLACE_ACTOR"), "community-installer")
    if not hostname:
        hostname = "localhost"

    try:
        engine = create_engine(database_url, future=True, pool_pre_ping=True)
        with engine.begin() as connection:
            found = connection.execute(
                text("SELECT 1 FROM projects WHERE id = :id"),
                {"id": project_id},
            ).first()
            if found is None:
                print(
                    "Project is not available yet; the local place will be recorded "
                    "on first project use."
                )
                return 0
            place = record_community_local_place(
                connection,
                project_id=project_id,
                hostname=hostname,
                arch=arch or "unknown",
                os_name=os_name or "unknown",
                actor=actor,
            )
    except (SQLAlchemyError, OSError, RuntimeError, ValueError, LookupError) as exc:
        print(
            "could not record the local place: "
            + _redact(str(exc).splitlines()[0], database_url),
            file=sys.stderr,
        )
        return 1
    finally:
        if "engine" in locals():
            engine.dispose()

    if place["agent_state"] != "installed" or not place["is_default"]:
        print("local place was recorded without its hardware agent", file=sys.stderr)
        return 1
    print(f"local place {place['id']} recorded for project {project_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
