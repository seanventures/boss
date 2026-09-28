#!/usr/bin/env python3
"""Provision a scoped Community MCP principal and launch stdio without echoing keys.

The setup command uses the installed owner key only to create a dedicated
project member, then binds a separate generated API key to that member in .env.
The launch command forwards only that key to a single MCP stdio process.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from reconcile_api_credentials import _key_list, _principal_map, _set, _values

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "scripts/install/community-compose.yml"
DEFAULT_PERMISSIONS = (
    "agent_chat", "command_submit", "task_create", "workflow_manage",
    "workflow_execute", "health_refresh", "audit_export",
)
PRINCIPAL = "mcp_operator"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirect)


class MCPSetupError(Exception):
    """Actionable setup failure with no secret-bearing response body."""


def _read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    values = _values(lines)
    for name in ("BOSS_API_KEY", "BOSS_MCP_API_KEY", "BOSS_API_KEYS",
                 "BOSS_API_KEY_PRINCIPALS"):
        found = [line.split("=", 1)[1] for line in lines if line.startswith(f"{name}=")]
        if len(set(found)) > 1:
            raise MCPSetupError(f"Conflicting {name} settings in .env")
    return lines, values


def _api_origin(values: dict[str, str]) -> str:
    port = values.get("BOSS_API_PORT", "8000")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise MCPSetupError("BOSS_API_PORT must be a local TCP port")
    return f"http://127.0.0.1:{port}"


def _request(origin: str, key: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    parsed = urlsplit(origin)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.path not in {"", "/"}:
        raise MCPSetupError("MCP setup accepts only the installed loopback API origin")
    data = json.dumps(body).encode() if body is not None else None
    request = Request(
        origin.rstrip("/") + path, data=data, method=method,
        headers={"X-Boss-Api-Key": key, "Content-Type": "application/json"},
    )
    try:
        with _OPENER.open(request, timeout=10) as response:
            status = response.status
            raw = response.read(128_001)
    except HTTPError as error:
        status = error.code
        raw = b""
    except (OSError, URLError) as error:
        raise MCPSetupError("Cannot reach the installed BOSS API on loopback") from error
    if len(raw) > 128_000:
        raise MCPSetupError("BOSS API response was unexpectedly large")
    try:
        value = json.loads(raw) if raw else {}
    except ValueError as error:
        raise MCPSetupError("BOSS API returned invalid JSON") from error
    if not isinstance(value, dict):
        raise MCPSetupError("BOSS API returned an unexpected response")
    return status, value


def configure(env_path: Path, project_id: str, permissions: list[str] | None = None) -> bool:
    lines, values = _read_env(env_path)
    before = list(lines)
    owner = values.get("BOSS_API_KEY", "")
    worker = values.get("BOSS_WORKER_API_KEY", "")
    if not owner or not worker:
        raise MCPSetupError("Install BOSS before configuring MCP access")
    existing_keys = set(_key_list(values.get("BOSS_API_KEYS", "")))
    bindings = _principal_map(values.get("BOSS_API_KEY_PRINCIPALS", ""))
    key = values.get("BOSS_MCP_API_KEY", "")
    if key and bindings.get(key) != PRINCIPAL:
        raise MCPSetupError("Existing MCP key is not bound to the dedicated member")
    if not key:
        key = secrets.token_hex(24)
        while key in existing_keys or key in bindings or key in {owner, worker}:
            key = secrets.token_hex(24)
    if key in {owner, worker} or any(ch in key for ch in ",:=\n\r"):
        raise MCPSetupError("MCP API key must be separate and map-safe")
    if (not project_id or len(project_id) > 128 or
            any(not (ch.isalnum() or ch in "_.-") for ch in project_id)):
        raise MCPSetupError("Choose a valid project ID")
    origin = _api_origin(values)
    status, listing = _request(origin, owner, "GET", "/v1/projects")
    if status != 200 or not isinstance(listing.get("items"), list):
        raise MCPSetupError("Owner API key could not list projects")
    if project_id not in {item.get("id") for item in listing["items"] if isinstance(item, dict)}:
        raise MCPSetupError("Selected project is not available to the installer owner")

    member_path = f"/v1/members/{quote(PRINCIPAL)}?project_id={quote(project_id)}"
    status, member = _request(origin, owner, "GET", member_path)
    if status not in {200, 404}:
        raise MCPSetupError(f"MCP member lookup failed with HTTP {status}")
    if status == 200 and member.get("status") != "active":
        raise MCPSetupError("Existing MCP member is not active")
    if status == 200 and (member.get("member_type") != "system" or
                          PRINCIPAL not in (member.get("role_keys") or [])):
        raise MCPSetupError("Existing MCP member does not have the expected dedicated role")
    if status == 404 or permissions is not None:
        selected = permissions if permissions is not None else list(DEFAULT_PERMISSIONS)
        if not selected or any(not p or not p.replace("_", "").isalnum() for p in selected):
            raise MCPSetupError("At least one valid permission is required")
        status, _ = _request(origin, owner, "POST", "/v1/members", {
            "project_id": project_id,
            "member_id": PRINCIPAL,
            "display_name": "MCP operator",
            "member_type": "system",
            "role_keys": [PRINCIPAL],
            "permissions": selected,
            "actor": "ceo",
        })
        if status != 201:
            raise MCPSetupError(f"MCP member configuration failed with HTTP {status}")

    keys = list(dict.fromkeys([*_key_list(values.get("BOSS_API_KEYS", "")), owner, worker, key]))
    bindings[owner] = "ceo"
    bindings[worker] = "system"
    bindings[key] = PRINCIPAL
    for name, value in (
        ("BOSS_MCP_API_KEY", key),
        ("BOSS_API_KEYS", ",".join(keys)),
        ("BOSS_API_KEY_PRINCIPALS", ",".join(f"{token}:{principal}" for token, principal in bindings.items())),
    ):
        lines = _set(lines, name, value)
    if lines == before:
        return False
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
    return True


def launch(env_path: Path, project_id: str) -> None:
    _, values = _read_env(env_path)
    key = values.get("BOSS_MCP_API_KEY", "")
    bindings = _principal_map(values.get("BOSS_API_KEY_PRINCIPALS", ""))
    if not key or bindings.get(key) != PRINCIPAL:
        raise MCPSetupError("MCP access is unbound; run make mcp-setup first")
    environment = dict(os.environ, BOSS_MCP_API_KEY=key)
    docker_command = _docker_command(environment, key)
    command = [
        *docker_command, "compose", "-f", str(COMPOSE), "--env-file", str(env_path),
        "exec", "-T", "-e", "BOSS_MCP_API_KEY", "api", "python", "-m",
        "boss_engine", "--project", project_id, "serve-mcp", "--api-url",
        "http://127.0.0.1:8000",
    ]
    os.execvpe(docker_command[0], command, environment)


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
    if shutil.which("docker") and subprocess.run(
        ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False, timeout=5,
    ).returncode == 0:
        prefix = ["docker"]
    elif shutil.which("sudo") and subprocess.run(
        ["sudo", "-n", "docker", "info"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False, timeout=5,
    ).returncode == 0:
        prefix = ["sudo", "-n", "docker"]
    else:
        raise MCPSetupError("Docker access is required to load the new API credential")
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
            changed = configure(args.env_file, project, args.permissions)
            if changed and args.restart_api:
                _restart_api(args.env_file)
            print(f"MCP member and dedicated API key ready for project {project}." +
                  (" Restart the API to load the new binding." if changed and not args.restart_api else ""))
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
