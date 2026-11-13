"""In-house coding agents as BOSS engines: the `process` and `http` harness kinds (BOSSAPP-853).

A team with its own agent — a script around an internal model, a service another team runs —
should not have to write a Python adapter (or fork BOSS) to put it behind the governed pipeline.
These two kinds are CONTRACTS instead: the agent keeps to a small, documented interface and BOSS
drives it like any other engine. docs/engineering/HARNESS_ADAPTERS.md is the contract's prose;
this module is its only implementation, and the conformance fakes in
tests/test_harness_breadth_853_854.py hold both to it.

Both kinds share BOSS's rule for CLIs: the agent edits files IN PLACE in the workspace, the git
diff is the change of record, and BOSS re-runs the tests itself. What the agent reports is kept
as the summary and transcript and is never trusted for pass/fail.

`process` (boss.harness.process/v1)
    argv           a template; placeholders {prompt} {prompt_file} {workspace} {model} {mode}
                   {result_file} are substituted, nothing else is interpreted (no shell)
    prompt_via     stdin | argv ({prompt} in argv) | file (BOSS_PROMPT_FILE / {prompt_file})
    environment    BOSS's deny-by-default agent environment, plus BOSS_HARNESS_CONTRACT,
                   BOSS_WORKSPACE, BOSS_MODE (write|read_only), BOSS_MODEL, BOSS_PROMPT_FILE,
                   BOSS_RESULT_FILE, and the kit: BOSS_MCP_CONFIG, BOSS_SKILLS_DIR
    result         optional JSON at BOSS_RESULT_FILE (RESULT_FIELDS); without it stdout is the
                   summary. Exit 0 = the agent ran; non-zero = the stage fails with its words.

`http` (boss.harness.http/v1)
    POST   {base}/v1/runs            {contract, prompt, mode, model, session_id?,
                                      workspace: {kind: "path", path, access}, kit}
                                      -> 201/202 {run_id, poll_after_ms?}
    GET    {base}/v1/runs/{run_id}   -> {status, summary?, usage?, tool_calls?, session_id?,
                                         error?}; status: queued|running|done|error|
                                         turn_cap|awaiting_approval
    DELETE {base}/v1/runs/{run_id}   BOSS cancels a run past the stage's deadline
    auth   `Authorization: Bearer <token>` from the env var the manifest names (never logged)
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import model_proxy as _model_proxy
from .engine_contract import EngineCapabilities, STOP_REASONS
from .harness_delivery import current as current_kit, delivers_kit
from .workspace_perms import tidies_workspace

_log = logging.getLogger(__name__)

PROCESS_CONTRACT = "boss.harness.process/v1"
HTTP_CONTRACT = "boss.harness.http/v1"
PLACEHOLDERS = ("prompt", "prompt_file", "workspace", "model", "mode", "result_file")
PROMPT_VIA = ("stdin", "argv", "file")
#: Keys a result (the process's result file, the http run's final poll) may carry.
RESULT_FIELDS = ("summary", "status", "usage", "tool_calls", "session_id", "error")
HTTP_STATUSES = ("queued", "running", "done", "error", "turn_cap", "awaiting_approval")
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


class CustomHarnessError(RuntimeError):
    """The custom agent could not run, or broke its contract."""


@dataclass(frozen=True)
class ProcessHarnessConfig:
    name: str
    argv: tuple[str, ...]
    prompt_via: str = "stdin"
    timeout: int = 2400
    review_timeout: int = 420
    env_passthrough: tuple[str, ...] = ()
    description: str = ""
    #: The kit reaches a process as $BOSS_MCP_CONFIG / $BOSS_SKILLS_DIR (harness_delivery).
    mcp_delivery: str = "env_manifest"
    skills_dir: str = "{env}"

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError(f"process harness {self.name!r}: argv is required")
        if self.prompt_via not in PROMPT_VIA:
            raise ValueError(f"process harness {self.name!r}: prompt_via must be one of "
                             f"{PROMPT_VIA}")
        for part in self.argv:
            for key in _PLACEHOLDER.findall(part):
                if key not in PLACEHOLDERS:
                    raise ValueError(f"process harness {self.name!r}: unknown placeholder "
                                     f"{{{key}}}; allowed: {', '.join(PLACEHOLDERS)}")
        if self.prompt_via == "argv" and not any("{prompt}" in p for p in self.argv):
            raise ValueError(f"process harness {self.name!r}: prompt_via=argv needs {{prompt}} "
                             "in argv")


@dataclass(frozen=True)
class HttpHarnessConfig:
    name: str
    base_url: str
    token_env: str = ""
    poll_seconds: float = 2.0
    timeout: int = 2400
    review_timeout: int = 420
    request_timeout: float = 30.0
    description: str = ""
    mcp_delivery: str = "env_manifest"
    skills_dir: str = "{env}"

    def __post_init__(self) -> None:
        if not re.match(r"^https?://[^\s/]+", self.base_url or ""):
            raise ValueError(f"http harness {self.name!r}: base_url must be an http(s) URL")


def _substitute(part: str, values: dict[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), part)


def normalise_report(report: Any, *, fallback_summary: str = "") -> dict[str, Any]:
    """A result file / final poll in the contract's keys; anything else is dropped."""
    report = report if isinstance(report, dict) else {}
    status = str(report.get("status") or "done")
    if status not in STOP_REASONS:
        status = "error"
    calls = [c for c in report.get("tool_calls") or [] if isinstance(c, dict) and c.get("name")]
    usage = report.get("usage") if isinstance(report.get("usage"), dict) else {}
    return {"summary": str(report.get("summary") or fallback_summary or "").strip(),
            "status": status, "usage": usage, "tool_calls": calls,
            "session_id": report.get("session_id") or None,
            "error": str(report.get("error") or "") or None}


class _CustomAgentBase:
    engine: str

    @property
    def capabilities(self) -> EngineCapabilities:
        # A contract BOSS cannot inspect inside: no trusted read-only mode (review seats run on
        # a throwaway copy), and cost is whatever the agent reports.
        return EngineCapabilities()

    def _finish(self, report: dict[str, Any], *, prompt: str, model: Optional[str],
                started: float, ok: bool) -> dict[str, Any]:
        from .engine_contract import canonical_tool_input, canonical_tool_name
        from .llm_telemetry import record_agent_turns, record_llm_call

        usage = dict(report.get("usage") or {})
        usage.setdefault("model", model or self.engine)
        cost = int(usage.pop("cost_usd_micros", 0) or 0)
        calls = report.get("tool_calls") or []
        turns = []
        for call in calls:
            name = canonical_tool_name(call.get("name"))
            turns.append({"direction": "tool_call", "tool_name": name,
                          "content": canonical_tool_input(name, call.get("input") or {})})
            if "output" in call or "ok" in call:
                turns.append({"direction": "tool_result", "tool_name": name,
                              "content": str(call.get("output") or "")[:4000],
                              "ok": bool(call.get("ok", True))})
        record_llm_call(engine_name=self.engine, usage=usage, cost_usd_micros=cost,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        turns=max(1, len(calls)), ok=ok,
                        error=None if ok else (report.get("error") or report["summary"])[:400])
        try:
            record_agent_turns(turns + [{"direction": "completion",
                                         "content": report["summary"][:8000], "ok": ok}],
                               engine_name=self.engine, model=model or "", prompt=prompt)
        except Exception:  # noqa: BLE001 — a transcript never fails a build
            pass
        out = {"summary": report["summary"][:8000] or f"({self.engine} produced no output)",
               "usage": usage, "cost_usd_micros": cost, "cost_unreported": cost == 0,
               "turns": max(1, len(calls)), "tool_calls": len(calls), "model": model or "",
               "session_id": report.get("session_id"), "stop_reason": report["status"]}
        kit = current_kit()
        if kit is not None:
            out["kit"] = kit.as_dict()
        if report["status"] == "awaiting_approval":
            out["awaiting_approval"] = True
        return out

    def run(self, spec: Any, repo: Any, sandbox: Any) -> Any:
        """Whole-task entry point, as GenericCliCodeAgent's: the diff is the change of record."""
        from .cli_harness import GenericCliCodeAgent

        return GenericCliCodeAgent.run(self, spec, repo, sandbox)  # type: ignore[arg-type]


class ProcessCodeAgent(_CustomAgentBase):
    """An in-house agent started as a process per turn (boss.harness.process/v1)."""

    def __init__(self, settings: Any, config: ProcessHarnessConfig) -> None:
        self.settings = settings
        self.spec = config
        self.engine = config.name
        self.mcp_harness = None

    def _env(self, extra: dict[str, str]) -> dict[str, str]:
        from .agent_env import agent_environment

        env = agent_environment(os.environ)
        for name in self.spec.env_passthrough:
            if os.environ.get(name):
                env[name] = os.environ[name]
        env.setdefault("HOME", os.environ.get("HOME", ""))
        env["CI"] = "1"
        env.update(extra)
        kit = current_kit()
        if kit is not None:
            env.update(kit.env_for(env))
        return _model_proxy.shield_env(env, settings=self.settings)

    @tidies_workspace
    @delivers_kit
    def run_role(self, prompt: str, cwd: Path, *, read_only: bool = False,
                 model: Optional[str] = None, resume_session_id: Optional[str] = None,
                 **_ignored: Any) -> dict:
        from . import harness_failures, runner_dispatch
        from .code_agent import _cap_to_deadline
        from .scheduling import low_priority

        harness_failures.raise_if_tripped()
        timeout = _cap_to_deadline(
            int(self.spec.review_timeout if read_only else self.spec.timeout), read_only=read_only)
        work = tempfile.mkdtemp(prefix="boss-process-")
        try:
            prompt_file = os.path.join(work, "prompt.txt")
            result_file = os.path.join(work, "result.json")
            Path(prompt_file).write_text(prompt, encoding="utf-8")
            mode = "read_only" if read_only else "write"
            values = {"prompt": prompt, "prompt_file": prompt_file, "workspace": str(cwd),
                      "model": model or "", "mode": mode, "result_file": result_file}
            argv = [_substitute(p, values) for p in self.spec.argv]
            env = self._env({"BOSS_HARNESS_CONTRACT": PROCESS_CONTRACT,
                             "BOSS_WORKSPACE": str(cwd), "BOSS_MODE": mode,
                             "BOSS_MODEL": model or "", "BOSS_PROMPT_FILE": prompt_file,
                             "BOSS_RESULT_FILE": result_file,
                             **({"BOSS_SESSION_ID": resume_session_id}
                                if resume_session_id else {})})
            started = time.monotonic()
            try:
                # THE DISPATCH SEAM (BOSSAPP-828), passed through exactly as the CLI path does.
                proc = runner_dispatch.run(
                    low_priority(argv), cwd=str(cwd),
                    input=(prompt if self.spec.prompt_via == "stdin" else None),
                    capture_output=True, text=True, timeout=timeout, start_new_session=True,
                    env=env, session_key=runner_dispatch.turn_key(self.engine, model or "",
                                                                  read_only, prompt),
                    collect_files=[result_file])
            except subprocess.TimeoutExpired:
                raise CustomHarnessError(
                    f"{self.engine} did not finish within {timeout}s") from None
            except FileNotFoundError as exc:
                raise CustomHarnessError(f"{self.engine} could not start: {exc}") from None
            report: Any = None
            if os.path.isfile(result_file):
                try:
                    report = json.loads(Path(result_file).read_text(encoding="utf-8"))
                except ValueError:
                    _log.warning("%s wrote a result file that is not JSON; using stdout",
                                 self.engine)
            ok = proc.returncode == 0
            normal = normalise_report(report, fallback_summary=(proc.stdout or "").strip()
                                      or (proc.stderr or "").strip())
            if not ok:
                normal["status"] = "error"
                normal["error"] = normal.get("error") or (proc.stderr or "")[-400:]
            out = self._finish(normal, prompt=prompt, model=model, started=started,
                               ok=ok and normal["status"] != "error")
            if not ok:
                raise CustomHarnessError(
                    f"{self.engine} exited {proc.returncode}: {out['summary'][:300]}")
            out["exit_code"] = proc.returncode
            return out
        finally:
            import shutil

            shutil.rmtree(work, ignore_errors=True)


class HttpCodeAgent(_CustomAgentBase):
    """An in-house agent behind HTTP (boss.harness.http/v1): submit, poll, cancel."""

    def __init__(self, settings: Any, config: HttpHarnessConfig, *,
                 opener: Optional[Callable[..., Any]] = None) -> None:
        self.settings = settings
        self.spec = config
        self.engine = config.name
        self.mcp_harness = None
        self._open = opener or urllib.request.urlopen

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        url = self.spec.base_url.rstrip("/") + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        token = os.environ.get(self.spec.token_env, "") if self.spec.token_env else ""
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with self._open(req, timeout=self.spec.request_timeout) as response:
                raw = response.read().decode("utf-8") or "{}"
        except urllib.error.HTTPError as exc:
            raise CustomHarnessError(f"{self.engine}: {method} {path} answered {exc.code}") \
                from None
        except (urllib.error.URLError, OSError) as exc:
            raise CustomHarnessError(f"{self.engine}: {method} {path} failed: {exc}") from None
        try:
            value = json.loads(raw)
        except ValueError:
            raise CustomHarnessError(f"{self.engine}: {method} {path} did not answer JSON") \
                from None
        return value if isinstance(value, dict) else {}

    @tidies_workspace
    @delivers_kit
    def run_role(self, prompt: str, cwd: Path, *, read_only: bool = False,
                 model: Optional[str] = None, resume_session_id: Optional[str] = None,
                 **_ignored: Any) -> dict:
        from . import harness_failures
        from .code_agent import _cap_to_deadline

        harness_failures.raise_if_tripped()
        timeout = _cap_to_deadline(
            int(self.spec.review_timeout if read_only else self.spec.timeout), read_only=read_only)
        kit = current_kit()
        kit_body: dict[str, Any] = {"mcpServers": {}, "skills_dir": None}
        if kit is not None:
            manifest = kit.env.get("BOSS_MCP_CONFIG")
            if manifest and os.path.isfile(manifest):
                try:
                    kit_body["mcpServers"] = json.loads(
                        Path(manifest).read_text(encoding="utf-8")).get("mcpServers") or {}
                except ValueError:
                    pass
            kit_body["skills_dir"] = kit.env.get("BOSS_SKILLS_DIR")
        body = {"contract": HTTP_CONTRACT, "prompt": prompt,
                "mode": "read_only" if read_only else "write", "model": model or "",
                "workspace": {"kind": "path", "path": str(Path(cwd).resolve()),
                              "access": "read_only" if read_only else "read_write"},
                "kit": kit_body}
        if resume_session_id:
            body["session_id"] = resume_session_id
        started = time.monotonic()
        submitted = self._request("POST", "/v1/runs", body)
        run_id = str(submitted.get("run_id") or "")
        if not run_id:
            raise CustomHarnessError(f"{self.engine}: POST /v1/runs answered without run_id")
        wait = max(0.05, float(submitted.get("poll_after_ms") or 0) / 1000.0
                   or self.spec.poll_seconds)
        deadline = started + timeout
        while True:
            state = self._request("GET", f"/v1/runs/{run_id}")
            status = str(state.get("status") or "")
            if status not in HTTP_STATUSES:
                raise CustomHarnessError(f"{self.engine}: unknown run status {status!r}")
            if status not in ("queued", "running"):
                break
            if time.monotonic() >= deadline:
                try:
                    self._request("DELETE", f"/v1/runs/{run_id}")
                except CustomHarnessError:
                    pass
                raise CustomHarnessError(f"{self.engine} did not finish within {timeout}s")
            time.sleep(min(wait, max(0.0, deadline - time.monotonic())))
        normal = normalise_report(state)
        ok = normal["status"] != "error"
        out = self._finish(normal, prompt=prompt, model=model, started=started, ok=ok)
        if not ok:
            raise CustomHarnessError(f"{self.engine} failed: "
                                     f"{(normal.get('error') or out['summary'])[:300]}")
        out["run_id"] = run_id
        return out
