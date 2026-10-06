"""Boot a disposable BOSS — Postgres, the real API, the real worker, optionally the console —
wired to the harness's stubs, with transport auth ON exactly as an installed BOSS runs it.

WHY NOT DOCKER COMPOSE FOR API AND WORKER. Building the three images takes the better part of
ten minutes, and a harness people do not run because it is slow protects nothing. The processes
here are the same entry points the images run (`uvicorn apps.api.app.main:app`,
`python -m apps.worker.boss_worker.main`, `next start`) with the same environment contract as
scripts/install/community-compose.yml: BOSS_REQUIRE_API_AUTH=true, an owner key bound to `ceo`
and a worker key bound to `system`. Postgres runs in Docker, under a container name of its own,
on a database named boss_e2e — never `boss`, and never the operator's install.

ISOLATION FROM THE DEVELOPER'S MACHINE. API and worker get a scratch HOME and a PATH whose first
entry is tests/e2e/stubs/bin, so the only `claude` they can find is the scripted one and no
logged-in subscription on this laptop can be reached or billed. The environment is BUILT, not
inherited: an OPENAI_API_KEY exported in the developer's shell must not turn a scripted run into
a paid one.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import E2E_ROOT, REPO_ROOT, teardown
from .target import Target, TargetError

STUBS = E2E_ROOT / "stubs"
STUB_BIN = STUBS / "bin"
PG_IMAGE = "postgres:16-alpine"
DB_NAME = "boss_e2e"
E2E_REPO_OWNER = "e2e-org"
E2E_REPO_NAME = "e2e-app"
E2E_GITHUB_TOKEN = "e2e-github-token"
# What may pass through from the developer's environment. Everything else is built.
PASSTHROUGH = ("LANG", "LC_ALL", "TMPDIR", "TERM", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
               "DOCKER_HOST")


LATEST_POINTER = REPO_ROOT / "artifacts" / "e2e" / "latest-stack.json"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_http(url: str, *, timeout: float, what: str, log: Optional[Path] = None,
              proc: Optional[subprocess.Popen] = None) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            raise TargetError(f"{what} exited with {proc.returncode} before answering. "
                              f"Its log: {log}\n{_tail(log)}")
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status < 500:
                    return
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return
            last = f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001 — not up yet
            last = str(exc)
        time.sleep(0.5)
    raise TargetError(f"{what} did not answer at {url} within {int(timeout)}s ({last}). "
                      f"Its log: {log}\n{_tail(log)}")


def _tail(path: Optional[Path], lines: int = 30) -> str:
    if not path or not path.exists():
        return ""
    return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])


@dataclass
class StackState:
    run_dir: str
    pg_container: str
    pg_port: int
    db_password: str
    api_port: int
    model_port: int
    fakes_port: int
    smtp_port: int
    web_port: Optional[int]
    owner_key: str
    worker_key: str
    console_password: str
    session_secret: str
    pids: dict[str, int] = field(default_factory=dict)
    edition: str = "community"
    # The mark everything this stack starts carries (teardown.py): a docker label value and an
    # environment variable. Older stack.json files have none; their run dir's name stands in.
    run_id: str = ""
    # Start time of each recorded PID, so a later `down` never signals a PID the OS
    # has since handed to something else.
    fingerprints: dict[str, str] = field(default_factory=dict)
    stopped: bool = False

    def __post_init__(self) -> None:
        if not self.run_id:
            self.run_id = f"e2e-{Path(self.run_dir).name}"

    @property
    def database_url(self) -> str:
        return (f"postgresql+psycopg://boss:{self.db_password}@127.0.0.1:{self.pg_port}/"
                f"{DB_NAME}")

    def target(self) -> Target:
        return Target(
            kind="local", api_url=f"http://127.0.0.1:{self.api_port}",
            owner_key=self.owner_key, worker_key=self.worker_key,
            console_url=f"http://127.0.0.1:{self.web_port}" if self.web_port else None,
            console_password=self.console_password,
            model_stub_url=f"http://127.0.0.1:{self.model_port}",
            fakes_url=f"http://127.0.0.1:{self.fakes_port}", smtp_port=self.smtp_port,
            database_url=self.database_url,
        )

    def save(self) -> Path:
        path = Path(self.run_dir) / "stack.json"
        path.write_text(json.dumps(asdict(self), indent=2))
        os.chmod(path, 0o600)  # holds the stack's throwaway keys
        latest = LATEST_POINTER
        if self.stopped:
            # A stopped stack is no longer "the" stack: `status` and `workflows` must say so
            # instead of calling a dead API (and `down` must not signal recorded PIDs again).
            if latest.exists() and latest.read_text().strip() == str(path):
                latest.unlink()
        else:
            latest.parent.mkdir(parents=True, exist_ok=True)
            latest.write_text(str(path))
        return path

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "StackState":
        if path is None:
            if not LATEST_POINTER.exists():
                raise TargetError("No local stack is up. Start one with `scripts/e2e.sh up`.")
            path = Path(LATEST_POINTER.read_text().strip())
            if not path.exists():
                raise TargetError(f"No local stack is up (the last one's record, {path}, is "
                                  f"gone). Start one with `scripts/e2e.sh up`.")
        state = cls(**json.loads(Path(path).read_text()))
        if state.stopped:
            raise TargetError(f"No local stack is up: the last one ({state.run_dir}) was "
                              f"stopped. Start one with `scripts/e2e.sh up`.")
        return state

    @classmethod
    def everything_recorded(cls) -> list["StackState"]:
        """Every stack.json under artifacts/e2e, stopped or not — for `down --all`."""
        found = []
        for path in sorted((REPO_ROOT / "artifacts" / "e2e").glob("*/stack.json")):
            try:
                found.append(cls(**json.loads(path.read_text())))
            except (OSError, ValueError, TypeError):
                continue
        return found


def service_env(state: StackState, *, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    """The API's and worker's environment: the community-compose contract, pointed at stubs."""
    home = Path(state.run_dir) / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {k: os.environ[k] for k in PASSTHROUGH if k in os.environ}
    model = f"http://127.0.0.1:{state.model_port}"
    fakes = f"http://127.0.0.1:{state.fakes_port}"
    # Subprocesses whose environment BOSS filters (the code agent's, deny-by-default) still
    # find the stubs through these files in the scratch HOME.
    (home / ".boss-e2e-model-stub-url").write_text(model)
    (home / ".boss-e2e-fakes-url").write_text(fakes)
    # The run's mark (teardown.py), also left in HOME for subprocesses with a filtered
    # environment, and the host's real docker for the labelling shim in stubs/bin.
    real_docker = teardown.real_docker() or ""
    (home / ".boss-e2e-run-id").write_text(state.run_id)
    (home / ".boss-e2e-run-dir").write_text(state.run_dir)
    (home / ".boss-e2e-real-docker").write_text(real_docker)
    ca = str(Path(state.run_dir) / "fakes" / "tls" / "ca.pem")
    # No credential helper. macOS's system gitconfig names osxkeychain, and `git clone` then
    # blocks on `credential-osxkeychain store` for the fake token until the sandbox's clone
    # timeout fires ("git clone timed out") — and would write a fake token into the
    # developer's real keychain if it ever got through.
    (home / ".gitconfig").write_text("[credential]\n\thelper =\n[init]\n\tdefaultBranch = main\n")
    env.update({
        "HOME": str(home),
        "PATH": os.pathsep.join([str(STUB_BIN), str(Path(sys.executable).parent),
                                 "/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin",
                                 "/usr/sbin", "/sbin"]),
        "PYTHONPATH": os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / "engine")]),
        "PYTHONUNBUFFERED": "1",
        "DATABASE_URL": state.database_url,
        "BOSS_EDITION": state.edition,
        # TRANSPORT AUTH ON, WITH REAL KEY→PRINCIPAL MAPPING. The whole point. See target.py.
        "BOSS_REQUIRE_API_AUTH": "true",
        "BOSS_API_KEY": state.owner_key,
        "BOSS_API_KEYS": f"{state.owner_key},{state.worker_key}",
        "BOSS_API_KEY_PRINCIPALS": f"{state.owner_key}:ceo,{state.worker_key}:system",
        "BOSS_WORKER_PRINCIPAL": "system",
        "BOSS_WORKER_API_KEY": state.worker_key,
        "BOSS_WORKER_ID": "e2e-worker-1",
        "BOSS_POLL_INTERVAL_SECONDS": "2",
        "BOSS_DEFAULT_ORG_ID": "org_boss",
        "BOSS_DEFAULT_WORKSPACE_ID": "ws_boss",
        "BOSS_DEFAULT_PROJECT_ID": "boss-dogfood",
        "BOSS_CONTROL_PLANE_URL": f"http://127.0.0.1:{state.api_port}",
        # A disposable stack is not an install to count: heartbeat and Improve BOSS are off
        # unless a walk asks for them (BOSS_E2E_TELEMETRY=on), and then they report to this
        # stack's own collector rather than leaving the machine.
        "BOSS_TELEMETRY": os.environ.get("BOSS_E2E_TELEMETRY", "off"),
        "BOSS_TELEMETRY_URL": f"http://127.0.0.1:{state.api_port}/v1/telemetry/ingest",
        "BOSS_INSTALL_METHOD": "e2e",
        "BOSS_ARTIFACTS_DIR": str(Path(state.run_dir) / "boss-artifacts"),
        "BOSS_ENABLE_CODE_TASK": "true",
        # An operator who wants `deploy` to work turns this on; the stack plays that operator.
        # NOTE scripts/install/community-compose.yml does not forward this variable, so on a
        # Community install (boss-1) it cannot be turned on and every deploy step fails with
        # "outward SSH deploys are disabled" — the harness reports that for a live target.
        "BOSS_SSH_HOST_DEPLOY_ALLOWED": "true",
        "BOSS_GOVERNED_CODING_PIPELINE": "true",
        "BOSS_BOOTSTRAP_SEED_API_ENABLED": "false",
        "BOSS_ACCOUNT_CONNECT_URL": "",
        # THE MODEL: every chat surface on the OpenAI-compatible stub, every CLI seat on the
        # scripted `claude`. Same adapters, same protocols, no spend.
        "BOSS_LLM_ENGINE": "openai",
        "BOSS_CHAT_LLM_ENGINE": "openai",
        "OPENAI_API_KEY": "e2e-stub-key",
        "OPENAI_BASE_URL": f"{model}/v1",
        "OPENAI_MODEL": "e2e-stub",
        "BOSS_CODE_AGENT_ENGINE": "claude_code",
        "BOSS_CLAUDE_CLI_PATH": str(STUB_BIN / "claude"),
        "BOSS_E2E_MODEL_STUB_URL": model,
        "BOSS_DECISION_BACKEND": "claude",
        # THE OUTSIDE WORLD, faked: mail to the SMTP sink, the tracker and GitHub to the fakes.
        "BOSS_EMAIL_REAL_SEND_ALLOWED": "true",
        "SMTP_HOST": "127.0.0.1",
        "SMTP_PORT": str(state.smtp_port),
        "BOSS_EMAIL_SMTP_USE_TLS": "false",
        "MAIL_FROM": "boss-e2e@example.test",
        "PLANE_BASE_URL": f"{fakes}/plane",
        "PLANE_API_KEY": "e2e-plane-key",
        "PLANE_WORKSPACE": "e2e",
        "BOSS_E2E_FAKES_URL": fakes,
        "BOSS_AGENT_ENV_ALLOW": "BOSS_E2E_MODEL_STUB_URL,BOSS_E2E_FAKES_URL,BOSS_E2E_RUN_ID",
        "BOSS_E2E_RUN_ID": state.run_id,
        "BOSS_E2E_RUN_DIR": state.run_dir,
        "BOSS_E2E_REAL_DOCKER": real_docker,
        # The OAuth client the YouTube connect flow uses. Google is the fakes (egress.py), so
        # the consent, the code exchange and the channel read-back all happen for real.
        "BOSS_GOOGLE_OAUTH_CLIENT_ID": "e2e-client.apps.googleusercontent.com",
        "BOSS_GOOGLE_OAUTH_CLIENT_SECRET": "e2e-client-secret",
        "BOSS_GOOGLE_OAUTH_REDIRECT_URI":
            f"http://127.0.0.1:{state.api_port}/v1/channels/youtube/callback",
        # EVERY OUTBOUND CALL THROUGH THE FAKES' PROXY, TRUSTING ONLY THIS RUN'S CA. GitHub,
        # git, YouTube and Drive are answered by fakes; any other host is refused and logged
        # (stubs/egress.py), so a scripted run cannot reach a real provider by accident.
        "HTTPS_PROXY": fakes, "https_proxy": fakes, "HTTP_PROXY": fakes, "http_proxy": fakes,
        "NO_PROXY": "127.0.0.1,localhost,::1", "no_proxy": "127.0.0.1,localhost,::1",
        "SSL_CERT_FILE": ca, "REQUESTS_CA_BUNDLE": ca, "CURL_CA_BUNDLE": ca,
        "GIT_SSL_CAINFO": ca, "NODE_EXTRA_CA_CERTS": ca,
        "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
        # The repository the build steps work in: a bare repo the git fake serves.
        "BOSS_GITHUB_TOKEN": E2E_GITHUB_TOKEN,
        "BOSS_GITHUB_OWNER": E2E_REPO_OWNER, "BOSS_GITHUB_REPO": E2E_REPO_NAME,
        "BOSS_TARGET_GITHUB_OWNER": E2E_REPO_OWNER, "BOSS_TARGET_GITHUB_REPO": E2E_REPO_NAME,
        "GIT_AUTHOR_NAME": "BOSS e2e", "GIT_AUTHOR_EMAIL": "e2e@example.test",
        "GIT_COMMITTER_NAME": "BOSS e2e", "GIT_COMMITTER_EMAIL": "e2e@example.test",
    })
    # A walk that needs more of the install configured (another provider pointed at the model
    # stub, a subscription token) says so here: a JSON object, `{model}` standing for the
    # model stub's URL. Used by the model router's walk (docs/product/MODEL_ROUTER.md).
    more = os.environ.get("BOSS_E2E_SERVICE_ENV", "").strip()
    if more:
        env.update({str(k): str(v).replace("{model}", model)
                    for k, v in json.loads(more).items()})
    if extra:
        env.update(extra)
    return env


class LocalStack:
    def __init__(self, run_dir: Optional[Path] = None, *, with_web: bool = True,
                 web_mode: str = "prod", edition: str = "community"):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.run_dir = Path(run_dir or REPO_ROOT / "artifacts" / "e2e" / stamp)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "logs").mkdir(exist_ok=True)
        self.with_web = with_web
        self.web_mode = web_mode
        self.procs: dict[str, subprocess.Popen] = {}
        self.state = StackState(
            run_dir=str(self.run_dir), run_id=f"e2e-{stamp}-{secrets.token_hex(3)}",
            pg_container=f"boss-e2e-pg-{stamp.replace('-', '')}",
            pg_port=free_port(), db_password=secrets.token_hex(12),
            api_port=free_port(), model_port=free_port(), fakes_port=free_port(),
            smtp_port=free_port(), web_port=free_port() if with_web else None,
            owner_key=f"e2e-owner-{secrets.token_hex(16)}",
            worker_key=f"e2e-worker-{secrets.token_hex(16)}",
            console_password=secrets.token_urlsafe(32),
            session_secret=secrets.token_urlsafe(32), edition=edition,
        )

    @classmethod
    def attach(cls, state: StackState, *, web_mode: str = "prod") -> "LocalStack":
        """The stack a previous `up` started, to add the console to it later."""
        stack = cls.__new__(cls)
        stack.run_dir = Path(state.run_dir)
        stack.with_web = True
        stack.web_mode = web_mode
        stack.procs = {}
        stack.state = state
        if not state.web_port:
            state.web_port = free_port()
        return stack

    # ------------------------------------------------------------------ pieces
    def _log(self, name: str) -> Path:
        return self.run_dir / "logs" / f"{name}.log"

    def _spawn(self, name: str, argv: list[str], *, env: dict[str, str],
               cwd: Path = REPO_ROOT) -> subprocess.Popen:
        handle = open(self._log(name), "ab")
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=handle,
                                stderr=subprocess.STDOUT, start_new_session=True)
        self.procs[name] = proc
        self.state.pids[name] = proc.pid
        self.state.fingerprints[name] = teardown.fingerprint(proc.pid) or ""
        self.state.save()
        return proc

    def start_postgres(self) -> None:
        if not shutil.which("docker"):
            raise TargetError("Docker is required for the local stack's Postgres. Install "
                              "Docker, or point the harness at a live install with --target live.")
        subprocess.run(
            [teardown.real_docker() or "docker", "run", "-d", "--rm",
             "--name", self.state.pg_container,
             "--label", f"{teardown.LABEL_KEY}={self.state.run_id}",
             "-e", "POSTGRES_USER=boss", "-e", f"POSTGRES_PASSWORD={self.state.db_password}",
             "-e", f"POSTGRES_DB={DB_NAME}",
             "-p", f"127.0.0.1:{self.state.pg_port}:5432", PG_IMAGE],
            check=True, stdout=subprocess.DEVNULL)
        self.state.save()
        import psycopg

        deadline = time.time() + 60
        url = self.state.database_url.replace("postgresql+psycopg", "postgresql")
        while True:
            try:
                with psycopg.connect(url, connect_timeout=2) as conn:
                    conn.execute("SELECT 1")
                return
            except Exception as exc:  # noqa: BLE001
                if time.time() > deadline:
                    raise TargetError(f"Postgres in {self.state.pg_container} never accepted "
                                      f"connections: {exc}") from exc
                time.sleep(0.5)

    def start_stubs(self) -> None:
        model = self._spawn("model-stub", [sys.executable, str(STUBS / "model_stub.py"),
                                           "--port", str(self.state.model_port)],
                            env={**os.environ, "PYTHONUNBUFFERED": "1"})
        wait_http(f"http://127.0.0.1:{self.state.model_port}/health", timeout=20,
                  what="model stub", log=self._log("model-stub"), proc=model)
        fakes = self._spawn("fakes", [sys.executable, str(STUBS / "fakes.py"),
                                      "--port", str(self.state.fakes_port),
                                      "--smtp-port", str(self.state.smtp_port),
                                      "--state-dir", str(self.run_dir / "fakes")],
                            env={**os.environ, "PYTHONUNBUFFERED": "1"})
        wait_http(f"http://127.0.0.1:{self.state.fakes_port}/health", timeout=20,
                  what="fakes", log=self._log("fakes"), proc=fakes)

    def start_api(self) -> None:
        env = service_env(self.state)
        api = self._spawn("api", [sys.executable, "-m", "uvicorn", "apps.api.app.main:app",
                                  "--host", "127.0.0.1", "--port", str(self.state.api_port)],
                          env=env)
        wait_http(f"http://127.0.0.1:{self.state.api_port}/health", timeout=240,
                  what="API", log=self._log("api"), proc=api)

    def seed(self) -> None:
        """The installer's seed, the same call install.sh makes after the API is healthy."""
        result = subprocess.run(
            [sys.executable, "-c", "from boss_engine.bootstrap import seed; seed()"],
            cwd=str(REPO_ROOT), env=service_env(self.state), capture_output=True, text=True)
        self._log("seed").write_text(result.stdout + result.stderr)
        if result.returncode != 0:
            raise TargetError(f"Seeding the e2e project failed; see {self._log('seed')}\n"
                              f"{result.stderr[-2000:]}")

    def start_worker(self) -> None:
        self._spawn("worker", [sys.executable, "-m", "apps.worker.boss_worker.main"],
                    env=service_env(self.state))

    def start_web(self) -> None:
        web_dir = REPO_ROOT / "apps" / "web"
        next_bin = web_dir / "node_modules" / ".bin" / "next"
        if not next_bin.exists():
            raise TargetError("apps/web/node_modules is missing, so the console cannot start. "
                              "Run `pnpm install` at the repository root, or pass --no-web.")
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR") if k in os.environ}
        env.update({
            "BOSS_API_URL": f"http://127.0.0.1:{self.state.api_port}",
            "NEXT_PUBLIC_BOSS_API_URL": f"http://127.0.0.1:{self.state.api_port}",
            "NEXT_PUBLIC_BOSS_PROJECT_ID": "boss-dogfood",
            "BOSS_API_KEY": self.state.owner_key,
            "BOSS_CONSOLE_PASSWORD": self.state.console_password,
            "BOSS_CONSOLE_SESSION_SECRET": self.state.session_secret,
            "NEXT_TELEMETRY_DISABLED": "1",
            "BOSS_E2E_RUN_ID": self.state.run_id,
        })
        port = str(self.state.web_port)
        if self.web_mode == "dev":
            argv = [str(next_bin), "dev", "-p", port, "-H", "127.0.0.1"]
        else:
            build = subprocess.run([str(next_bin), "build"], cwd=str(web_dir), env=env,
                                   stdout=open(self._log("web-build"), "wb"),
                                   stderr=subprocess.STDOUT)
            if build.returncode != 0:
                raise TargetError(f"`next build` failed; see {self._log('web-build')}\n"
                                  f"{_tail(self._log('web-build'))}")
            argv = [str(next_bin), "start", "-p", port, "-H", "127.0.0.1"]
        web = self._spawn("web", argv, env=env, cwd=web_dir)
        wait_http(f"http://127.0.0.1:{port}/login", timeout=300, what="console",
                  log=self._log("web"), proc=web)

    # ------------------------------------------------------------------ lifecycle
    def up(self) -> Target:
        try:
            self.start_postgres()
            self.start_stubs()
            self.start_api()
            self.seed()
            self.start_worker()
            if self.with_web:
                self.start_web()
        except BaseException:
            self.down()
            raise
        self.state.save()
        return self.state.target()

    def down(self) -> list[str]:
        return stop_state(self.state)


def stop_state(state: StackState) -> list[str]:
    """Stop the stack and everything it started; mark it stopped. Returns what is STILL alive
    afterwards, each as a sentence naming the command that removes it — empty when clean."""
    notes = teardown.stop(state.run_id, state.run_dir, state.pids, state.fingerprints,
                          state.pg_container)
    for note in notes:
        print(f"e2e down: {note}", file=sys.stderr)
    state.stopped = True
    try:
        state.save()
    except OSError:
        pass
    return teardown.leftovers(state.run_id, state.run_dir)


def stack_info(state: StackState) -> dict[str, Any]:
    target = state.target()
    return {"run_dir": state.run_dir, "api": target.api_url, "console": target.console_url,
            "model_stub": target.model_stub_url, "fakes": target.fakes_url,
            "smtp_port": state.smtp_port, "postgres": f"127.0.0.1:{state.pg_port}/{DB_NAME}"}
