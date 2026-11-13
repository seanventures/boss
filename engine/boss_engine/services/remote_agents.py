"""Hosted remote agents as harnesses (BOSSAPP-855).

Some teams already run coding agents as a SERVICE — a managed agent platform, a cloud agent
product, their own agent fleet — and want them governed by BOSS, not replaced. BOSSAPP-853's
`http` kind drives an agent that works on BOSS's workspace path; a hosted agent cannot see that
path. It works on the REPOSITORY in its own sandbox and hands back a change. So this is a
separate harness kind, `remote_agent`, on a contract of its own:

boss.harness.remote/v1
    POST   {base}/v1/runs        {contract, prompt, mode, model, session_id?,
                                  repository: {url, branch, base_sha},
                                  spend_ceiling_usd_micros, deliver: "diff"}
                                 -> 201/202 {run_id, poll_after_ms?}
    GET    {base}/v1/runs/{id}   -> {status, summary?, usage?: {cost_usd_micros, ...},
                                     events?: [...], session_id?, error?,
                                     result?: {kind: "diff", patch} |
                                              {kind: "pr", url, patch? | patch_url?}}
    DELETE {base}/v1/runs/{id}   BOSS cancels: past the stage deadline, or past the ceiling
    auth   `Authorization: Bearer <token>` from the env var the profile names (never logged)

THE PROFILE IS ORG-LEVEL AND MUST BE COMPLETE BEFORE IT RUNS (remote_agent_profiles):
  * a SPEND CEILING (mandatory, > 0) per BOSS run: BOSS sends what is left of it with every
    submission, adds up the cost every poll reports, and cancels the remote run the moment the
    total reaches it;
  * a RETENTION ACKNOWLEDGEMENT: the code leaves the building, and a person must have recorded
    that they accept the provider's retention terms (who, when, what they acknowledged).
    Without it the harness refuses to start.

THE OUTPUT GOES THROUGH THE SAME GATES. The returned patch is applied to BOSS's own workspace
with `git apply` (paths inside the tree only, never .git/), so the diff of record, the tests BOSS
re-runs, the review seats and the PR are exactly those of any other engine. What the agent
reports is kept as the summary and is never trusted for pass/fail.

THE VERIFIED RULE (harness_registry): every remote profile registers verified=False — a governed
task has not completed on it — so Auto never picks it; an operator pins it. Profiles register
lazily: the registry asks this module for `remote:<name>` the first time a build names it.

Provider adapters: this module speaks the provider-neutral contract only. A provider whose API
differs (a managed agent platform's own endpoints) is reached through a small adapter service
that speaks boss.harness.remote/v1; no provider-specific client ships here yet.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .custom_harness import CustomHarnessError, _CustomAgentBase, normalise_report

_log = logging.getLogger(__name__)

CONTRACT = "boss.harness.remote/v1"
KIND = "remote_agent"
PREFIX = "remote:"
PROVIDERS = ("generic",)
STATUSES = ("queued", "running", "done", "error", "turn_cap", "awaiting_approval")
MAX_PATCH_BYTES = 10 * 1024 * 1024
_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,39}$")


class RemoteAgentError(ValueError):
    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class RemoteProfile:
    id: str
    org_id: str
    name: str
    provider: str
    base_url: str
    token_env: str
    spend_ceiling_usd_micros: int
    retention_ack_by: Optional[str]
    retention_ack_at: Any
    status: str
    poll_seconds: float = 3.0
    timeout: int = 3600
    description: str = ""

    @property
    def harness_name(self) -> str:
        return PREFIX + self.name

    @property
    def runnable(self) -> Optional[str]:
        """Why this profile cannot run, or None."""
        if self.status != "active":
            return f"remote agent {self.name!r} is {self.status}"
        if not self.retention_ack_at:
            return (f"remote agent {self.name!r} has no retention acknowledgement: a person must "
                    "accept the provider's retention terms before code is sent to it")
        if int(self.spend_ceiling_usd_micros or 0) <= 0:
            return f"remote agent {self.name!r} has no spend ceiling"
        return None


# ── profiles ──────────────────────────────────────────────────────────────────────────────────

_COLS = ("id, org_id, name, provider, base_url, token_env, spend_ceiling_usd_micros, "
         "retention_ack_by, retention_ack_at, retention_terms, status, poll_seconds, "
         "timeout_seconds, description, created_by, created_at")


def _org_of(connection: Connection, project_id: str) -> str:
    org = connection.execute(
        text("SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
             "WHERE p.id = :p"), {"p": project_id}).scalar()
    if not org:
        raise RemoteAgentError(f"no project {project_id!r}", 404)
    return str(org)


def _profile(row: Any) -> RemoteProfile:
    return RemoteProfile(
        id=row["id"], org_id=row["org_id"], name=row["name"], provider=row["provider"],
        base_url=row["base_url"], token_env=row["token_env"] or "",
        spend_ceiling_usd_micros=int(row["spend_ceiling_usd_micros"]),
        retention_ack_by=row["retention_ack_by"], retention_ack_at=row["retention_ack_at"],
        status=row["status"], poll_seconds=float(row["poll_seconds"]),
        timeout=int(row["timeout_seconds"]), description=row["description"] or "")


def view(row: Any) -> dict[str, Any]:
    p = _profile(row)
    return {"id": p.id, "name": p.name, "harness": p.harness_name, "provider": p.provider,
            "base_url": p.base_url, "token_env": p.token_env or None,
            "token_present": bool(p.token_env and os.environ.get(p.token_env)),
            "spend_ceiling_usd": p.spend_ceiling_usd_micros / 1_000_000,
            "retention": {"acknowledged": bool(p.retention_ack_at),
                          "by": p.retention_ack_by,
                          "at": p.retention_ack_at.isoformat() if p.retention_ack_at else None,
                          "terms": row["retention_terms"]},
            "status": p.status, "verified": False, "runnable": p.runnable is None,
            "blocked_because": p.runnable, "description": p.description}


def _validate(name: str, provider: str, base_url: str, token_env: str, ceiling_usd: Any) -> int:
    if not _NAME.match(name or ""):
        raise RemoteAgentError("name must be 2-40 lower-case letters, digits, '_' or '-'")
    if provider not in PROVIDERS:
        raise RemoteAgentError(f"provider must be one of {list(PROVIDERS)} (a provider with "
                               "its own API is reached through an adapter that speaks "
                               f"{CONTRACT})")
    if not re.match(r"^https://[^\s/]+", base_url or "") and not re.match(
            r"^http://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", base_url or ""):
        raise RemoteAgentError("base_url must be an https URL (http only for localhost)")
    if token_env and not re.match(r"^[A-Z][A-Z0-9_]{0,63}$", token_env):
        raise RemoteAgentError("token_env must be an environment variable NAME")
    if isinstance(ceiling_usd, bool) or not isinstance(ceiling_usd, (int, float)) \
            or ceiling_usd <= 0:
        raise RemoteAgentError("spend_ceiling_usd is required and must be positive")
    return int(round(float(ceiling_usd) * 1_000_000))


def create_profile(connection: Connection, project_id: str, *, actor: str, name: str,
                   base_url: str, spend_ceiling_usd: Any, token_env: str = "",
                   provider: str = "generic", description: str = "",
                   poll_seconds: float = 3.0, timeout_seconds: int = 3600) -> dict[str, Any]:
    from .core import emit_event, make_id

    micros = _validate(name, provider, base_url, token_env, spend_ceiling_usd)
    org = _org_of(connection, project_id)
    if connection.execute(text("SELECT 1 FROM remote_agent_profiles WHERE org_id = :o AND "
                               "name = :n"), {"o": org, "n": name}).first():
        raise RemoteAgentError(f"a remote agent named {name!r} already exists", 409)
    row = connection.execute(
        text(f"INSERT INTO remote_agent_profiles (id, org_id, name, provider, base_url, "
             f"token_env, spend_ceiling_usd_micros, poll_seconds, timeout_seconds, description, "
             f"created_by) VALUES (:id, :o, :n, :pv, :u, :t, :c, :ps, :ts, :d, :by) "
             f"RETURNING {_COLS}"),
        {"id": make_id("rag"), "o": org, "n": name, "pv": provider, "u": base_url.rstrip("/"),
         "t": token_env or None, "c": micros, "ps": float(poll_seconds),
         "ts": int(timeout_seconds), "d": description[:400], "by": actor}).mappings().one()
    emit_event(connection, project_id, "remote_agent.created", actor, "remote_agent", row["id"],
               {"name": name, "provider": provider, "spend_ceiling_usd_micros": micros})
    forget(name)
    return view(row)


def acknowledge_retention(connection: Connection, project_id: str, profile_id: str, *,
                          actor: str, terms: str) -> dict[str, Any]:
    """A person accepts the provider's retention terms, in their words. Required to run."""
    from .core import emit_event

    if not (terms or "").strip():
        raise RemoteAgentError("say what you are acknowledging (the provider's retention terms)")
    row = connection.execute(
        text(f"UPDATE remote_agent_profiles SET retention_ack_by = :by, retention_ack_at = now(), "
             f"retention_terms = :t, updated_at = now() WHERE id = :id AND org_id = :o "
             f"RETURNING {_COLS}"),
        {"by": actor, "t": terms.strip()[:2000], "id": profile_id,
         "o": _org_of(connection, project_id)}).mappings().first()
    if row is None:
        raise RemoteAgentError(f"no remote agent {profile_id!r}", 404)
    emit_event(connection, project_id, "remote_agent.retention_acknowledged", actor,
               "remote_agent", profile_id, {"name": row["name"]})
    forget(row["name"])
    return view(row)


def update_profile(connection: Connection, project_id: str, profile_id: str, *, actor: str,
                   spend_ceiling_usd: Any = None, status: Optional[str] = None) -> dict[str, Any]:
    from .core import emit_event

    sets, params = [], {"id": profile_id, "o": _org_of(connection, project_id)}
    if spend_ceiling_usd is not None:
        if isinstance(spend_ceiling_usd, bool) or not isinstance(spend_ceiling_usd, (int, float)) \
                or spend_ceiling_usd <= 0:
            raise RemoteAgentError("spend_ceiling_usd must be positive")
        sets.append("spend_ceiling_usd_micros = :c")
        params["c"] = int(round(float(spend_ceiling_usd) * 1_000_000))
    if status is not None:
        if status not in ("active", "disabled"):
            raise RemoteAgentError("status must be active or disabled")
        sets.append("status = :s")
        params["s"] = status
    if not sets:
        raise RemoteAgentError("nothing to change")
    row = connection.execute(
        text(f"UPDATE remote_agent_profiles SET {', '.join(sets)}, updated_at = now() "
             f"WHERE id = :id AND org_id = :o RETURNING {_COLS}"), params).mappings().first()
    if row is None:
        raise RemoteAgentError(f"no remote agent {profile_id!r}", 404)
    emit_event(connection, project_id, "remote_agent.updated", actor, "remote_agent", profile_id,
               {k: v for k, v in params.items() if k in ("c", "s")})
    forget(row["name"])
    return view(row)


def list_profiles(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(f"SELECT {_COLS} FROM remote_agent_profiles WHERE org_id = :o ORDER BY name"),
        {"o": _org_of(connection, project_id)}).mappings().all()
    return [view(r) for r in rows]


def load(connection: Connection, name: str, project_id: Optional[str] = None
         ) -> Optional[RemoteProfile]:
    """The profile `name` of the project's org. Without a project: only when exactly one org
    has a profile of that name — a name is never resolved to another org's agent by guess."""
    if project_id:
        rows = connection.execute(
            text(f"SELECT {_COLS} FROM remote_agent_profiles WHERE name = :n AND org_id = :o"),
            {"n": name, "o": _org_of(connection, project_id)}).mappings().all()
    else:
        rows = connection.execute(
            text(f"SELECT {_COLS} FROM remote_agent_profiles WHERE name = :n LIMIT 2"),
            {"n": name}).mappings().all()
    return _profile(rows[0]) if len(rows) == 1 else None


def _project_in_context() -> Optional[str]:
    from .llm_telemetry import get_llm_context

    return (get_llm_context() or {}).get("project_id") or None


# ── the spend ceiling, per BOSS run ───────────────────────────────────────────────────────────

_SPENT: dict[tuple[str, str], int] = {}
_SPENT_LOCK = threading.Lock()


def _run_key() -> str:
    from .llm_telemetry import get_llm_context

    ctx = get_llm_context() or {}
    return str(ctx.get("run_id") or ctx.get("workflow_run_id") or "adhoc")


def spent(profile: str, run_key: str) -> int:
    with _SPENT_LOCK:
        return _SPENT.get((profile, run_key), 0)


def _add_spent(profile: str, run_key: str, micros: int) -> int:
    with _SPENT_LOCK:
        total = _SPENT.get((profile, run_key), 0) + max(0, int(micros))
        _SPENT[(profile, run_key)] = total
        return total


# ── the harness ───────────────────────────────────────────────────────────────────────────────

class RemoteAgentCodeAgent(_CustomAgentBase):
    """A hosted agent driven over boss.harness.remote/v1: submit, poll, collect a patch, apply."""

    def __init__(self, settings: Any, profile: RemoteProfile, *,
                 opener: Optional[Callable[..., Any]] = None,
                 git: Optional[Callable[..., Any]] = None) -> None:
        self.settings = settings
        self.profile = profile
        self.engine = profile.harness_name
        self.mcp_harness = None
        self._open = opener or urllib.request.urlopen
        self._git = git

    def _request(self, method: str, url: str, body: Optional[dict] = None, *,
                 raw: bool = False) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        token = os.environ.get(self.profile.token_env, "") if self.profile.token_env else ""
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with self._open(req, timeout=30) as resp:
                payload = resp.read(MAX_PATCH_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise CustomHarnessError(f"{self.engine}: {method} answered {exc.code}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise CustomHarnessError(f"{self.engine}: {method} failed: {exc}") from None
        if len(payload) > MAX_PATCH_BYTES:
            raise CustomHarnessError(f"{self.engine}: the answer is larger than "
                                     f"{MAX_PATCH_BYTES} bytes")
        if raw:
            return payload.decode("utf-8", errors="replace")
        try:
            value = json.loads(payload.decode("utf-8") or "{}")
        except ValueError:
            raise CustomHarnessError(f"{self.engine}: {method} did not answer JSON") from None
        return value if isinstance(value, dict) else {}

    def _base(self) -> str:
        return self.profile.base_url.rstrip("/")

    def _repository(self, cwd: Path) -> dict[str, Any]:
        out = {"url": None, "branch": None, "base_sha": None}
        for key, argv in (("url", ["git", "remote", "get-url", "origin"]),
                          ("branch", ["git", "rev-parse", "--abbrev-ref", "HEAD"]),
                          ("base_sha", ["git", "rev-parse", "HEAD"])):
            try:
                done = self._run_git(argv, cwd)
                if done.returncode == 0:
                    out[key] = (done.stdout or "").strip() or None
            except Exception:  # noqa: BLE001 — the agent may not need every field
                pass
        if out["url"]:
            # A credential embedded in a clone URL never leaves BOSS.
            out["url"] = re.sub(r"//[^@/]+@", "//", out["url"])
        return out

    def _run_git(self, argv: list[str], cwd: Path, *, input: Optional[str] = None) -> Any:
        if self._git is not None:
            return self._git(argv, cwd=str(cwd), input=input)
        from . import owned_process

        return owned_process.run(argv, cwd=str(cwd), input=input, capture_output=True,
                                 text=True, timeout=300)

    def _apply(self, cwd: Path, patch: str) -> list[str]:
        """Apply the remote agent's change to BOSS's workspace; the files it touched."""
        if not patch.strip():
            return []
        touched = sorted(set(re.findall(r"^\+\+\+ b/(\S+)", patch, re.M)
                             + re.findall(r"^--- a/(\S+)", patch, re.M)))
        for path in touched:
            parts = Path(path).parts
            if Path(path).is_absolute() or ".." in parts or (parts and parts[0] == ".git"):
                raise CustomHarnessError(f"{self.engine}: the patch touches {path!r}, outside "
                                         "the working tree")
        done = self._run_git(["git", "apply", "--whitespace=nowarn", "-"], cwd, input=patch)
        if done.returncode != 0:
            raise CustomHarnessError(f"{self.engine}: its patch does not apply: "
                                     f"{(done.stderr or '').strip()[-300:]}")
        return touched

    def run_role(self, prompt: str, cwd: Path, *, read_only: bool = False,
                 model: Optional[str] = None, resume_session_id: Optional[str] = None,
                 **_ignored: Any) -> dict:
        from . import harness_failures
        from .code_agent import _cap_to_deadline

        harness_failures.raise_if_tripped()
        why = self.profile.runnable
        if why:
            raise CustomHarnessError(why)
        run_key = _run_key()
        ceiling = self.profile.spend_ceiling_usd_micros
        already = spent(self.profile.name, run_key)
        if already >= ceiling:
            raise CustomHarnessError(
                f"{self.engine} reached its spend ceiling (${ceiling / 1e6:.2f}) for this run")
        timeout = _cap_to_deadline(int(min(self.profile.timeout, 420) if read_only
                                       else self.profile.timeout), read_only=read_only)
        cwd = Path(cwd)
        body = {"contract": CONTRACT, "prompt": prompt,
                "mode": "read_only" if read_only else "write", "model": model or "",
                "repository": self._repository(cwd),
                "spend_ceiling_usd_micros": ceiling - already, "deliver": "diff"}
        if resume_session_id:
            body["session_id"] = resume_session_id
        started = time.monotonic()
        submitted = self._request("POST", f"{self._base()}/v1/runs", body)
        run_id = str(submitted.get("run_id") or "")
        if not run_id:
            raise CustomHarnessError(f"{self.engine}: POST /v1/runs answered without run_id")
        wait = max(0.05, float(submitted.get("poll_after_ms") or 0) / 1000.0
                   or self.profile.poll_seconds)
        deadline = started + timeout
        seen_cost = 0
        events: list[dict] = []
        while True:
            state = self._request("GET", f"{self._base()}/v1/runs/{run_id}")
            status = str(state.get("status") or "")
            if status not in STATUSES:
                raise CustomHarnessError(f"{self.engine}: unknown run status {status!r}")
            events.extend(e for e in state.get("events") or [] if isinstance(e, dict))
            cost = int((state.get("usage") or {}).get("cost_usd_micros") or 0)
            if cost > seen_cost:
                total = _add_spent(self.profile.name, run_key, cost - seen_cost)
                seen_cost = cost
                if total >= ceiling and status in ("queued", "running"):
                    self._cancel(run_id)
                    raise CustomHarnessError(
                        f"{self.engine} reached its spend ceiling (${ceiling / 1e6:.2f}) for "
                        "this run; BOSS cancelled the remote run")
            if status not in ("queued", "running"):
                break
            if time.monotonic() >= deadline:
                self._cancel(run_id)
                raise CustomHarnessError(f"{self.engine} did not finish within {timeout}s")
            time.sleep(min(wait, max(0.0, deadline - time.monotonic())))
        report = {**state, "tool_calls": state.get("tool_calls") or [
            {"name": e.get("tool"), "input": e.get("input") or {}} for e in events
            if e.get("tool")]}
        normal = normalise_report(report)
        ok = normal["status"] != "error"
        touched: list[str] = []
        if ok and not read_only:
            touched = self._apply(cwd, self._patch(state.get("result") or {}))
        out = self._finish(normal, prompt=prompt, model=model, started=started, ok=ok)
        if not ok:
            raise CustomHarnessError(f"{self.engine} failed: "
                                     f"{(normal.get('error') or out['summary'])[:300]}")
        out.update(run_id=run_id, files_changed=touched,
                   spend={"run_usd_micros": spent(self.profile.name, run_key),
                          "ceiling_usd_micros": ceiling})
        return out

    def _patch(self, result: dict) -> str:
        kind = result.get("kind")
        if kind in (None, "diff"):
            return str(result.get("patch") or "")
        if kind == "pr":
            if result.get("patch"):
                return str(result["patch"])
            url = str(result.get("patch_url") or "")
            if url.startswith("https://") or url.startswith(self._base()):
                return self._request("GET", url, raw=True)
            raise CustomHarnessError(f"{self.engine}: a pr result needs patch or an https "
                                     "patch_url, so BOSS can gate the change itself")
        raise CustomHarnessError(f"{self.engine}: unknown result kind {kind!r}")

    def _cancel(self, run_id: str) -> None:
        try:
            self._request("DELETE", f"{self._base()}/v1/runs/{run_id}")
        except CustomHarnessError:
            pass


# ── lazy registration ─────────────────────────────────────────────────────────────────────────

def _builder(name: str):
    def build(settings: Any) -> Any:
        from ..db import begin

        with begin() as connection:
            profile = load(connection, name, _project_in_context())
        if profile is None:
            raise CustomHarnessError(f"no remote agent {name!r} for this project's org")
        why = profile.runnable
        if why:
            raise CustomHarnessError(why)
        return RemoteAgentCodeAgent(settings, profile)
    return build


def resolve(key: str) -> Optional[Any]:
    """harness_registry's resolver for `remote:<name>`: registers the profile (verified=False)
    the first time a build names it. Unknown or unreadable: None (the registry says unknown)."""
    if not key.startswith(PREFIX):
        return None
    name = key[len(PREFIX):]
    try:
        from ..db import begin

        with begin() as connection:
            profile = load(connection, name, _project_in_context())
    except Exception:  # noqa: BLE001 — no database: nothing to resolve
        _log.debug("remote agents: profile lookup failed", exc_info=True)
        return None
    if profile is None:
        return None
    from .harness_registry import register_harness

    return register_harness(
        profile.harness_name, _builder(name), kind=KIND, verified=False, replace=True,
        description=(profile.description or f"Hosted agent at {profile.base_url}")
        + f" [remote agent, spend ceiling ${profile.spend_ceiling_usd_micros / 1e6:.2f}/run]",
        requires=((profile.token_env,) if profile.token_env else ())
        + ("a retention acknowledgement",), source=f"remote:{profile.id}")


def forget(name: str) -> None:
    """Drop a cached registration so the next build re-reads the profile."""
    from . import harness_registry

    harness_registry._REGISTRY.pop(PREFIX + name, None)


def clear_spend_for_tests() -> None:
    with _SPENT_LOCK:
        _SPENT.clear()
