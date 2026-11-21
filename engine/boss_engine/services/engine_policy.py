"""WHAT AN ENGINE'S LOGIN MAY DO HERE — PROVED AT SETUP, NOT FOUND OUT MID-BUILD.

WHY THIS EXISTS
---------------
An engine's login can carry its ORGANISATION's policy into BOSS's container:

  * Codex on a ChatGPT Enterprise/Business workspace reads the org's "requirements" (cloud-
    managed, or requirements.toml). They can forbid `danger-full-access`, the mode BOSS runs a
    writer in. Codex says so once ("Configured value for `permission_profile` is disallowed by
    requirements; falling back from ...") and carries on READ-ONLY: the test author wrote
    nothing, every command died in bubblewrap, and BOSS reported "no test files" (an enterprise
    install, 2026-11).
  * Claude Code on a managed account (server-managed settings, or managed-settings.json) can
    force its own Bash sandbox, deny tools, or forbid bypass modes.
  * Any engine whose own sandbox cannot start in a container (bwrap needs user namespaces that
    Docker's default seccomp profile and Ubuntu 24.04's AppArmor withhold).

Setup's old proof ran one read-only "reply with BOSS_OK" turn in an empty directory: it never
asked for the writer's mode and never ran a command, so it passed under every one of these.

WHAT THIS DOES (every coding-CLI login, through the same contract a build uses, run WHERE BUILDS
RUN: a queued job under a build's sandbox identity — see "The check runs WHERE BUILDS RUN")
  * `writer_check` — one writer session in an empty directory that must WRITE a file and RUN a
    command (`printf BOSS_PROBE_OK > .boss-probe && cat .boss-probe`). BOSS checks the file on
    disk and the command's output in the turn stream, not the model's claim.
  * `codex_sandbox_proof` — no model: `codex sandbox` in workspace-write mode, with network
    access and the toolchain caches (~/.npm, ~/.cache incl. the Go build cache, ~/go, ~/.m2)
    writable, must write a file, touch every cache and reach the package registry.
  * `check_engine` — the writer check in today's mode; when the login turns out MANAGED and
    the writer cannot write, the sandbox proof; when that passes, the writer check again in
    workspace-write. The outcome is recorded PER LOGIN (`record`, on the login's entitlement
    source) and read by the engine at run time (`recorded`).

THE RULE FOR CODEX (`codex_writer_mode`): workspace-write ONLY when the login is managed AND the
sandbox was proved here with network access. Everything else keeps today's mode
(danger-full-access inside BOSS's own sandbox — the boundary BOSS relies on is the per-build
uid, the egress fence, the ephemeral clone and the container, never the engine's inner sandbox).
A blanket switch to workspace-write breaks Codex on every stock Docker host (bwrap cannot start
under the default seccomp profile) and undoes BOSSAPP-1350's review seats.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

_log = logging.getLogger(__name__)

#: Where the recorded policy lives on an entitlement source's connection_metadata.
POLICY_KEY = "engine_policy"
#: Codex's ChatGPT login (cli_harness codex spec `entitlement_source_id`).
CODEX_SOURCE = "openai_codex_chatgpt"
#: The doc a person is pointed at, by file and section.
DOC = "docs/community/ENGINE_SANDBOX.md"

PROBE_FILE = ".boss-probe"
PROBE_TOKEN = "BOSS_PROBE_OK"
PROBE_COMMAND = f"printf {PROBE_TOKEN} > {PROBE_FILE} && cat {PROBE_FILE}"
PROBE_PROMPT = (
    "This is BOSS's engine check: it proves this session can write a file and run a command. "
    "Run exactly this one shell command with your shell tool, then reply with its output and "
    f"nothing else:\n\n{PROBE_COMMAND}\n\nDo not run anything else and do not edit any other "
    "file.")

#: The words a writer's tool result uses when the write or the command was refused.
_REFUSED = re.compile(
    r"read-only file system|permission denied|operation not permitted|has been denied"
    r"|blocked by|not allowed|is disallowed|sandbox", re.I)
#: Any diagnostic that says the CLI is reading its organisation's requirements.
MANAGED_MARKER = re.compile(
    r"by requirements|requirements\.toml|enterprise[_ ]managed|managed by your organization"
    r"|by your organization's (?:policy|managed settings)|enterprise (?:managed )?policy", re.I)

#: How long a recorded policy is trusted in one process before it is read again.
_CACHE_SECONDS = 60.0
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()


# ── The record, per login ─────────────────────────────────────────────────────────────────────

def record(connection: Any, source_id: Optional[str], policy: Mapping[str, Any]) -> None:
    """Keep the policy on the login's entitlement source (every project's row for it: the login
    is the install's, not one project's)."""
    if not source_id:
        return
    from sqlalchemy import text

    connection.execute(
        text("UPDATE entitlement_sources SET connection_metadata = "
             "COALESCE(connection_metadata, '{}'::jsonb) || jsonb_build_object("
             "CAST(:key AS text), CAST(:policy AS jsonb)), updated_at = now() "
             "WHERE source_id = :s"),
        {"key": POLICY_KEY, "policy": json.dumps(dict(policy), default=str), "s": source_id})
    forget(source_id)


def forget(source_id: Optional[str] = None) -> None:
    with _CACHE_LOCK:
        if source_id is None:
            _CACHE.clear()
        else:
            _CACHE.pop(str(source_id), None)


def recorded(source_id: Optional[str]) -> dict[str, Any]:
    """The policy Setup recorded for this login, or {} (never checked, or unreadable — then the
    engine keeps today's mode). Never raises."""
    if not source_id:
        return {}
    now = time.monotonic()
    with _CACHE_LOCK:
        held = _CACHE.get(str(source_id))
        if held and now - held[0] < _CACHE_SECONDS:
            return dict(held[1])
    found: dict[str, Any] = {}
    try:
        from sqlalchemy import text

        from ..db import begin

        with begin() as c:
            row = c.execute(
                text("SELECT connection_metadata -> :key FROM entitlement_sources "
                     "WHERE source_id = :s AND connection_metadata ? :key "
                     "ORDER BY updated_at DESC NULLS LAST LIMIT 1"),
                {"key": POLICY_KEY, "s": str(source_id)}).first()
        value = row[0] if row else None
        if isinstance(value, str):
            value = json.loads(value)
        if isinstance(value, dict):
            found = value
    except Exception:  # noqa: BLE001 — no record is today's mode, never a failed session
        _log.debug("engine policy for %s unreadable", source_id, exc_info=True)
    with _CACHE_LOCK:
        _CACHE[str(source_id)] = (now, found)
    return dict(found)


# ── Codex: the writer's mode ─────────────────────────────────────────────────────────────────

def cache_roots(env: Optional[Mapping[str, str]] = None) -> list[str]:
    """The toolchain caches a writer's installs and builds write to, for the child's HOME:
    npm, the XDG cache (pip, the Go build cache), Go's module cache, Maven's repository."""
    env = env if env is not None else os.environ
    home = str(env.get("HOME") or os.path.expanduser("~"))
    roots = [os.path.join(home, ".npm"), os.path.join(home, ".cache"),
             os.path.join(home, "go"), os.path.join(home, ".m2")]
    for name in ("npm_config_cache", "GOCACHE", "GOMODCACHE", "PIP_CACHE_DIR", "YARN_CACHE_FOLDER",
                 "PNPM_HOME"):
        value = str(env.get(name) or "").strip()
        if value and os.path.isabs(value):
            roots.append(value)
    return list(dict.fromkeys(roots))


def _toml_list(paths: list[str]) -> str:
    return "[" + ",".join(json.dumps(p) for p in paths) + "]"


def codex_workspace_config(*, network: bool, roots: list[str]) -> list[str]:
    """The `-c` overrides that give Codex's workspace-write sandbox what a writer needs."""
    config: list[str] = []
    if network:
        config += ["-c", "sandbox_workspace_write.network_access=true"]
    if roots:
        config += ["-c", f"sandbox_workspace_write.writable_roots={_toml_list(roots)}"]
    return config


def uses_workspace_write(policy: Optional[Mapping[str, Any]]) -> bool:
    """THE RULE: managed login, sandbox proved WHERE BUILDS RUN (the engine-check job, under a
    build's sandbox identity — never the API request), network reachable from it."""
    p = policy or {}
    proved_in = p.get("proved_in") if isinstance(p.get("proved_in"), Mapping) else {}
    return bool(p.get("managed") and p.get("sandbox_proven") and p.get("network")
                and p.get("writer_mode") == "workspace-write" and proved_in.get("job"))


def codex_writer_mode(policy: Optional[Mapping[str, Any]],
                      env: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    """{mode, config} for a Codex writer (and a review seat on BOSS's copy)."""
    if not uses_workspace_write(policy):
        return {"mode": "danger-full-access", "config": []}
    return {"mode": "workspace-write",
            "config": codex_workspace_config(network=True, roots=cache_roots(env))}


def workload_env(env: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """The environment a workload spawned now actually gets: under a build's sandbox identity
    its own HOME and caches (sandbox_identity.env), else `env` (or this process's)."""
    from . import sandbox_identity

    ident = sandbox_identity.current()
    if ident is not None:
        return ident.env(dict(env) if env is not None else None)
    return dict(env if env is not None else os.environ)


def prepare_cache_roots(env: Optional[Mapping[str, str]] = None) -> None:
    """Codex makes a writable root writable only when it exists: create them first — AS the
    workload (a build's sandbox uid), so that uid can write in them."""
    from . import owned_process, sandbox_identity

    roots = cache_roots(workload_env(env))
    if sandbox_identity.current() is not None:
        try:
            owned_process.run(["mkdir", "-p", *roots], timeout=30, capture_output=True)
        except Exception:  # noqa: BLE001 — a root that cannot be made is reported by the proof
            pass
        return
    for root in roots:
        try:
            os.makedirs(root, exist_ok=True)
        except OSError:
            pass


def codex_downgrades(text: object) -> list[str]:
    """The lines in which Codex says its organisation's requirements took a sandbox mode away
    (cli_harness.CODEX_DOWNGRADE_PATTERN), redacted and short."""
    from .cli_harness import CODEX_DOWNGRADE_PATTERN
    from ..identity.redaction import redact_text

    out = []
    for line in str(text or "").splitlines():
        if re.search(CODEX_DOWNGRADE_PATTERN, line, re.I):
            out.append(redact_text(line.strip())[:300])
    return list(dict.fromkeys(out))[:5]


def codex_sandbox_proof(binary: str = "codex", *, env: Optional[Mapping[str, str]] = None,
                        timeout: int = 60,
                        run: Optional[Callable[..., Any]] = None) -> dict[str, Any]:
    """Codex's own workspace-write sandbox, proved here with no model: it must start, write in
    its workspace, write every toolchain cache and reach the package registry the installs use.
    {sandbox_proven, network, writable_roots, missing_roots, error}."""
    from . import owned_process

    # Under a build's sandbox identity the command runs AS that uid with ITS environment (HOME,
    # the egress CONNECT proxy): the roots are that HOME's. `run` here is owned_process.run,
    # which wraps the spawn for the bound identity.
    spawn_env = None if env is None else dict(env)
    real = workload_env(spawn_env)
    roots = cache_roots(real)
    prepare_cache_roots(spawn_env)
    lines = [f"printf ok > {PROBE_FILE} && echo BOSS_WRITE_OK"]
    for root in roots:
        q = shlex.quote(root)
        lines.append(f"(touch {q}/{PROBE_FILE} && rm -f {q}/{PROBE_FILE} && "
                     f"echo BOSS_ROOT_OK {q}) 2>/dev/null")
    # The registry the person's installs use (an internal one when .npmrc names it).
    lines.append("(timeout 20 npm ping >/dev/null 2>&1 && echo BOSS_NET_OK) || echo BOSS_NET_FAIL")
    script = "; ".join(lines)
    argv = [binary, "sandbox", "-c", 'sandbox_mode="workspace-write"',
            *codex_workspace_config(network=True, roots=roots), "--", "sh", "-c", script]
    run = run or owned_process.run
    with tempfile.TemporaryDirectory(prefix="boss-sandbox-proof-") as work:
        try:
            done = run(argv, cwd=work, env=spawn_env, capture_output=True, text=True,
                       timeout=timeout)
            out = f"{getattr(done, 'stdout', '') or ''}\n{getattr(done, 'stderr', '') or ''}"
        except subprocess.TimeoutExpired:
            return {"sandbox_proven": False, "network": False, "writable_roots": [],
                    "missing_roots": roots, "error": f"codex sandbox did not finish in {timeout}s"}
        except OSError as exc:
            return {"sandbox_proven": False, "network": False, "writable_roots": [],
                    "missing_roots": roots, "error": f"codex sandbox could not start: {exc}"}
    from . import harness_failures

    verdict = harness_failures.classify(out, engine="codex", where="stream")
    wrote = "BOSS_WRITE_OK" in out
    ok_roots = [r for r in roots if f"BOSS_ROOT_OK {r}" in out
                or f"BOSS_ROOT_OK {shlex.quote(r)}" in out]
    return {"sandbox_proven": wrote and verdict is None,
            "network": "BOSS_NET_OK" in out,
            "writable_roots": ok_roots,
            "missing_roots": [r for r in roots if r not in ok_roots],
            "error": (verdict.message if verdict is not None else
                      ("" if wrote else _first_line(out)))}


# ── Claude Code: managed settings on disk ────────────────────────────────────────────────────

#: Where Claude Code reads an administrator's managed settings (Linux, macOS).
CLAUDE_MANAGED_PATHS = ("/etc/claude-code/managed-settings.json",
                        "/Library/Application Support/ClaudeCode/managed-settings.json")


def claude_managed_settings(paths: tuple[str, ...] = CLAUDE_MANAGED_PATHS) -> dict[str, Any]:
    """What a managed-settings file forces on Claude Code here, by key — never a value that could
    be a secret (`env`, `apiKeyHelper` and the like are not read). Server-managed settings are not
    on disk; the writer check catches what they do."""
    found: dict[str, Any] = {"managed": False, "files": []}
    for path in paths:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        found["managed"] = True
        found["files"].append(path)
        sandbox = data.get("sandbox") if isinstance(data.get("sandbox"), dict) else {}
        perms = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
        if sandbox.get("enabled") is True:
            found["sandbox_forced"] = True
        if sandbox.get("allowUnsandboxedCommands") is False:
            found["unsandboxed_commands_allowed"] = False
        if perms.get("disableBypassPermissionsMode"):
            found["bypass_disabled"] = True
        deny = [str(r)[:80] for r in (perms.get("deny") or []) if isinstance(r, str)]
        if deny:
            found["denies"] = deny[:10]
        if perms.get("defaultMode"):
            found["default_mode"] = str(perms.get("defaultMode"))[:40]
    return found


# ── The writer check, every engine ───────────────────────────────────────────────────────────

def _first_line(text: str) -> str:
    for line in str(text or "").splitlines():
        if line.strip() and not line.startswith("WARNING:"):
            from ..identity.redaction import redact_text

            return redact_text(line.strip())[:240]
    return ""


def writer_check(settings: Any, provider: str, *, agent: Any = None,
                 policy: Optional[Mapping[str, Any]] = None,
                 workdir: Optional[Callable[[], Path]] = None) -> dict[str, Any]:
    """One writer session that must write `.boss-probe` and run a command.

    {state: ok | cannot_write | unproven, wrote, ran, verdict, managed, diagnostics, error}.
    `cannot_write` only on evidence: the harness said so (its sandbox, its organisation's
    policy), or the session tried and the write was refused. A session that simply did not try
    is `unproven`, never a failed Setup."""
    from . import harness_failures

    if agent is None:
        from .code_agent import make_code_agent

        agent = make_code_agent(settings, engine=provider)
    if policy is not None:
        try:
            agent.engine_policy = dict(policy)   # the mode under test, not a stale record
        except Exception:  # noqa: BLE001
            pass
    turns: list[dict[str, Any]] = []
    out: dict[str, Any] = {}
    verdict = None
    error = ""
    import shutil

    # In the engine-check job the directory is inside the build sandbox's run directory (a
    # directory outside it would become the sandbox's HOME and the probe would land there).
    made = workdir() if workdir is not None else Path(tempfile.mkdtemp(prefix="boss-writer-check-"))
    with contextlib.ExitStack() as cleanup:
        cleanup.callback(shutil.rmtree, str(made), True)
        work = str(made)
        try:
            out = agent.run_role(PROBE_PROMPT, Path(work), read_only=False,
                                 on_turn=lambda t: turns.append(dict(t))) or {}
        except harness_failures.HarnessFailure as failure:
            verdict = failure.verdict
        except Exception as exc:  # noqa: BLE001 — an engine that failed is reported, not raised
            error = f"{type(exc).__name__}: {exc}"[:300]
            verdict = harness_failures.classify(str(exc), engine=provider, where="stream",
                                                writer=True)
        try:
            wrote = (Path(work) / PROBE_FILE).read_text(encoding="utf-8").strip() == PROBE_TOKEN
        except OSError:
            wrote = False
    results = [t for t in turns if t.get("direction") == "tool_result"]
    said = "\n".join(str(t.get("content") or "")[:2000] for t in results)
    ran = PROBE_TOKEN in said or (wrote and PROBE_TOKEN in str(out.get("summary") or ""))
    from ..identity.redaction import redact_text

    # Redacted before anything keeps them: a CLI diagnostic can quote a URL with a token in it.
    diagnostics = [redact_text(str(d))[:300] for d in (out.get("diagnostics") or [])]
    if verdict is not None:
        diagnostics.append(verdict.message)
    managed = bool((verdict is not None and verdict.kind == "policy_denied")
                   or any(MANAGED_MARKER.search(d) for d in diagnostics + [said]))
    refused = bool(results) and not wrote and bool(_REFUSED.search(said))
    if wrote and ran:
        state = "ok"
    elif verdict is not None or refused:
        state = "cannot_write"
    else:
        state = "unproven"
    return {"state": state, "wrote": wrote, "ran": ran, "managed": managed,
            "verdict": verdict.as_dict() if verdict is not None else None,
            "diagnostics": list(dict.fromkeys(diagnostics))[:5],
            "refusal": _first_line(said) if refused else "",
            "error": redact_text(error)[:300] if error else ""}


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def check_engine(settings: Any, provider: str, *, agent_factory: Optional[Callable[[], Any]] = None,
                 sandbox_prover: Optional[Callable[[], dict]] = None,
                 workdir: Optional[Callable[[], Path]] = None,
                 proved_in: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """The policy for one engine's login, proved: what Setup records and the engine reads.

    {engine, checked_at, managed, writer_mode, can_write, writer_check, proved_in, ...}.
    `proved_in`: where it ran ({"job": True, "isolated": …} from the engine-check job); a
    policy proved anywhere else never selects workspace-write (uses_workspace_write)."""
    make = agent_factory or (lambda: None)
    where = dict(proved_in or {"job": False})
    first = writer_check(settings, provider, agent=make(), policy={}, workdir=workdir)
    policy: dict[str, Any] = {"engine": provider, "checked_at": _now(),
                              "managed": first["managed"], "writer_mode": "default",
                              "writer_check": first, "proved_in": where}
    if provider == "claude_code":
        managed = claude_managed_settings()
        policy["managed_settings"] = managed
        policy["managed"] = policy["managed"] or managed["managed"]
    final = first
    if provider == "codex" and first["managed"] and first["state"] != "ok":
        # THE LOGIN'S ORGANISATION FORBIDS FULL ACCESS. Its own sandbox is the only way left to
        # write: prove it starts here, with the network and the caches a writer needs.
        proof = (sandbox_prover or (lambda: codex_sandbox_proof(_codex_binary(settings))))()
        # PROVED means all of it: the sandbox starts, writes, and every toolchain cache a
        # writer's installs and builds use is writable from inside it.
        policy.update(sandbox_proven=bool(proof.get("sandbox_proven"))
                      and not proof.get("missing_roots"),
                      network=bool(proof.get("network")),
                      writable_roots=list(proof.get("writable_roots") or []),
                      missing_roots=list(proof.get("missing_roots") or []),
                      sandbox_error=str(proof.get("error") or "")[:300],
                      downgrades=codex_downgrades("\n".join(first.get("diagnostics") or [])))
        if policy["sandbox_proven"] and policy["network"]:
            policy["writer_mode"] = "workspace-write"
            final = writer_check(settings, provider, agent=make(), policy=policy,
                                 workdir=workdir)
            policy["writer_check"] = final
            if final["state"] != "ok":
                policy["writer_mode"] = "unavailable"
        else:
            policy["writer_mode"] = "unavailable"
    policy["can_write"] = final["state"] == "ok"
    policy["state"] = final["state"]
    return policy


def _codex_binary(settings: Any) -> str:
    try:
        from .cli_harness import load_specs

        spec = load_specs(settings).get("codex")
        return spec.binary if spec is not None else "codex"
    except Exception:  # noqa: BLE001
        return "codex"


def setup_sentence(provider: str, policy: Mapping[str, Any]) -> Optional[str]:
    """One sentence for Setup when the login definitively cannot write here, else None."""
    if policy.get("state") != "cannot_write":
        return None
    check = policy.get("writer_check") or {}
    verdict = check.get("verdict") or {}
    if provider == "codex" and policy.get("managed"):
        if not policy.get("sandbox_proven"):
            why = ("its organisation's Codex requirements forbid the full access a BOSS writer "
                   "runs with, and Codex's own sandbox cannot start in this container ("
                   f"{policy.get('sandbox_error') or 'no detail'})")
        elif not policy.get("network"):
            why = ("its organisation's Codex requirements forbid full access, and Codex's own "
                   "sandbox cannot reach the package registry from here")
        else:
            why = "Codex still could not write in its own workspace sandbox"
        return (f"{provider} is signed in but cannot write here: {why}. See {DOC} "
                "(\"Managed engine policies\" and \"Agent commands fail with a namespace "
                "error\"); a host administrator fixes this, retrying the build will not.")
    if verdict.get("kind") == "sandbox_unavailable":
        return (f"{provider} is signed in but its own sandbox cannot start here: "
                f"{verdict.get('message')}. See {DOC} (\"Agent commands fail with a namespace "
                "error\").")
    if verdict:
        return f"{provider} is signed in but cannot write here: {verdict.get('sentence')}"
    return (f"{provider} is signed in but cannot write here: its write was refused "
            f"({check.get('refusal') or 'no detail'}). See {DOC} (\"Managed engine policies\").")


# ── The check runs WHERE BUILDS RUN: a leased job, a build's sandbox identity ─────────────────
#
# Not in the HTTP request (review of #258): there it ran in the API's process, as the API's uid,
# with the API's environment (DATABASE_URL, provider keys) — the boundary IDENTITY.md §7.6 keeps
# every build's writer behind — and it proved the API's seccomp, HOME and network, not a build's.
# Connect records "checking" and queues this job; the worker leases it and hands it to the API
# container (where the sandbox helper and the CLIs are), which runs it under owned_process +
# sandbox_identity exactly as a build's writer runs: the per-run uid, its own HOME, boss-egress's
# CONNECT proxy, the agent allow-list environment. No DB transaction is held while it runs.

JOB_KIND = "engine.check"
OWNER_KIND = "engine_check"
#: What one check may spend on a METERED login (a writer session, two for a managed Codex
#: login), reserved through the governed proof before any model call. A subscription: $0.
CHECK_ESTIMATE_USD_MICROS = 250_000

_STATE_WORDS = {
    "checking": ("Checking…", "grey", "BOSS is running one writer session where builds run: it "
                                      "must write a file and run a command."),
    "ok": ("Can write", "green", "A writer session wrote a file and ran a command where builds "
                                 "run."),
    "unproven": ("Write not proven", "amber", "The check's session did not run its command; "
                                              "nothing is assumed. Re-check to try again."),
    "cannot_write": ("Cannot write", "red", ""),
    "not_checked": ("Not checked", "amber", "The write check could not run."),
}


def summary(policy: Any, provider: str = "") -> Optional[dict[str, Any]]:
    """What Setup shows for a login's write check: {state, label, tone, tip}, or None."""
    if not isinstance(policy, Mapping) or not policy.get("state"):
        return None
    state = str(policy.get("state"))
    label, tone, tip = _STATE_WORDS.get(state, ("Not checked", "amber", ""))
    if state == "cannot_write":
        tip = setup_sentence(provider or str(policy.get("engine") or ""), policy) or ""
    elif state == "not_checked":
        tip = str(policy.get("error") or tip)[:300]
    return {"state": state, "label": label, "tone": tone, "tip": tip,
            "checked_at": policy.get("checked_at"),
            "writer_mode": policy.get("writer_mode")}


def _is_metered(settings: Any, provider: str) -> bool:
    """Whether this engine's login bills a key (its spec passes an *_API_KEY that is set)."""
    try:
        from .cli_harness import load_specs

        spec = load_specs(settings).get(provider)
    except Exception:  # noqa: BLE001
        return False
    names = [n for n in (getattr(spec, "env_passthrough", ()) or ()) if n.endswith("_API_KEY")]
    return any((os.environ.get(n) or "").strip() for n in names)


def start_check(connection: Any, project_id: str, provider: str, source_id: Optional[str],
                actor: str) -> dict[str, Any]:
    """Record "checking" for the login and queue the engine-check job. Returns at once."""
    from .jobs import enqueue_job

    record(connection, source_id, {"engine": provider, "state": "checking",
                                   "requested_at": _now()})
    job = enqueue_job(connection, project_id, JOB_KIND,
                      {"provider": provider, "source_id": source_id, "actor": actor},
                      max_attempts=1,
                      dedupe_key=f"{JOB_KIND}:{source_id}:{int(time.time() // 60)}")
    return {"state": "checking", "job_id": (job or {}).get("id")}


def execute_job(engine: Any, settings: Any, *, job_id: str, worker_id: str,
                lease_token: str) -> dict[str, Any]:
    """The worker's leased `engine.check` job, run in the API container (the CLIs and the sandbox
    helper are there), fenced by the lease."""
    from .jobs import JobLeaseService, LeaseNotFoundError

    with engine.begin() as c:                          # a short transaction: the lease only
        try:
            job = JobLeaseService(settings).require_active_lease(c, worker_id, job_id, lease_token)
        except LeaseNotFoundError as exc:
            return {"status": "failed", "retry": False, "error": f"lease: {exc}"}
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else \
        json.loads(job.get("payload") or "{}")
    return run_check(engine, settings, job_id=str(job_id), project_id=str(job["project_id"]),
                     provider=str(payload.get("provider") or ""),
                     source_id=payload.get("source_id"),
                     actor=str(payload.get("actor") or "ceo"))


def run_check(engine: Any, settings: Any, *, job_id: str, project_id: str, provider: str,
              source_id: Optional[str], actor: str, check: Optional[Callable[..., dict]] = None,
              governed: Optional[Callable[..., dict]] = None) -> dict[str, Any]:
    """The check itself, under a build's owner and sandbox identity, spend governed like the
    connection proof. Records the policy (or why it could not run) on the login."""
    from . import owned_process, sandbox_identity

    owner_id = f"engchk_{job_id}"
    held: dict[str, Any] = {}
    error = ""
    try:
        with owned_process.owner_scope(OWNER_KIND, owner_id, project_id=project_id, fresh=True):
            with sandbox_identity.bind(owner_id, project_id=project_id) as ident:
                where = {"job": True, "isolated": ident is not None,
                         "uid": ident.uid if ident is not None else None,
                         "network_fenced": sandbox_identity.network_fenced()}
                workdir = ((lambda: ident.workspace("engine-check-")) if ident is not None
                           else None)

                def prove() -> dict[str, Any]:
                    try:
                        held["policy"] = (check or check_engine)(
                            settings, provider, workdir=workdir, proved_in=where)
                    except Exception as exc:  # noqa: BLE001 — a check that could not run
                        held["error"] = f"{type(exc).__name__}: {exc}"
                    return {"ok": True, "exit_code": 0, "replied": PROBE_TOKEN, "error": None}

                if governed is None:
                    from .setup_service import governed_proof as governed
                amount = CHECK_ESTIMATE_USD_MICROS if _is_metered(settings, provider) else 0
                proof = governed(engine, settings, project_id, provider, actor, prove=prove,
                                 amount=amount, intent=f"engine-check:{job_id}")
                if "policy" not in held and not held.get("error"):
                    held["error"] = (str((proof or {}).get("error") or "")
                                     or "the governed check did not run")
    except sandbox_identity.SandboxUnavailable as exc:
        error = f"the build sandbox is unavailable: {exc}"
    except Exception as exc:  # noqa: BLE001 — recorded, never a crashed job
        _log.exception("engine check %s failed", job_id)
        error = f"{type(exc).__name__}: {exc}"
    policy = held.get("policy")
    if not policy:
        from ..identity.redaction import redact_text

        policy = {"engine": provider, "state": "not_checked", "checked_at": _now(),
                  "error": redact_text(error or held.get("error") or "")[:300]}
    try:
        with engine.begin() as c:
            record(c, source_id, policy)
    except Exception:  # noqa: BLE001
        _log.warning("engine check %s could not be recorded", job_id, exc_info=True)
    return {"status": "done", "state": policy.get("state"),
            "writer_mode": policy.get("writer_mode")}


__all__ = ["CHECK_ESTIMATE_USD_MICROS", "JOB_KIND", "execute_job", "run_check", "start_check",
           "summary", "workload_env", "CLAUDE_MANAGED_PATHS", "CODEX_SOURCE", "DOC", "MANAGED_MARKER", "POLICY_KEY",
           "PROBE_COMMAND", "PROBE_FILE", "PROBE_PROMPT", "PROBE_TOKEN", "cache_roots",
           "check_engine", "claude_managed_settings", "codex_downgrades", "codex_sandbox_proof",
           "codex_workspace_config", "codex_writer_mode", "forget", "prepare_cache_roots",
           "record", "recorded", "setup_sentence", "uses_workspace_write", "writer_check"]
