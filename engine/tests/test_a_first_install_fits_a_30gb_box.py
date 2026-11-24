"""A first install fits a 30 GB box (BOSSAPP-1458).

The enterprise lane's VM (4 vCPU, 8 GB, 30 GB disk) failed `install` with "no space left on device"
while exporting the worker image. Every Python service was built from one fat stage (6.84 GB each
on disk); the console's build pushed BuildKit's garbage collector to drop the Python cache; then the
worker was built AGAIN (kg-mcp had built the same image) from scratch, as new, unshared layers.

The class fix, held here without Docker:
  * the Python images are one family of stages, and an image carries only what its service runs
    (egress: no browser, Node, CLIs, compilers or toolchains; mcp-tools: the headless browser only);
  * the installer builds each image ONCE, builds an image family together, and on a small disk
    drops a finished family's build cache before the next family (prune.sh --room --cache-only);
  * every image has a size budget (scripts/install/image_budget.py), checked by the lane, with a
    failure sentence that passes the failure_words contract;
  * the installer's "a first install needs N GB" is the measured number.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from boss_engine.services import failure_words as fw

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "Dockerfile.community"
COMPOSE = REPO / "scripts" / "install" / "community-compose.yml"
sys.path.insert(0, str(REPO / "scripts" / "install"))
import image_budget  # noqa: E402


def _stages() -> dict[str, tuple[str, str]]:
    """stage name -> (its FROM, its body)."""
    text = DOCKERFILE.read_text()
    out = {}
    parts = re.split(r"(?m)^(?=FROM )", text)
    for part in parts:
        m = re.match(r"FROM\s+(\S+)\s+AS\s+(\S+)", part)
        if m:
            out[m.group(2)] = (m.group(1), part)
    return out


def _chain(target: str) -> list[str]:
    stages, chain = _stages(), [target]
    while stages[chain[-1]][0] in stages:
        chain.append(stages[chain[-1]][0])
    return chain


def _body(chain: list[str]) -> str:
    stages = _stages()
    return "\n".join(stages[s][1] for s in chain)


# ---- the image family ------------------------------------------------------------------------

def test_the_python_images_are_one_family_and_the_slim_ones_carry_no_agent_runtime():
    for target in ("api", "worker", "runner"):
        assert _chain(target)[-4:] == ["python-app", "python-runtime", "python-browser", "python-base"][-4:]
        assert "python-runtime" in _chain(target), target
    assert _chain("egress") == ["egress", "python-base"]
    assert _chain("mcp-tools") == ["mcp-tools", "python-browser", "python-base"]
    for target in ("egress", "mcp-tools"):
        body = _body(_chain(target))
        for heavy in ("npm ci", "build-essential", "ffmpeg", "postgresql-client", "pydeps-agent",
                      "COPY --from=toolchains /opt/toolchains /opt/toolchains", "boss-sandbox-exec"):
            assert heavy not in body, (target, heavy)
        assert "COPY --from=pydeps /opt/pydeps/usr/local /usr/local" in body
        assert "COPY --from=app-src /app /app" in body
    # The toolchain MANIFEST only, in egress: it opens a toolchain's hosts when the install has it.
    assert "COPY --from=toolchains /opt/toolchains/manifest.json /opt/toolchains/manifest.json" in _body(_chain("egress"))
    assert "/opt/toolchains" not in _body(_chain("mcp-tools"))
    assert "playwright install" not in _body(_chain("egress"))
    assert "playwright install --with-deps --only-shell chromium" in _body(_chain("mcp-tools"))


def test_python_dependencies_come_last_so_a_requirements_change_rebuilds_only_them():
    """A requirements bump must not rebuild (and re-store beside the rollback image) the browser,
    the CLIs or the toolchains: the dependencies are their own stages, copied in after them."""
    stages = _stages()
    for stage in ("python-base", "python-browser", "python-runtime"):
        body = stages[stage][1]
        assert "requirements.txt" not in body and "pip install --no-cache-dir --root" not in body, stage
        assert "COPY --from=pydeps" not in body, stage
    app = stages["python-app"][1]
    assert app.index("COPY --from=pydeps ") < app.index("COPY --from=pydeps-agent ") < app.index("COPY --from=app-src")
    # Each stage reads only its part of the requirements (a COPY is cached by content).
    assert "COPY --from=pins /pins/core.txt" in stages["pydeps"][1]
    assert "COPY --from=pins /pins/agent.txt" in stages["pydeps-agent"][1]
    assert "COPY --from=pins /pins/playwright.txt" in stages["python-browser"][1]
    assert "requirements.txt" in stages["pins"][1]


def test_the_runtime_user_keeps_the_ids_every_earlier_install_has():
    base = _stages()["python-base"][1]
    assert "addgroup --system --gid 102 boss" in base
    assert "adduser --system --uid 100 --ingroup boss" in base


def test_the_code_is_assembled_once_and_copied_whole():
    stages = _stages()
    assert stages["app-src"][0] == "python-base"
    copies = [s for s, (_f, body) in stages.items() if "COPY --from=app-src /app /app" in body]
    assert sorted(copies) == ["egress", "mcp-tools", "python-app"]
    # One assembled tree: no image stage copies source files of its own.
    for stage in ("python-base", "python-browser", "python-runtime", "python-app", "egress", "mcp-tools"):
        assert not re.search(r"(?m)^COPY (engine|apps|tools|templates) ", stages[stage][1]), stage


def test_the_glibc_image_does_not_carry_the_musl_claude_cli():
    runtime = _stages()["python-runtime"][1]
    assert "rm -rf /opt/boss-clis/node_modules/@anthropic-ai/claude-code-linux-*-musl" in runtime
    assert "claude --version" in runtime


# ---- the installer builds each image once, families together ----------------------------------

def _installer_block(name: str) -> str:
    source = (REPO / "install.sh").read_text()
    return source.split(f"# ---- {name} ---", 1)[1].split(f"# ---- end {name} ---", 1)[0].split("\n", 1)[1]


def _run_builds(tmp_path: Path, services: dict) -> tuple[list[str], str]:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"services": services}))
    log = tmp_path / "log"
    fake = tmp_path / "compose"
    fake.write_text(f"""#!/usr/bin/env python3
import os, pathlib, sys
args = sys.argv[1:]
if args[-3:] == ["config", "--format", "json"]:
    print(pathlib.Path({str(config)!r}).read_text())
elif "build" in args:
    open({str(log)!r}, "a").write("build " + args[-1] + "\\n")
""")
    fake.chmod(0o755)
    work = tmp_path / "install"
    (work / "scripts" / "install").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "install" / "build_order.py", work / "scripts" / "install")
    (work / "scripts" / "install" / "prune.sh").write_text(
        f'printf "prune %s\\n" "$*" >> {str(log)!r}\n'
        "echo \"Docker's disk has 9 GB free, under the 20 GB floor (BOSS_DISK_FLOOR_GB): keeping 0 GB of build cache\"\n")
    script = tmp_path / "build.sh"
    script.write_text(
        "set -euo pipefail\nui_info() { printf 'INFO %s\\n' \"$*\"; }\nui_warn() { printf 'WARN %s\\n' \"$*\"; }\n"
        "die() { printf '%s\\n' \"$*\" >&2; exit 1; }\n"
        f"COMPOSE=({fake}); COMPOSE_PROFILES=''; ENV_FILE=.env; INSTALL_DIR={work}\n"
        "BOSS_SOURCE_SHA=x BOSS_BUILT_AT=y\n"
        "COMPOSE_FILES='-f scripts/install/community-compose.yml'\n"
        + _installer_block("serial image builds"))
    result = subprocess.run(["bash", str(script)], cwd=work, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return log.read_text().splitlines(), result.stdout


def _community(target: str, image: str) -> dict:
    return {"build": {"context": str(REPO), "dockerfile": "Dockerfile.community", "target": target},
            "image": image}


def test_each_image_builds_once_and_each_family_together_with_its_cache_dropped_between(tmp_path):
    # Compose's own order, as the lane saw it: the console between two Python images, and kg-mcp
    # on the worker's image.
    services = {
        "api": _community("api", "boss-community-api:t"),
        "build-services": {"build": {"context": str(REPO / "scripts" / "install"),
                                     "dockerfile": "db-image/Dockerfile"}, "image": "boss-community-db:pg"},
        "db": {"build": {"context": str(REPO / "scripts" / "install"),
                         "dockerfile": "db-image/Dockerfile"}, "image": "boss-community-db:pg"},
        "egress": _community("egress", "boss-community-egress:t"),
        "kg-mcp": _community("worker", "boss-community-worker:t"),
        "mcp-tools": _community("mcp-tools", "boss-community-mcp-tools:t"),
        "web": _community("web", "boss-community-web:t"),
        "worker": _community("worker", "boss-community-worker:t"),
    }
    calls, out = _run_builds(tmp_path, services)
    builds = [c.split()[1] for c in calls if c.startswith("build ")]
    # The worker image builds once, under the service named like it (not kg-mcp).
    assert sorted(builds) == ["api", "db", "egress", "mcp-tools", "web", "worker"]
    python = [i for i, b in enumerate(builds) if b in ("api", "egress", "worker", "mcp-tools")]
    assert python == list(range(python[0], python[0] + 4)), builds   # one family, back to back
    # The cache is dropped between families (three families: Python, the database, the console),
    # with --cache-only: no image or container is touched mid-build.
    prunes = [c for c in calls if c.startswith("prune ")]
    assert len(prunes) == 2 and all("--room --cache-only" in p for p in prunes), calls
    assert not calls[0].startswith("prune ") and not calls[-1].startswith("prune ")
    assert "under the 20 GB floor" in out


def test_one_family_never_prunes_mid_family(tmp_path):
    services = {n: _community(n, f"boss-community-{n}:t") for n in ("api", "worker", "egress", "mcp-tools")}
    calls, _ = _run_builds(tmp_path, services)
    assert not any(c.startswith("prune ") for c in calls)


def test_prune_cache_only_touches_no_image_or_container(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "docker.log"
    docker = bindir / "docker"
    docker.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {log}\n"
                      "case \"$1\" in images) printf 'boss-community-api\\n' ;; esac\nexit 0\n")
    docker.chmod(0o755)
    install = tmp_path / "install"
    install.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("BOSS_")}
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    prune = REPO / "scripts" / "install" / "prune.sh"
    out = subprocess.run(["bash", str(prune), "--dry-run", "--cache-only", "--room", "--dir", str(install)],
                         capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode == 0, out.stderr
    would = [line for line in out.stdout.splitlines() if "would run:" in line]
    assert any("builder prune" in line for line in would)
    assert not any(w in line for line in would for w in (" rmi ", " rm ", "image prune"))


# ---- the size budget -----------------------------------------------------------------------------

def test_every_image_the_installer_builds_from_the_dockerfile_has_a_budget():
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    targets = {s["build"]["target"] for s in services.values()
               if (s.get("build") or {}).get("dockerfile") == "Dockerfile.community"}
    assert targets <= set(image_budget.BUDGETS), targets - set(image_budget.BUDGETS)
    # The slim images are budgeted as slim: well under the full ones.
    assert image_budget.BUDGETS["egress"] < 1_000_000_000
    assert image_budget.BUDGETS["mcp-tools"] < image_budget.BUDGETS["api"] / 2


def test_an_image_over_budget_is_said_in_words_a_person_can_act_on():
    assert image_budget.over_budget("egress", image_budget.BUDGETS["egress"], set()) is None
    said = image_budget.over_budget("egress", image_budget.BUDGETS["egress"] + 300_000_000, set())
    assert said and "egress" in said and "0.30 GB" in said
    assert fw.problems(what=said.split(". ", 1)[0], action=said.split(". ", 1)[1]) == []
    # The opt-in security toolchain is allowed on top of the full images only.
    big = image_budget.BUDGETS["api"] + 1_000_000_000
    assert image_budget.over_budget("api", big, {"java", "go", "security"}) is None
    assert image_budget.over_budget("api", big, {"java", "go"})


def test_the_budget_check_fails_on_an_image_over_budget_and_skips_one_not_built(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    sizes = {"boss-community-api:t": 3_000_000_000, "boss-community-egress:t": 2_000_000_000}
    docker = bindir / "docker"
    docker.write_text(f"""#!/usr/bin/env python3
import json, sys
sizes = {sizes!r}
a = sys.argv[1:]
if a[:1] == ["version"]: sys.exit(0)
if a[:1] == ["history"]:
    if a[-1] not in sizes: sys.exit(1)
    print(sizes[a[-1]] - 1000); print(1000); print(0); sys.exit(0)
if a[:1] == ["run"]: print('{{"java": {{}}, "go": {{}}}}'); sys.exit(0)
sys.exit(1)
""")
    docker.chmod(0o755)
    out = subprocess.run([sys.executable, str(REPO / "scripts" / "install" / "image_budget.py"),
                          "--tag", "t", "--docker", str(docker)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 1, out.stdout
    assert "egress" in out.stdout and "OVER" in out.stdout and "over its" in out.stdout
    assert re.search(r"api\s+3\.00 GB .* ok", out.stdout)
    assert "web" in out.stdout and "not built here" in out.stdout


def test_the_first_install_preflight_says_the_measured_need():
    installer = (REPO / "install.sh").read_text()
    m = re.search(r'BOSS_INSTALL_NEEDS_GB="\$\{BOSS_INSTALL_NEEDS_GB:-(\d+)\}"', installer)
    assert m and int(m.group(1)) == image_budget.FIRST_INSTALL_NEEDS_GB


# ---- every path that builds the images builds them the same way -----------------------------------

def test_every_build_path_uses_the_one_build_order():
    installer = (REPO / "install.sh").read_text()
    assert "python3 scripts/install/build_order.py" in installer
    boss = (REPO / "scripts" / "install" / "boss.sh").read_text()
    assert 'compose --build-images "$services"' in boss and "compose build $services" not in boss
    assert "scripts/install/build_images.sh" in boss
    remote = (REPO / "scripts" / "install" / "remote.sh").read_text()
    assert "bash scripts/install/build_images.sh --dir . --only '$services' -- $compose_build" in remote
    assert "$compose_build build" not in remote
    self_fix = (REPO / "scripts" / "install" / "self_fix_installer.py").read_text()
    assert 'host.compose("build"' not in self_fix and self_fix.count("host.build_images(") == 3
    script = (REPO / "scripts" / "install" / "build_images.sh").read_text()
    assert "build_order.py" in script and "--room --cache-only" in script


def test_build_images_builds_each_image_once_in_families_and_names_a_failure(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"services": {
        "api": _community("api", "boss-community-api:t"),
        "kg-mcp": _community("worker", "boss-community-worker:t"),
        "web": {**_community("web", "boss-community-web:t"), "profiles": ["web"]},
        "worker": _community("worker", "boss-community-worker:t"),
        "egress": _community("egress", "boss-community-egress:t"),
    }}))
    log = tmp_path / "log"
    fake = tmp_path / "docker"
    fake.write_text(f"""#!/usr/bin/env python3
import os, sys
a = sys.argv[1:]
if a[-3:] == ["config", "--format", "json"]:
    print(open({str(config)!r}).read()); sys.exit(0)
if "build" in a:
    open({str(log)!r}, "a").write("build " + " ".join(a[a.index("build") + 1:]) + "\\n")
    sys.exit(7 if a[-1] == os.environ.get("FAIL", "") else 0)
""")
    fake.chmod(0o755)
    script = REPO / "scripts" / "install" / "build_images.sh"
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}
    # prune.sh is the real one: the fake docker answers nothing it needs, so it trims nothing.
    ok = subprocess.run(["bash", str(script), "--dir", str(tmp_path), "--build-arg", "A=1", "--",
                         str(fake), "compose", "--profile", "web"], capture_output=True, text=True,
                        env=env, timeout=60)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    lines = log.read_text().splitlines()
    assert [line.split()[-1] for line in lines] == ["api", "worker", "egress", "web"], lines
    assert all("--build-arg A=1" in line for line in lines)
    log.unlink()
    only = subprocess.run(["bash", str(script), "--only", "api egress", "--", str(fake), "compose"],
                          capture_output=True, text=True, env=env, timeout=60)
    assert only.returncode == 0
    assert [line.split()[-1] for line in log.read_text().splitlines()] == ["api", "egress"]
    log.unlink()
    bad = subprocess.run(["bash", str(script), "--", str(fake), "compose"], capture_output=True,
                         text=True, env={**env, "FAIL": "worker"}, timeout=60)
    assert bad.returncode == 7 and "The worker image did not build" in bad.stderr
    assert [line.split()[-1] for line in log.read_text().splitlines()] == ["api", "worker"]
    said = bad.stderr.strip().splitlines()[-1]
    assert fw.problems(what=said.split("; ", 1)[0], action=said.split("; ", 1)[1]) == []


def test_a_build_without_the_shared_cache_never_writes_to_the_images_browsers(monkeypatch, tmp_path):
    from boss_engine.services import sandbox

    monkeypatch.setattr(sandbox, "_TOOL_CACHE_ROOT", "/proc/no-such-dir/cache")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    env = sandbox._shared_tool_caches()
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(tmp_path / "boss-pw-browsers")
    assert (tmp_path / "boss-pw-browsers").stat().st_mode & 0o1777 == 0o1777
