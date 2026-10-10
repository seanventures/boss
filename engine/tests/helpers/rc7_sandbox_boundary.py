"""Owned-container qualification for BOSS-480/481; never run on the host.

Requires the real candidate helper/entrypoint and required isolation. Uses local
Git and synthetic markers only. No provider, database or credential-store calls.
"""
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
from types import SimpleNamespace

from boss_engine.identity import runs
from boss_engine.services import owned_process, repo_host, sandbox_identity
from boss_engine.services.repo_files import RepoFiles, RepoFileRefused
from boss_engine.services.sandbox import EphemeralSandbox


def main():
    assert Path("/.dockerenv").exists(), "only run inside the owned disposable container"
    assert os.getuid() != 0
    assert sandbox_identity.mode() == "required"
    assert os.environ["BOSS_SANDBOX_NETWORK"] == "required"
    assert os.environ["BOSS_UID_POOL_REQUIRED"] == "1"
    assert sandbox_identity.available() and sandbox_identity.network_fenced()
    # Keep process census out of this test: the actual wrapper/helper/spawn still execute.
    owned_process.processes = lambda: []
    boss_uid = os.getuid()
    private = Path.home() / "rc7-owned-proof"
    private.mkdir(mode=0o700)
    protected = private / "protected-marker"
    protected.write_text("OWNED_CONTROL_PLANE_MARKER")
    protected.chmod(0o600)
    origin = private / "origin"
    origin.mkdir()
    safe_env = {"PATH": os.defpath, "HOME": str(private), "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null"}

    def git(root, *args):
        return subprocess.run(["git", "-C", str(root), *args], env=safe_env,
                              check=True, capture_output=True, text=True).stdout

    git(origin, "init", "--template=", "-b", "main")
    git(origin, "config", "user.name", "owned-fixture")
    git(origin, "config", "user.email", "owned@example.invalid")
    (origin / "base.txt").write_text("base\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-m", "base")
    repo_host.clone_url_for = lambda *a: str(origin)
    settings = SimpleNamespace(sandbox_root_path=None, sandbox_clone_depth=1,
                               sandbox_integration_branch="main",
                               sandbox_validation_timeout_seconds=20)
    proofs = []
    os.environ["BOSS_RC7_SYNTHETIC"] = "CONTROL_PLANE_ONLY"
    for mechanism in ("healthy", "filter", "include-filter", "hook", "fsmonitor", "gitdir"):
        owner_id = "rc7-owned-" + mechanism
        with owned_process.owner_scope("code_run", owner_id), sandbox_identity.bind(owner_id) as ident:
            assert ident.uid != boss_uid
            with EphemeralSandbox(settings, task_id=owner_id) as sandbox:
                sandbox.provision("owned", "repo")
                root = sandbox.path
                assert root.stat().st_uid == boss_uid
                assert stat.S_IMODE(root.stat().st_mode) == 0o2777
                assert sandbox._git_env == {} and sandbox._git_own_config == []
                assert sandbox._git(["branch", "--show-current"]).strip() == "boss/" + owner_id

                def workload(code):
                    proc = owned_process.run([sys.executable, "-c", code], capture_output=True,
                                             text=True, timeout=20, sandboxed=True, cwd=str(root),
                                             env={"PATH": os.defpath})
                    assert proc.returncode == 0, proc.stderr
                    return proc.stdout

                # The actual wrapper supplies safe.directory across the UID ownership boundary.
                uid = int(workload("import os; print(os.getuid())"))
                assert uid == ident.uid
                if mechanism == "healthy":
                    workload("from pathlib import Path; Path('base.txt').write_text('changed\\n'); "
                             "Path('new.txt').write_text('new\\n')")
                    assert sorted((f.path, f.content) for f in sandbox.diff()) == [
                        ("base.txt", "changed\n"), ("new.txt", "new\n")]
                    sandbox._git(["commit", "-m", "healthy"])
                    # The run's own real shim works; a second UID cannot read that socket.
                    shim = runs.Shim(run_id=owner_id)
                    shim.egress_credential = "SYNTHETIC_RUN_ONLY"
                    sock = runs.serve_shim_socket(shim)
                    try:
                        request = ("import socket; s=socket.socket(socket.AF_UNIX); s.settimeout(2); "
                                   f"s.connect({sock!r}); s.sendall(b'get\\n'); "
                                   "assert b'password=SYNTHETIC_RUN_ONLY' in s.recv(4096)")
                        workload(request)
                        with owned_process.owner_scope("code_run", "rc7-owned-other"), \
                                sandbox_identity.bind("rc7-owned-other"):
                            denied = owned_process.run([sys.executable, "-c", request],
                                                       capture_output=True, timeout=10,
                                                       env={"PATH": os.defpath})
                            assert denied.returncode != 0
                    finally:
                        runs.close_shim_socket(owner_id)
                    # Host-reader safety covers a symlink planted by the actual run UID.
                    workload(f"import os; os.symlink({str(protected)!r}, 'escape.txt')")
                    with RepoFiles(root) as reader:
                        try:
                            reader.read_text("escape.txt")
                        except RepoFileRefused:
                            pass
                        else:
                            raise AssertionError("host reader followed workload symlink")
                    proofs.append({"case": mechanism, "boss_uid": boss_uid, "run_uid": uid,
                                   "diff": True, "own_shim": True, "other_shim_denied": True,
                                   "host_reader_refused": True})
                    continue

                probe = root / "probe.py"
                probe.write_text(
                    "import json,os,pathlib,sys\n"
                    "denied=False\n"
                    "try:\n"
                    f" pathlib.Path({str(protected)!r}).read_text()\n"
                    "except PermissionError:\n denied=True\n"
                    "pathlib.Path('proof.json').write_text(json.dumps({'uid':os.getuid(),"
                    "'protected_denied':denied,'control_env_absent':"
                    "'BOSS_RC7_SYNTHETIC' not in os.environ,'own_git_env_absent':"
                    "'BOSS_EGR_CREDENTIAL' not in os.environ}))\n"
                    "sys.stdout.write(sys.stdin.read() if len(sys.argv)==1 else 'token\\0')\n")
                command = shlex.quote(sys.executable) + " " + shlex.quote(str(probe))
                # Install attacker config using the real run UID, not privileged Git.
                sandbox._git_env = {"BOSS_EGR_CREDENTIAL": "SYNTHETIC_CONTROL_ONLY"}
                if mechanism in ("filter", "include-filter", "gitdir"):
                    workload("from pathlib import Path; Path('.gitattributes').write_text('*.txt filter=probe\\n'); "
                             "Path('new.txt').write_text('new\\n')")
                    if mechanism == "include-filter":
                        sandbox._git(["config", "--file", str(root / "included.cfg"),
                                      "filter.probe.clean", command])
                        sandbox._git(["config", "include.path", str(root / "included.cfg")])
                    else:
                        sandbox._git(["config", "filter.probe.clean", command])
                    if mechanism == "gitdir":
                        workload("from pathlib import Path; Path('.git').rename('redirect.git'); "
                                 "Path('.git').write_text('gitdir: redirect.git\\n')")
                    sandbox._git(["add", "-A"])
                elif mechanism == "hook":
                    hook_text = "#!/bin/sh\nexec " + command + " </dev/null\n"
                    workload("from pathlib import Path; p=Path('.git/hooks/pre-commit'); "
                             f"p.write_text({hook_text!r}); p.chmod(0o755)")
                    sandbox._git(["commit", "--allow-empty", "-m", "owned-hook"])
                else:
                    sandbox._git(["config", "core.fsmonitor", command])
                    sandbox._git(["status", "--porcelain"])
                proof = json.loads((root / "proof.json").read_text())
                assert proof == {"uid": uid, "protected_denied": True,
                                 "control_env_absent": True, "own_git_env_absent": True}, proof
                proofs.append({"case": mechanism, "boss_uid": boss_uid, **proof})
    files = ["sandbox.py", "repo_files.py", "owned_process.py", "sandbox_identity.py"]
    services = Path(__import__("boss_engine.services.sandbox", fromlist=["x"]).__file__).parent
    fingerprints = {f: hashlib.sha256((services / f).read_bytes()).hexdigest() for f in files}
    print(json.dumps({"passed": proofs, "source_sha256": fingerprints,
                      "network_fence": "real entrypoint required mode; network-none container",
                      "helper": "candidate C helper, real file capabilities, actual UID switch",
                      "census": "stubbed; process termination lifecycle not qualified"}, indent=2))


if __name__ == "__main__":
    main()
