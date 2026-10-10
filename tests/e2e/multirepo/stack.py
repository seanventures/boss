"""The multi-repo proof's ONE disposable compose stack (project `bossmr` by default).

Its own ports, its own images (built from THIS checkout under the tag `mr`), fresh secrets in a
0600 env file that is never printed, the fake GitHub as the organisation `acme` with three
repositories (compose-override.yml), and the stub deployment host (deploy-host.yml). `down()`
removes its containers AND volumes; the driver calls it in a `finally`.

Never touches another compose project: every command carries `-p <project>`, and `up()` refuses
the operator's `boss-community` tag.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "tests"))

from e2e.harness import identity as I  # noqa: E402

COMPOSE_FILES = [REPO / "scripts/install/community-compose.yml", REPO / "tests/e2e/compose/build-override.yml",
                 REPO / "tests/e2e/compose/deploy-host.yml", HERE / "compose-override.yml"]
PORTS = {"BOSS_WEB_PORT": "53400", "BOSS_API_PORT": "58400", "BOSS_POSTGRES_PORT": "55804",
         "BOSS_PREVIEW_PORT": "58401", "INFISICAL_PORT": "58480", "FXD_FAKEGH_PORT": "58490"}
SERVICES = ("db", "api", "worker", "egress", "fakegh", "web")


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)


class Stack:
    def __init__(self, workdir: Path, project: str = "bossmr", tag: str = "mr"):
        if tag in ("", "boss-community") or project in ("boss-community", "boss"):
            raise SystemExit("refusing to run the proof on the operator's project or images")
        self.dir = workdir
        self.project = project
        self.tag = tag
        self.env_file = workdir / "stack.env"
        self.web = f"http://localhost:{PORTS['BOSS_WEB_PORT']}"
        self.api = f"http://127.0.0.1:{PORTS['BOSS_API_PORT']}"
        self.fake = f"http://127.0.0.1:{PORTS['FXD_FAKEGH_PORT']}"

    # ---- env -------------------------------------------------------------------------------------
    def write_env(self) -> None:
        """Fresh secrets every time (0600, never printed)."""
        sha = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True).stdout.strip()
        tok = lambda: secrets.token_urlsafe(32)  # noqa: E731
        api, wk = tok(), tok()
        values = {
            "COMPOSE_PROFILES": "web", "BOSS_COMMUNITY_IMAGE_TAG": self.tag,
            "BOSS_SOURCE_SHA": sha, "BOSS_BUILT_AT": time.strftime("%Y-%m-%d"),
            **PORTS,
            "BOSS_CONSOLE_PUBLIC_ORIGIN": self.web,
            "BOSS_PREVIEW_ORIGIN": f"http://127.0.0.1:{PORTS['BOSS_PREVIEW_PORT']}",
            "BOSS_AUTHZ_MODE": "enforce",
            "BOSS_DB_PASSWORD": tok(), "BOSS_API_KEY": api, "BOSS_WORKER_API_KEY": wk,
            "BOSS_API_KEYS": f"{api},{wk}", "BOSS_API_KEY_PRINCIPALS": f"{api}:ceo,{wk}:system",
            "BOSS_CONSOLE_PASSWORD": tok(), "BOSS_CONSOLE_SESSION_SECRET": tok(),
            "INFISICAL_AUTH_SECRET": tok(), "INFISICAL_ENCRYPTION_KEY": secrets.token_hex(16),
            "BOSS_TELEMETRY": "off", "DO_NOT_TRACK": "1",
        }
        _write_private(self.env_file, "".join(f"{k}={v}\n" for k, v in values.items()))

    def value(self, key: str) -> str:
        for line in self.env_file.read_text().splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1]
        return ""

    # ---- compose ---------------------------------------------------------------------------------
    def base(self) -> list[str]:
        cmd = ["docker", "compose", "-p", self.project, "--profile", "web", "--env-file", str(self.env_file)]
        for f in COMPOSE_FILES:
            cmd += ["-f", str(f)]
        return cmd

    def compose(self, *args: str, timeout: int = 1800, log: Optional[Path] = None,
                check: bool = True) -> subprocess.CompletedProcess:
        out = open(log, "a") if log else subprocess.PIPE
        try:
            done = subprocess.run([*self.base(), *args], stdout=out, stderr=subprocess.STDOUT if log else subprocess.PIPE,
                                  text=True, timeout=timeout)
        finally:
            if log:
                out.close()
        if check and done.returncode != 0:
            tail = log.read_text()[-800:] if log else (done.stderr or "")[-800:]
            raise RuntimeError(f"compose {' '.join(args[:2])} failed: {tail}")
        return done

    def build(self) -> None:
        self.compose("build", "api", "worker", "egress", "web", log=self.dir / "build.log", timeout=3600)

    def up(self, wait_s: int = 400) -> None:
        self.compose("up", "-d", *SERVICES, log=self.dir / "up.log", timeout=900)
        deadline = time.time() + wait_s
        while time.time() < deadline:
            try:
                if I.setup_status(self.web).get("claimed") is not None:
                    return
            except Exception:  # noqa: BLE001 — not answering yet
                pass
            time.sleep(4)
        raise RuntimeError(f"{self.project}: the console did not answer within {wait_s}s")

    def exec(self, service: str, argv: list[str], *, user: str = "boss", input_text: Optional[str] = None,
             timeout: int = 300) -> subprocess.CompletedProcess:
        return subprocess.run(["docker", "exec", "-i", "-u", user, f"{self.project}-{service}-1", *argv],
                              input=input_text, capture_output=True, text=True, timeout=timeout)

    def seed_and_claim(self) -> None:
        """The installer's bootstrap seed, the first Owner's claim, one seeded person per role
        (accounts.json, 0600) — the way the other compose walks do it."""
        done = self.exec("api", ["python", "-c", "from boss_engine.bootstrap import seed; seed()"])
        if done.returncode != 0:
            raise RuntimeError("bootstrap seed failed: " + done.stderr[-400:])
        cf = COMPOSE_FILES[0]
        host = I.compose_host(self.project, cf, self.env_file)
        if I.setup_status(self.web).get("claimed") is False:
            pw = I.claim_password()
            _write_private(self.dir / "owner.json", json.dumps({"email": I.Claimant().email, "password": pw}))
            I.claim_owner(self.web, I.setup_token(host), I.Claimant(password=pw))
        I.seed_accounts(host, I.Claimant().email, self.dir / "accounts.json",
                        copy_out=I.compose_copy_out(self.project, cf, self.env_file))

    def down(self) -> None:
        self.compose("down", "-v", "--remove-orphans", log=self.dir / "down.log", timeout=900, check=False)

    def left_running(self) -> list[str]:
        out = subprocess.run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={self.project}",
                              "--format", "{{.Names}}"], capture_output=True, text=True).stdout.split()
        return out
