"""Consecutive upgrades prune stale Docker build cache before disk_check and after build (BOSSAPP-1412)."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PRUNE_SH = REPO / "scripts" / "install" / "prune.sh"
UPGRADE_SH = REPO / "scripts" / "install" / "upgrade.sh"


def _git(*args: str, cwd: Path) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "BOSS Test",
            "GIT_AUTHOR_EMAIL": "boss-test@example.test",
            "GIT_COMMITTER_NAME": "BOSS Test",
            "GIT_COMMITTER_EMAIL": "boss-test@example.test",
        },
    )
    return res.stdout.strip()


def test_three_consecutive_upgrades_prune_build_cache_on_tight_disk(tmp_path: Path):
    origin = tmp_path / "origin.git"
    _git("init", "--bare", str(origin), cwd=tmp_path)

    source = tmp_path / "source"
    _git("clone", str(origin), str(source), cwd=tmp_path)
    (source / "scripts" / "install").mkdir(parents=True)
    shutil.copy2(PRUNE_SH, source / "scripts" / "install" / "prune.sh")
    shutil.copy2(UPGRADE_SH, source / "scripts" / "install" / "upgrade.sh")
    (source / "scripts" / "install" / "prune.sh").chmod(0o755)
    (source / "scripts" / "install" / "upgrade.sh").chmod(0o755)
    (source / "scripts" / "install" / "community-compose.yml").write_text(
        "services:\n  api:\n    image: boss-community-api:boss-community\n",
        encoding="utf-8",
    )
    (source / "scripts" / "install" / "backup.sh").write_text(
        "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8"
    )
    (source / "scripts" / "install" / "post_check.sh").write_text(
        "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8"
    )
    # Simulated installer: advances checkout to --ref and adds 10.0 GB of build cache.
    (source / "install.sh").write_text(
        """#!/usr/bin/env bash
set -euo pipefail
DIR="" REF=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --backup-dir) shift 2 ;;
    *) shift ;;
  esac
done
git -C "$DIR" reset -q --hard "$REF"
python3 - <<'PY'
import json, os, pathlib
p = pathlib.Path(os.environ["FAKE_DOCKER_STATE"])
st = json.loads(p.read_text())
st["cache_gb"] = round(st["cache_gb"] + 10.0, 1)
st["free_gb"] = round(max(0.0, st["free_gb"] - 10.0), 1)
p.write_text(json.dumps(st))
PY
echo "SIM_INSTALLER_BUILT $REF"
""",
        encoding="utf-8",
    )
    (source / "install.sh").chmod(0o755)
    (source / "VERSION").write_text("0\n", encoding="utf-8")
    _git("add", "-A", cwd=source)
    _git("commit", "-m", "v0", cwd=source)
    _git("branch", "-M", "main", cwd=source)
    _git("push", "-u", "origin", "main", cwd=source)

    installed = tmp_path / "installed"
    _git("clone", str(origin), str(installed), cwd=tmp_path)
    (installed / ".env").write_text(
        "COMPOSE_PROJECT_NAME=boss-community\nBOSS_COMMUNITY_IMAGE_TAG=boss-community\n",
        encoding="utf-8",
    )
    (installed / ".boss-release").write_text("BOSS_RELEASE_REF=main\n", encoding="utf-8")

    # Commits 1, 2, 3 on origin/main
    commits = []
    for n in range(1, 4):
        (source / "VERSION").write_text(f"{n}\n", encoding="utf-8")
        _git("add", "VERSION", cwd=source)
        _git("commit", "-m", f"v{n}", cwd=source)
        _git("push", "origin", "main", cwd=source)
        commits.append(_git("rev-parse", "HEAD", cwd=source))

    state_file = tmp_path / "docker_state.json"
    state_file.write_text(json.dumps({"free_gb": 20.0, "cache_gb": 10.0}), encoding="utf-8")
    docker_log = tmp_path / "docker.log"

    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_df = bindir / "df"
    fake_df.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'Filesystem 1024-blocks Used Available Capacity Mounted on'\n"
        "echo '/dev/host 999999999 1 104857600 10% /'\n",
        encoding="utf-8",
    )
    fake_df.chmod(0o755)

    fake_docker = bindir / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env python3
import json, os, pathlib, re, sys

args = sys.argv[1:]
state_path = pathlib.Path(os.environ["FAKE_DOCKER_STATE"])
log_path = pathlib.Path(os.environ["FAKE_DOCKER_LOG"])
with log_path.open("a", encoding="utf-8") as f:
    f.write(" ".join(args) + "\\n")
st = json.loads(state_path.read_text(encoding="utf-8"))

cmd = args[0] if args else ""
joined = " ".join(args)
if cmd == "info":
    if "--format" in args:
        print("/var/lib/docker|Docker Desktop")
    sys.exit(0)
if cmd == "context":
    print("unix:///var/run/docker.sock")
    sys.exit(0)
if cmd == "image" and len(args) > 1 and args[1] == "inspect":
    print("sha256:abc123")
    sys.exit(0)
if cmd == "images":
    if "--format" in args and "{{.Repository}}" in joined:
        print("boss-community-api")
    elif "--format" in args and "{{.CreatedAt}}" in joined:
        print("2026-11-25|boss-community-api:boss-community")
    sys.exit(0)
if cmd == "ps":
    sys.exit(0)
if cmd == "tag":
    sys.exit(0)
if cmd == "compose":
    if "ps" in args:
        sys.exit(0)
    sys.exit(0)
if cmd == "run":
    kb = int(round(st["free_gb"] * 1048576))
    print("Filesystem 1024-blocks Used Available Capacity Mounted on")
    print(f"overlay 62914560 1 {kb} 70% /")
    sys.exit(0)
if cmd == "system" and len(args) > 1 and args[1] == "df":
    print("Images|12.0GB")
    print(f"Build Cache|{st['cache_gb']:.1f}GB")
    sys.exit(0)
if cmd == "builder" and len(args) > 1 and args[1] == "prune":
    m = re.search(r"--keep-storage\\s+(\\d+)gb", joined)
    keep = float(m.group(1)) if m else 0.0
    if st["cache_gb"] > keep:
        freed = round(st["cache_gb"] - keep, 1)
        st["cache_gb"] = round(keep, 1)
        st["free_gb"] = round(st["free_gb"] + freed, 1)
        state_path.write_text(json.dumps(st), encoding="utf-8")
    sys.exit(0)
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_docker.chmod(fake_docker.stat().st_mode | stat.S_IEXEC)

    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_STATE": str(state_file),
        "FAKE_DOCKER_LOG": str(docker_log),
        "HOME": str(tmp_path / "home"),
    }
    (tmp_path / "home").mkdir()

    for idx, target_commit in enumerate(commits, start=1):
        docker_log.write_text("", encoding="utf-8")
        # Advance origin/main one commit at a time or target each commit explicitly.
        res = subprocess.run(
            ["bash", str(installed / "scripts" / "install" / "upgrade.sh"), "--dir", str(installed), "--ref", target_commit, "--no-rollback"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert res.returncode == 0, f"upgrade {idx} failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        assert _git("rev-parse", "HEAD", cwd=installed) == target_commit
        assert "build cache: freed " in res.stdout, res.stdout
        # Must report freed > 0 before disk_check (10.0 -> 5.0 GB) AND after build (15.0 -> 10.0 GB).
        freed_lines = [ln for ln in res.stdout.splitlines() if ln.startswith("build cache: freed ")]
        assert len(freed_lines) == 2, f"expected pre- and post-build prune output on run {idx}: {res.stdout}"
        assert "freed 5.0 GB (before 10.0 GB, after 5.0 GB)" in freed_lines[0]
        assert "freed 5.0 GB (before 15.0 GB, after 10.0 GB)" in freed_lines[1]
        assert "incremental build" in res.stdout
        calls = docker_log.read_text(encoding="utf-8").splitlines()
        prune_indices = [i for i, c in enumerate(calls) if c.startswith("builder prune ")]
        assert len(prune_indices) >= 2
