"""THE SCRIPTED CONFORMANCE ENGINE: an engine that obeys the engine contract with NO model.

Engine name "scripted". It plays a deterministic script per pipeline stage, so BOSS's whole build
pipeline — stages, gates, fix rounds, the pull request, events, the build profile and the failure
words — runs end to end for free, and the SAME assertions can be made of it as of any real engine
(engine/tests/test_every_engine_keeps_the_same_contract.py, tests/e2e/gate/engines.py).

What it plays, by stage (the stage is read from the LLM context the pipeline sets, as every
engine's telemetry is):

    standards / spec / plan / design   the three design sections (=== STANDARDS === …)
    review_plan                        PLAN: OK
    write_tests                        writes the fixture's tests; replies "TEST PLAN: …"
    implement / solo / repair / quality writes the fixture's app (or a faulty one, see below)
    review_code                        a verdict naming the changed files (or a BLOCKER finding)
    security_check                     VERDICT: CLEAN
    consistency_check                  CONSISTENT: YES
    check_tests                        the mutation probe: changes one line; the arbiter: CODE_WRONG
    rule_scan                          one rule on the first symbol listed (rule_scan.scripted_reply)

The app is a known-good fixture (tests/e2e/fixtures/plain-node by default; BOSS_SCRIPTED_FIXTURE
names another directory). Its files are split into tests (written by write_tests) and the app
(written by implement); `BOSS_SCRIPTED_APP_DIR` (default: the directory the prompt names, else
"plain") is where they go.

FAULTS (BOSS_SCRIPTED_FAULTS, comma-separated), each injected once and fixed on the next writing
turn, to exercise the fix rounds:

    failing_test    the first implement writes an app whose tests fail (the slug fixture's slug.js;
                    on any other fixture, the first changed .js/.mjs/.py module fails to import)
    blocker         the first review_code answers with a BLOCKER finding
    crash_on_start  the first implement writes a server that exits on start
    crash           every session raises (an engine that cannot run at all)

SELF-FIX STAGES (`self_fix_*`, BOSSAPP-571) play the scripts of self_fix_scripts.py, chosen by
BOSS_SCRIPTED_SELF_FIX=<script>:<fixture dir>.

CAPABILITIES (BOSS_SCRIPTED_CAPS, comma-separated, default none): "resume", "read_only",
"turn_limit", "reports_cost", "streams_turns", "plain_completion". The default is the poorest
engine — no sessions, no read-only mode, no plain completion — so BOSS's fallbacks are what run;
the conformance tests also run it with every capability, and the two must behave the same.
"""
from __future__ import annotations

import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

from .engine_contract import EngineCapabilities
from .workspace_perms import tidies_workspace

ENGINE = "scripted"
MODEL = "scripted-v1"

_DEFAULT_FIXTURE = (Path(__file__).resolve().parents[3] / "tests" / "e2e" / "fixtures"
                    / "plain-node" / "plain")

_DESIGN = (
    "=== STANDARDS ===\n- ESM, Node >= 20, no dependencies; tests under node --test.\n"
    "=== SPEC ===\n- slug(text) lowercases, joins words with single hyphens and trims them.\n"
    "- GET /?q=<text> answers {\"slug\": slug(text)} as JSON.\n"
    "=== PLAN ===\n1. tests in test/slug.test.js\n2. src/slug.js\n3. src/server.js (PORT)\n"
    "=== WORK PACKAGES ===\n- one package: the whole app\n"
)

_BROKEN_SLUG = ("// scripted fault: failing_test\nexport function slug(text) {\n"
                "  return String(text ?? \"\");\n}\n")
_CRASHING_SERVER = ("// scripted fault: crash_on_start\n"
                    "throw new Error(\"scripted fault: the server crashes on start\");\n")


#: The generic `failing_test` fault, appended to a module so that importing it fails.
_GENERIC_BREAK = {
    ".js": "\nthrow new Error(\"scripted fault: failing_test\");\n",
    ".mjs": "\nthrow new Error(\"scripted fault: failing_test\");\n",
    ".py": "\nraise RuntimeError(\"scripted fault: failing_test\")\n",
}


class ScriptedFault(RuntimeError):
    """The `crash` fault: an engine that cannot run at all."""


def _flags(env: str) -> set[str]:
    return {f.strip().lower() for f in (os.environ.get(env) or "").split(",") if f.strip()}


def _is_test(rel: str) -> bool:
    low = rel.lower()
    return ("/test/" in f"/{low}" or "/tests/" in f"/{low}" or ".test." in low
            or ".spec." in low)


class _RunState:
    """What the script has done in one run (faults fire once per run)."""

    def __init__(self) -> None:
        self.fired: set[str] = set()
        self.calls: list[dict[str, Any]] = []


_STATES: dict[str, _RunState] = {}
_STATES_LOCK = threading.Lock()


def run_state(run_id: Optional[str]) -> _RunState:
    with _STATES_LOCK:
        return _STATES.setdefault(run_id or "-", _RunState())


def reset() -> None:
    with _STATES_LOCK:
        _STATES.clear()


class ScriptedEngine:
    """A deterministic engine with no model behind it (see the module docstring)."""

    engine = ENGINE

    def __init__(self, settings: Any = None, *, faults: Optional[set[str]] = None,
                 caps: Optional[set[str]] = None, fixture: Optional[Path] = None,
                 app_dir: Optional[str] = None) -> None:
        self.settings = settings
        self.faults = set(faults) if faults is not None else _flags("BOSS_SCRIPTED_FAULTS")
        names = set(caps) if caps is not None else _flags("BOSS_SCRIPTED_CAPS")
        self.capabilities = EngineCapabilities(**{k: True for k in names
                                                  if k in EngineCapabilities.__dataclass_fields__})
        self.fixture = Path(fixture or os.environ.get("BOSS_SCRIPTED_FIXTURE") or _DEFAULT_FIXTURE)
        self.app_dir = app_dir if app_dir is not None else os.environ.get("BOSS_SCRIPTED_APP_DIR")

    # -- the fixture ------------------------------------------------------------------------
    def _files(self) -> dict[str, str]:
        if not self.fixture.is_dir():
            raise ScriptedFault(f"the scripted engine's fixture {self.fixture} is missing "
                                "(set BOSS_SCRIPTED_FIXTURE)")
        out = {}
        for path in sorted(self.fixture.rglob("*")):
            if path.is_file() and "node_modules" not in path.parts:
                out[path.relative_to(self.fixture).as_posix()] = path.read_text(encoding="utf-8")
        return out

    def _app_root(self, prompt: str) -> str:
        if self.app_dir is not None:
            return self.app_dir.strip("/")
        m = re.search(r"Work ONLY under `([^`]+?)/?`", prompt or "") or re.search(
            r"inside `([^`]+?)/`", prompt or "")
        return (m.group(1).strip("/") if m else "plain")

    # -- the session --------------------------------------------------------------------------
    @tidies_workspace
    def run_role(self, prompt: str, cwd: Path, *, read_only: bool = False,
                 model: Optional[str] = None, resume_session_id: Optional[str] = None,
                 max_turns: Optional[int] = None, parallel_agents: int = 0, **_extra: Any) -> dict:
        from .llm_telemetry import get_llm_context, record_agent_turns, record_llm_call

        ctx = get_llm_context()
        stage = str(ctx.get("stage") or "").strip().lower() or _stage_from_prompt(prompt)
        state = run_state(ctx.get("run_id"))
        state.calls.append({"stage": stage, "read_only": read_only, "resumed": resume_session_id,
                            "max_turns": max_turns, "prompt_head": (prompt or "")[:4000],
                            # The whole prompt, so a conformance test can see what reached it.
                            "full_prompt": prompt or ""})
        if "crash" in self.faults:
            record_llm_call(engine_name=self.engine, usage={"model": MODEL}, latency_ms=0,
                            ok=False, error="scripted fault: crash")
            raise ScriptedFault("scripted fault: the engine crashed")
        root = Path(cwd)
        app = self._app_root(prompt)
        turns: list[dict[str, Any]] = []
        summary, written = self._play(stage, prompt, root, app, read_only, state, turns)
        for i, t in enumerate(turns):
            t["turn_index"] = i
        turns.append({"direction": "completion", "content": summary, "turn_index": len(turns)})
        record_agent_turns(turns, engine_name=self.engine, model=MODEL, prompt=prompt)
        usage = {"model": MODEL, "prompt_tokens": len(prompt) // 4,
                 "completion_tokens": len(summary) // 4, "cache_read_tokens": 0,
                 "cache_write_tokens": 0, "billing": "local"}
        record_llm_call(engine_name=self.engine, usage=usage, cost_usd_micros=0, latency_ms=1,
                        turns=1)
        return {
            "summary": summary,
            "usage": usage,
            "cost_usd_micros": 0,
            "cost_unreported": not self.capabilities.reports_cost,
            "turns": 1,
            "tool_calls": len(written),
            "model": MODEL,
            "session_id": (f"scr-{uuid.uuid4().hex[:12]}" if self.capabilities.resume else None),
            "stop_reason": "done",
        }

    def run_plain(self, prompt: str, *, model: Optional[str] = None, **_extra: Any) -> dict:
        """The plain one-shot (engine_contract.run_verdict): the same scripted reply the stage
        would get from run_role, with no working tree at all. The call is kept in the run state
        with transport="plain" so a test can assert which path a verdict rode."""
        import tempfile

        from .llm_telemetry import get_llm_context, record_agent_turns, record_llm_call

        ctx = get_llm_context()
        stage = str(ctx.get("stage") or "").strip().lower() or _stage_from_prompt(prompt)
        state = run_state(ctx.get("run_id"))
        state.calls.append({"stage": stage, "read_only": True, "resumed": None,
                            "max_turns": 1, "transport": "plain",
                            "prompt_head": (prompt or "")[:4000], "full_prompt": prompt or ""})
        if "crash" in self.faults:
            record_llm_call(engine_name=self.engine, usage={"model": MODEL}, latency_ms=0,
                            ok=False, error="scripted fault: crash")
            raise ScriptedFault("scripted fault: the engine crashed")
        turns: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="boss-scripted-plain-") as cwd:
            summary, _written = self._play(stage, prompt, Path(cwd), self._app_root(prompt),
                                           True, state, turns)
        record_agent_turns([{"direction": "completion", "content": summary, "turn_index": 0}],
                           engine_name=self.engine, model=MODEL, prompt=prompt)
        usage = {"model": MODEL, "prompt_tokens": len(prompt) // 4,
                 "completion_tokens": len(summary) // 4, "cache_read_tokens": 0,
                 "cache_write_tokens": 0, "billing": "local"}
        record_llm_call(engine_name=self.engine, usage=usage, cost_usd_micros=0, latency_ms=1,
                        turns=1)
        return {
            "summary": summary,
            "usage": usage,
            "cost_usd_micros": 0,
            "cost_unreported": not self.capabilities.reports_cost,
            "turns": 1,
            "model": MODEL,
            "stop_reason": "done",
        }

    def run(self, spec: Any, repo: Any, sandbox: Any) -> Any:
        from .code_agent import CodeAgentResult
        from .verification_target import run_until_failure

        out = self.run_role(f"TASK: {spec.title}\n{spec.instructions}", sandbox.path)
        tests = run_until_failure(sandbox, spec.test_commands)
        return CodeAgentResult(
            summary=out["summary"], diff=sandbox.diff(), diff_summary=sandbox.diff_summary(),
            tests=tests, tests_passed=all(t.ok for t in tests) if tests else False,
            cost_usd_micros=0, usage=out["usage"], transcript=out["summary"], turns=1,
            engine=self.engine)

    # -- the script ---------------------------------------------------------------------------
    def _write(self, root: Path, rel: str, content: str, turns: list, written: list) -> None:
        from .repo_files import RepoFiles

        with RepoFiles(root) as files:
            files.write_text(rel, content)
        turns.append({"direction": "tool_call", "tool_name": "write_file",   # a non-Claude name:
                      "content": {"path": rel, "content": content[:200]}})   # BOSS normalises it
        turns.append({"direction": "tool_result", "tool_name": "write_file",
                      "content": f"wrote {rel}", "ok": True})
        written.append(rel)

    def _play(self, stage: str, prompt: str, root: Path, app: str, read_only: bool,
              state: _RunState, turns: list) -> tuple[str, list[str]]:
        if stage.startswith("self_fix_"):
            # BOSS FIXING ITSELF (self_fix_scripts.py): Mender's turns, scripted per fault.
            from .self_fix_scripts import play as play_self_fix

            summary, written = play_self_fix(stage, prompt, root, read_only=read_only,
                                             state=state)
            for rel in written:
                turns.append({"direction": "tool_call", "tool_name": "write_file",
                              "content": {"path": rel}})
            return summary, written
        if stage == "rule_scan":
            # SCAN FOR BUSINESS RULES (knowledge/rule_scan.py): its script lives beside it.
            from .knowledge.rule_scan import scripted_reply

            return scripted_reply(prompt), []
        written: list[str] = []
        files = self._files()
        prefix = f"{app}/" if app else ""
        tests = {prefix + rel: body for rel, body in files.items() if _is_test(rel)}
        impl = {prefix + rel: body for rel, body in files.items() if not _is_test(rel)}
        text = prompt or ""
        if "ROUTING: NOT_A_CODE_TASK" in text or stage == "review_plan":
            return "The plan covers the spec and the tests come first.\nPLAN: OK", written
        if stage in ("standards", "spec", "plan", "design"):
            return _DESIGN, written
        if "ARBITER:" in text:
            return ("The failing test encodes the criteria; the code is wrong.\n"
                    "ARBITER: CODE_WRONG — the implementation, not the test"), written
        if stage == "check_tests" or "Mutation testing" in text:
            if read_only:
                return "Checked the tests against the criteria.", written
            target = next((p for p in impl if p.endswith("slug.js")), None)
            if target and (root / target).exists():
                body = (root / target).read_text(encoding="utf-8")
                self._write(root, target, body.replace(".toLowerCase()", ""), turns, written)
                return f"Changed one line in {target}: dropped the lower-casing.", written
            return "Nothing to mutate.", written
        if stage == "write_tests" and not read_only:
            if "--- TEST BUDGET ---" in text:
                return self._test_plan(tests), written
            for rel, body in tests.items():
                self._write(root, rel, body, turns, written)
            return self._test_plan(tests), written
        if stage == "review_code" or ("VERDICT: SURVIVED" in text and read_only):
            names = ", ".join(sorted(impl)) or "the change"
            if "blocker" in self.faults and "review_blocker" not in state.fired:
                state.fired.add("review_blocker")
                target = next(iter(sorted(impl)), "src/slug.js")
                return (f"Reviewed {names}.\nFINDING: BLOCKER {target}:1 — scripted fault: the "
                        "export is missing its input validation.\nVERDICT: REFUTED"), written
            return (f"Reviewed {names}: the change implements the spec and its tests cover it. "
                    "No defects found.\nVERDICT: SURVIVED"), written
        if stage == "security_check":
            return (f"Reviewed {', '.join(sorted(impl))}: no injection, auth, secret or "
                    "traversal issue reachable from input.\nVERDICT: CLEAN"), written
        if stage == "consistency_check":
            return (f"{', '.join(sorted(impl))} and {', '.join(sorted(tests))} agree with the "
                    "criteria.\nCONSISTENT: YES"), written
        if read_only:
            return f"Read {', '.join(sorted(impl))}. Nothing further.", written
        # A writing stage: implement, solo, repair, quality, a fix round, an install fix.
        broken = self._generic_failing_test(root, impl, state)
        for rel, body in impl.items():
            if rel == broken:
                body = body + _GENERIC_BREAK[Path(rel).suffix]
            if rel.endswith("slug.js") and "failing_test" in self.faults \
                    and "failing_test" not in state.fired:
                state.fired.add("failing_test")
                body = _BROKEN_SLUG
            if rel.endswith("server.js") and "crash_on_start" in self.faults \
                    and "crash_on_start" not in state.fired:
                state.fired.add("crash_on_start")
                body = _CRASHING_SERVER
            current = (root / rel).read_text(encoding="utf-8") if (root / rel).exists() else None
            if current != body:
                self._write(root, rel, body, turns, written)
        # THE APP SAYS IT IS A SERVICE, as a real agent's build does (preview_service.
        # RUN_DECLARATION_FILE): without it BOSS reads the app as a library whose tests are its
        # proof, never starts the server, and a server that crashes on start ships unnoticed.
        server = next((p for p in impl if p.endswith("server.js")), None)
        if server:
            declared = f"{prefix}boss.preview.command"
            command = f"node {server[len(prefix):]}\n"
            if not (root / declared).exists() or (root / declared).read_text() != command:
                self._write(root, declared, command, turns, written)
        if stage == "write_tests" or stage == "solo":
            for rel, body in tests.items():
                if not (root / rel).exists():
                    self._write(root, rel, body, turns, written)
        changed = ", ".join(written) or "nothing (already in place)"
        return f"Implemented the slug service. Changed: {changed}.", written

    def _generic_failing_test(self, root: Path, impl: dict[str, str],
                              state: _RunState) -> Optional[str]:
        """The `failing_test` fault on a fixture with no slug.js (the harness benchmark's tasks):
        the first implementation file this turn changes is written so that importing it fails,
        once per run; the next writing turn writes it whole. None when the fault does not fire."""
        if "failing_test" not in self.faults or "failing_test" in state.fired:
            return None
        if any(rel.endswith("slug.js") for rel in impl):
            return None     # the slug fixture's own fault (_BROKEN_SLUG) plays it
        for rel in sorted(impl):
            if Path(rel).suffix not in _GENERIC_BREAK:
                continue
            current = (root / rel).read_text(encoding="utf-8") if (root / rel).exists() else None
            if current != impl[rel]:
                state.fired.add("failing_test")
                return rel
        return None

    @staticmethod
    def _test_plan(tests: dict[str, str]) -> str:
        cases = []
        for rel, body in tests.items():
            for name in re.findall(r'test\("([^"]+)"', body):
                cases.append(f"- {rel}: {name}")
        return "TEST PLAN:\n" + "\n".join(cases or ["- (none)"])


def _stage_from_prompt(prompt: str) -> str:
    text = prompt or ""
    for marker, stage in (("QA engineer", "write_tests"), ("security reviewer", "security_check"),
                          ("internal consistency", "consistency_check"),
                          ("design reviewer", "review_plan"), ("Mutation testing", "check_tests")):
        if marker in text:
            return stage
    return "implement"


ENABLE_ENV = "BOSS_SCRIPTED_ENGINE"


def enabled() -> bool:
    """Only where a test or the conformance gate turned it on: never an operator's choice."""
    return (os.environ.get(ENABLE_ENV) or "").strip().lower() in ("1", "true", "yes")


def build(settings: Any) -> ScriptedEngine:
    """The harness registry's builder (code_agent._register_builtin_harnesses)."""
    if not enabled():
        raise RuntimeError(
            "the scripted engine is BOSS's conformance engine and plays no model; it runs only "
            f"under the conformance gate ({ENABLE_ENV}=1). Choose a real engine.")
    return ScriptedEngine(settings)
