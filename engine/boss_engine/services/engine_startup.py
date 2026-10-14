"""CAN THE ENGINE A STEP WILL ACTUALLY USE START HERE?

WHY THIS EXISTS
---------------
A HelpDesk build on the laptop had its Build job pinned to `claude_agent_sdk` on the API key
(`setup-anthropic`). Every stage that ran a command failed with

    Sandbox is required but failed to initialize: Sandbox dependencies not available:
    socat not installed. Restart to retry.

while the pre-check said "a model is connected: claude_code (subscription)" — the CHAT job's
answer, on a subscription that was disconnected. It looked at neither the engine routing would
actually use nor whether that engine could start.

So this module answers both, for the pre-check's `sandbox.engine` and `sandbox.model`:

  * `build_routes` / `chat_route` — the engine, model and entitlement source each stage will
    use, through the SAME code the worker routes with (`model_router.stages.lane_engines`, the
    StageRouter's Fixed answer, `model_routing.resolve_job`): job pins (`setup.job_set`), the
    task's own `model_config`, the profile. Nothing here re-decides a route.
  * `source_state` — whether that source is usable: a disconnected, revoked or credential-less
    source is never "connected".
  * `probe` — the engine's own start-up, the way a build starts it (make_code_agent →
    run_role, sandbox and all), as one tiny session that must run one harmless command. Its
    cost goes through the engine's normal metering (run_role → record_llm_call).

The binaries an engine needs are its own declaration (harness_registry.RuntimeBinary). The
claude_agent_sdk engine no longer declares bubblewrap/socat: it stopped forcing the Claude CLI's
own sandbox once its session held a model-proxy run token instead of the key (model_proxy). The
probe starts that proxy exactly as a build does, so a proxy that cannot listen is the probe's
`will_fail`, in the proxy's own words.
"""
from __future__ import annotations

import contextlib
import contextvars
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

#: The build stages whose engines a build-like step starts: the writing lane and the judging lane.
BUILD_STAGES = ("write_tests", "implement", "review_code", "security_check")
#: The stages a `review` step reads (the build's review panel).
REVIEW_STAGES = ("review_code", "security_check", "consistency_check", "check_tests")

#: The probe: one harmless command through the engine's shell tool, and the output that says
#: it ran (the arithmetic is the shell's, so the reply carries the tool's result).
PROBE_COMMAND = "echo boss-engine-$((6*7))"
PROBE_MARK = "boss-engine-42"
PROBE_PROMPT = ("BOSS is checking that this agent can start and run a command. Run exactly this "
                f"shell command with your Bash tool: {PROBE_COMMAND}\n"
                "Then reply with only the command's output. Do nothing else.")
PROBE_MAX_TURNS = 3
PROBE_TIMEOUT_SECONDS = 120

#: Entitlement source states that are never a working connection.
_DEAD_CONNECTION = frozenset({"revoked", "disconnected", "error"})
_DEAD_STATUS = frozenset({"revoked", "disabled", "inactive", "missing_oauth_connection",
                          "runtime_unavailable"})


@dataclass(frozen=True)
class EngineRoute:
    """Which engine a stage (or the chat job) runs on, which model, on which source, and why."""

    stage: str
    engine: str
    model: Optional[str]
    source_id: Optional[str]
    why: str

    def key(self) -> tuple[str, str, str]:
        return (self.engine, self.source_id or "", self.model or "")

    def describe(self) -> str:
        if not self.engine:
            return f"{self.stage} → no engine ({self.why})"
        return (f"{self.stage} → {self.engine} / {self.model or 'its default model'} via "
                f"{self.source_id or 'the install environment'} ({self.why})")


def default_model(engine: str, settings: Any) -> Optional[str]:
    """The model an engine runs when the route names none (what the engine itself falls back to)."""
    if engine == "claude_agent_sdk":
        from .agent_sdk_engine import DEFAULT_MODEL

        return str(getattr(settings, "agent_sdk_model", "") or "") or DEFAULT_MODEL
    knob = {"claude_code": "claude_cli_model", "codex": "codex_cli_model"}.get(engine)
    value = str(getattr(settings, knob, "") or "") if knob else ""
    return value or None


def build_routes(connection: Any, settings: Any, project_id: str,
                 spec: Optional[Mapping[str, Any]] = None,
                 stages: Sequence[str] = BUILD_STAGES) -> list[EngineRoute]:
    """The route of each build stage, as the governed pipeline resolves it at its start: the
    lane engines (`lane_engines`, the pipeline's own call) and the StageRouter's Fixed answer
    per stage. In Smart mode the router may move a stage to another connected source at run
    time; the evidence says so."""
    from .model_router import store
    from .model_router.stages import StageRouter, lane_engines, pinned_engine, task_pin
    from .model_routing import CHOSEN, resolve_job
    from .pipeline_profile_service import resolve_for_run

    spec = dict(spec or {})
    s, build_engine, review_engine = lane_engines(connection, settings, project_id, spec)
    profile = resolve_for_run(connection, project_id, spec.get("profile"), s)
    router = StageRouter(transaction=lambda: contextlib.nullcontext(connection), settings=s,
                         project_id=project_id, run_id="", spec=spec,
                         title=str(spec.get("title") or ""), profile=profile,
                         build_engine=build_engine, review_engine=review_engine,
                         budget_state="green")
    try:
        smart = store.mode_of(connection, s, project_id) == "smart"
    except Exception:  # noqa: BLE001 — an unreadable mode is the default, Fixed
        smart = False
    try:
        from .setup_service import connected_engines

        connected = connected_engines(connection, s, project_id)
    except Exception:  # noqa: BLE001
        connected = {}
    jobs = {lane: resolve_job(connection, s, project_id, lane) for lane in ("build", "review")}
    pinned = pinned_engine(spec)
    out = []
    for stage in stages:
        legacy = router.legacy(stage, budget=False)
        engine = (legacy.engine or "").lower()
        job = jobs["review" if stage in REVIEW_STAGES else "build"]
        if task_pin(spec, stage) is not None or (pinned and engine == pinned):
            why = "pinned by the task"
        elif job.engine == engine and job.why == CHOSEN:
            why = "pinned in Repo and models"
        elif job.engine == engine:
            why = f"auto: {job.why}"
        else:
            why = legacy.why
        if smart:
            why += "; Smart routing may move it to another connected source"
        source = (job.source_id if job.engine == engine and job.source_id
                  else (connected.get(engine) or {}).get("source_id"))
        model = (legacy.model or (job.model if job.engine == engine else None)
                 or default_model(engine, s))
        out.append(EngineRoute(stage, engine, model, source, why))
    return out


def chat_route(connection: Any, settings: Any, project_id: str) -> EngineRoute:
    """The chat job's route — what a meeting, a panel, an architecture document or a research
    step talks to (`model_routing.choose` is the same call)."""
    from .model_routing import CHOSEN, resolve_job, routed_settings

    s = routed_settings(connection, settings, project_id)
    job = resolve_job(connection, s, project_id, "chat")
    why = "pinned in Repo and models" if job.why == CHOSEN else (
        f"auto: {job.why}" if job.engine else job.why)
    return EngineRoute("chat", job.engine, job.model or (default_model(job.engine, s)
                                                         if job.engine else None),
                       job.source_id, why)


def source_state(connection: Any, project_id: str, source_id: Optional[str]
                 ) -> tuple[Optional[bool], str]:
    """(usable, in words) for an entitlement source. None when there is no row to read (an
    install-environment credential, or a source this project never stored)."""
    if not source_id or connection is None:
        return None, "the install environment"
    from sqlalchemy import text

    row = connection.execute(text(
        "SELECT status, connection_status, credential_secret_ref_id FROM entitlement_sources "
        "WHERE project_id = :p AND source_id = :s"), {"p": project_id, "s": source_id}
    ).mappings().first()
    if row is None:
        return None, f"{source_id}: no such source"
    status = str(row["status"] or "")
    conn = str(row["connection_status"] or "")
    has_key = bool(row["credential_secret_ref_id"])
    if conn in _DEAD_CONNECTION:
        return False, f"{source_id} is {conn}"
    if status in _DEAD_STATUS:
        return False, f"{source_id} is {status}"
    if status == "missing_credentials" and not has_key:
        return False, f"{source_id} is missing_credentials"
    if conn != "connected" and not has_key:
        return False, f"{source_id} is {conn or 'not connected'} and holds no key"
    return True, f"{source_id} {'connected' if conn == 'connected' else 'key stored'}"


# ── The live probe ────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProbeResult:
    verdict: str           # ready | will_fail | unknown
    detail: str
    cost_usd_micros: int = 0


def _probe_settings(settings: Any) -> Any:
    """The build's settings with the read-only seat capped tiny: a few turns, two minutes."""
    if not hasattr(settings, "model_copy"):
        return settings
    return settings.model_copy(update={"code_agent_review_timeout_seconds": PROBE_TIMEOUT_SECONDS,
                                       "code_agent_review_max_turns": PROBE_MAX_TURNS})


def _transient(detail: str) -> bool:
    from .code_agent import _TRANSIENT_MARKERS

    lowered = detail.lower()
    return any(m in lowered for m in _TRANSIENT_MARKERS)


def probe(route: EngineRoute, settings: Any, connection: Any, project_id: str, *,
          make_agent: Optional[Callable[[], Any]] = None) -> ProbeResult:
    """Start `route`'s engine the way a build does — `make_code_agent` on the project's lane
    credential, `run_role` in a working directory, the engine's own sandbox on — and ask it to
    run PROBE_COMMAND. Unpaced (a busy lane must not stall the pre-check). Never raises.

    A sandbox that cannot start, a refused credential, an account with no credit: the engine's
    own message, `will_fail`. A timeout or an overloaded API: `unknown`."""
    from ..identity.redaction import redact_text

    def build() -> Any:
        from .model_routing import routed_settings

        # The project's choices layered on, as the pipeline does (lane_engines).
        probe_settings = _probe_settings(routed_settings(connection, settings, project_id))
        from .code_agent import make_code_agent

        agent = make_code_agent(probe_settings, engine=route.engine, connection=connection,
                                project_id=project_id)
        return getattr(agent, "_inner", agent)

    def run() -> dict:
        from .llm_telemetry import set_llm_context

        # Metered like any run_role: one llm_calls row on this project, stage "precheck".
        set_llm_context(project_id=project_id, stage="precheck", run_id=None,
                        db_engine=getattr(connection, "engine", None))
        agent = (make_agent or build)()
        with tempfile.TemporaryDirectory(prefix="boss-engine-probe-") as cwd:
            return agent.run_role(PROBE_PROMPT, Path(cwd), read_only=True, model=route.model,
                                  max_turns=PROBE_MAX_TURNS)

    try:
        out = contextvars.copy_context().run(run) or {}
    except Exception as exc:  # noqa: BLE001 — the engine's own failure IS the answer
        detail = redact_text(f"{type(exc).__name__}: {exc}")[:400]
        return ProbeResult("unknown" if _transient(detail) else "will_fail", detail)
    summary = redact_text(str(out.get("summary") or ""))
    cost = int(out.get("cost_usd_micros") or 0)
    spent = f"{int(out.get('turns') or 0)} turn(s), ${cost / 1e6:.4f}"
    if PROBE_MARK in summary:
        return ProbeResult("ready", f"started with its sandbox and ran `{PROBE_COMMAND}` "
                                    f"({spent})", cost)
    return ProbeResult("unknown", f"started, but did not show the command's output ({spent}): "
                                  f"{summary.strip()[:160] or '(empty reply)'}", cost)


__all__ = ["BUILD_STAGES", "EngineRoute", "PROBE_COMMAND", "ProbeResult", "REVIEW_STAGES",
           "build_routes", "chat_route", "default_model", "probe", "source_state"]
