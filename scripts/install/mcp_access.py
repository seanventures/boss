#!/usr/bin/env python3
"""Give an agent client MCP access to one project, then launch stdio without echoing keys.

`setup` asks the BOSS host shell for a service account "MCP (<project>)" bound in that one project
to the capabilities chosen (by default what rc5 granted), with a key pinned to the project
(`boss_engine.identity.cli mcp-key`, run inside the api container as the boss user). The key goes
into .env as BOSS_MCP_API_KEY and is never printed. An rc5 key (a hex key bound to the
`mcp_operator` member in BOSS_API_KEYS) is replaced and its binding removed: it stopped being
able to change members at the claim and stops entirely at the legacy cutoff.
`serve` forwards only that key to a single MCP stdio process.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from reconcile_api_credentials import _key_list, _principal_map, _set, _values

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "scripts/install/community-compose.yml"
DEFAULT_PERMISSIONS = (
    "agent_chat", "command_submit", "task_create", "workflow_manage",
    "workflow_execute", "health_refresh", "audit_export",
)
#: the rc5 member an MCP hex key was bound to (still recognised, and replaced, by setup)
PRINCIPAL = "mcp_operator"
KEY_PREFIX = "boss_sak_"


class MCPSetupError(Exception):
    """Actionable setup failure with no secret-bearing response body."""


def _read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    values = _values(lines)
    for name in ("BOSS_API_KEY", "BOSS_MCP_API_KEY", "BOSS_MCP_PROJECT_ID", "BOSS_API_KEYS",
                 "BOSS_API_KEY_PRINCIPALS"):
        found = [line.split("=", 1)[1] for line in lines if line.startswith(f"{name}=")]
        if len(set(found)) > 1:
            raise MCPSetupError(f"Conflicting {name} settings in .env")
    return lines, values


def _valid_project(project_id: str) -> bool:
    return bool(project_id) and len(project_id) <= 128 and all(
        ch.isalnum() or ch in "_.-" for ch in project_id)


def _mint(env_path: Path, project_id: str, permissions: list[str] | None) -> str:
    """The new key, from the host shell inside the api container. Never printed."""
    prefix = _docker_prefix()
    command = [*prefix, "compose", "-f", str(COMPOSE), "--env-file", str(env_path),
               "exec", "-T", "-u", "boss", "api", "python", "-m", "boss_engine.identity.cli",
               "mcp-key", "--project", project_id, "--quiet"]
    for permission in permissions or []:
        command += ["--permission", permission]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False,
                            timeout=120)
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    key = lines[-1] if lines else ""
    if result.returncode != 0 or not key.startswith(KEY_PREFIX):
        # stderr says why ("no project", "unknown permission"); it never carries the key
        reason = (result.stderr or "").strip().splitlines()[-1:] or ["BOSS did not issue a key"]
        raise MCPSetupError(reason[0][:200])
    return key


def configure(env_path: Path, project_id: str, permissions: list[str] | None = None,
              *, mint=None) -> tuple[bool, bool]:
    """(changed .env, the API must restart). The API restarts only when an rc5 hex key's binding
    was removed from BOSS_API_KEYS; a service-account key works the moment it is issued."""
    lines, values = _read_env(env_path)
    before = list(lines)
    if not _valid_project(project_id):
        raise MCPSetupError("Choose a valid project ID")
    if permissions is not None and (not permissions or any(
            not p or not p.replace("_", "").replace(":", "").isalnum() for p in permissions)):
        raise MCPSetupError("At least one valid permission is required")
    configured_project = values.get("BOSS_MCP_PROJECT_ID", "")
    if configured_project and configured_project != project_id:
        raise MCPSetupError(
            "This installed MCP key is bound to another project; use a separate principal and key"
        )
    key = values.get("BOSS_MCP_API_KEY", "")
    bindings = _principal_map(values.get("BOSS_API_KEY_PRINCIPALS", ""))
    if key and not key.startswith(KEY_PREFIX) and bindings.get(key) != PRINCIPAL:
        raise MCPSetupError("Existing MCP key is not bound to the dedicated member")
    if key.startswith(KEY_PREFIX) and permissions is None:
        return False, False          # already a service-account key for this project
    new_key = (mint or _mint)(env_path, project_id, permissions)
    if not new_key.startswith(KEY_PREFIX):
        raise MCPSetupError("BOSS did not issue a service-account key")
    restart = False
    if key and not key.startswith(KEY_PREFIX):
        # the rc5 hex key: unbind it so it stops working once the API reloads .env
        keys = [k for k in _key_list(values.get("BOSS_API_KEYS", "")) if k != key]
        bindings.pop(key, None)
        lines = _set(lines, "BOSS_API_KEYS", ",".join(keys))
        lines = _set(lines, "BOSS_API_KEY_PRINCIPALS",
                     ",".join(f"{token}:{principal}" for token, principal in bindings.items()))
        restart = True
    lines = _set(lines, "BOSS_MCP_API_KEY", new_key)
    lines = _set(lines, "BOSS_MCP_PROJECT_ID", project_id)
    if lines == before:
        return False, False
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=env_path.parent,
                                         prefix=f"{env_path.name}.mcp.", delete=False) as output:
            temporary = Path(output.name)
            output.write("\n".join(lines) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, env_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True, restart


def launch(env_path: Path, project_id: str) -> None:
    _, values = _read_env(env_path)
    if values.get("BOSS_MCP_PROJECT_ID") != project_id:
        raise MCPSetupError("MCP key is not pinned to this project; run make mcp-setup")
    key = values.get("BOSS_MCP_API_KEY", "")
    bindings = _principal_map(values.get("BOSS_API_KEY_PRINCIPALS", ""))
    if not key or (not key.startswith(KEY_PREFIX) and bindings.get(key) != PRINCIPAL):
        raise MCPSetupError("MCP access is unbound; run make mcp-setup first")
    if not key.startswith(KEY_PREFIX):
        print("This MCP key is the old kind and stops working when the shared credentials do. "
              "Run make mcp-setup to replace it.", file=sys.stderr)
    environment = dict(os.environ, BOSS_MCP_API_KEY=key)
    docker_command = _docker_command(environment, key)
    command = [
        *docker_command, "compose", "-f", str(COMPOSE), "--env-file", str(env_path),
        "exec", "-T", "-u", "boss", "-e", "BOSS_MCP_API_KEY", "api", "python", "-m",
        "boss_engine", "--project", project_id, "serve-mcp", "--api-url",
        "http://127.0.0.1:8000",
    ]
    os.execvpe(docker_command[0], command, environment)


def _docker_prefix() -> list[str]:
    if shutil.which("docker") and subprocess.run(
        ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False, timeout=5,
    ).returncode == 0:
        return ["docker"]
    if shutil.which("sudo") and subprocess.run(
        ["sudo", "-n", "docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False, timeout=5,
    ).returncode == 0:
        return ["sudo", "-n", "docker"]
    raise MCPSetupError("Docker access is required to issue the MCP key")


def _docker_command(environment: dict[str, str], key: str) -> list[str]:
    if shutil.which("docker") and subprocess.run(
        ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False, timeout=5, env=environment,
    ).returncode == 0:
        return ["docker"]
    # The installer supports noninteractive sudo before the docker-group login
    # takes effect. Verify that sudo preserves only the dedicated MCP key;
    # otherwise `compose exec -e NAME` would start an unbound session.
    if shutil.which("sudo"):
        sudo = ["sudo", "-n", "--preserve-env=BOSS_MCP_API_KEY"]
        probe = subprocess.run([*sudo, "env"], capture_output=True, text=True,
                               check=False, timeout=5, env=environment)
        if (probe.returncode == 0 and f"BOSS_MCP_API_KEY={key}" in probe.stdout.splitlines()
                and subprocess.run([*sudo, "docker", "info"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   check=False, timeout=5, env=environment).returncode == 0):
            return [*sudo, "docker"]
    raise MCPSetupError("Docker access or approved sudo environment forwarding is required")


def _restart_api(env_path: Path) -> None:
    prefix = _docker_prefix()
    command = [*prefix, "compose", "-f", str(COMPOSE), "--env-file", str(env_path),
               "up", "-d", "--wait", "--no-deps", "--force-recreate", "api"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                            check=False, timeout=240)
    if result.returncode != 0:
        raise MCPSetupError("The API service did not restart with the MCP credential")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "serve"))
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--project", default=None)
    parser.add_argument("--permission", action="append", dest="permissions")
    parser.add_argument("--restart-api", action="store_true",
                        help="setup only: reload API key binding when .env changes")
    args = parser.parse_args()
    try:
        _, values = _read_env(args.env_file)
        project = (args.project or os.environ.get("BOSS_PROJECT_ID") or
                   values.get("BOSS_DEFAULT_PROJECT_ID", "boss-dogfood"))
        if args.action == "setup":
            _changed, restart = configure(args.env_file, project, args.permissions)
            if restart and args.restart_api:
                _restart_api(args.env_file)
            print(f"MCP access ready for project {project}: a service-account key in "
                  "BOSS_MCP_API_KEY. Start it with: make mcp" +
                  (" (restart the API first to end the old key)" if restart and not args.restart_api else ""))
        else:
            launch(args.env_file, project)
    except MCPSetupError as error:
        print(f"MCP access setup failed: {error}", file=sys.stderr)
        return 2
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # Never interpolate an exception: upstream errors can contain submitted keys.
        print("MCP access setup failed. Check the local API, project, .env, and Docker access.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
