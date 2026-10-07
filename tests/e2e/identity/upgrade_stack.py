"""The disposable compose project for the upgrade walk (bossfxd3), and a scratch "install directory"
that points the REAL host tools (scripts/install/boss.sh, checkup.sh, make mcp-setup) at it.

THE STACK. Same images as the other FX-D stacks (BOSS_COMMUNITY_IMAGE_TAG from the env file, `fxda`),
its own ports and FRESH secrets in a 0600 env file that is never printed. `down()` removes its
containers and volumes; the walk calls it in a `finally`.

THE SCRATCH INSTALL. boss.sh finds an install by its directory: `<dir>/.env` and
`<dir>/scripts/install/community-compose.yml`. The walk makes `<scratch>/install` with a copy of this
worktree's scripts/ and Makefile and the stack's env as `.env`, and runs the real scripts there.
From then on that `.env` IS the stack's env file (boss.sh and mcp_access.py write keys into it).

WHY A docker SHIM. Every host tool pins the operator's own project: the compose file says
`name: boss-community`, boss.sh defaults COMPOSE_PROJECT_NAME to it, and checkup.sh execs into the
literal container names `boss-community-api-1` / `-web-1` and filters `ps` by `boss-community`.
Run unmodified on this machine they would read (and `make mcp-setup --restart-api` would RECREATE)
the operator's real install. The shim, first on PATH for those commands only, pins every
`docker compose` to this walk's project (`-p`) and maps the literal `boss-community` container
names (never image references) to it. It changes nothing else; the walk records the hardcoded names as a finding.
"""
from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from e2e.identity import kit as K
from e2e.identity.stack2 import Stack2, _env_lines, refresh_secrets

REPO = K.REPO
PORTS3 = {"BOSS_WEB_PORT": "53300", "BOSS_API_PORT": "58300", "BOSS_POSTGRES_PORT": "55803",
          "BOSS_PREVIEW_PORT": "58301", "INFISICAL_PORT": "58380"}
FRESH = ("BOSS_DB_PASSWORD", "BOSS_API_KEY", "BOSS_WORKER_API_KEY", "BOSS_CONSOLE_PASSWORD",
         "BOSS_CONSOLE_SESSION_SECRET", "INFISICAL_AUTH_SECRET")


def write_env(path: Path, values: dict[str, str]) -> None:
    K.write_private(path, "".join(f"{k}={v}\n" for k, v in values.items()))


def read_env(path: Path) -> dict[str, str]:
    return dict(_env_lines(path))


class Stack3(Stack2):
    """bossfxd3: Stack2's compose/seed/host/down, with this walk's env file and ports."""

    @classmethod
    def create(cls, shape_env: Path, env_file: Path, *, project: str = "bossfxd3", tag: str = "fxda",
               extra: Optional[dict[str, str]] = None) -> "Stack3":
        values = read_env(shape_env)           # the SHAPE only; every secret is replaced below
        values.update(PORTS3)
        values["BOSS_COMMUNITY_IMAGE_TAG"] = tag
        values["BOSS_CONSOLE_PUBLIC_ORIGIN"] = f"http://localhost:{PORTS3['BOSS_WEB_PORT']}"
        values["BOSS_PREVIEW_ORIGIN"] = f"http://127.0.0.1:{PORTS3['BOSS_PREVIEW_PORT']}"
        values["BOSS_AUTHZ_MODE"] = "enforce"
        values["COMPOSE_PROFILES"] = "web"
        refresh_secrets(values, FRESH)
        values["INFISICAL_ENCRYPTION_KEY"] = secrets.token_hex(16)
        values.update(extra or {})
        for k in FRESH:
            K.secret(values[k])
        write_env(env_file, values)
        return cls(project, env_file, env_file.parent / f"{project}.override.yml", dict(PORTS3))

    def use_env(self, env_file: Path) -> None:
        """From now on compose reads this env file (the scratch install's .env)."""
        self.env_file = env_file
        self.stack.env_file = env_file

    def secrets_now(self) -> None:
        """Register every value of the env file that looks like a credential."""
        for k, v in read_env(self.env_file).items():
            if any(w in k for w in ("KEY", "PASSWORD", "SECRET", "TOKEN")):
                K.secret(v)
                for part in v.split(","):
                    K.secret(part.split(":")[0])

    def up_with(self, override_yaml: Optional[str] = None, services: tuple[str, ...] = (),
                wait_s: int = 300, check_ready: bool = True) -> None:
        """`up -d` with an optional raw YAML override (compose `!reset` tags need YAML)."""
        if override_yaml is None:
            if self.override_file.exists():
                self.override_file.unlink()
        else:
            self.override_file.write_text(override_yaml)
        self.compose("up", "-d", *(services or ("db", "api", "web", "worker", "egress")), timeout=900)
        if check_ready:
            self.wait_ready(wait_s)

    def wait_ready(self, wait_s: int = 300) -> None:
        import time
        deadline = time.time() + wait_s
        while time.time() < deadline:
            st, _ = K.http("GET", f"{self.web}/v1/auth/setup/status")
            if st == 200:
                return
            time.sleep(3)
        raise RuntimeError(f"{self.project} did not answer /v1/auth/setup/status within {wait_s}s")

    def api_health(self) -> int:
        st, _ = K.http("GET", f"{self.api}/health", timeout=10)
        return st

    def container_state(self, service: str = "api") -> str:
        done = subprocess.run(["docker", "inspect", "-f", "{{.State.Status}}|{{.State.ExitCode}}|{{.RestartCount}}",
                               f"{self.project}-{service}-1"], capture_output=True, text=True, timeout=30)
        return done.stdout.strip() or done.stderr.strip()[:200]

    def in_api(self, *argv: str, user: str = "boss", timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run([*self._base(), "exec", "-T", "-u", user, "api", *argv],
                              capture_output=True, text=True, timeout=timeout)


# ---- the docker shim and the scratch install ----------------------------------------------------

SHIM = r"""#!/usr/bin/env bash
# FX-D upgrade walk: pin host tools to the walk's compose project (see upgrade_stack.py).
real=%(real)s
proj="${FXD_PROJECT:?FXD_PROJECT unset: refusing to guess a compose project}"
args=()
for a in "$@"; do
  # only container names (boss-community-<service>-<n>) and `ps --filter name=boss-community…`;
  # image references (boss-community-api:<tag>) are left alone
  if [[ "$a" =~ ^boss-community-[a-z0-9-]+-[0-9]+$ ]]; then a="$proj-${a#boss-community-}"
  elif [[ "$a" == name=boss-community* ]]; then a="name=$proj${a#name=boss-community}"
  fi
  args+=("$a")
done
if [[ "${args[0]:-}" == compose ]]; then
  exec "$real" compose -p "$proj" "${args[@]:1}"
fi
exec "$real" "${args[@]}"
"""


def make_shim(bin_dir: Path) -> Path:
    real = shutil.which("docker")
    if not real:
        raise RuntimeError("docker is not on PATH")
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "docker"
    shim.write_text(SHIM % {"real": real})
    shim.chmod(0o755)
    return shim


def make_install(root: Path, env_from: Path) -> Path:
    """<root>/install: scripts/ and Makefile copied from this worktree, .env from the stack."""
    inst = root / "install"
    if inst.exists():
        shutil.rmtree(inst)
    inst.mkdir(parents=True)
    shutil.copytree(REPO / "scripts", inst / "scripts", symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(REPO / "Makefile", inst / "Makefile")
    shutil.copy2(env_from, inst / ".env")
    os.chmod(inst / ".env", 0o600)
    return inst


def tool_env(project: str, shim_dir: Path, *, home: Optional[Path] = None, tag: str = "fxda",
             extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    """The environment the real host tools run with: the shim first on PATH, the project pinned."""
    env = dict(os.environ)
    env["PATH"] = f"{shim_dir}:{env.get('PATH', '')}"
    env["FXD_PROJECT"] = project
    env["COMPOSE_PROJECT_NAME"] = project
    env["BOSS_COMMUNITY_IMAGE_TAG"] = tag
    if home is not None:
        env["DOCKER_CONFIG"] = env.get("DOCKER_CONFIG") or str(Path.home() / ".docker")
        env["HOME"] = str(home)
        env.pop("XDG_CONFIG_HOME", None)
    for k in ("BOSS_API_KEY", "BOSS_HOST_API_KEY", "BOSS_INSTALL_DIR", "BOSS_ENV_FILE"):
        env.pop(k, None)
    env.update(extra or {})
    return env


def run_tool(argv: list[str], env: dict[str, str], cwd: Path, timeout: int = 600) -> dict[str, Any]:
    done = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)
    return {"rc": done.returncode, "out": done.stdout, "err": done.stderr}
