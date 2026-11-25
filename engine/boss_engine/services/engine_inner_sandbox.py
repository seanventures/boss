"""Engine inner-sandbox support: does a coding engine's OWN sandbox start here, and if not,
which layer stops it — in plain words, with the fix.

An administrator opts an install in with BOSS_ENGINE_INNER_SANDBOX in .env (`seccomp` or
`seccomp+apparmor`; docs/community/ENGINE_SANDBOX.md). scripts/install/compose_files.sh then
gives api, worker and runner BOSS's seccomp profile (and the AppArmor profile `boss-engine`) and
sets the same variable in their environment, which is how this module knows the mode the
container was CREATED with. Two callers, one diagnosis:

  * `boss doctor` (scripts/install/checkup.sh) runs `python -m boss_engine.services.
    engine_inner_sandbox --expect <mode from .env>` in the worker;
  * the Setup engine check (engine_policy.check_engine) runs `check()` for Codex where builds
    run, and Setup shows the sentence.

The probe is Codex's own sandbox with no model: `codex sandbox` in workspace-write running
`true`. Nothing here changes the mode a writer runs in; that stays engine_policy's rule.

THROUGH THE BUILD'S LAUNCHER (RCA row 12, BOSSAPP-1433 R5). A build starts Codex with
owned_process.popen under a build owner holding a sandbox uid, so the spawn is wrapped by
owned_process.workload_spawn: `boss-sandbox-exec run <uid> -- codex ...`, with the sandbox's
environment. The launcher sets PR_SET_NO_NEW_PRIVS before exec'ing, which is exactly what made
an AppArmor exec transition into bubblewrap fail on hosts where `codex sandbox` run directly
passed (#269). So the probe is spawned the same way (`as_a_build` + owned_process.run): under the
engine-check job it reuses the job's sandbox identity; from `boss doctor` it holds one for the
probe. The result names the launcher it went through.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional

ENV = "BOSS_ENGINE_INNER_SANDBOX"
PROFILE = "boss-engine"
DOC = "docs/community/ENGINE_SANDBOX.md"
MODES = ("off", "seccomp", "seccomp+apparmor")
_LABEL_FILES = ("/proc/self/attr/apparmor/current", "/proc/self/attr/current")
_NAMESPACE_DENIED = ("no permissions to create a new namespace", "creating new namespace failed",
                     "unshare failed", "clone: operation not permitted",
                     "setting up uid map: permission denied", "operation not permitted")
_MOUNT_DENIED = ("failed to make / slave", "mount", "pivot_root")

FIX_RECREATE = ("Run `boss start`: it recreates the containers whose settings changed, with "
                "what .env asks for.")
FIX_APPARMOR = ("Load the profile on the Docker host: `sudo scripts/sandbox/load_apparmor.sh` "
                "(Colima: `colima --profile boss ssh -- sudo \"$PWD/scripts/sandbox/"
                "load_apparmor.sh\"` from the install directory), then `boss start`.")


def mode(env: Optional[Mapping[str, str]] = None) -> str:
    """The mode this container was created with (the overlay sets it), else off."""
    raw = (env if env is not None else os.environ).get(ENV, "")
    value = raw.strip().strip("'\"").lower()
    if value in ("", "0", "off", "false", "no", "none"):
        return "off"
    if value in ("seccomp+apparmor", "apparmor+seccomp"):
        return "seccomp+apparmor"
    return "seccomp" if value == "seccomp" else "off"


def apparmor_label(files: tuple[str, ...] = _LABEL_FILES) -> Optional[str]:
    """This process's AppArmor label ("boss-engine (enforce)", "docker-default (enforce)"), or
    None on a host without AppArmor (Docker Desktop, most non-Ubuntu kernels)."""
    for name in files:
        try:
            text = Path(name).read_text(encoding="utf-8", errors="replace").strip("\x00\n ")
        except OSError:
            continue
        if text:
            return text
    return None


#: The owner kind the probe runs under when no engine-check job holds one (engine_policy's).
OWNER_KIND = "engine_check"


@contextlib.contextmanager
def as_a_build(owner_id: Optional[str] = None) -> Iterator[dict[str, Any]]:
    """Run the block where a build's Codex runs: under an owner holding a build sandbox uid, so
    every owned_process spawn inside is wrapped by the build's launcher (boss-sandbox-exec).

    Reuses the identity already bound (the engine-check job binds one); otherwise binds its own
    for the block, as a build does (sandbox_identity.bind). Yields {launcher, uid, error}:
    launcher "boss-sandbox-exec" when spawns are wrapped, "none" when isolation is off (or
    `auto` without the helper: builds then run unwrapped too), "unavailable" when isolation is
    required and the launcher cannot hold a uid (builds cannot start either)."""
    from . import owned_process, sandbox_identity

    held = sandbox_identity.current()
    if held is not None:
        yield {"launcher": "boss-sandbox-exec", "uid": held.uid, "error": ""}
        return
    # Unique per check: two checks in one process must not share (and release) one identity.
    owner_id = owner_id or f"engchk_inner_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    with owned_process.owner_scope(OWNER_KIND, owner_id, fresh=True), \
            contextlib.ExitStack() as stack:
        try:
            ident = stack.enter_context(sandbox_identity.bind(owner_id))
        except Exception as exc:  # noqa: BLE001 — SandboxUnavailable, a helper or disk error:
            # the launcher cannot hold a uid, said as the launcher layer, never a traceback
            yield {"launcher": "unavailable", "uid": None, "error": str(exc)[:240]}
            return
        yield {"launcher": "boss-sandbox-exec" if ident is not None else "none",
               "uid": ident.uid if ident is not None else None, "error": ""}


def probe(binary: str = "codex", *, run: Optional[Callable[..., Any]] = None,
          env: Optional[Mapping[str, str]] = None, timeout: int = 60) -> dict[str, Any]:
    """Codex's own sandbox with no model: workspace-write running `true`. {ok, output}.

    Spawned with owned_process.run, the build's spawn: inside `as_a_build` it goes through
    boss-sandbox-exec exactly as a build's Codex does."""
    argv = [binary, "sandbox", "-c", 'sandbox_mode="workspace-write"', "--", "true"]
    if run is None:
        from . import owned_process

        run = owned_process.run
    try:
        done = run(argv, capture_output=True, text=True, timeout=timeout,
                   env=None if env is None else dict(env))
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": f"codex sandbox did not finish in {timeout}s"}
    except OSError as exc:
        return {"ok": False, "output": f"codex sandbox could not run: {exc}"}
    output = f"{getattr(done, 'stdout', '') or ''}\n{getattr(done, 'stderr', '') or ''}".strip()
    return {"ok": getattr(done, "returncode", 1) == 0, "output": output[-1000:]}


def _first(output: str) -> str:
    for line in (output or "").splitlines():
        if line.strip():
            return line.strip()[:240]
    return "no output"


def diagnose(*, expected: str, actual: str, label: Optional[str],
             result: Mapping[str, Any]) -> dict[str, Any]:
    """{ok, layer, sentence, fix}. `expected` is .env's mode (doctor) or the container's own
    (Setup); `actual` is the container's; `label` its AppArmor label; `result` the probe's."""
    out = str(result.get("output") or "")
    low = out.lower()
    has_apparmor = label is not None and label != "unconfined"
    if expected == "off":
        if result.get("ok"):
            return {"ok": True, "layer": "", "sentence": "Codex's own sandbox starts here.",
                    "fix": ""}
        return {"ok": False, "layer": "not_enabled",
                "sentence": ("Codex's own sandbox cannot start here, and engine inner-sandbox "
                             "support is off. BOSS's writers do not need it unless the Codex "
                             "login's organisation forbids full access."),
                "fix": (f"If it does, set {ENV}=seccomp (Docker Desktop) or "
                        f"{ENV}=seccomp+apparmor (Colima, Ubuntu) in .env, then `boss start`. "
                        f"See {DOC}.")}
    if actual != expected:
        return {"ok": False, "layer": "not_applied",
                "sentence": (f".env asks for engine inner-sandbox support ({expected}), but the "
                             f"running containers were created with {actual}."),
                "fix": FIX_RECREATE}
    if expected == "seccomp+apparmor" and has_apparmor and not label.startswith(PROFILE):
        return {"ok": False, "layer": "apparmor_profile",
                "sentence": (f"The containers do not run under BOSS's AppArmor profile "
                             f"({PROFILE}); they run under {label.split(' ')[0]}."),
                "fix": FIX_APPARMOR}
    if result.get("ok"):
        note = ""
        if expected == "seccomp+apparmor" and not has_apparmor:
            note = " This Docker host has no AppArmor, so the seccomp profile alone applies."
        return {"ok": True, "layer": "",
                "sentence": f"Codex's own sandbox starts here ({expected}).{note}", "fix": ""}
    if any(word in low for word in _NAMESPACE_DENIED) or any(w in low for w in _MOUNT_DENIED):
        if has_apparmor:
            return {"ok": False, "layer": "apparmor",
                    "sentence": ("AppArmor on the Docker host refused Codex's sandbox (its "
                                 "namespaces or mounts): " + _first(out)),
                    "fix": ("Have a host administrator inspect AppArmor audit denials and compare "
                            "the loaded profile with this release. Reload only if it is stale. "
                            "An exec denial marked 'no new privs' is a profile-transition "
                            "conflict, not evidence of a missing helper path; do not disable "
                            "no-new-privileges or AppArmor. After correcting the host setup, "
                            "press Re-check on Codex in Setup › Models. " + FIX_APPARMOR)}
        return {"ok": False, "layer": "kernel",
                "sentence": ("The containers were created with BOSS's seccomp profile, yet the "
                             "namespaces are still refused, so the Docker host's kernel refuses "
                             "unprivileged user namespaces: " + _first(out)),
                "fix": ("On the Docker host, allow them: `sysctl user.max_user_namespaces` must "
                        "be above 0 (and `kernel.unprivileged_userns_clone=1` where it exists). "
                        "If a container was recreated by hand, without `boss`, run `boss start` "
                        f"first. See {DOC}.")}
    return {"ok": False, "layer": "engine",
            "sentence": "Codex's sandbox command failed: " + _first(out),
            "fix": f"Check that Codex is installed in the image (`codex --version`). See {DOC}."}


def check(binary: str = "codex", *, expected: Optional[str] = None,
          run: Optional[Callable[..., Any]] = None, env: Optional[Mapping[str, str]] = None,
          label: Optional[str] = "__read__") -> dict[str, Any]:
    """The whole check where this process runs: mode, AppArmor label, probe (through the
    build's launcher, `as_a_build`), diagnosis. `launcher` says which path was proved."""
    actual = mode()
    seen = apparmor_label() if label == "__read__" else label
    with as_a_build() as where:
        if where["launcher"] == "unavailable":
            return {"ok": False, "layer": "launcher", "mode": actual, "apparmor": seen,
                    "launcher": "unavailable",
                    "sentence": ("BOSS's build launcher (boss-sandbox-exec) cannot hold a "
                                 "sandbox here, so no build can start Codex: "
                                 + where["error"]),
                    "fix": f"Run `boss doctor` and fix the sandbox helper it names. See {DOC}."}
        result = probe(binary, run=run, env=env)
    found = diagnose(expected=expected or actual, actual=actual, label=seen, result=result)
    found.update(mode=actual, apparmor=seen, launcher=where["launcher"])
    return found


def main(argv: list[str]) -> int:
    """`boss doctor` (checkup.sh): one line, `ok <sentence>` or `bad <sentence> Fix: <fix>`."""
    expected = None
    if "--expect" in argv:
        expected = argv[argv.index("--expect") + 1]
        if expected not in MODES:
            print(f"bad {ENV}={expected} is not one of: seccomp, seccomp+apparmor")
            return 1
    found = check(expected=expected)
    if "--json" in argv:
        print(json.dumps(found))
    elif found["ok"]:
        print(f"ok {found['sentence']} (probed through {found.get('launcher') or 'none'})")
    else:
        print(f"bad [{found['layer']}] {found['sentence']} Fix: {found['fix']} "
              f"(probed through {found.get('launcher') or 'none'})")
    return 0 if found["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
