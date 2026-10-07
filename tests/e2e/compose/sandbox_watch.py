"""Watches the api container's /proc during a build (build_walk.py starts it as root with
`docker exec -d`): every process of the per-run sandbox uid pool, what it is, whether its argv,
environment or checkout's git remote carries a credential, and — once, from inside a live run
checkout, as that run's uid with that process's own environment — whether a push to `main` is
refused. Writes one JSON file; never a secret (only booleans and redacted text).

    python sandbox_watch.py <out.json> [seconds]      stop early: touch <out.json>.stop
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

POOL = range(61000, 62000)
CRED = re.compile(rb"(ghs_[A-Za-z0-9_]+|gh[pou]_[A-Za-z0-9_]+|boss_egr_[A-Za-z0-9_]+|x-access-token:[^@\s]+@|"
                  rb"https?://[^/@\s:]+:[^/@\s]+@)")
REDACT = re.compile(r"(ghs_|gh[pou]_|boss_egr_|boss_pat_|boss_sak_)[A-Za-z0-9_\-]+|://[^/@\s]+@")


def redact(s: str) -> str:
    return REDACT.sub(lambda m: (m.group(1) + "<redacted>") if m.group(1) else "://<redacted>@", s)


def status_ids(pid: str) -> tuple[int, int]:
    uid = gid = -1
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("Uid:"):
            uid = int(line.split()[1])
        elif line.startswith("Gid:"):
            gid = int(line.split()[1])
    return uid, gid


def git_root(path: str) -> str | None:
    p = Path(path)
    for cand in (p, *p.parents):
        if (cand / ".git" / "config").exists():
            return str(cand)
    return None


def remotes(root: str) -> list[dict]:
    out = []
    try:
        text = Path(root, ".git", "config").read_text(errors="replace")
    except OSError:
        return out
    for m in re.finditer(r"^\s*url\s*=\s*(\S+)", text, re.M):
        url = m.group(1)
        out.append({"url": redact(url), "has_credential": bool(CRED.search(url.encode())) or "@" in url.split("//", 1)[-1].split("/", 1)[0]})
    return out


def probe_push_main(pid: str, uid: int, gid: int, root: str) -> dict:
    """As the run's uid, with the run process's own environment, from the run's checkout."""
    try:
        env = dict(kv.split("=", 1) for kv in Path(f"/proc/{pid}/environ").read_bytes().decode(errors="replace").split("\0") if "=" in kv)
    except OSError as exc:
        return {"error": type(exc).__name__}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")

    def as_run():
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
    res = {}
    for name, ref in (("main", "HEAD:refs/heads/main"),):
        try:
            done = subprocess.run(["git", "-C", root, "push", "--porcelain", "origin", ref], env=env, preexec_fn=as_run,
                                  capture_output=True, text=True, timeout=60)
            res[name] = {"rc": done.returncode, "out": redact((done.stdout + done.stderr)[-500:])}
        except Exception as exc:  # noqa: BLE001
            res[name] = {"error": f"{type(exc).__name__}: {redact(str(exc))[:200]}"}
    res["env_names"] = sorted(k for k in env if k.startswith(("GIT_", "BOSS_SHIM", "HTTP", "HTTPS")))
    return res


def main() -> None:
    out = Path(sys.argv[1])
    until = time.time() + float(sys.argv[2] if len(sys.argv) > 2 else 900)
    seen: dict[str, dict] = {}
    report = {"started": time.time(), "api_uid": None, "processes": [], "probe": None}
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                cmd = (d / "cmdline").read_bytes()
                if b"apps.api.serve" in cmd and status_ids(d.name)[0] != 0 and not cmd.startswith(b"/bin/sh"):
                    report["api_uid"] = status_ids(d.name)[0]
                    break
            except OSError:
                pass
    probed = False
    while time.time() < until and not Path(str(out) + ".stop").exists():
        for d in Path("/proc").iterdir():
            pid = d.name
            if not pid.isdigit() or pid in seen:
                continue
            try:
                uid, gid = status_ids(pid)
                if uid not in POOL:
                    continue
                raw = (d / "cmdline").read_bytes()
                envb = (d / "environ").read_bytes()
                cwd = os.readlink(d / "cwd")
            except OSError:
                continue
            argv = [redact(a.decode(errors="replace"))[:120] for a in raw.split(b"\0") if a][:6]
            root = git_root(cwd)
            rec = {"pid": int(pid), "uid": uid, "gid": gid, "argv": argv, "cwd": cwd,
                   "argv_has_credential": bool(CRED.search(raw)), "env_has_github_token": bool(re.search(rb"gh[spou]_[A-Za-z0-9]", envb)),
                   "env_has_proxy": b"HTTPS_PROXY=" in envb or b"https_proxy=" in envb,
                   "checkout": root, "remotes": remotes(root) if root else [], "at": time.time()}
            seen[pid] = rec
            report["processes"].append(rec)
            if root and not probed and os.environ.get("FXD_PROBE_MAIN", "1") == "1":
                probed = True
                report["probe"] = {"pid": int(pid), "uid": uid, "checkout": root, **probe_push_main(pid, uid, gid, root)}
        out.write_text(json.dumps(report, indent=1))
        time.sleep(0.05)
    report["finished"] = time.time()
    out.write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
