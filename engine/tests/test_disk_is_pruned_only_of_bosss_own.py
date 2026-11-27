"""Disk hygiene removes only what BOSS made (stability spec §4a.8).

boss-1 sat at 80% disk with 46.5 GB of build cache growing ~1.3 GB per deploy; nothing pruned.
The fix prunes after every deploy and above 75% disk — and must never become the thing that
deletes a customer's volumes or another project's images.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))

PRUNE = ROOT / "scripts" / "install" / "prune.sh"


def test_the_host_prune_never_touches_volumes_or_prunes_the_system():
    body = PRUNE.read_text(encoding="utf-8")
    code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
    for forbidden in ("system prune", "--volumes", "volume rm", "volume prune", "rmi -f $(",
                      "container prune"):
        assert forbidden not in code, forbidden
    # Build cache is shared across projects: beyond the plain capped prune, only BOSS's own scope
    # (a Docker that runs only BOSS, or BOSS's buildx builder) is cleared — every broad prune
    # (--all in any spelling, or no --keep-storage) runs through that scope and in the THEN
    # branch of its ownership check; never in an else, never outside it.
    assert 'OWNS_CACHE="$DEDICATED"' in code and 'buildx prune --builder "$BUILDER"' in code
    lines = code.splitlines()
    broad = [i for i, line in enumerate(lines) if _prunes(line) and _broad(line)]
    assert len(broad) >= 3, "the cap fallback, the Mac floor and the Docker floor trims"
    for i in broad:
        assert '"${CACHE_PRUNE[@]}"' in lines[i], lines[i]
        clauses = _enclosing(lines, i)
        # The check itself, alone: `( "$OWNS_CACHE" -eq 1 || "$ROOM" -eq 1 )` is not ownership.
        assert 'if [[ "$OWNS_CACHE" -eq 1 ]]; then' in clauses, (lines[i], clauses)


_ALL_FLAG = re.compile(r"(?:^|\s)(?:-[a-z]*a[a-z]*|--all)(?=\s|$|\))")


def _prunes(line: str) -> bool:
    t = line.strip()
    return t.startswith("run ") and ("CACHE_PRUNE" in t or "builder prune" in t or "buildx prune" in t)


def _broad(line: str) -> bool:
    flags = line.split("prune", 1)[-1] if "prune" in line else line.split('"${CACHE_PRUNE[@]}"', 1)[-1]
    return bool(_ALL_FLAG.search(flags)) or "--keep-storage" not in flags


def _enclosing(lines: list[str], i: int) -> list[str]:
    """The if/elif/else clauses a line sits in, innermost first (case and loops are transparent)."""
    clauses, depth, skipping = [], 0, False
    for j in range(i - 1, -1, -1):
        t = lines[j].strip()
        if t.startswith(("if (", "else if (")):
            continue           # an awk program's own if, not the shell's
        if t.startswith("if ") and re.search(r"(;|\s)fi$", t):
            continue
        if t == "fi" or t.startswith(("fi ", "fi;")) or re.search(r"(;|\s)fi$", t):
            depth += 1         # also `else …; fi`: its statement is closed, walk past it
        elif t.startswith("if "):
            if depth:
                depth -= 1
            elif skipping:
                skipping = False
            else:
                clauses.append(t)
        elif t.startswith(("elif ", "else")) and not depth:
            if not skipping:
                clauses.append(t)
                skipping = True
    return clauses


def test_the_guard_reads_else_branches_and_every_spelling_of_all():
    assert all(_broad(f"run \"${{CACHE_PRUNE[@]}}\" {f} --keep-storage 1gb") for f in ("-af", "-fa", "-a -f", "--all -f"))
    assert _broad('run "${D[@]}" builder prune -f')                      # keep-less
    assert not _broad('run "${CACHE_PRUNE[@]}" -f --keep-storage "${CACHE_CAP_GB}gb"')
    code = ['if [[ "$OWNS_CACHE" -eq 1 ]]; then', '  x', 'else', '  run "${CACHE_PRUNE[@]}" -af', 'fi']
    assert _enclosing(code, 3) == ["else"]
    nested = ['if [[ "$OWNS_CACHE" -eq 1 ]]; then', '  if [[ a ]]; then', '    y', '  fi',
              '  run "${CACHE_PRUNE[@]}" --all', 'fi']
    assert _enclosing(nested, 4) == ['if [[ "$OWNS_CACHE" -eq 1 ]]; then']


def test_no_other_place_clears_a_whole_build_cache_on_a_machine_that_may_not_be_boss_alone():
    """The same rule outside prune.sh: a broad build-cache prune exists only where the machine is
    BOSS's — the enterprise lane's own throwaway box, and an added server the person marked as
    running only what BOSS deploys (add_server.cache_prune)."""
    allowed = {"scripts/enterprise-lane.sh", "engine/boss_engine/services/add_server.py",
               "engine/boss_engine/services/disk_budget.py",      # the sentence naming it for the person
               "scripts/install/prune.sh"}                         # guarded structurally above
    found = subprocess.run(["git", "grep", "-l", "-E", r"(builder|buildx) prune [^\n]*(-[a-z]*a[a-z]* |-af|--all)",
                            "--", "engine/boss_engine", "apps/api/app", "scripts", "install.sh"],
                           cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert set(found) <= allowed, sorted(set(found) - allowed)
    from boss_engine.services import add_server

    assert "-af" not in add_server.cache_prune({})[0] and add_server.cache_prune({})[1]
    assert "-af" in add_server.cache_prune({"dedicated": True})[0]


def test_every_deploy_path_prunes_after_it_succeeds():
    boss = (ROOT / "scripts" / "install" / "boss.sh").read_text(encoding="utf-8")
    remote = (ROOT / "scripts" / "install" / "remote.sh").read_text(encoding="utf-8")
    assert boss.count("post_deploy_prune") >= 3          # defined + upgrade + update
    assert remote.count("prune_remote") >= 3             # defined + install + update
    assert "prune.sh --dedicated" in remote


def _fake_docker(tmp_path: Path) -> Path:
    log = tmp_path / "docker.log"
    exe = tmp_path / "docker"
    exe.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


def test_the_reaper_prunes_build_cache_only_on_a_dedicated_host(tmp_path, monkeypatch):
    from boss_engine.services import resource_reaper

    exe = _fake_docker(tmp_path)
    full = {"total_gb": 77.0, "free_gb": 16.0, "used_fraction": 0.8}
    monkeypatch.delenv("BOSS_DEDICATED_HOST", raising=False)
    out = resource_reaper.prune_disk(binary=str(exe), install_id="inst_x", usage=full)
    log = (tmp_path / "docker.log").read_text()
    assert out["pruned"] and "builder" not in log
    assert "image prune -f --filter label=boss.install_id=inst_x" in log

    monkeypatch.setenv("BOSS_DEDICATED_HOST", "1")
    resource_reaper.prune_disk(binary=str(exe), install_id="inst_x", usage=full)
    log = (tmp_path / "docker.log").read_text()
    assert "builder prune -f --filter until=72h --keep-storage 15gb" in log
    assert "volume" not in log and "system" not in log


def test_below_the_threshold_nothing_is_pruned(tmp_path, monkeypatch):
    from boss_engine.services import resource_reaper

    exe = _fake_docker(tmp_path)
    monkeypatch.setenv("BOSS_DEDICATED_HOST", "1")
    out = resource_reaper.prune_disk(binary=str(exe), install_id="inst_x",
                                     usage={"total_gb": 77.0, "free_gb": 40.0,
                                            "used_fraction": 0.48})
    assert out["pruned"] is False
    assert not (tmp_path / "docker.log").exists()
    assert os.environ["BOSS_DEDICATED_HOST"] == "1"


def test_cleanup_preview_does_not_mutate_even_on_a_full_dedicated_host(tmp_path):
    _fake_docker(tmp_path)
    df = tmp_path / "df"
    df.write_text("#!/bin/sh\nprintf 'Filesystem Size Used Avail Use%% Mounted\\n/dev/test 100G 96G 4G 96%% /\\n'\n")
    df.chmod(df.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
           "BOSS_DEDICATED_HOST": "1"}
    result = subprocess.run(["bash", str(PRUNE), "--dry-run"], env=env,
                            capture_output=True, text=True, check=True)
    calls = (tmp_path / "docker.log").read_text().splitlines()
    # Reads only (`context inspect` and `system df` measure Docker's disk and cache).
    assert calls and all(call.split()[0] in {"info", "ps", "images", "context", "system"}
                         for call in calls)
    assert not any(call.split()[0] == "run" for call in calls)
    assert "would run: docker image prune" in result.stdout
    assert "would run: docker builder prune" in result.stdout
