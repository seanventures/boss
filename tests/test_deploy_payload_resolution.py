"""Which app gets deployed, and how it is run.

Two bugs live here, both found by an actual deployment rather than by reading code:

  * A stock-app target went live serving SUDOKU. `_build_dist` picked the first `*/index.html`
    in alphabetical order from a repo holding three apps, and the HTTP health check passed —
    a 200 from the wrong app is still a 200. Health verifies liveness, never identity.
  * The stock app is a server, not a file tree: it serves its own UI *and* its /api/* routes.
    Shipping only its web/ directory to nginx yields a UI where every action fails.

`_build_dist` therefore returns (root, mode, entry) and defers "which app" to the SAME resolver
the preview uses. These tests pin that contract, including the tuple shape — a stale bare
`return work` on the static path survived the first edit precisely because the live deployment
exercised only the `py` branch.

Run directly (pytest collection wipes the local BOSS database):
    PYTHONPATH=engine .venv/bin/python tests/test_deploy_payload_resolution.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "engine")

from boss_engine.services import deploy_service as ds  # noqa: E402


class _Settings:
    github_token = ""
    target_repo_host = "github"
    target_github_owner = "acme"
    target_github_repo = "sandbox"


_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        _failures.append(f"{name}: {detail}")
        print(f"  FAIL  {name} — {detail}")


def _with_fixture(fixture: Path):
    """Replace the git clone with a copy of `fixture`, leaving the rest of _build_dist real."""
    real_run = ds.subprocess.run

    def fake_run(argv, *a, **kw):
        if isinstance(argv, list) and argv[:2] == ["git", "clone"]:
            dest = Path(argv[-1])
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(fixture, dest)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return real_run(argv, *a, **kw)

    return fake_run, real_run


def _build(fixture: Path, changed):
    fake, real = _with_fixture(fixture)
    ds.subprocess.run = fake
    try:
        return ds._build_dist(_Settings(), "acme", "sandbox", "branch", changed=changed)
    finally:
        ds.subprocess.run = real


def _multi_app_repo(root: Path) -> Path:
    """A repo holding three apps — the shape that produced the Sudoku deployment."""
    (root / "web").mkdir(parents=True)
    (root / "web" / "index.html").write_text("<title>Sudoku</title>")
    (root / "web-stock").mkdir()
    (root / "web-stock" / "index.html").write_text("<title>Vantage Trade</title>")
    (root / "web-nqueens").mkdir()
    (root / "web-nqueens" / "index.html").write_text("<title>N-Queens</title>")
    (root / "serve_stock.py").write_text("# stock server\n")
    return root


def test_python_app_deploys_as_runtime_not_static() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = _multi_app_repo(Path(tmp) / "repo")
        root, mode, entry = _build(fixture, ["serve_stock.py", "web-stock/index.html"])
        check("mode is py for a server app", mode == "py", f"got {mode!r}")
        check("entry is the server this task wrote", entry == "serve_stock.py", f"got {entry!r}")
        check("payload is laid out under dist/", (root / "dist").is_dir())
        check("payload carries the SERVER, not just the web dir",
              (root / "dist" / "serve_stock.py").is_file())
        check("payload carries the app's UI", (root / "dist" / "web-stock" / "index.html").is_file())
        shutil.rmtree(root, ignore_errors=True)


def test_static_app_picks_the_one_this_task_built() -> None:
    """The Sudoku regression: with three apps present, the task's own app must win."""
    with tempfile.TemporaryDirectory() as tmp:
        fixture = _multi_app_repo(Path(tmp) / "repo")
        (fixture / "serve_stock.py").unlink()  # no server -> a purely static deployment
        root, mode, entry = _build(fixture, ["web-nqueens/index.html"])
        check("mode is static", mode == "static", f"got {mode!r}")
        check("returns a 3-tuple, not a bare path", isinstance(root, Path))
        served = (root / "dist" / "index.html").read_text()
        check("serves N-QUEENS, the app this task built", "N-Queens" in served,
              f"served {served!r} — the wrong-app deployment bug")
        check("does NOT serve Sudoku", "Sudoku" not in served)
        shutil.rmtree(root, ignore_errors=True)


def test_ambiguous_repo_fails_closed() -> None:
    """No signal from the task + several candidates = refuse. Wrong app is worse than none."""
    with tempfile.TemporaryDirectory() as tmp:
        fixture = _multi_app_repo(Path(tmp) / "repo")
        (fixture / "serve_stock.py").unlink()
        try:
            _build(fixture, [])
            check("ambiguous repo is refused", False, "it deployed something anyway")
        except RuntimeError as exc:
            check("ambiguous repo is refused", "cannot tell which app" in str(exc),
                  f"got {exc}")
        except Exception as exc:  # noqa: BLE001
            check("ambiguous repo is refused", False, f"unexpected {type(exc).__name__}: {exc}")


def test_run_command_matches_the_mode() -> None:
    py = ds._docker_run_cmd("stock-app", 3004, "/opt/boss-apps/stock-app", "py", "serve_stock.py")
    check("py mode runs python, not nginx", "python:3.12-slim" in py and "nginx" not in py)
    check("py mode publishes the declared port", "-p 3004:80" in py)
    check("py mode mounts read-WRITE for sqlite", "/dist:/app " in py and ":/app:ro" not in py)
    check("py mode injects PORT/HOST", "-e PORT=80" in py and "-e HOST=0.0.0.0" in py)

    static = ds._docker_run_cmd("some-app", 8080, "/opt/boss-apps/some-app")
    check("static mode runs nginx", "nginx:alpine" in static and "python" not in static)
    check("static mode mounts read-only", ":/usr/share/nginx/html:ro" in static)

    for name, cmd in (("py", py), ("static", static)):
        check(f"{name} mode retries the rm/run port race",
              "external connectivity" in cmd and "docker rm -f" in cmd)


def test_second_deploy_can_replace_what_the_first_one_left() -> None:
    """A container writes into its bind mount as ROOT; the deploy user then cannot delete it.

    The first deploy succeeded and the second died on
    `rm: cannot remove '.../__pycache__/*.pyc': Permission denied`, stranding the app on its
    first release. Two independent guards, because either alone is insufficient: stop creating
    the files, AND be able to remove files we did not create (a SQLite file still appears).
    """
    rm = ds._rm_rf_cmd("/opt/boss-apps/stock-app/dist")
    check("cheap path first", rm.startswith("rm -rf /opt/boss-apps/stock-app/dist"))
    check("falls back to a ROOT container when something survives",
          "docker run --rm" in rm and "alpine" in rm,
          "an unprivileged rm cannot delete root-owned files; there must be a fallback")
    check("fallback targets the mounted path, not the host path",
          "/_x/dist" in rm)
    check("never fails the deploy on cleanup", rm.rstrip().endswith("true"))

    py = ds._docker_run_cmd("a", 3004, "/opt/boss-apps/a", "py", "s.py")
    check("container does not write bytecode into the mount",
          "-e PYTHONDONTWRITEBYTECODE=1" in py,
          "root-owned __pycache__ is what broke the second deploy")
    check("a persistent data mount exists outside the replaced tree",
          "/opt/boss-apps/a/data:/data" in py and "-e DATA_DIR=/data" in py,
          "without it, app state lives in the tree every deploy destroys")

    # The destructive steps must ALL use the root-capable removal, not just the one that
    # happened to fail first. A guard applied to one path is not a guard.
    src = open("engine/boss_engine/services/deploy_service.py").read()
    transfer = ds._transfer_shell_cmd(
        work=Path("/tmp/w"), key_path="/tmp/k", host="h", user="u", ssh_port=22,
        remote="/opt/boss-apps/a")
    check("the transfer's replace is root-capable", "docker run --rm" in transfer)
    check("the transfer creates the persistent data dir", "/data" in transfer)
    check("rollback also uses the root-capable removal",
          src.count("_rm_rf_cmd(remote") >= 3,
          "transfer, rollback and staging must all use it")


if __name__ == "__main__":
    for fn in (test_python_app_deploys_as_runtime_not_static,
               test_second_deploy_can_replace_what_the_first_one_left,
               test_static_app_picks_the_one_this_task_built,
               test_ambiguous_repo_fails_closed,
               test_run_command_matches_the_mode):
        print(f"\n{fn.__name__}")
        fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all deploy payload-resolution checks passed")
