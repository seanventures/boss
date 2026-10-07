"""A second, disposable compose project for the journeys that need what a shared, claimed stack
cannot give: an UNCLAIMED install (J1, J2, J14), API env this run controls (J7's
BOSS_OUTBOUND_ALLOW_PRIVATE for a LAN Keycloak), and the network-trusted console (J14).

It runs the SAME images as the main stack (BOSS_COMMUNITY_IMAGE_TAG from its env file) with its own
ports and FRESH secrets, written to a 0600 env file and never printed. `down()` removes its
containers AND volumes; callers run it in a `finally`.

    s2 = Stack2.from_main(main_env_file, project="bossfxd2", ports=PORTS)
    s2.up(overrides={"api": {...env}, "web": {...env}}, extra_services=None)
    s2.seed()                      # the installer's bootstrap seed: without it there is no org
    ...
    s2.down()
"""
from __future__ import annotations

import json
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from e2e.identity import kit as K

PORTS = {"BOSS_WEB_PORT": "53200", "BOSS_API_PORT": "58200", "BOSS_POSTGRES_PORT": "55802",
         "BOSS_PREVIEW_PORT": "58201", "INFISICAL_PORT": "58280"}
FRESH = ("BOSS_DB_PASSWORD", "BOSS_API_KEY", "BOSS_WORKER_API_KEY", "BOSS_CONSOLE_PASSWORD",
         "BOSS_CONSOLE_SESSION_SECRET", "INFISICAL_AUTH_SECRET")


def _env_lines(path: Path) -> list[tuple[str, str]]:
    out = []
    for line in path.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out.append((k.strip(), v))
    return out


class Stack2:
    def __init__(self, project: str, env_file: Path, override_file: Path, ports: dict[str, str]):
        self.project = project
        self.env_file = env_file
        self.override_file = override_file
        self.ports = ports
        self.web = f"http://localhost:{ports['BOSS_WEB_PORT']}"
        self.api = f"http://127.0.0.1:{ports['BOSS_API_PORT']}"
        self.stack = K.Stack(project=project, web=self.web, api=self.api, env_file=env_file)

    @classmethod
    def from_main(cls, main_env: Path, *, project: str = "bossfxd2", ports: Optional[dict[str, str]] = None,
                  out_dir: Optional[Path] = None) -> "Stack2":
        ports = dict(ports or PORTS)
        out_dir = out_dir or main_env.parent
        env_file = out_dir / "stack2.env"
        values = dict(_env_lines(main_env))
        values.update(ports)
        values["BOSS_CONSOLE_PUBLIC_ORIGIN"] = f"http://localhost:{ports['BOSS_WEB_PORT']}"
        values["BOSS_PREVIEW_ORIGIN"] = f"http://127.0.0.1:{ports['BOSS_PREVIEW_PORT']}"
        for k in FRESH:
            values[k] = secrets.token_urlsafe(32)
        values["INFISICAL_ENCRYPTION_KEY"] = secrets.token_hex(16)
        K.write_private(env_file, "".join(f"{k}={v}\n" for k, v in values.items()))
        return cls(project, env_file, out_dir / f"{project}.override.yml", ports)

    # ---- env -----------------------------------------------------------------------------------------
    def value(self, key: str) -> str:
        """A value from this stack's env file (a secret: register it before use)."""
        for k, v in _env_lines(self.env_file):
            if k == key:
                return K.secret(v) or v
        return ""

    def set_values(self, **kv: str) -> None:
        values = dict(_env_lines(self.env_file))
        values.update(kv)
        K.write_private(self.env_file, "".join(f"{k}={v}\n" for k, v in values.items()))

    # ---- compose -------------------------------------------------------------------------------------
    def _base(self) -> list[str]:
        # The console is behind the `web` profile; the main stack's installer enables it, but a copied
        # env file does not carry that (round 2: J14's TLS proxy "depends on undefined service web").
        cmd = ["docker", "compose", "-p", self.project, "--profile", "web", "--env-file", str(self.env_file),
               "-f", str(K.COMPOSE_FILE)]
        if self.override_file.exists():
            cmd += ["-f", str(self.override_file)]
        return cmd

    def compose(self, *args: str, timeout: int = 600, check: bool = True) -> subprocess.CompletedProcess:
        done = subprocess.run([*self._base(), *args], capture_output=True, text=True, timeout=timeout)
        if check and done.returncode != 0:
            raise RuntimeError(f"compose {' '.join(args[:2])} failed: " + K.scrub(done.stderr.strip())[-600:])
        return done

    def write_override(self, services: dict[str, dict[str, Any]]) -> None:
        """A compose override as JSON (YAML is a superset): {service: {environment: {...}, ...}}."""
        self.override_file.write_text(json.dumps({"services": services}, indent=2))

    def up(self, services: Optional[dict[str, dict[str, Any]]] = None, wait_s: int = 300) -> None:
        if services is not None:
            self.write_override(services)
        elif self.override_file.exists():
            self.override_file.unlink()
        self.compose("up", "-d", "db", "api", "web", "worker", "egress", *([s for s in (services or {}) if s not in
                     ("db", "api", "web", "worker", "egress")]), timeout=900)
        deadline = time.time() + wait_s
        while time.time() < deadline:
            st, _ = K.http("GET", f"{self.web}/v1/auth/setup/status")
            if st == 200:
                return
            time.sleep(3)
        raise RuntimeError(f"{self.project} did not answer /v1/auth/setup/status within {wait_s}s")

    def seed(self) -> None:
        """The installer's bootstrap seed (org, workspace, project). Without it the claim 404s."""
        done = self.compose("exec", "-T", "-u", "boss", "api", "python", "-c",
                            "from boss_engine.bootstrap import seed; seed()", timeout=300, check=False)
        if done.returncode != 0:
            raise RuntimeError("bootstrap seed failed: " + K.scrub(done.stderr.strip())[-400:])

    def host(self) -> Any:
        """Run `python <argv>` in the api container as `boss` (a host-shell command)."""
        def run(argv: list[str]) -> subprocess.CompletedProcess:
            return subprocess.run([*self._base(), "exec", "-T", "-u", "boss", "-e", "BOSS_ENVIRONMENT=e2e", "api",
                                   "python", *argv], capture_output=True, text=True, timeout=180)
        return run

    def down(self) -> None:
        self.compose("down", "-v", "--remove-orphans", timeout=600, check=False)

    def reset(self, services: Optional[dict[str, dict[str, Any]]] = None) -> None:
        self.down()
        self.up(services)
        self.seed()

    def network(self) -> str:
        return f"{self.project}_default"

    def logs(self, service: str, tail: int = 200) -> str:
        return K.scrub(self.compose("logs", "--no-color", "--tail", str(tail), service, check=False).stdout)


def docker_headroom_gib() -> float:
    """Free memory in the docker VM: total minus the sum of running containers' usage."""
    info = subprocess.run(["docker", "info", "--format", "{{.MemTotal}}"], capture_output=True, text=True).stdout.strip()
    total = int(info or 0)
    used = 0.0
    stats = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}"], capture_output=True,
                           text=True).stdout.splitlines()
    units = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}
    for line in stats:
        cur = line.split("/")[0].strip()
        for u in ("GiB", "MiB", "KiB", "B"):
            if cur.endswith(u):
                try:
                    used += float(cur[: -len(u)]) * units[u]
                except ValueError:
                    pass
                break
    return (total - used) / 1024 ** 3
